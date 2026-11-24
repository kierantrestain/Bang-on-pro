"""``python -m bangonpro.ai_engine.department`` - production department CLI.

Every command prints one JSON object on stdout (consumed by the C++ Agent
Control Centre). ``run`` output keeps the V1.1 ``artifacts`` contract
(``timeline_commands_json``, ``kdenlive_project``, ``transcript_json``).

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from bangonpro.ai_engine.department.agents import JobOptions
from bangonpro.ai_engine.department.cache import AnalysisCache
from bangonpro.ai_engine.department.jobs import ApprovalRequiredError, ProductionJob
from bangonpro.ai_engine.department.learning import (
    PreferenceError,
    ProfilePreferenceStore,
    suggest_from_decisions,
    suggest_from_project_diff,
)
from bangonpro.ai_engine.department.measurement import (
    MeasurementLog,
    modelled_estimate,
    savings_report,
)
from bangonpro.ai_engine.department.registry import (
    AGENT_ORDER,
    AUTOMATION_MODES,
    capability_matrix,
)


def _emit(payload: Any) -> int:
    sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")
    return 0


def _job_payload(job: ProductionJob) -> dict[str, Any]:
    d = job.doc
    return {
        "job_id": d["job_id"],
        "job_dir": str(job.dir),
        "status": d["status"],
        "mode": d["mode"],
        "needs_clarification": d["status"] == "needs_clarification",
        "clarification_question": d.get("clarification_question"),
        "agents": {a: {k: r.get(k) for k in ("status", "active", "decision", "confidence", "summary", "operation_count", "stale", "error")} for a, r in d["agents"].items()},
        "artifacts": d.get("artifacts", {}),
        "source_integrity": d.get("source_integrity"),
        "processing_elapsed_sec": d.get("processing_elapsed_sec"),
    }


def _parse_overrides(enable: list[str], disable: list[str]) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for a in enable or []:
        out[a] = True
    for a in disable or []:
        out[a] = False
    for a in out:
        if a not in AGENT_ORDER:
            raise SystemExit(f"Unknown agent '{a}'")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bangonpro-department", description="Bang On Pro AI Production Department")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("agents", help="Print the agent capability matrix")

    r = sub.add_parser("run", help="Create and run a production job")
    r.add_argument("brief", nargs="?", default="")
    r.add_argument("--inputs", nargs="*", default=[])
    r.add_argument("--profile", default=None)
    r.add_argument("--mode", choices=AUTOMATION_MODES, default="assisted")
    r.add_argument("--job-root", default=str(Path.home() / ".local/share/bangonpro/jobs"))
    r.add_argument("--job-id", default=None)
    r.add_argument("--project-id", default=None)
    r.add_argument("--sequence-id", default=None)
    r.add_argument("--kdenlive-project", default=None)
    r.add_argument("--project-state", default=None)
    r.add_argument("--export", action="store_true")
    r.add_argument("--series", default="default")
    r.add_argument("--episode-title", default="")
    r.add_argument("--budget-usd", type=float, default=0.0)
    r.add_argument("--model-dir", default=None)
    r.add_argument("--prefs-root", default=None)
    r.add_argument("--enable", nargs="*", default=[])
    r.add_argument("--disable", nargs="*", default=[])
    r.add_argument("--plan-only", action="store_true")

    for name in ("status", "resume", "cancel", "summary", "rebuild"):
        q = sub.add_parser(name)
        q.add_argument("job_dir")

    rr = sub.add_parser("rerun", help="Rerun one agent (and later agents) from its checkpoint")
    rr.add_argument("job_dir")
    rr.add_argument("agent")
    rr.add_argument("--profile-overrides", default=None, help="JSON overlay to refine this rerun")

    rb = sub.add_parser("rollback")
    rb.add_argument("job_dir")
    rb.add_argument("agent")

    dc = sub.add_parser("decide", help="Accept or reject an agent's edits (optionally individual ops)")
    dc.add_argument("job_dir")
    dc.add_argument("agent")
    dc.add_argument("decision", choices=("accept", "reject"))
    dc.add_argument("--reject-ops", nargs="*", default=[])
    dc.add_argument("--note", default="")

    pr = sub.add_parser("promote", help="Copy the draft into a project path (replacing requires --approve)")
    pr.add_argument("job_dir")
    pr.add_argument("target")
    pr.add_argument("--approve", action="store_true")

    ln = sub.add_parser("learn", help="Profile preference learning")
    ln.add_argument("action", choices=("suggest", "list", "approve", "reject", "rollback", "reset", "versions", "diff"))
    ln.add_argument("--profile", required=True)
    ln.add_argument("--series", default="default")
    ln.add_argument("--prefs-root", default=None)
    ln.add_argument("--id", default=None)
    ln.add_argument("--version", type=int, default=None)
    ln.add_argument("--ai-project", default=None)
    ln.add_argument("--final-project", default=None)

    ms = sub.add_parser("measure", help="Editing-time measurement")
    ms.add_argument("action", choices=("start", "activity", "finish", "report", "estimate"))
    ms.add_argument("--root", default=str(Path.home() / ".local/share/bangonpro/measurement"))
    ms.add_argument("--deliverable", default="")
    ms.add_argument("--kind", default="agent_assisted")
    ms.add_argument("--editor", default="editor")
    ms.add_argument("--job-dir", default=None)
    ms.add_argument("--session", default=None)
    ms.add_argument("--category", default="editing")
    ms.add_argument("--quality", type=float, default=None)
    ms.add_argument("--evidence-class", default="physical_hardware")
    ms.add_argument("--jobs", nargs="*", default=[])

    cc = sub.add_parser("cache", help="Shared analysis cache")
    cc.add_argument("action", choices=("stats", "clear", "prune"))

    a = p.parse_args(argv)

    if a.cmd == "agents":
        return _emit({"agents": capability_matrix(), "modes": list(AUTOMATION_MODES)})

    if a.cmd == "run":
        opts = JobOptions(
            mode=a.mode,
            export_video=a.export,
            budget_usd=a.budget_usd,
            series_id=a.series,
            episode_title=a.episode_title,
            model_dir=a.model_dir,
            prefs_root=a.prefs_root,
            enabled_overrides=_parse_overrides(a.enable, a.disable),
        )
        job = ProductionJob.create(
            job_root=a.job_root, brief=a.brief, inputs=a.inputs or None, profile_ref=a.profile, mode=a.mode, options=opts,
            project_id=a.project_id, sequence_id=a.sequence_id, kdenlive_project=a.kdenlive_project, project_state=a.project_state, job_id=a.job_id,
        )
        if not a.plan_only:
            job.run()
        return _emit(_job_payload(job))

    if a.cmd == "learn":
        store = ProfilePreferenceStore(a.profile, a.series, a.prefs_root)
        try:
            if a.action == "suggest":
                from bangonpro.show_profiles.loader import load_show_profile

                profile, _ = store.apply_to_profile(load_show_profile(a.profile))
                return _emit({"suggestions": store.propose(suggest_from_decisions(profile, store.corrections()))})
            if a.action == "diff":
                from bangonpro.ai_engine.planner import ingest_kdenlive_project
                from bangonpro.show_profiles.loader import load_show_profile

                profile, _ = store.apply_to_profile(load_show_profile(a.profile))

                def _shots(path: str) -> list[dict[str, Any]]:
                    ctx = ingest_kdenlive_project(path)
                    return [{"camera_id": Path(c.file_path or c.clip_name).stem, "duration_sec": c.duration_sec} for c in ctx.clips if c.track_id.upper().startswith("V") and c.clip_kind != "kdenlivetitle"]

                sugg = suggest_from_project_diff(profile, _shots(a.ai_project), _shots(a.final_project), profile.get("camera_roles", {}))
                return _emit({"suggestions": store.propose(sugg)})
            if a.action == "list":
                return _emit({"active_version": store.active_version(), "suggestions": store.suggestions()})
            if a.action in ("approve", "reject"):
                v = store.decide_suggestion(a.id, approve=a.action == "approve")
                return _emit({"suggestion_id": a.id, "decision": a.action, "active_version": store.active_version(), "new_version": v})
            if a.action == "rollback":
                return _emit({"active_version": store.rollback(int(a.version or 0))})
            if a.action == "reset":
                return _emit({"active_version": store.reset()})
            return _emit({"active_version": store.active_version(), "versions": store.list_versions()})
        except PreferenceError as exc:
            return _emit({"error": str(exc)}) or 2

    if a.cmd == "measure":
        log = MeasurementLog(a.root)
        if a.action == "start":
            job_id = ProductionJob(a.job_dir).doc.get("job_id") if a.job_dir else None
            return _emit({"session_id": log.start(deliverable_id=a.deliverable, kind=a.kind, editor=a.editor, job_id=job_id, evidence_class=a.evidence_class)})
        if a.action == "activity":
            log.activity(a.session, a.category)
            return _emit({"ok": True})
        if a.action == "finish":
            log.finish(a.session, quality_rating=a.quality)
            return _emit({"ok": True})
        if a.action == "estimate":
            return _emit(modelled_estimate(ProductionJob(a.job_dir).doc))
        jobs = {ProductionJob(j).doc["job_id"]: ProductionJob(j).doc for j in a.jobs}
        return _emit(savings_report(log.sessions(), jobs))

    if a.cmd == "cache":
        c = AnalysisCache()
        if a.action == "clear":
            return _emit({"removed": c.clear()})
        if a.action == "prune":
            return _emit({"removed": c.prune()})
        return _emit(c.stats())

    job = ProductionJob(a.job_dir)
    if not job.doc:
        return _emit({"error": f"No job at {a.job_dir}"}) or 2
    if a.cmd == "status":
        return _emit(job.doc)
    if a.cmd == "summary":
        return _emit(json.loads((job.dir / "summary.json").read_text(encoding="utf-8")))
    if a.cmd == "cancel":
        job.cancel()
        return _emit({"cancel_requested": True})
    if a.cmd == "resume":
        job.resume()
    elif a.cmd == "rebuild":
        job.rebuild_draft()
    elif a.cmd == "rerun":
        job.rerun(a.agent, profile_overrides=json.loads(a.profile_overrides) if a.profile_overrides else None)
    elif a.cmd == "rollback":
        job.rollback(a.agent)
    elif a.cmd == "decide":
        job.decide(a.agent, a.decision, rejected_op_ids=a.reject_ops, note=a.note)
    elif a.cmd == "promote":
        try:
            return _emit(job.promote(a.target, approve=a.approve))
        except ApprovalRequiredError as exc:
            _emit({"approval_required": True, "error": str(exc)})
            return 3
    return _emit(_job_payload(job))


if __name__ == "__main__":
    raise SystemExit(main())
