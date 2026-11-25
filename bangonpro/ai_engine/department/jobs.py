"""Production jobs: automation modes, checkpoints, resume, rerun, decisions and drafts.

A ``ProductionJob`` runs the specialist agents of the AI Production Department
against one project inside an isolated draft directory. Every agent's output is
a validated Timeline Command API v1 batch; the job keeps:

* ``job.json``      - live status polled by the Agent Control Centre
* ``plan.json``     - the Executive Editing Agent's plan (active agents + reasons)
* ``checkpoints/``  - full serialised project state after every agent
* ``events.jsonl``  - append-only activity log (which agent did what and why)
* ``draft/``        - isolated draft project, captions, reports and renders

Autonomous modes auto-accept reversible edits into the draft. Replacing an
existing edit (``promote``) and publishing always require explicit approval.
Original source media is never written; its SHA-256 is re-verified at the end.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import copy
import json
import shutil
import threading
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bangonpro.ai_engine.agents.ingest_sync import verify_source_integrity
from bangonpro.ai_engine.commands import validate_timeline_command_batch
from bangonpro.ai_engine.department.agents import (
    HANDLERS,
    AgentContext,
    AgentOutcome,
    InsufficientStorageError,
    JobOptions,
    compute_activation,
    parse_brief_flags,
    select_local_asr_model,
    shape_intent_for_mode,
)
from bangonpro.ai_engine.department.cache import AnalysisCache
from bangonpro.ai_engine.department.checkpoint import (
    deserialize_state,
    serialize_state,
    write_json_atomic,
)
from bangonpro.ai_engine.department.learning import ProfilePreferenceStore
from bangonpro.ai_engine.department.registry import (
    AGENT_ORDER,
    AGENTS_BY_ID,
    AUTOMATION_MODES,
    AUTONOMOUS_MODES,
    KIND_EDITORIAL,
    KIND_FINISHING,
    MODE_ASSISTED,
    MODE_MANUAL,
    transitive_dependents,
)
from bangonpro.ai_engine.orchestrator import CancellationToken, PipelineCancelledError
from bangonpro.ai_engine.pipeline import map_inputs_to_profile_cameras
from bangonpro.ai_engine.planner import ingest_project_context, interpret_instruction
from bangonpro.ai_engine.project_builder import (
    compile_command_batches,
    emit_kdenlive_xml,
    emit_otio_timeline,
)
from bangonpro.ai_engine.state import ProjectAnalysisState
from bangonpro.show_profiles.loader import load_show_profile

JOB_SCHEMA_VERSION = "1.2"
TERMINAL_AGENT_STATES = frozenset({"completed", "skipped", "failed", "blocked", "rolled_back"})


class ApprovalRequiredError(PermissionError):
    """Raised when an action needs explicit editor approval (replace edit / publish)."""


def _utc() -> str:
    return datetime.now(UTC).isoformat()


class FileCancellationToken(CancellationToken):
    """Cancellation token that also honours a ``CANCEL`` file written by the UI/CLI."""

    def __init__(self, cancel_path: Path) -> None:
        super().__init__()
        self._cancel_path = cancel_path

    def _poll(self) -> None:
        if not super().is_cancelled and self._cancel_path.exists():
            reason = self._cancel_path.read_text(encoding="utf-8").strip() or "Cancelled from Agent Control Centre"
            self.cancel(reason)

    @property
    def is_cancelled(self) -> bool:
        self._poll()
        return super().is_cancelled

    def check(self, context: str = "") -> None:
        self._poll()
        super().check(context)


class ProductionJob:
    def __init__(self, job_dir: str | Path) -> None:
        self.dir = Path(job_dir).resolve()
        self.job_path = self.dir / "job.json"
        self.plan_path = self.dir / "plan.json"
        self.ckpt_dir = self.dir / "checkpoints"
        self.events_path = self.dir / "events.jsonl"
        self.cancel_path = self.dir / "CANCEL"
        self.draft_dir = self.dir / "draft"
        self.doc: dict[str, Any] = {}
        self.state: ProjectAnalysisState | None = None
        self._lock = threading.Lock()
        self._last_write = 0.0
        if self.job_path.exists():
            self.doc = json.loads(self.job_path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ setup
    @classmethod
    def create(
        cls,
        *,
        job_root: str | Path,
        brief: str,
        inputs: list[str | Path] | dict[str, str | Path] | None = None,
        profile_ref: str | Path | None = None,
        mode: str = MODE_ASSISTED,
        options: JobOptions | None = None,
        project_id: str | None = None,
        sequence_id: str | None = None,
        kdenlive_project: str | None = None,
        project_state: str | None = None,
        job_id: str | None = None,
    ) -> ProductionJob:
        if mode not in AUTOMATION_MODES:
            raise ValueError(f"Unknown automation mode '{mode}' (expected one of {', '.join(AUTOMATION_MODES)})")
        opts = options or JobOptions()
        opts.mode = mode
        jid = job_id or f"job_{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}_{uuid.uuid4().hex[:6]}"
        job = cls(Path(job_root) / jid)
        job.dir.mkdir(parents=True, exist_ok=True)
        job.draft_dir.mkdir(parents=True, exist_ok=True)

        context = ingest_project_context(
            kdenlive_project=kdenlive_project,
            project_state=project_state,
            inputs=inputs,
            project_id=project_id or f"bop_{uuid.uuid4().hex[:10]}",
            sequence_id=sequence_id or "seq_main",
        )
        interp = interpret_instruction(
            brief,
            context=context,
            export_override=True if opts.export_video else None,
            allow_default_empty=mode in AUTONOMOUS_MODES,
            provider_config=opts.provider_config,
        )
        profile = load_show_profile(profile_ref or interp.intent.profile_hint or context.profile_id or "podcast_multicam")
        cams: dict[str, str] = {}
        if inputs:
            cams = map_inputs_to_profile_cameras(inputs, profile)
        else:
            derived = context.extract_existing_media_inputs()
            if derived:
                cams = map_inputs_to_profile_cameras(dict(derived), profile)
        n_video = sum(1 for p in cams.values() if Path(p).suffix.lower() not in (".wav", ".mp3", ".flac", ".aac", ".m4a"))
        flags = parse_brief_flags(brief)
        intent = shape_intent_for_mode(interp.intent, mode, flags, n_video, opts.export_video)
        activation = compute_activation(intent, mode, flags, profile, n_inputs=len(cams), n_video=n_video, overrides=opts.enabled_overrides)

        state = ProjectAnalysisState(
            project_id=project_id or context.project_id or f"bop_{uuid.uuid4().hex[:10]}",
            sequence_id=sequence_id or context.sequence_id or "seq_main",
            profile=profile,
            output_dir=str(job.draft_dir),
            instruction=brief,
            intent=intent,
            dry_run=False,
        )
        state.revision = context.revision
        state.input_camera_files = cams
        state.metadata["project_context"] = context.summary()
        state.metadata["interpreter_backend"] = interp.interpreter_backend
        if opts.model_dir:
            state.metadata["model_dir"] = str(Path(opts.model_dir).expanduser().resolve())
        # Use genuine local neural ASR whenever a runnable Whisper model is installed (no downloads here).
        asr_model = select_local_asr_model(opts.model_dir, opts.asr_model_id)
        if asr_model:
            state.metadata["asr_model_id"] = asr_model
            state.metadata["enable_whisper"] = True
            state.metadata["whisper_model"] = asr_model.rsplit("-", 1)[-1] if asr_model.startswith("faster-whisper-") else "tiny"

        plan = {
            "job_id": jid,
            "mode": mode,
            "brief": brief,
            "brief_flags": flags,
            "intent": {k: v for k, v in asdict(intent).items() if isinstance(v, (bool, str, float, int)) or v is None},
            "profile_id": profile.get("profile_id"),
            "series_id": opts.series_id,
            "inputs": cams,
            "activation": activation,
            "agent_order": list(AGENT_ORDER),
            "approval_policy": {
                "auto_accept_reversible_edits": mode in AUTONOMOUS_MODES,
                "replace_existing_edit": "requires_approval",
                "publish": "requires_approval",
                "source_media": "read_only",
            },
            "budget_usd": opts.budget_usd,
            "executive_llm_cost_usd": 0.0,
            "interpreter_backend": interp.interpreter_backend,
            "asr_model_id": state.metadata.get("asr_model_id"),
            "needs_clarification": bool(intent.needs_clarification),
            "clarification_question": intent.clarification_question,
            "plan_path": str(job.plan_path),
        }
        write_json_atomic(job.plan_path, plan)
        job.doc = {
            "schema_version": JOB_SCHEMA_VERSION,
            "job_id": jid,
            "status": "planned",
            "mode": mode,
            "created_at_utc": _utc(),
            "updated_at_utc": _utc(),
            "project": {
                "project_id": state.project_id,
                "sequence_id": state.sequence_id,
                "base_revision": state.revision,
                "source_kdenlive_project": kdenlive_project,
            },
            "profile_id": profile.get("profile_id"),
            "series_id": opts.series_id,
            "options": {k: v for k, v in asdict(opts).items() if k != "provider_config"},
            "progress": 0.0,
            "current_agent": None,
            "agents": {
                a: {
                    "agent_id": a,
                    "display_name": AGENTS_BY_ID[a].display_name,
                    "active": activation[a]["active"],
                    "activation_reason": activation[a]["reason"],
                    "status": "pending" if activation[a]["active"] else "skipped",
                    "decision": None,
                    "batches": [],
                    "attempts": 0,
                }
                for a in AGENT_ORDER
            },
            "batches": {},
            "decisions": [],
            "approvals": [],
            "budget": {"limit_usd": opts.budget_usd, "spent_usd": 0.0},
            "artifacts": {},
        }
        job.state = state
        job._save(force=True)
        job._checkpoint(-1, "initial", state)
        job._event("executive", "job_created", {"mode": mode, "active_agents": [a for a in AGENT_ORDER if activation[a]["active"]]})
        return job

    # ------------------------------------------------------------- persistence
    def _save(self, *, force: bool = False) -> None:
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_write < 0.4:
                return
            self._last_write = now
            self.doc["updated_at_utc"] = _utc()
            write_json_atomic(self.job_path, self.doc)

    def _event(self, agent: str, event: str, details: dict[str, Any] | None = None) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        rec = {"ts_utc": _utc(), "agent": agent, "event": event, "details": details or {}}
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    def _checkpoint(self, idx: int, agent_id: str, state: ProjectAnalysisState) -> Path:
        path = self.ckpt_dir / f"{idx + 1:02d}_{agent_id}.json"
        write_json_atomic(path, {"agent_index": idx, "agent_id": agent_id, "saved_at_utc": _utc(), "state": serialize_state(state)})
        self.doc.setdefault("checkpoints", []).append(str(path))
        return path

    def _load_checkpoint_before(self, agent_id: str) -> ProjectAnalysisState:
        idx = AGENT_ORDER.index(agent_id)
        candidates = sorted(self.ckpt_dir.glob("*.json"))
        best: Path | None = None
        for p in candidates:
            data = json.loads(p.read_text(encoding="utf-8"))
            if int(data["agent_index"]) < idx:
                best = p
        if best is None:
            raise FileNotFoundError(f"No checkpoint found before agent '{agent_id}'")
        return deserialize_state(json.loads(best.read_text(encoding="utf-8"))["state"])

    def _latest_state(self) -> ProjectAnalysisState:
        files = sorted(self.ckpt_dir.glob("*.json"))
        if not files:
            raise FileNotFoundError("Job has no checkpoints")
        return deserialize_state(json.loads(files[-1].read_text(encoding="utf-8"))["state"])

    @property
    def plan(self) -> dict[str, Any]:
        return json.loads(self.plan_path.read_text(encoding="utf-8"))

    def options(self) -> JobOptions:
        raw = dict(self.doc.get("options", {}))
        return JobOptions(**{**raw, "provider_config": None})

    # --------------------------------------------------------------- execution
    def run(self, *, start_agent: str | None = None, progress_callback: Any = None) -> dict[str, Any]:
        plan = self.plan
        if self.state is None:
            self.state = self._load_checkpoint_before(start_agent) if start_agent else self._latest_state()
        st = self.state
        opts = self.options()
        if self.cancel_path.exists():
            self.cancel_path.unlink()
        token = FileCancellationToken(self.cancel_path)
        cache = AnalysisCache()
        if plan.get("needs_clarification") and self.doc["mode"] == MODE_ASSISTED:
            self.doc["status"] = "needs_clarification"
            self.doc["clarification_question"] = plan.get("clarification_question")
            self._save(force=True)
            return self.doc
        if self.doc["mode"] == MODE_MANUAL and start_agent is None:
            self.doc["status"] = "planned_manual"
            self._save(force=True)
            return self.doc

        self.doc["status"] = "running"
        self.doc["started_at_utc"] = self.doc.get("started_at_utc") or _utc()
        t_job = time.perf_counter()
        start_idx = AGENT_ORDER.index(start_agent) if start_agent else 0
        failed_or_blocked: set[str] = set()
        order = list(AGENT_ORDER)
        try:
            for idx, agent_id in enumerate(order):
                rec = self.doc["agents"][agent_id]
                if idx < start_idx:
                    continue
                if not rec["active"]:
                    rec["status"] = "skipped"
                    continue
                if rec["status"] == "completed" and start_agent is None:
                    continue  # resume: keep finished work
                blockers = [d for d in AGENTS_BY_ID[agent_id].depends_on if d in failed_or_blocked]
                if blockers:
                    rec["status"] = "blocked"
                    rec["error"] = f"Blocked by failed dependency: {', '.join(blockers)}"
                    failed_or_blocked.add(agent_id)
                    continue
                self._run_agent(idx, agent_id, st, token, cache, opts, plan, progress_callback, failed_or_blocked)
            self._finalise(st, time.perf_counter() - t_job, failed_or_blocked)
        except PipelineCancelledError as exc:
            cur = self.doc.get("current_agent")
            if cur:
                self.doc["agents"][cur]["status"] = "cancelled"
            self.doc["status"] = "cancelled"
            self.doc["cancel_reason"] = str(exc)
            self._event(cur or "executive", "job_cancelled", {"reason": str(exc)})
            self._save(force=True)
        except InsufficientStorageError as exc:
            self.doc["status"] = "blocked_insufficient_storage"
            self.doc["error"] = str(exc)
            self._event("media_ingest", "blocked_insufficient_storage", {"error": str(exc)})
            self._save(force=True)
        return self.doc

    def _run_agent(
        self,
        idx: int,
        agent_id: str,
        st: ProjectAnalysisState,
        token: FileCancellationToken,
        cache: AnalysisCache,
        opts: JobOptions,
        plan: dict[str, Any],
        progress_callback: Any,
        failed_or_blocked: set[str],
    ) -> None:
        rec = self.doc["agents"][agent_id]
        rec.update({"status": "running", "started_at_utc": _utc(), "progress": 0.0, "error": None})
        rec["attempts"] = int(rec.get("attempts", 0)) + 1
        self.doc["current_agent"] = agent_id
        self._event(agent_id, "agent_started", {"reason": rec["activation_reason"]})
        self._save(force=True)
        n = len(AGENT_ORDER)

        def _progress(frac: float, msg: str) -> None:
            rec["progress"] = round(min(1.0, max(0.0, float(frac))), 3)
            rec["message"] = msg
            self.doc["progress"] = round((idx + rec["progress"]) / n, 4)
            if progress_callback:
                progress_callback(agent_id, self.doc["progress"], msg)
            self._save()

        ctx = AgentContext(agent_id=agent_id, state=st, token=token, progress=_progress, cache=cache, options=opts, plan=plan)
        t0 = time.perf_counter()
        try:
            outcome: AgentOutcome = HANDLERS[agent_id](ctx)
        except (PipelineCancelledError, InsufficientStorageError):
            rec["duration_sec"] = round(time.perf_counter() - t0, 3)
            raise
        except Exception as exc:  # noqa: BLE001 - isolate agent failures, keep independent work
            rec.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}", "duration_sec": round(time.perf_counter() - t0, 3)})
            failed_or_blocked.add(agent_id)
            self._event(agent_id, "agent_failed", {"error": rec["error"]})
            self._save(force=True)
            return
        token.check(f"after {agent_id}")
        committed = []
        auto = self.doc["mode"] in AUTONOMOUS_MODES
        for batch in outcome.batches:
            batch = copy.deepcopy(batch)
            batch["expected_revision"] = st.revision
            validate_timeline_command_batch(batch)
            needs_approval = bool(batch.get("actor", {}).get("requires_approval")) or not auto
            st.command_batches.append(batch)
            st.revision += 1
            rid = batch["request_id"]
            committed.append(rid)
            self.doc["batches"][rid] = {
                "agent_id": agent_id,
                "engine_agent_id": batch["actor"].get("agent_id"),
                "label": batch.get("transaction_label") or batch.get("label", ""),
                "operation_count": len(batch["operations"]),
                "operation_types": dict(_count_types(batch)),
                "decision": "pending" if needs_approval else "auto_accepted",
                "rejected_op_ids": [],
                "revision_after": st.revision,
            }
        rec.update(
            {
                "status": "completed",
                "finished_at_utc": _utc(),
                "duration_sec": round(time.perf_counter() - t0, 3),
                "progress": 1.0,
                "batches": committed,
                "operation_count": sum(self.doc["batches"][r]["operation_count"] for r in committed),
                "confidence": outcome.confidence,
                "summary": outcome.summary,
                "rationale": outcome.rationale,
                "outputs": outcome.outputs,
                "neural_inference": outcome.neural_inference,
                "fallback_reason": outcome.fallback_reason,
                "review_items": outcome.review_items,
                "est_external_cost_usd": outcome.est_external_cost_usd,
                "cache_hits": outcome.cache_hits,
                "metrics": outcome.metrics,
                "skipped_reason": outcome.skipped_reason,
                "decision": "pending" if (committed and not auto) else ("auto_accepted" if committed else None),
            }
        )
        self.doc["budget"]["spent_usd"] = round(self.doc["budget"]["spent_usd"] + outcome.est_external_cost_usd, 4)
        self._checkpoint(idx, agent_id, st)
        self._event(agent_id, "agent_completed", {"summary": outcome.summary, "batches": committed, "confidence": outcome.confidence, "rationale": outcome.rationale})
        self._save(force=True)

    def _finalise(self, st: ProjectAnalysisState, elapsed: float, failed: set[str]) -> None:
        self.doc["current_agent"] = None
        integrity = verify_source_integrity(st) if st.media_assets else {}
        self.doc["source_integrity"] = {"verified": all(integrity.values()) if integrity else None, "per_source": integrity}
        self.rebuild_draft(st)
        self.doc["processing_elapsed_sec"] = round(elapsed + float(self.doc.get("processing_elapsed_sec", 0.0)), 3)
        self.doc["status"] = "completed_with_errors" if failed else ("awaiting_review" if self.doc["mode"] == MODE_ASSISTED else "draft_ready")
        self.doc["progress"] = 1.0
        summary = self.build_summary(st)
        write_json_atomic(self.dir / "summary.json", summary)
        (self.dir / "summary.md").write_text(render_summary_markdown(summary), encoding="utf-8")
        self.doc["artifacts"]["summary_json"] = str(self.dir / "summary.json")
        self.doc["artifacts"]["summary_md"] = str(self.dir / "summary.md")
        self._event("executive", "job_verified", {"status": self.doc["status"], "source_integrity": self.doc["source_integrity"]["verified"]})
        self._save(force=True)
        self.state = st

    # --------------------------------------------------------- draft & output
    def excluded_agents(self) -> tuple[set[str], set[str]]:
        rejected = {a for a, r in self.doc["agents"].items() if r.get("decision") == "rejected"}
        excluded: set[str] = set(rejected)
        stale: set[str] = set()
        for a in rejected:
            if AGENTS_BY_ID[a].kind != KIND_EDITORIAL:
                stale.update(d for d in transitive_dependents(a) if d in ("quality_control", "distribution"))
                continue
            for d in transitive_dependents(a):
                (excluded if AGENTS_BY_ID[d].kind in (KIND_EDITORIAL, KIND_FINISHING) else stale).add(d)
        return excluded, stale - excluded

    def included_batches(self, st: ProjectAnalysisState) -> list[dict[str, Any]]:
        excluded, _ = self.excluded_agents()
        base = int(self.doc["project"]["base_revision"])
        out: list[dict[str, Any]] = []
        for b in st.command_batches:
            meta = self.doc["batches"].get(b["request_id"], {})
            if meta.get("agent_id") in excluded or meta.get("decision") == "rejected":
                continue
            nb = copy.deepcopy(b)
            drop = set(meta.get("rejected_op_ids", []))
            if drop:
                nb["operations"] = [op for op in nb["operations"] if op["op_id"] not in drop]
                if not nb["operations"]:
                    continue
            nb["expected_revision"] = base + len(out)
            out.append(nb)
        return out

    def rebuild_draft(self, st: ProjectAnalysisState | None = None) -> dict[str, Any]:
        st = st or self.state or self._latest_state()
        batches = self.included_batches(st)
        cmds_path = write_json_atomic(
            self.draft_dir / "timeline_commands.json",
            {
                "api_version": "1.0",
                "project_id": st.project_id,
                "sequence_id": st.sequence_id,
                "base_revision": self.doc["project"]["base_revision"],
                "final_revision": int(self.doc["project"]["base_revision"]) + len(batches),
                "batch_agents": {b["request_id"]: self.doc["batches"].get(b["request_id"], {}).get("agent_id") for b in batches},
                "batches": batches,
            },
        )
        arts = self.doc.setdefault("artifacts", {})
        arts["timeline_commands_json"] = str(cmds_path)
        if any(op["type"] == "place_clip" for b in batches for op in b["operations"]):
            compiled = compile_command_batches(batches)
            arts["kdenlive_project"] = str(emit_kdenlive_xml(compiled, self.draft_dir / f"{st.sequence_id}_draft.kdenlive"))
            arts["otio_timeline"] = str(emit_otio_timeline(compiled, self.draft_dir / f"{st.sequence_id}_draft.otio"))
            arts["unresolved_effects"] = str(len(compiled.unresolved_effects))
        for k in ("transcript_json", "captions_srt", "captions_ass", "qc_report_json", "rendered_video", "delivery_manifest_json", "highlights_json", "media_inventory_json"):
            if k in st.artifacts:
                arts[k] = st.artifacts[k]
        write_json_atomic(self.draft_dir / "analysis_state.json", st.to_dict())
        _, stale = self.excluded_agents()
        for a, r in self.doc["agents"].items():
            r["stale"] = a in stale
        self._save(force=True)
        return arts

    # --------------------------------------------------------------- controls
    def cancel(self, reason: str = "Cancelled from Agent Control Centre") -> None:
        self.cancel_path.write_text(reason, encoding="utf-8")
        self._event("executive", "cancel_requested", {"reason": reason})

    def resume(self, progress_callback: Any = None) -> dict[str, Any]:
        for r in self.doc["agents"].values():
            if r["status"] in ("running", "cancelled", "failed", "blocked"):
                r["status"] = "pending"
        self.state = self._latest_state()
        self._event("executive", "job_resumed", {})
        return self.run(progress_callback=progress_callback)

    def rerun(self, agent_id: str, *, profile_overrides: dict[str, Any] | None = None, progress_callback: Any = None) -> dict[str, Any]:
        """Re-execute one agent (and everything after it) from the checkpoint preceding it."""
        if agent_id == "executive":
            raise ValueError("Create a new job to re-plan; the executive plan is immutable per job")
        st = self._load_checkpoint_before(agent_id)
        if profile_overrides:
            from bangonpro.ai_engine.department.learning import (
                deep_merge,
                validate_overlay,
            )

            st.profile = deep_merge(st.profile, validate_overlay(profile_overrides))
            self._event(agent_id, "refinement_applied", {"profile_overrides": profile_overrides})
        idx = AGENT_ORDER.index(agent_id)
        for a in AGENT_ORDER[idx:]:
            r = self.doc["agents"][a]
            if a == agent_id and not r["active"]:
                r["active"] = True
                r["activation_reason"] = "Enabled by user rerun request"
            if r["active"]:
                for rid in r.get("batches", []):
                    self.doc["batches"].pop(rid, None)
                r.update({"status": "pending", "decision": None, "batches": []})
        self.state = st
        self._event(agent_id, "rerun_requested", {})
        return self.run(start_agent=agent_id, progress_callback=progress_callback)

    def rollback(self, agent_id: str) -> dict[str, Any]:
        """Discard every agent after ``agent_id`` and restore the draft to that checkpoint."""
        idx = AGENT_ORDER.index(agent_id)
        nxt = AGENT_ORDER[idx + 1] if idx + 1 < len(AGENT_ORDER) else None
        st = self._load_checkpoint_before(nxt) if nxt else self._latest_state()
        for a in AGENT_ORDER[idx + 1:]:
            r = self.doc["agents"][a]
            for rid in r.get("batches", []):
                self.doc["batches"].pop(rid, None)
            if r["active"]:
                r.update({"status": "rolled_back", "batches": [], "decision": None})
        self.state = st
        self._event(agent_id, "rolled_back_to", {})
        self.rebuild_draft(st)
        return self.doc

    def decide(self, agent_id: str, decision: str, *, rejected_op_ids: list[str] | None = None, note: str = "") -> dict[str, Any]:
        if decision not in ("accept", "reject"):
            raise ValueError("decision must be 'accept' or 'reject'")
        rec = self.doc["agents"][agent_id]
        st = self.state or self._latest_state()
        rec["decision"] = "accepted" if decision == "accept" else "rejected"
        drop = set(rejected_op_ids or [])
        for rid in rec.get("batches", []):
            meta = self.doc["batches"][rid]
            meta["decision"] = rec["decision"]
            if drop and decision == "accept":
                meta["rejected_op_ids"] = sorted(drop)
        rej_ops: list[dict[str, Any]] = []
        acc_ops: list[dict[str, Any]] = []
        for b in st.command_batches:
            if b["request_id"] not in rec.get("batches", []):
                continue
            for op in b["operations"]:
                summary = {"agent_id": agent_id, "type": op["type"], "op_id": op["op_id"], "params": {k: v for k, v in (op.get("params") or {}).items() if isinstance(v, (str, int, float, bool))}}
                (rej_ops if decision == "reject" or op["op_id"] in drop else acc_ops).append(summary)
        entry = {"ts_utc": _utc(), "agent_id": agent_id, "decision": rec["decision"], "rejected_op_ids": sorted(drop), "note": note}
        self.doc["decisions"].append(entry)
        store = ProfilePreferenceStore(str(self.doc["profile_id"]), str(self.doc.get("series_id", "default")), self.doc["options"].get("prefs_root"))
        store.record_correction({"job_id": self.doc["job_id"], "agent_id": agent_id, "decision": rec["decision"], "rejected_ops": rej_ops, "accepted_ops": acc_ops, "note": note})
        self._event(agent_id, f"agent_{rec['decision']}", entry)
        self.state = st
        self.rebuild_draft(st)
        return self.doc

    def promote(self, target_path: str | Path, *, approve: bool = False) -> dict[str, Any]:
        """Copy the draft project to ``target_path``; replacing an existing edit requires approval."""
        arts = self.rebuild_draft()
        src = arts.get("kdenlive_project")
        if not src:
            raise FileNotFoundError("Draft has no timeline to promote")
        target = Path(target_path).expanduser().resolve()
        sources = {Path(p).resolve() for p in (self.state or self._latest_state()).input_camera_files.values()}
        if target in sources:
            raise PermissionError("Refusing to overwrite original source media")
        backup = None
        if target.exists():
            if not approve:
                raise ApprovalRequiredError(f"'{target}' already exists; replacing an existing edit requires explicit approval (--approve)")
            backup = target.with_name(f"{target.name}.bak-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}")
            shutil.copy2(target, backup)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        rec = {"ts_utc": _utc(), "action": "promote_draft", "target": str(target), "replaced_existing": backup is not None, "backup": str(backup) if backup else None, "approved": bool(approve)}
        self.doc["approvals"].append(rec)
        self._event("executive", "draft_promoted", rec)
        self._save(force=True)
        return rec

    # --------------------------------------------------------------- reporting
    def build_summary(self, st: ProjectAnalysisState) -> dict[str, Any]:
        agents = []
        accepted_ops = rejected_ops = pending_ops = 0
        for a in AGENT_ORDER:
            r = self.doc["agents"][a]
            ops = int(r.get("operation_count", 0) or 0)
            if r.get("decision") in ("accepted", "auto_accepted"):
                accepted_ops += ops
            elif r.get("decision") == "rejected":
                rejected_ops += ops
            elif ops:
                pending_ops += ops
            agents.append({k: r.get(k) for k in ("agent_id", "display_name", "active", "activation_reason", "status", "decision", "summary", "rationale", "confidence", "duration_sec", "operation_count", "neural_inference", "fallback_reason", "review_items", "cache_hits", "est_external_cost_usd", "error", "stale")})
        review = [f"[{r['agent_id']}] {item}" for r in agents for item in (r.get("review_items") or [])]
        return {
            "job_id": self.doc["job_id"],
            "mode": self.doc["mode"],
            "status": self.doc["status"],
            "profile_id": self.doc["profile_id"],
            "series_id": self.doc.get("series_id"),
            "brief": st.instruction,
            "processing_elapsed_sec": self.doc.get("processing_elapsed_sec"),
            "agents": agents,
            "operations": {"auto_accepted_or_accepted": accepted_ops, "rejected": rejected_ops, "pending_review": pending_ops},
            "qc_passed": st.qc_report.passed,
            "qc_issue_count": len(st.qc_report.issues),
            "review_items": review,
            "source_integrity": self.doc.get("source_integrity"),
            "external_cost_usd": self.doc["budget"]["spent_usd"],
            "artifacts": self.doc.get("artifacts", {}),
            "approvals_required": ["Replace an existing edit (promote --approve)", "Publish deliverables"],
        }


def _count_types(batch: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for op in batch["operations"]:
        out[op["type"]] = out.get(op["type"], 0) + 1
    return out


def render_summary_markdown(s: dict[str, Any]) -> str:
    lines = [
        f"# Production summary - {s['job_id']}",
        "",
        f"- Mode: **{s['mode']}**  |  Status: **{s['status']}**  |  Profile: `{s['profile_id']}` (series `{s['series_id']}`)",
        f"- Processing time: {s['processing_elapsed_sec']} s  |  External cost: ${s['external_cost_usd']:.2f}",
        f"- Operations: {s['operations']['auto_accepted_or_accepted']} applied to draft, {s['operations']['pending_review']} awaiting review, {s['operations']['rejected']} rejected",
        f"- QC: {'PASSED' if s['qc_passed'] else 'FAILED'} ({s['qc_issue_count']} findings)  |  Source media unchanged: {(s.get('source_integrity') or {}).get('verified')}",
        "",
        "| Agent | Status | Ops | Confidence | Neural | Summary |",
        "|---|---|---|---|---|---|",
    ]
    for a in s["agents"]:
        neural = {True: "yes", False: "fallback", None: "n/a"}[a.get("neural_inference")]
        lines.append(f"| {a['display_name']} | {a['status']}{' (stale)' if a.get('stale') else ''} | {a.get('operation_count') or 0} | {a.get('confidence') if a.get('confidence') is not None else ''} | {neural} | {(a.get('summary') or a.get('activation_reason') or '').replace('|', '/')} |")
    if s["review_items"]:
        lines += ["", "## Needs human review", ""] + [f"- {i}" for i in s["review_items"]]
    lines += ["", "## Approvals still required", ""] + [f"- {i}" for i in s["approvals_required"]]
    return "\n".join(lines) + "\n"
