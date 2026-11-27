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
import hashlib
import html
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import resource as _resource
except ImportError:  # pragma: no cover
    _resource = None  # type: ignore[assignment]

from bangonpro.ai_engine.agents.ingest_sync import (
    compute_file_sha256,
    verify_source_integrity,
)
from bangonpro.ai_engine.commands import validate_timeline_command_batch
from bangonpro.ai_engine.department import quality_loop as ql
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
    verify_checkpoint_integrity,
    write_checkpoint_atomic,
    write_json_atomic,
)
from bangonpro.ai_engine.department.errors import explain
from bangonpro.ai_engine.department.learning import (
    ProfilePreferenceStore,
    deep_merge,
    validate_overlay,
)
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
from bangonpro.ai_engine.department.stages import editor_stages
from bangonpro.ai_engine.department.worker import (
    JobBusyError,
    WorkerTerminated,
    worker_alive,
    worker_session,
)
from bangonpro.ai_engine.orchestrator import CancellationToken, PipelineCancelledError
from bangonpro.ai_engine.pipeline import map_inputs_to_profile_cameras
from bangonpro.ai_engine.planner import (
    ingest_project_context,
    interpret_instruction,
    shared_reasoning_interpreter,
)
from bangonpro.ai_engine.project_builder import (
    compile_command_batches,
    emit_kdenlive_xml,
    emit_otio_timeline,
)
from bangonpro.ai_engine.state import ProjectAnalysisState
from bangonpro.show_profiles.loader import load_show_profile, user_profile_dirs

JOB_SCHEMA_VERSION = "1.2"
PAUSE_FILENAME = "PAUSE"
TERMINAL_AGENT_STATES = frozenset({"completed", "skipped", "failed", "blocked", "rolled_back"})


class ApprovalRequiredError(PermissionError):
    """Raised when an action needs explicit editor approval (replace edit / publish)."""


class PipelinePausedError(Exception):
    """Raised between stages when a cooperative PAUSE sentinel is present."""


def _utc() -> str:
    return datetime.now(UTC).isoformat()


def _utc_ms() -> str:
    """Millisecond precision: what Qt's ISODateWithMs parser reads exactly (the app's stall report)."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _collect_resource_usage() -> dict[str, Any]:
    """Lightweight stdlib-only memory (RSS MB) and CPU time telemetry."""
    rss_mb = 0.0
    cpu_sec = 0.0
    if _resource is not None:
        try:
            ru = _resource.getrusage(_resource.RUSAGE_SELF)
            cpu_sec = float(ru.ru_utime + ru.ru_stime)
            if sys.platform == "darwin":
                rss_mb = float(ru.ru_maxrss) / (1024.0 * 1024.0)
            else:
                rss_mb = float(ru.ru_maxrss) / 1024.0
        except OSError:
            pass
    # Prefer live resident set size from /proc/self/statm on Linux when available.
    try:
        statm = Path("/proc/self/statm").read_text(encoding="utf-8").split()
        if len(statm) >= 2:
            page_size = os.sysconf("SC_PAGE_SIZE")
            rss_mb = (int(statm[1]) * page_size) / (1024.0 * 1024.0)
    except (OSError, ValueError, AttributeError):
        pass
    return {
        "rss_mb": round(rss_mb, 2),
        "cpu_time_sec": round(cpu_sec, 3),
        "updated_at_utc": _utc(),
    }


def _write_bytes_atomic(target: Path, data: bytes) -> None:
    """Write ``data`` to a unique sibling temp file, fsync, and atomically replace ``target``."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    closed = False
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        closed = True
        os.replace(tmp_path, target)
    except BaseException:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        tmp_path.unlink(missing_ok=True)
        raise


def _copy_file_atomic(src: Path, target: Path) -> None:
    """Copy ``src`` to ``target`` via a unique sibling temp file + fsync + atomic replace."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    closed = False
    try:
        with open(src, "rb") as src_fh, os.fdopen(fd, "wb") as dst_fh:
            shutil.copyfileobj(src_fh, dst_fh, length=1024 * 1024)
            dst_fh.flush()
            os.fsync(dst_fh.fileno())
        closed = True
        os.replace(tmp_path, target)
    except BaseException:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        tmp_path.unlink(missing_ok=True)
        raise


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
        self.pause_path = self.dir / PAUSE_FILENAME
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
        project_uuid: str | None = None,
        project_name: str | None = None,
        project_path: str | None = None,
        sequence_id: str | None = None,
        sequence_uuid: str | None = None,
        sequence_name: str | None = None,
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
        # V1.6: the edit type is an explicit request, and every clause of the editor's words is accounted for.
        editor_brief = brief
        if opts.edit_type == "social_clips" and not parse_brief_flags(brief).get("social"):
            brief = f"{brief.rstrip()}{'.' if brief.strip() and not brief.rstrip().endswith('.') else ''} Make vertical social clips of the strongest moments.".strip()
        from bangonpro.ai_engine.department.brief_coverage import brief_coverage, interpret_unhandled, job_reasoning_summary

        coverage = brief_coverage(editor_brief)
        if opts.provider_config is None:
            brief, coverage = interpret_unhandled(brief, coverage, job_id=jid)
        interp = interpret_instruction(
            brief,
            context=context,
            export_override=True if opts.export_video else None,
            allow_default_empty=mode in AUTONOMOUS_MODES,
            provider_config=opts.provider_config,
            llm_interpreter=shared_reasoning_interpreter(task_id=jid) if opts.provider_config is None else None,
        )
        auto_profile = None
        if not (profile_ref or interp.intent.profile_hint or context.profile_id):
            # "Automatic" style: one camera is a talking head, two an interview, more a multicamera studio.
            n_cams = _count_video_inputs(inputs) or len(context.extract_existing_media_inputs() or {})
            auto_profile = "youtube_talking_head" if n_cams <= 1 else ("interview_two_camera" if n_cams == 2 else "podcast_multicam")
        profile = load_show_profile(profile_ref or interp.intent.profile_hint or context.profile_id or auto_profile or "podcast_multicam", custom_dirs=user_profile_dirs())
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

        effective_project_id = project_id or project_uuid or context.project_id or f"bop_{uuid.uuid4().hex[:10]}"
        effective_project_uuid = project_uuid or effective_project_id
        effective_sequence_id = sequence_id or sequence_uuid or context.sequence_id or "seq_main"
        effective_sequence_uuid = sequence_uuid or effective_sequence_id
        effective_project_path = project_path or kdenlive_project
        effective_project_name = project_name or (Path(effective_project_path).stem if effective_project_path else "")

        state = ProjectAnalysisState(
            project_id=effective_project_id,
            sequence_id=effective_sequence_id,
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
            "review_brief": bool(interp.intent.needs_clarification and not intent.needs_clarification and brief.strip()),
            "editor_brief": editor_brief,
            "edit_type": opts.edit_type,
            "profile_chosen_automatically": auto_profile is not None,
            "brief_coverage": coverage,
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
                "project_uuid": effective_project_uuid,
                "project_name": effective_project_name,
                "project_path": effective_project_path,
                "sequence_id": state.sequence_id,
                "sequence_uuid": effective_sequence_uuid,
                "sequence_name": sequence_name or "",
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
            "edit_type": opts.edit_type,
            "brief_coverage": coverage,
            "reasoning": job_reasoning_summary(jid, coverage),
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
            self.doc["resource_usage"] = _collect_resource_usage()
            if self.doc.get("agents"):
                self.doc["stages"] = editor_stages(self.doc)
            write_json_atomic(self.job_path, self.doc)

    def _event(self, agent: str, event: str, details: dict[str, Any] | None = None) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        rec = {"ts_utc": _utc(), "agent": agent, "event": event, "details": details or {}}
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    def _checkpoint(self, idx: int, agent_id: str, state: ProjectAnalysisState) -> Path:
        path = self.ckpt_dir / f"{idx + 1:02d}_{agent_id}.json"
        write_checkpoint_atomic(path, {"agent_index": idx, "agent_id": agent_id, "saved_at_utc": _utc(), "state": serialize_state(state)})
        self.doc.setdefault("checkpoints", []).append(str(path))
        return path

    def _valid_checkpoints(self) -> list[tuple[int, Path]]:
        """Checkpoints that agree with job.json and pass SHA-256 verification, in agent order.

        A checkpoint is trusted only when its JSON parses cleanly, ``verify_checkpoint_integrity``
        passes, and its agent is recorded as completed (or it is the initial state). This guards
        against torn writes, tampered/corrupt checkpoints, workers killed between writing a
        checkpoint and marking the agent completed, and leftover checkpoints after rollback/rerun.
        """
        out: list[tuple[int, Path]] = []
        agents = self.doc.get("agents", {})
        for p in sorted(self.ckpt_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue  # torn write from an interrupted worker
            if not verify_checkpoint_integrity(data):
                continue  # corrupt or truncated checkpoint; skipped so resume re-runs cleanly
            aid, aidx = str(data.get("agent_id")), int(data.get("agent_index", -1))
            if aid == "initial" or agents.get(aid, {}).get("status") == "completed":
                out.append((aidx, p))
        return out

    def _load_checkpoint_before(self, agent_id: str) -> ProjectAnalysisState:
        idx = AGENT_ORDER.index(agent_id)
        best: Path | None = None
        for aidx, p in self._valid_checkpoints():
            if aidx < idx:
                best = p
        if best is None:
            raise FileNotFoundError(f"No checkpoint found before agent '{agent_id}'")
        return deserialize_state(json.loads(best.read_text(encoding="utf-8"))["state"])

    def _latest_state(self) -> ProjectAnalysisState:
        files = self._valid_checkpoints()
        if not files:
            raise FileNotFoundError("Job has no checkpoints")
        return deserialize_state(json.loads(files[-1][1].read_text(encoding="utf-8"))["state"])

    def _discard_checkpoints_from(self, idx: int) -> None:
        """Delete checkpoints of agents at or after ``idx``: they no longer describe the draft."""
        for p in sorted(self.ckpt_dir.glob("*.json")):
            try:
                aidx = int(json.loads(p.read_text(encoding="utf-8")).get("agent_index", -1))
            except (OSError, ValueError):
                aidx = idx
            if aidx >= idx:
                p.unlink(missing_ok=True)
        self.doc["checkpoints"] = [c for c in self.doc.get("checkpoints", []) if Path(c).exists()]

    @property
    def plan(self) -> dict[str, Any]:
        return json.loads(self.plan_path.read_text(encoding="utf-8"))

    def options(self) -> JobOptions:
        raw = dict(self.doc.get("options", {}))
        return JobOptions(**{**raw, "provider_config": None})

    # --------------------------------------------------------------- execution
    def run(self, *, start_agent: str | None = None, progress_callback: Any = None) -> dict[str, Any]:
        """Execute the plan while owning the job; every exit leaves a truthful terminal state.

        V1.5 left ``status: running`` behind whenever anything outside an agent raised (draft
        building, source verification, a moved file), so the app showed a frozen production.
        """
        with worker_session(self.dir, command="rerun" if start_agent else "run"):
            self._t_run = time.perf_counter()
            try:
                return self._run_owned(start_agent=start_agent, progress_callback=progress_callback)
            except (WorkerTerminated, KeyboardInterrupt) as exc:
                self._record_stop(exc)
                raise
            except BaseException as exc:
                self._record_failure(exc)
                raise

    def _record_stop(self, exc: BaseException) -> None:
        """The worker was asked to stop: cancelled/paused when the editor asked, otherwise interrupted."""
        cancelled = self.cancel_path.exists()
        paused = not cancelled and self.pause_path.exists()
        state = "cancelled" if cancelled else ("paused" if paused else "interrupted")
        for r in self.doc.get("agents", {}).values():
            if r.get("status") == "running":
                r["status"] = state
        self.doc["status"] = state
        if cancelled:
            self.doc["cancel_reason"] = self.cancel_path.read_text(encoding="utf-8").strip() or "Cancelled"
        elif paused:
            self.doc["pause_reason"] = self.pause_path.read_text(encoding="utf-8").strip() or "Paused"
            self.doc["paused_at_utc"] = _utc()
        else:
            self.doc["interruption"] = {"at_utc": _utc(), "stage": self._stage_name(), "reason": str(exc) or type(exc).__name__}
            self.doc["error_help"] = _interrupted_help()
        self.doc.pop("finalising", None)
        self._add_processing_time(time.perf_counter() - getattr(self, "_t_run", time.perf_counter()))
        self._event(self.doc.get("current_agent") or "executive", f"job_{state}", {"reason": str(exc)})
        self._save(force=True)

    def _record_failure(self, exc: BaseException) -> None:
        """Anything unexpected ends the run as failed (or blocked when the editor can fix it), never 'running'."""
        cur = self.doc.get("current_agent")
        stage = self._stage_name()
        help_ = explain(exc, context=cur or "")
        status = {"missing_media": "blocked_missing_media", "no_space": "blocked_insufficient_storage"}.get(help_["code"], "failed")
        for aid, r in self.doc.get("agents", {}).items():
            if r.get("status") == "running":
                r.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}", "help": explain(exc, context=aid)})
        signature = f"{stage}|{type(exc).__name__}|{str(exc)[:160]}"
        self.doc["repeated_failure"] = self.doc.get("failure_signature") == signature
        self.doc["failure_signature"] = signature
        self.doc.update({"status": status, "error": f"{type(exc).__name__}: {exc}", "error_help": help_, "failed_stage": stage})
        self.doc.pop("finalising", None)
        self._add_processing_time(time.perf_counter() - getattr(self, "_t_run", time.perf_counter()))
        self._event(cur or "executive", "job_failed", {"stage": stage, "error": self.doc["error"], "traceback": traceback.format_exc(limit=12)[-4000:]})
        try:
            self._save(force=True)
        except OSError:
            pass  # the disk itself failed; the app still sees the process exit and the lock released

    def _stage_name(self) -> str | None:
        if self.doc.get("finalising"):
            return "finalising"
        return self.doc.get("current_agent")

    # ----------------------------------------------------------- reconciliation
    def reconcile(self) -> bool:
        """Report a job left 'running' (or never started) by a dead worker as interrupted.

        Takes the job lock non-blockingly, so it can never overwrite a live worker's state.
        Returns True when the recorded state changed.
        """
        if self.doc.get("status") not in ("running", "planned"):
            return False
        try:
            with worker_session(self.dir, command="reconcile"):
                return self._reconcile_owned()
        except JobBusyError:
            return False

    def _reconcile_owned(self) -> bool:
        status = self.doc.get("status")
        if status == "planned":
            # A plan is written by `run` moments before the worker takes the job; give it time.
            try:
                age = (datetime.now(UTC) - datetime.fromisoformat(str(self.doc.get("updated_at_utc")))).total_seconds()
            except ValueError:
                age = 1e9
            if self.doc.get("plan_only") or age < 30.0:
                return False
        elif status != "running":
            return False
        cancelled = self.cancel_path.exists()
        paused = not cancelled and self.pause_path.exists()
        state = "cancelled" if cancelled else ("paused" if paused else "interrupted")
        stage = self._stage_name()
        for r in self.doc.get("agents", {}).values():
            if r.get("status") == "running":
                r["status"] = state
        self.doc["status"] = state
        if paused:
            self.doc["pause_reason"] = self.pause_path.read_text(encoding="utf-8").strip() or "Paused from Agent Control Centre"
            self.doc["paused_at_utc"] = _utc()
        else:
            self.doc["interruption"] = {"at_utc": _utc(), "stage": stage, "worker_pid": self.doc.get("worker_pid"), "reason": "worker process ended without recording a result"}
            if not cancelled:
                self.doc["error_help"] = _interrupted_help()
        self.doc.pop("finalising", None)
        self._event("executive", "worker_lost" if not paused else "job_paused", {"stage": stage, "worker_pid": self.doc.get("worker_pid"), "recorded_as": state})
        self._save(force=True)
        return True

    def worker_alive(self) -> bool:
        return worker_alive(self.dir)

    def _run_owned(self, *, start_agent: str | None = None, progress_callback: Any = None) -> dict[str, Any]:
        plan = self.plan
        if self.state is None:
            self.state = self._load_checkpoint_before(start_agent) if start_agent else self._latest_state()
        st = self.state
        opts = self.options()
        if self.cancel_path.exists():
            self.cancel_path.unlink()
        if self.pause_path.exists():
            self.pause_path.unlink()
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

        from bangonpro.ai_engine.department.readiness import preflight_storage

        ok, msg, free = preflight_storage(self.draft_dir, int(opts.min_free_bytes))
        if not ok:
            # Blocked before any processing: nothing is half-written, and Resume works once space is freed.
            self.doc["status"] = "blocked_insufficient_storage"
            self.doc["error"] = msg
            self.doc["error_help"] = explain(InsufficientStorageError(msg))
            self._event("executive", "blocked_insufficient_storage", {"error": msg, "free_bytes": free, "preflight": True})
            self._save(force=True)
            return self.doc
        self.doc["status"] = "running"
        self.doc["started_at_utc"] = self.doc.get("started_at_utc") or _utc()
        self.doc["worker_pid"] = os.getpid()
        for stale in ("error", "error_help", "interruption", "failed_stage", "repeated_failure", "cancel_reason", "pause_reason", "paused_at_utc"):
            self.doc.pop(stale, None)
        self.doc["last_progress_utc"] = _utc_ms()
        t_job = time.perf_counter()
        start_idx = AGENT_ORDER.index(start_agent) if start_agent else 0
        failed_or_blocked: set[str] = set()
        order = list(AGENT_ORDER)

        def _check_pause(stage_hint: str) -> None:
            token.check(stage_hint)
            if self.pause_path.exists():
                reason = self.pause_path.read_text(encoding="utf-8").strip() or "Paused from Agent Control Centre"
                raise PipelinePausedError(reason)

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
                _check_pause(f"before {agent_id}")
                blockers = [d for d in AGENTS_BY_ID[agent_id].depends_on if d in failed_or_blocked]
                if blockers:
                    rec["status"] = "blocked"
                    rec["error"] = f"Blocked by failed dependency: {', '.join(blockers)}"
                    failed_or_blocked.add(agent_id)
                    continue
                self._run_agent(idx, agent_id, st, token, cache, opts, plan, progress_callback, failed_or_blocked)
                self._attach_help(agent_id)
                _check_pause(f"after {agent_id}")
                _test_delay(token, self.pause_path)
                _check_pause(f"after delay {agent_id}")
            _check_pause("before finalise")
            self._finalise(st, time.perf_counter() - t_job, failed_or_blocked, token)
        except PipelinePausedError as exc:
            cur = self.doc.get("current_agent")
            if cur and self.doc["agents"][cur].get("status") == "running":
                self.doc["agents"][cur]["status"] = "paused"
            self.doc.pop("finalising", None)
            self.doc["status"] = "paused"
            self.doc["pause_reason"] = str(exc)
            self.doc["paused_at_utc"] = _utc()
            self._add_processing_time(time.perf_counter() - t_job)
            self._event(cur or "executive", "job_paused", {"reason": str(exc)})
            self._save(force=True)
        except PipelineCancelledError as exc:
            cur = self.doc.get("current_agent")
            if cur and self.doc["agents"][cur].get("status") == "running":
                self.doc["agents"][cur]["status"] = "cancelled"
            self.doc.pop("finalising", None)
            self.doc["status"] = "cancelled"
            self.doc["cancel_reason"] = str(exc)
            self._add_processing_time(time.perf_counter() - t_job)
            self._event(cur or "executive", "job_cancelled", {"reason": str(exc)})
            self._save(force=True)
        except InsufficientStorageError as exc:
            self.doc["status"] = "blocked_insufficient_storage"
            self.doc["error"] = str(exc)
            self.doc["error_help"] = explain(exc)
            self._add_processing_time(time.perf_counter() - t_job)
            self._event("media_ingest", "blocked_insufficient_storage", {"error": str(exc)})
            self._save(force=True)
        return self.doc

    def _add_processing_time(self, elapsed: float) -> None:
        """Machine time of a run that stopped early (cancelled / blocked); _finalise adds a full run's."""
        self.doc["processing_elapsed_sec"] = round(float(elapsed) + float(self.doc.get("processing_elapsed_sec") or 0.0), 3)

    def _attach_help(self, agent_id: str) -> None:
        """Add a plain-language fix to a failed agent or a failed render (outside ``_run_agent``)."""
        rec = self.doc["agents"][agent_id]
        rec.pop("help", None)
        if rec.get("status") == "failed" and rec.get("error"):
            rec["help"] = explain(str(rec["error"]), context=agent_id)
        failed_renders = [r for r in rec.get("review_items") or [] if str(r).startswith("Render failed")]
        if rec.get("status") == "completed" and failed_renders:
            rec["help"] = explain("render failed", context="export")
            rec["review_items"] = [f"{r}. {rec['help']['fix']}" if r in failed_renders else r for r in rec["review_items"]]
        if "help" in rec:
            self._save(force=True)

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

        def _progress(frac: float, msg: str) -> None:
            # Monotonic: sub-stages (multi-window sync, quality-loop reruns) restart their own 0..1
            rec["progress"] = round(min(1.0, max(float(rec.get("progress") or 0.0), float(frac))), 3)
            rec["message"] = msg
            self.doc["progress"] = self._overall_progress()
            self.doc["last_progress_utc"] = _utc_ms()
            if progress_callback:
                progress_callback(agent_id, self.doc["progress"], msg)
            self._save()

        ctx = AgentContext(agent_id=agent_id, state=st, token=token, progress=_progress, cache=cache, options=opts, plan=plan)
        t0 = time.perf_counter()
        ql.ensure_extensions_loaded()
        pre_snapshot = serialize_state(st) if agent_id in ql.ASSESSORS else None
        loop_log: dict[str, Any] | None = None
        try:
            outcome: AgentOutcome = HANDLERS[agent_id](ctx)
            if pre_snapshot is not None:
                outcome, loop_log = self._quality_loop(agent_id, ctx, outcome, pre_snapshot)
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
                "quality_loop": loop_log,
                "quality_score": (loop_log or {}).get("final_score"),
            }
        )
        self.doc["budget"]["spent_usd"] = round(self.doc["budget"]["spent_usd"] + outcome.est_external_cost_usd, 4)
        self._checkpoint(idx, agent_id, st)
        self._event(agent_id, "agent_completed", {"summary": outcome.summary, "batches": committed, "confidence": outcome.confidence, "rationale": outcome.rationale})
        self._save(force=True)

    def _quality_loop(
        self,
        agent_id: str,
        ctx: AgentContext,
        outcome: AgentOutcome,
        pre_snapshot: dict[str, Any],
    ) -> tuple[AgentOutcome, dict[str, Any] | None]:
        """Assess -> correct -> verify, bounded; keep a correction only if measurably better.

        Every attempt, accepted or rejected, is charged to the job: its machine time goes into
        ``quality_loop_processing_sec`` (and the agent's ``duration_sec`` / job
        ``processing_elapsed_sec``), its external cost into the budget. An attempt is skipped
        when its projected external cost would exceed the job budget.
        """
        st = ctx.state
        first = ql.assess(agent_id, ctx, outcome)
        if first is None:
            return outcome, None
        first_cost = float(outcome.est_external_cost_usd or 0.0)
        guard_best = ql.guard_issues(st)
        log: dict[str, Any] = {
            "metric": first.metric,
            "initial_score": first.score,
            "initial": first.to_dict(),
            "initial_guard_critical": sorted({i.code for i in guard_best if i.severity in ql.CRITICAL_SEVERITIES}),
            "attempts": [],
        }
        best, best_outcome, current = first, outcome, first
        tried: list[str] = []
        attempts = 0
        attempt_cost_total = 0.0
        attempt_time_total = 0.0
        handler = HANDLERS[agent_id]
        budget = self.doc["budget"]
        while current.correction and attempts < ql.MAX_ATTEMPTS_PER_AGENT and int(self.doc.get("quality_loop_attempts", 0)) < ql.MAX_ATTEMPTS_PER_JOB:
            key = json.dumps(current.correction, sort_keys=True, default=str)
            if key in tried:
                break
            tried.append(key)
            projected = float(budget.get("spent_usd", 0.0)) + first_cost + attempt_cost_total + first_cost
            if first_cost > 0 and projected > float(budget.get("limit_usd", 0.0)):
                log["attempts"].append({"attempt": attempts + 1, "correction": current.correction, "accepted": False, "duration_sec": 0.0, "est_external_cost_usd": 0.0, "verdict": f"skipped: projected external cost ${projected:.2f} exceeds the job budget ${float(budget.get('limit_usd', 0.0)):.2f}"})
                break
            ctx.token.check(f"quality loop {agent_id}")
            attempts += 1
            self.doc["quality_loop_attempts"] = int(self.doc.get("quality_loop_attempts", 0)) + 1
            try:
                overlay = validate_overlay(current.correction)
            except ValueError as exc:
                log["attempts"].append({"attempt": attempts, "correction": current.correction, "accepted": False, "duration_sec": 0.0, "est_external_cost_usd": 0.0, "verdict": f"rejected: invalid correction ({exc})"})
                break
            trial = deserialize_state(pre_snapshot)
            trial.profile = deep_merge(trial.profile, overlay)
            attempt_dir = self.dir / "quality_loop" / f"{agent_id}_{attempts}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            trial.output_dir = str(attempt_dir)
            tctx = AgentContext(agent_id=agent_id, state=trial, token=ctx.token, progress=ctx.progress, cache=ctx.cache, options=ctx.options, plan=ctx.plan)
            ctx.progress(0.95, f"Verifying correction {attempts}: {current.correction_reason or 'profile adjustment'}")
            t_attempt = time.perf_counter()
            try:
                t_outcome = handler(tctx)
                after = ql.assess(agent_id, tctx, t_outcome)
                guard_after = ql.guard_issues(trial)
            except (PipelineCancelledError, InsufficientStorageError):
                attempt_time_total += time.perf_counter() - t_attempt
                self.doc["quality_loop_processing_sec"] = round(float(self.doc.get("quality_loop_processing_sec", 0.0)) + attempt_time_total, 3)
                raise
            except Exception as exc:  # noqa: BLE001 - a failed correction keeps the original output
                dt = time.perf_counter() - t_attempt
                attempt_time_total += dt
                log["attempts"].append({"attempt": attempts, "correction": overlay, "accepted": False, "duration_sec": round(dt, 3), "est_external_cost_usd": 0.0, "verdict": f"rejected: correction run failed ({type(exc).__name__}: {exc})"})
                self._event(agent_id, "quality_correction_failed", {"attempt": attempts, "error": f"{type(exc).__name__}: {exc}"})
                break
            dt = time.perf_counter() - t_attempt
            attempt_time_total += dt
            a_cost = float(t_outcome.est_external_cost_usd or 0.0)
            attempt_cost_total += a_cost
            if after is None:
                log["attempts"].append({"attempt": attempts, "correction": overlay, "accepted": False, "duration_sec": round(dt, 3), "est_external_cost_usd": a_cost, "verdict": "rejected: assessor returned no assessment"})
                break
            ok, verdict = ql.accept_correction(best, after, guard_before=guard_best, guard_after=guard_after)
            log["attempts"].append(
                {
                    "attempt": attempts,
                    "correction": overlay,
                    "reason": current.correction_reason,
                    "score": after.score,
                    "defects": [asdict(d) for d in after.defects],
                    "guard_critical": sorted({i.code for i in guard_after if i.severity in ql.CRITICAL_SEVERITIES}),
                    "accepted": ok,
                    "verdict": verdict,
                    "duration_sec": round(dt, 3),
                    "est_external_cost_usd": round(a_cost, 4),
                    "neural_inference": t_outcome.neural_inference,
                    "cache_hits": list(t_outcome.cache_hits),
                }
            )
            self._event(agent_id, "quality_correction_" + ("accepted" if ok else "rejected"), {"attempt": attempts, "verdict": verdict, "correction": overlay, "duration_sec": round(dt, 3)})
            if ok:
                _relocate_attempt_outputs(attempt_dir, Path(st.output_dir), trial, t_outcome)
                trial.output_dir = st.output_dir
                st.__dict__.update(trial.__dict__)
                best, best_outcome, guard_best = after, t_outcome, guard_after
            current = after
        # Everything that ran is charged: the kept outcome reports the job's total spend for this agent.
        best_outcome.est_external_cost_usd = round(first_cost + attempt_cost_total, 4)
        self.doc["quality_loop_processing_sec"] = round(float(self.doc.get("quality_loop_processing_sec", 0.0)) + attempt_time_total, 3)
        self.doc["quality_loop_cost_usd"] = round(float(self.doc.get("quality_loop_cost_usd", 0.0)) + attempt_cost_total, 4)
        log["final_score"] = best.score
        log["final_defects"] = [asdict(d) for d in best.defects]
        log["corrections_applied"] = sum(1 for a in log["attempts"] if a.get("accepted"))
        log["attempt_processing_sec"] = round(attempt_time_total, 3)
        log["attempt_cost_usd"] = round(attempt_cost_total, 4)
        review = list(best_outcome.review_items)
        for d in best.defects:
            if d.severity in ("warning", "error") or d.subjective:
                item = ("Judgement call: " if d.subjective else "") + d.message
                if item not in review:
                    review.append(item)
        if best_outcome.confidence < ql.LOW_CONFIDENCE_REVIEW and not best_outcome.skipped_reason:
            review.append(f"Low confidence ({best_outcome.confidence:.2f}): review this agent's decisions")
        best_outcome.review_items = review
        return best_outcome, log

    def _overall_progress(self) -> float:
        """Share of the active agents' work that is finished (a running agent adds its own reported fraction)."""
        active = [r for r in self.doc["agents"].values() if r.get("active")]
        if not active:
            return 0.0
        done = sum(1.0 for r in active if r.get("status") in ("completed", "skipped"))
        done += sum(min(1.0, max(0.0, float(r.get("progress") or 0.0))) for r in active if r.get("status") == "running")
        return round(min(0.99, done / len(active)), 4)

    def _finalising(self, message: str, fraction: float | None = None) -> None:
        self.doc["finalising"] = {"message": message, "fraction": None if fraction is None else round(float(fraction), 3)}
        self.doc["last_progress_utc"] = _utc_ms()
        self._save(force=fraction is None)

    def _verify_sources(self, st: ProjectAnalysisState, token: Any) -> dict[str, bool]:
        """Re-hash every source (read-only) with measurable progress; missing footage blocks the job."""
        assets = list(st.media_assets.items())
        missing = [a.file_path for _, a in assets if not Path(a.file_path).is_file()]
        if missing:
            names = ", ".join(Path(m).name for m in missing)
            raise FileNotFoundError(2, f"Source media missing at the end of the production ({names})", missing[0])
        total = sum(Path(a.file_path).stat().st_size for _, a in assets) or 1
        done = 0
        out: dict[str, bool] = {}
        for cam, asset in assets:
            h = hashlib.sha256()
            with open(asset.file_path, "rb") as fh:
                while chunk := fh.read(8 << 20):
                    h.update(chunk)
                    done += len(chunk)
                    self._finalising("Verifying your footage is unchanged", done / total)
                    if token is not None:
                        token.check("verifying source media")
            out[cam] = h.hexdigest() == asset.sha256
        return out

    def _finalise(self, st: ProjectAnalysisState, elapsed: float, failed: set[str], token: Any = None) -> None:
        self.doc["current_agent"] = None
        self._finalising("Verifying your footage is unchanged")
        integrity = self._verify_sources(st, token) if st.media_assets else {}
        self.doc["source_integrity"] = {"verified": all(integrity.values()) if integrity else None, "per_source": integrity}
        self._finalising("Building the edited sequence")
        if token is not None:
            token.check("building the edited sequence")
        self.rebuild_draft(st)
        self._finalising("Running final checks on the edited sequence")
        # The same independent draft QC the agents' tasks use checks the timeline that was actually emitted.
        for d in (self.doc.get("draft_qc") or {}).get("defects", []):
            if d["severity"] in ql.CRITICAL_SEVERITIES and d["agent"] in self.doc["agents"]:
                item = f"Draft QC {d['code']} at {d['start']:.1f}-{d['end']:.1f}s: {d['message']}"
                items = self.doc["agents"][d["agent"]].setdefault("review_items", [])
                if item not in items:
                    items.append(item)
        _consolidate_review_items(self.doc)
        self.doc["final_verification"] = ql.run_final_verifiers(st, self.doc)
        if "draft_qc" in self.doc:
            self.doc["final_verification"]["draft_qc"] = {k: v for k, v in self.doc["draft_qc"].items() if k != "defects"}
        self.doc["processing_elapsed_sec"] = round(elapsed + float(self.doc.get("processing_elapsed_sec", 0.0)), 3)
        self.doc["status"] = "completed_with_errors" if failed else ("awaiting_review" if self.doc["mode"] == MODE_ASSISTED else "draft_ready")
        from bangonpro.ai_engine.department.brief_coverage import job_reasoning_summary

        self.doc["reasoning"] = job_reasoning_summary(str(self.doc["job_id"]), self.doc.get("brief_coverage"))
        self.doc.pop("finalising", None)
        self.doc["finished_at_utc"] = _utc()
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
            from bangonpro.ai_engine.department.edit_layer import (
                EditLayer,
                file_sha256,
                protect_manual_edits,
                regenerate_captions,
            )

            # V1.4: direct agent edits (MCP tools, correction loop) are replayed after the agents' batches.
            layer = EditLayer(self.draft_dir)
            project = self.draft_dir / f"{st.sequence_id}_draft.kdenlive"
            if layer.exists:
                base_sig = hashlib.sha1(json.dumps([b["request_id"] for b in batches]).encode()).hexdigest()[:16]  # noqa: S324 - change detection
                if layer.doc.get("base_signature") != base_sig:
                    layer.doc["base_signature"] = base_sig
                    layer.bump()
                compiled, _, report = layer.replay(batches)
                for row in report:
                    for e in layer.doc["entries"]:
                        if e["entry_id"] == row["entry"]:
                            e["replay_status"], e["replay_messages"] = row["status"], row["messages"][:5]
                if compiled.captions_edited:
                    regenerate_captions(compiled, self.draft_dir)
                protect_manual_edits(project, layer)
            else:
                compiled = compile_command_batches(batches)
            self.doc["draft_qc"] = _draft_qc_summary(st, compiled)
            arts["kdenlive_project"] = str(emit_kdenlive_xml(compiled, project))
            arts["otio_timeline"] = str(emit_otio_timeline(compiled, self.draft_dir / f"{st.sequence_id}_draft.otio"))
            arts["unresolved_effects"] = str(len(compiled.unresolved_effects))
            if layer.exists:
                layer.doc["emitted"] = {"sha256": file_sha256(project), "path": str(project), "revision": layer.revision}
                layer.save()
        for k in ("transcript_json", "captions_srt", "captions_ass", "qc_report_json", "rendered_video", "delivery_manifest_json", "highlights_json", "media_inventory_json"):
            if k in st.artifacts:
                arts[k] = st.artifacts[k]
        write_json_atomic(self.draft_dir / "analysis_state.json", st.to_dict())
        from bangonpro.ai_engine.department.project_index import build_project_index

        arts["project_index_json"] = str(write_json_atomic(self.draft_dir / "project_index.json", build_project_index(st)))
        _, stale = self.excluded_agents()
        for a, r in self.doc["agents"].items():
            r["stale"] = a in stale
        self._save(force=True)
        return arts

    # --------------------------------------------------------------- controls
    def cancel(self, reason: str = "Cancelled from Agent Control Centre") -> None:
        self.cancel_path.write_text(reason, encoding="utf-8")
        self._event("executive", "cancel_requested", {"reason": reason})

    @classmethod
    def pause(cls, job_dir: str | Path, *, reason: str = "Paused from Agent Control Centre") -> dict[str, Any]:
        """Request a cooperative pause between stages; reconciles immediately if no worker is alive."""
        job = cls(job_dir)
        if not job.doc:
            raise FileNotFoundError(f"No production job at {job_dir}")
        job.pause_path.write_text(reason, encoding="utf-8")
        job._event("executive", "pause_requested", {"reason": reason})
        alive = job.worker_alive()
        if not alive and job.doc.get("status") in ("running", "planned"):
            try:
                with worker_session(job.dir, command="pause"):
                    job._reconcile_owned()
            except JobBusyError:
                alive = True
        return {
            "status": job.doc.get("status"),
            "pause_requested": True,
            "worker_alive": alive,
            "job_dir": str(job.dir),
        }

    @classmethod
    def discard(cls, job_dir: str | Path) -> dict[str, Any]:
        """Safely delete a stopped job directory without ever touching source footage.

        Refuses with :class:`JobBusyError` while a live worker holds ``worker.lock``.
        """
        raw = Path(job_dir).expanduser()
        if raw.is_symlink():
            raise PermissionError(f"Refusing to discard symlink path: {raw}")
        target = raw.resolve()
        if not target.is_dir() or not (target / "job.json").is_file():
            raise ValueError(f"Not a valid production job directory (missing job.json): {target}")
        if worker_alive(target):
            raise JobBusyError("Cannot discard a production while its worker is still running. Pause or cancel it first.")
        with worker_session(target, command="discard"):
            # Verify no original source footage resides inside target before removal.
            plan_path = target / "plan.json"
            if plan_path.is_file():
                try:
                    plan = json.loads(plan_path.read_text(encoding="utf-8"))
                    for src_path in (plan.get("inputs") or {}).values():
                        if src_path:
                            sp = Path(str(src_path)).expanduser().resolve()
                            if sp == target or sp.is_relative_to(target):
                                raise PermissionError(f"Refusing to discard job directory containing source media: {sp}")
                except (OSError, ValueError):
                    pass
            shutil.rmtree(target)
        return {"status": "discarded", "job_dir": str(target)}

    def resume(self, progress_callback: Any = None) -> dict[str, Any]:
        """Continue from the last finished agent. Refuses (JobBusyError) while another worker owns the job.

        Work of an agent that did not finish (or whose checkpoint failed integrity verification) is
        discarded, not merged: only verified checkpoints of agents recorded as completed are trusted,
        and their batches are the only ones kept, so a resumed production never duplicates an edit.
        """
        with worker_session(self.dir, command="resume"):
            self._reconcile_owned()
            self.pause_path.unlink(missing_ok=True)
            valid = self._valid_checkpoints()
            max_valid_idx = valid[-1][0] if valid else -1
            for aid, r in self.doc["agents"].items():
                aidx = AGENT_ORDER.index(aid) if aid in AGENT_ORDER else -1
                if r["status"] in ("running", "cancelled", "failed", "blocked", "interrupted", "paused"):
                    r.update({"status": "pending", "error": None, "help": None})
                elif r.get("active") and r["status"] == "completed" and aidx > max_valid_idx:
                    # Checkpoint for this completed agent was corrupt/missing: re-run it cleanly.
                    r.update({"status": "pending", "decision": None, "batches": [], "error": None, "help": None})
            kept = {rid for r in self.doc["agents"].values() if r.get("status") == "completed" for rid in r.get("batches", [])}
            orphans = [rid for rid in self.doc.get("batches", {}) if rid not in kept]
            for rid in orphans:
                self.doc["batches"].pop(rid, None)
            self.doc["resume_attempts"] = int(self.doc.get("resume_attempts") or 0) + 1
            self.state = self._latest_state()
            self._event("executive", "job_resumed", {"attempt": self.doc["resume_attempts"], "discarded_partial_batches": len(orphans)})
            return self.run(progress_callback=progress_callback)

    def rerun(self, agent_id: str, *, profile_overrides: dict[str, Any] | None = None, progress_callback: Any = None, scope: dict[str, Any] | None = None) -> dict[str, Any]:
        """Re-execute one agent (and everything after it) from the checkpoint preceding it."""
        if agent_id == "executive":
            raise ValueError("Create a new job to re-plan; the executive plan is immutable per job")
        st = self._load_checkpoint_before(agent_id)
        st.metadata.pop("revision_scope", None)  # a stale scope from an earlier revision never re-applies
        if scope:
            st.metadata["revision_scope"] = scope
        if profile_overrides:
            from bangonpro.ai_engine.department.learning import (
                deep_merge,
                validate_overlay,
            )

            cleaned = validate_overlay(profile_overrides)
            st.profile = deep_merge(st.profile, cleaned)
            self._event(agent_id, "refinement_applied", {"profile_overrides": profile_overrides})
            # Recorded per job so the Learning Agent can spot the same correction across jobs.
            ProfilePreferenceStore(str(self.doc["profile_id"]), str(self.doc.get("series_id") or "default"), self.doc["options"].get("prefs_root")).record_correction(
                {"kind": "parameter_override", "job_id": self.doc["job_id"], "agent_id": agent_id, "overrides": cleaned}
            )
        idx = AGENT_ORDER.index(agent_id)
        self._discard_checkpoints_from(idx)
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

    # ------------------------------------------------------- conversational revisions
    def _locks(self, exclude: tuple[float, float] | None = None) -> list[list[float]]:
        out = [list(r) for r in self.doc.get("locked_ranges", [])]
        for r in self.doc.get("revised_ranges", []):
            if exclude is None or r[1] <= exclude[0] or r[0] >= exclude[1]:
                out.append(list(r))
        return out

    def revise(self, instruction: str, *, range_sec: tuple[float, float] | None = None, range_timeline: tuple[float, float] | None = None, progress_callback: Any = None) -> dict[str, Any]:
        """Apply a plain-language revision: interpret, snapshot, rerun only what it affects, keep the rest."""
        from bangonpro.ai_engine.department.revisions import (
            interpret_revision,
            previous_spans,
            timeline_to_master,
        )

        st_now = self._latest_state()
        if range_timeline is not None:
            sel = timeline_to_master(st_now, float(range_timeline[0]), float(range_timeline[1]))
            if sel is None:
                raise ValueError("The timeline selection is outside the draft; select a range on the draft timeline")
        else:
            sel = (float(range_sec[0]), float(range_sec[1])) if range_sec else None
        duration = max((a.duration_sec for a in st_now.media_assets.values()), default=0.0)
        plan = interpret_revision(instruction, st_now.profile, duration_sec=duration, selected_range=sel)
        revs = self.doc.setdefault("revisions", [])
        rec: dict[str, Any] = {"revision": len(revs) + 1, "ts_utc": _utc(), "instruction": instruction, "plan": plan, "status": None}
        if plan["not_understood"] or plan["needs_selection"]:
            if sel is not None:
                rec["selection_limited"] = True
            rec["status"] = "needs_selection" if plan["needs_selection"] else "not_understood"
            rec["message"] = ("Select the section on the timeline, then send the revision again" if plan["needs_selection"]
                              else "The full instruction could not be interpreted. Try e.g. 'tighten the opening', 'more reaction shots', 'preserve the suspense before reveals', 'five vertical clips'")
            self._event("executive", "revision_" + rec["status"], {"instruction": instruction})
            self.doc["last_revision"] = rec
            self._save(force=True)
            return rec
        snap = self.dir / "revisions" / f"rev_{rec['revision']:02d}"
        snap.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.ckpt_dir, snap / "checkpoints", dirs_exist_ok=True)
        shutil.copy2(self.job_path, snap / "job.json")
        for f in self.draft_dir.glob("*.*"):
            if f.suffix in (".kdenlive", ".otio", ".json", ".srt", ".ass"):
                shutil.copy2(f, snap / f.name)
        rng = tuple(plan["range_sec"]) if plan["range_sec"] else None
        locks = self._locks(exclude=rng)
        scope = {"range": list(rng) if rng else None, "locks": locks, "previous_spans": previous_spans(st_now)} if (rng or locks) else None
        overlay = dict(plan["overlay"])
        if rng is None and overlay:  # whole-project changes accumulate; range changes live in their range
            from bangonpro.ai_engine.department.learning import deep_merge

            self.doc["revision_overlay"] = deep_merge(self.doc.get("revision_overlay") or {}, overlay)
            overlay = self.doc["revision_overlay"]
        elif self.doc.get("revision_overlay"):
            from bangonpro.ai_engine.department.learning import deep_merge

            overlay = deep_merge(self.doc["revision_overlay"], overlay)
        t0 = time.perf_counter()
        self._event("executive", "revision_started", {"revision": rec["revision"], "instruction": instruction, "actions": plan["actions"], "range_sec": plan["range_sec"], "start_agent": plan["start_agent"]})
        doc = self.rerun(plan["start_agent"], profile_overrides=overlay or None, progress_callback=progress_callback, scope=scope)
        if rng:
            self.doc.setdefault("revised_ranges", []).append([rng[0], rng[1]])
        st = self.state or self._latest_state()
        rec.update({"status": doc["status"], "processing_sec": round(time.perf_counter() - t0, 3), "snapshot": str(snap),
                    "rerun_from": plan["start_agent"], "kept_from_previous": st.metadata.get("revision_scope_applied")})
        self.doc["revisions"].append(rec)
        self.doc["last_revision"] = {k: rec[k] for k in ("revision", "instruction", "status")}
        self._event("executive", "revision_completed", {"revision": rec["revision"], "status": doc["status"]})
        self._save(force=True)
        return rec

    def lock_range(self, start: float, end: float, *, timeline: bool = False) -> list[list[float]]:
        """Lock a master-clock (or draft-timeline) range: later revisions keep it exactly as it is."""
        from bangonpro.ai_engine.department.revisions import timeline_to_master

        rng = timeline_to_master(self._latest_state(), start, end) if timeline else (float(min(start, end)), float(max(start, end)))
        if rng is None:
            raise ValueError("The selection is outside the draft timeline")
        self.doc.setdefault("locked_ranges", []).append([round(rng[0], 3), round(rng[1], 3)])
        self._event("executive", "range_locked", {"range_sec": list(rng)})
        self._save(force=True)
        return self.doc["locked_ranges"]

    def undo_revision(self) -> dict[str, Any]:
        """Restore the draft exactly as it was before the most recent revision."""
        revs = [r for r in self.doc.get("revisions", []) if r.get("snapshot") and not r.get("undone")]
        if not revs:
            raise ValueError("No revision to undo")
        last = revs[-1]
        snap = Path(last["snapshot"])
        saved = json.loads((snap / "job.json").read_text(encoding="utf-8"))
        shutil.rmtree(self.ckpt_dir, ignore_errors=True)
        shutil.copytree(snap / "checkpoints", self.ckpt_dir)
        for f in snap.glob("*.*"):
            if f.name != "job.json":
                shutil.copy2(f, self.draft_dir / f.name)
        history = self.doc.get("revisions", [])
        for r in history:
            if r["revision"] == last["revision"]:
                r["undone"] = True
        saved["revisions"] = history
        self.doc = saved
        self.state = self._latest_state()
        self.rebuild_draft(self.state)
        self._event("executive", "revision_undone", {"revision": last["revision"]})
        self._save(force=True)
        return last

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
        self._discard_checkpoints_from(idx + 1)
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
        """Copy the draft project atomically to ``target_path``; replacing an existing edit requires approval."""
        arts = self.rebuild_draft()
        src = arts.get("kdenlive_project")
        if not src:
            raise FileNotFoundError("Draft has no timeline to promote")
        raw_target = Path(target_path).expanduser()
        if raw_target.is_symlink():
            raise PermissionError(f"Refusing to promote to a symlink target: {raw_target}")
        target = raw_target.resolve()
        sources = {Path(p).resolve() for p in (self.state or self._latest_state()).input_camera_files.values()}
        if target in sources or (target.exists() and any(s.exists() and os.path.samefile(target, s) for s in sources)):
            raise PermissionError("Refusing to overwrite original source media")
        backup = None
        if target.exists():
            if not approve:
                raise ApprovalRequiredError(f"'{target}' already exists; replacing an existing edit requires explicit approval (--approve)")
            backup = target.with_name(f"{target.name}.bak-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}")
            _copy_file_atomic(target, backup)
        _copy_file_atomic(Path(src), target)
        rec = {"ts_utc": _utc(), "action": "promote_draft", "target": str(target), "replaced_existing": backup is not None, "backup": str(backup) if backup else None, "approved": bool(approve)}
        self.doc["approvals"].append(rec)
        self._event("executive", "draft_promoted", rec)
        self._save(force=True)
        return rec

    def source_inventory(self) -> dict[str, dict[str, Any]]:
        """Expected source media per camera: path, SHA-256 and size (from ingest)."""
        st = self.state or self._latest_state()
        out: dict[str, dict[str, Any]] = {}
        for cam, path in st.input_camera_files.items():
            out[cam] = {"path": str(path), "sha256": None, "size_bytes": None, "aliases": {str(path), str(Path(path).expanduser().absolute())}}
        for row in (st.metadata.get("media_inventory") or {}).get("inputs", []):
            if row.get("camera_id") in out and row.get("sha256"):
                out[row["camera_id"]].update({"sha256": row["sha256"], "size_bytes": row.get("size_bytes")})
                out[row["camera_id"]]["aliases"].add(str(row.get("path")))
        for cam, asset in st.media_assets.items():
            if cam in out and asset.sha256:
                out[cam].update({"sha256": asset.sha256, "size_bytes": asset.file_size_bytes})
                out[cam]["aliases"].add(str(asset.file_path))
        return out

    def relink(self, search_dirs: list[str | Path], *, project_files: list[str | Path] | None = None, dry_run: bool = False) -> dict[str, Any]:
        """Find moved or renamed source media by SHA-256 and repoint the job (and saved projects).

        Only files whose content hash matches the hash recorded at ingest are accepted, so a
        different take with the same name is never linked. Source media are only read. The job's
        checkpoints and plan are rewritten, the draft is rebuilt, and each ``project_files``
        entry (an editor's saved ``.kdenlive``) is updated in place after a timestamped backup.
        """
        expected = self.source_inventory()
        moved = {cam: e for cam, e in expected.items() if not Path(e["path"]).is_file() or (e["sha256"] and e["size_bytes"] and Path(e["path"]).stat().st_size != e["size_bytes"])}
        unverifiable = sorted(cam for cam, e in moved.items() if not e["sha256"])
        wanted = {cam: e for cam, e in moved.items() if e["sha256"]}
        by_size: dict[int, list[str]] = {}
        for cam, e in wanted.items():
            by_size.setdefault(int(e["size_bytes"] or -1), []).append(cam)
        found: dict[str, str] = {}
        for root in search_dirs:
            base = Path(root).expanduser()
            if not base.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    f = Path(dirpath) / name
                    try:
                        size = f.stat().st_size
                    except OSError:
                        continue
                    for cam in by_size.get(size, []):
                        if cam not in found and compute_file_sha256(f, use_cache=False) == wanted[cam]["sha256"]:
                            found[cam] = str(f.resolve())
        mapping = {alias: new for cam, new in found.items() for alias in wanted[cam]["aliases"] if alias and alias != new}
        result: dict[str, Any] = {
            "relinked": {cam: {"from": wanted[cam]["path"], "to": new} for cam, new in sorted(found.items())},
            "still_missing": sorted(set(wanted) - set(found)),
            "unverifiable": unverifiable,
            "projects_updated": [],
            "dry_run": dry_run,
        }
        if dry_run or not mapping:
            return result
        # Validate all target project files up front before mutating any checkpoints or plan.json.
        st_before = self.state or self._latest_state()
        protected_sources = {Path(p).resolve() for p in st_before.input_camera_files.values()}
        protected_sources.update(Path(p).resolve() for p in found.values())
        validated_projects: list[Path] = []
        for project in project_files or []:
            raw_pf = Path(project).expanduser()
            if raw_pf.is_symlink():
                raise PermissionError(f"Refusing to overwrite symlink project file: {raw_pf}")
            pf = raw_pf.resolve()
            if not pf.is_file():
                continue
            if pf in protected_sources or any(s.exists() and os.path.samefile(pf, s) for s in protected_sources):
                raise PermissionError(f"Refusing to overwrite original source media: {pf}")
            validated_projects.append(pf)
        for _, ckpt in self._valid_checkpoints():
            data = json.loads(ckpt.read_text(encoding="utf-8"))
            write_checkpoint_atomic(ckpt, _replace_paths(data, mapping))
        write_json_atomic(self.plan_path, _replace_paths(self.plan, mapping))
        self.state = self._latest_state()
        for pf in validated_projects:
            text = pf.read_text(encoding="utf-8")
            updated = _sub_paths(text, {html.escape(old, quote=False): html.escape(new, quote=False) for old, new in mapping.items()})
            if updated != text:
                backup = pf.with_name(f"{pf.name}.bak-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}")
                _copy_file_atomic(pf, backup)
                _write_bytes_atomic(pf, updated.encode("utf-8"))
                result["projects_updated"].append({"project": str(pf), "backup": str(backup)})
        self.doc.setdefault("relinks", []).append({"ts_utc": _utc(), **{k: result[k] for k in ("relinked", "still_missing", "projects_updated")}})
        self._event("executive", "media_relinked", {"relinked": result["relinked"], "still_missing": result["still_missing"]})
        self.rebuild_draft(self.state)
        integrity = verify_source_integrity(self.state) if self.state.media_assets else {}
        result["source_integrity"] = {"verified": all(integrity.values()) if integrity else None, "per_source": integrity}
        return result

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
            agents.append({k: r.get(k) for k in ("agent_id", "display_name", "active", "activation_reason", "status", "decision", "summary", "rationale", "confidence", "duration_sec", "operation_count", "neural_inference", "fallback_reason", "review_items", "cache_hits", "est_external_cost_usd", "error", "stale", "quality_score", "quality_loop")})
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
            "final_verification": self.doc.get("final_verification", {}),
            "quality_loop_attempts": int(self.doc.get("quality_loop_attempts", 0)),
            "quality_loop_processing_sec": float(self.doc.get("quality_loop_processing_sec", 0.0)),
            "quality_loop_cost_usd": float(self.doc.get("quality_loop_cost_usd", 0.0)),
        }


# One review item per root cause, owned by the agent whose action fixes it (review workload).
_REVIEW_ROOT_CAUSES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("no_spoken_transcript", "editorial_intelligence", re.compile(r"whisper|spoken transcript|speech regions only|captions (are|will be) withheld|vad placeholders", re.I)),
    ("social_not_rendered", "distribution", re.compile(r"SOCIAL_WITHOUT_EXPORT|(social|highlight)[^.]*not rendered", re.I)),
    ("speaker_names_missing", "graphics", re.compile(r"no names for|no speakers identified for lower thirds|speaker_names", re.I)),
    ("colour_unmatched", "colourist", re.compile(r"hit the gain limit|differs from \S+ by delta e|grey-world fallback", re.I)),
    ("vertical_framing_uncertain", "distribution", re.compile(r"vertical clip \d+: no subject evidence", re.I)),
    ("uncertain_speaker", "rough_cut", re.compile(r"low-confidence transcript segment|shots? to check for pacing or angle", re.I)),
)


def _draft_qc_summary(st: ProjectAnalysisState, compiled: Any) -> dict[str, Any]:
    """Independent QC of the compiled draft (``draft_qc.check_draft``); a QC failure is reported, never fatal."""
    from bangonpro.ai_engine.department.draft_qc import DraftView, check_draft

    try:
        defects = check_draft(DraftView(st, compiled))
    except Exception as exc:  # noqa: BLE001 - QC must not stop the draft being written
        return {"checked": False, "error": f"{type(exc).__name__}: {exc}"[:300], "defects": []}
    keep = ("code", "severity", "agent", "confidence", "start", "end", "message")
    return {"checked": True, "defect_count": len(defects), "error_count": sum(d["severity"] in ql.CRITICAL_SEVERITIES for d in defects),
            "codes": sorted({d["code"] for d in defects}), "defects": [{k: d[k] for k in keep} for d in defects[:50]]}


def _consolidate_review_items(doc: dict[str, Any]) -> None:
    """Drop repeats of the same root cause raised by several agents; keep the owner's item."""
    agents = doc.get("agents", {})
    for _cause, owner, pat in _REVIEW_ROOT_CAUSES:
        hits = [(a, i) for a in AGENT_ORDER for i, item in enumerate(agents[a].get("review_items") or []) if pat.search(str(item))]
        if len(hits) < 2:
            continue
        keep = next((h for h in hits if h[0] == owner), hits[0])
        drop: dict[str, set[int]] = {}
        for a, i in hits:
            if (a, i) != keep:
                drop.setdefault(a, set()).add(i)
        # Details of the merged items are kept in the surviving item (nothing is hidden)
        extra = [str(agents[a]["review_items"][i]) for a, i in hits if (a, i) != keep]
        ka, ki = keep
        agents[ka]["review_items"][ki] = f"{agents[ka]['review_items'][ki]} (also: {'; '.join(extra)})"
        for a, idx in drop.items():
            agents[a]["review_items"] = [x for j, x in enumerate(agents[a]["review_items"]) if j not in idx]
    doc["review_items_consolidated"] = True


def _relocate_attempt_outputs(attempt_dir: Path, draft_dir: Path, trial: ProjectAnalysisState, outcome: AgentOutcome) -> None:
    """Move an accepted correction's artefacts into the draft and rewrite their paths."""
    src_prefix, dst_prefix = str(attempt_dir), str(draft_dir)
    for f in attempt_dir.rglob("*"):
        if f.is_file():
            dest = draft_dir / f.relative_to(attempt_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)

    def _fix(v: Any) -> Any:
        return v.replace(src_prefix, dst_prefix, 1) if isinstance(v, str) and v.startswith(src_prefix) else v

    trial.artifacts = {k: _fix(v) for k, v in trial.artifacts.items()}
    outcome.outputs = {k: _fix(v) for k, v in outcome.outputs.items()}


def _sub_paths(text: str, mapping: dict[str, str]) -> str:
    """Single-pass replacement (longest path first) so a new path is never rewritten again."""
    if not mapping:
        return text
    pattern = re.compile("|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True)))
    return pattern.sub(lambda m: mapping[m.group(0)], text)


def _replace_paths(value: Any, mapping: dict[str, str]) -> Any:
    """Rewrite every occurrence of an old source path inside a JSON structure."""
    if isinstance(value, str):
        return _sub_paths(value, mapping) if any(k in value for k in mapping) else value
    if isinstance(value, list):
        return [_replace_paths(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: _replace_paths(v, mapping) for k, v in value.items()}
    return value


def _test_delay(token: CancellationToken, pause_path: Path | None = None) -> None:
    """Test-only slow motion (``BANGONPRO_TEST_AGENT_DELAY_SEC``) used to interrupt/pause a worker mid-job."""
    try:
        delay = float(os.environ.get("BANGONPRO_TEST_AGENT_DELAY_SEC") or 0.0)
    except ValueError:
        delay = 0.0
    end = time.monotonic() + max(0.0, min(delay, 60.0))
    while time.monotonic() < end:
        token.check("test delay")
        if pause_path is not None and pause_path.exists():
            return
        time.sleep(0.05)


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


def _interrupted_help() -> dict[str, Any]:
    return {
        "code": "interrupted",
        "title": "Production stopped before it finished",
        "message": "Bang On Pro was closed, or the production process stopped unexpectedly. Every finished stage is kept.",
        "fix": "Press Resume to continue from the last finished stage. Nothing is redone twice.",
        "fix_action": "resume_job",
    }


_AUDIO_SUFFIXES = (".wav", ".mp3", ".flac", ".aac", ".m4a")


def _count_video_inputs(inputs: list[str | Path] | dict[str, str | Path] | None) -> int:
    if not inputs:
        return 0
    paths = list(inputs.values()) if isinstance(inputs, dict) else [str(i).split("=", 1)[-1] for i in inputs]
    return sum(1 for p in paths if Path(str(p)).suffix.lower() not in _AUDIO_SUFFIXES)
