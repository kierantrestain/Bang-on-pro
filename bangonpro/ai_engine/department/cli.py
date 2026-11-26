"""``python -m bangonpro.ai_engine.department`` - production department CLI.

Every command prints one JSON object on stdout (consumed by the C++ Agent
Control Centre), except ``measure report`` which prints a table unless
``--json`` is given. ``run`` output keeps the V1.1 ``artifacts`` contract
(``timeline_commands_json``, ``kdenlive_project``, ``transcript_json``).
Failures print ``{"error", "help"}`` where ``help`` is a plain-language fix
(see ``errors.explain``) and exit with status 2.

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
from bangonpro.ai_engine.department.errors import explain
from bangonpro.ai_engine.department.jobs import ApprovalRequiredError, ProductionJob
from bangonpro.ai_engine.department.learning import (
    PreferenceError,
    ProfilePreferenceStore,
    detect_recurring_preferences,
    diff_job_against_final,
    export_preference_bundle,
    import_preference_bundle,
    shots_from_kdenlive,
    suggest_from_decisions,
    suggest_from_project_diff,
)
from bangonpro.ai_engine.department.measurement import (
    MeasurementLog,
    modelled_estimate,
    render_table,
    savings_report,
    write_report_files,
)
from bangonpro.ai_engine.department.queue import JobQueue, QueueError, job_history
from bangonpro.ai_engine.department.registry import (
    AGENT_ORDER,
    AUTOMATION_MODES,
    capability_matrix,
)
from bangonpro.ai_engine.department.version import APP_VERSION

DEFAULT_JOB_ROOT = str(Path.home() / ".local/share/bangonpro/jobs")
DEFAULT_MEASURE_ROOT = str(Path.home() / ".local/share/bangonpro/measurement")


def _emit(payload: Any) -> int:
    sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")
    return 0


def _fail(exc: BaseException | str, code: int = 2) -> int:
    _emit({"error": str(exc), "help": explain(exc)})
    return code


def _job_payload(job: ProductionJob) -> dict[str, Any]:
    d = job.doc
    return {
        "job_id": d["job_id"],
        "job_dir": str(job.dir),
        "status": d["status"],
        "mode": d["mode"],
        "needs_clarification": d["status"] == "needs_clarification",
        "clarification_question": d.get("clarification_question"),
        "agents": {a: {k: r.get(k) for k in ("status", "active", "decision", "confidence", "summary", "operation_count", "stale", "error", "help")} for a, r in d["agents"].items()},
        "artifacts": d.get("artifacts", {}),
        "source_integrity": d.get("source_integrity"),
        "processing_elapsed_sec": d.get("processing_elapsed_sec"),
        "error": d.get("error"),
        "error_help": d.get("error_help"),
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


def _add_job_args(r: argparse.ArgumentParser) -> None:
    r.add_argument("brief", nargs="?", default="")
    r.add_argument("--inputs", nargs="*", default=[])
    r.add_argument("--profile", default=None)
    r.add_argument("--mode", choices=AUTOMATION_MODES, default="assisted")
    r.add_argument("--job-root", default=DEFAULT_JOB_ROOT)
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
    r.add_argument("--asr-model", default=None, help="Installed Whisper model id (default: best installed)")
    r.add_argument("--prefs-root", default=None)
    r.add_argument("--enable", nargs="*", default=[])
    r.add_argument("--disable", nargs="*", default=[])


def _options_from_args(a: argparse.Namespace) -> JobOptions:
    return JobOptions(
        mode=a.mode,
        export_video=a.export,
        budget_usd=a.budget_usd,
        series_id=a.series,
        episode_title=a.episode_title,
        model_dir=a.model_dir,
        asr_model_id=a.asr_model,
        prefs_root=a.prefs_root,
        enabled_overrides=_parse_overrides(a.enable, a.disable),
    )


def _queue(a: argparse.Namespace) -> JobQueue:
    return JobQueue(a.queue_root, job_root=a.job_root if getattr(a, "job_root", None) else None)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bangonpro-department", description="Bang On Pro AI Production Department")
    p.add_argument("--version", action="version", version=f"Bang On Pro {APP_VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("agents", help="Print the agent capability matrix")

    r = sub.add_parser("run", help="Create and run a production job")
    _add_job_args(r)
    r.add_argument("--plan-only", action="store_true")

    for name in ("status", "resume", "cancel", "summary", "rebuild"):
        q = sub.add_parser(name)
        q.add_argument("job_dir")

    rr = sub.add_parser("rerun", help="Rerun one agent (and later agents) from its checkpoint")
    rr.add_argument("job_dir")
    rr.add_argument("agent")
    rr.add_argument("--profile-overrides", default=None, help="JSON overlay to refine this rerun")

    rv = sub.add_parser("revise", help="Apply a plain-language revision to a draft (whole project or a range)")
    rv.add_argument("job_dir")
    rv.add_argument("instruction")
    rv.add_argument("--range", nargs=2, type=float, default=None, metavar=("START", "END"), help="master-clock seconds")
    rv.add_argument("--range-timeline", nargs=2, type=float, default=None, metavar=("START", "END"), help="seconds on the draft timeline")
    lk = sub.add_parser("lock", help="Lock a range so revisions keep it unchanged")
    lk.add_argument("job_dir")
    lk.add_argument("start", type=float)
    lk.add_argument("end", type=float)
    lk.add_argument("--timeline", action="store_true", help="range is on the draft timeline")
    ur = sub.add_parser("undo-revision", help="Restore the draft from before the last revision")
    ur.add_argument("job_dir")

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

    rl = sub.add_parser("relink", help="Find moved source media by SHA-256 and repoint the job")
    rl.add_argument("job_dir")
    rl.add_argument("--search", nargs="+", required=True, help="Folders to search (recursively)")
    rl.add_argument("--project", nargs="*", default=[], help="Saved .kdenlive projects to update (a backup is kept)")
    rl.add_argument("--dry-run", action="store_true")

    ln = sub.add_parser("learn", help="Profile preference learning")
    ln.add_argument("action", choices=("suggest", "recurring", "list", "approve", "reject", "rollback", "reset", "versions", "diff", "export", "import"))
    ln.add_argument("--profile", default=None)
    ln.add_argument("--series", default="default")
    ln.add_argument("--prefs-root", default=None)
    ln.add_argument("--id", default=None)
    ln.add_argument("--version", type=int, default=None)
    ln.add_argument("--ai-project", default=None)
    ln.add_argument("--final-project", default=None)
    ln.add_argument("--job", default=None, help="Job folder: compare its draft with --final-project")
    ln.add_argument("--min-jobs", type=int, default=2)
    ln.add_argument("--out", default=None, help="Bundle file to write (export)")
    ln.add_argument("--bundle", default=None, help="Bundle file to read (import)")
    ln.add_argument("--editor", default="")
    ln.add_argument("--any-profile", action="store_true", help="Allow importing a bundle made for another profile")

    ms = sub.add_parser("measure", help="Editing-time measurement")
    ms.add_argument("action", choices=("start", "activity", "pause", "resume", "finish", "link", "open", "report", "estimate"))
    ms.add_argument("--root", default=DEFAULT_MEASURE_ROOT)
    ms.add_argument("--deliverable", default="")
    ms.add_argument("--kind", default="agent_assisted")
    ms.add_argument("--editor", default="editor")
    ms.add_argument("--job-dir", default=None)
    ms.add_argument("--session", default=None)
    ms.add_argument("--category", default="editing")
    ms.add_argument("--detail", default="")
    ms.add_argument("--quality", type=float, default=None, help="Editor's 1-5 quality rating for the deliverable")
    ms.add_argument("--notes", default="")
    ms.add_argument("--evidence-class", default="physical_hardware")
    ms.add_argument("--jobs", nargs="*", default=[])
    ms.add_argument("--out-dir", default=None, help="Where report JSON and CSV are written (default: <root>/reports)")
    ms.add_argument("--json", action="store_true", help="Print the report as JSON instead of a table")
    ms.add_argument("--idle-cutoff-sec", type=float, default=300.0)

    cc = sub.add_parser("cache", help="Shared analysis cache")
    cc.add_argument("action", choices=("stats", "clear", "prune"))
    cc.add_argument("--namespace", default=None, help="Limit to one namespace (see stats)")
    cc.add_argument("--max-mb", type=float, default=None, help="prune: target size in MB")
    cc.add_argument("--cache-dir", default=None)

    sub.add_parser("models", help="Local model status (no downloads)").add_argument("--model-dir", default=None)

    rd = sub.add_parser("readiness", help="System check: hardware, storage, tools, runtime, models, cache")
    rd.add_argument("--job-root", default=None)
    rd.add_argument("--cache-dir", default=None)
    rd.add_argument("--output-dir", default=None)
    rd.add_argument("--model-dir", default=None)
    rd.add_argument("--quick", action="store_true", help="Skip the slower hardware and accelerator probe")

    dg = sub.add_parser("diagnostics", help="Export a sanitised diagnostics zip")
    dg.add_argument("action", choices=("export",))
    dg.add_argument("--out", required=True)
    dg.add_argument("--job", nargs="*", default=[], help="Job folders to include (default: the most recent jobs)")
    dg.add_argument("--job-root", default=None)
    dg.add_argument("--recent", type=int, default=3)
    dg.add_argument("--queue-root", default=None)
    dg.add_argument("--cache-dir", default=None)
    dg.add_argument("--model-dir", default=None)
    dg.add_argument("--include-media", action="store_true", help="Also include transcripts, captions, timelines and renders (opt-in)")
    dg.add_argument("--quick", action="store_true")

    qu = sub.add_parser("queue", help="Production queue and series batches")
    qsub = qu.add_subparsers(dest="queue_cmd", required=True)
    qa = qsub.add_parser("add", help="Queue a production (same settings as run)")
    _add_job_args(qa)
    qa.add_argument("--priority", type=int, default=0)
    qa.add_argument("--queue-root", default=None)
    qs = qsub.add_parser("series", help="Queue a series batch from an episodes JSON file")
    qs.add_argument("episodes_json", help='[{"title": "Ep 1", "inputs": ["cam_wide=/path/wide.mp4", ...], "brief": "..."}]')
    qs.add_argument("--profile", required=True)
    qs.add_argument("--series", required=True)
    qs.add_argument("--mode", choices=AUTOMATION_MODES, default="autonomous_first_cut")
    qs.add_argument("--brief", default="")
    qs.add_argument("--export", action="store_true")
    qs.add_argument("--priority", type=int, default=0)
    qs.add_argument("--job-root", default=DEFAULT_JOB_ROOT)
    qs.add_argument("--queue-root", default=None)
    for name in ("list", "status"):
        ql_ = qsub.add_parser(name)
        ql_.add_argument("--queue-root", default=None)
    qr = qsub.add_parser("run", help="Run queued productions one after another")
    qr.add_argument("--queue-root", default=None)
    qr.add_argument("--max-jobs", type=int, default=None)
    qr.add_argument("--stale-after-sec", type=float, default=None)
    for name in ("cancel", "remove"):
        qc = qsub.add_parser(name)
        qc.add_argument("queue_id")
        qc.add_argument("--queue-root", default=None)
    qp = qsub.add_parser("priority")
    qp.add_argument("queue_id")
    qp.add_argument("priority", type=int)
    qp.add_argument("--queue-root", default=None)

    hi = sub.add_parser("history", help="Recent production jobs and the queue")
    hi.add_argument("--job-root", default=DEFAULT_JOB_ROOT)
    hi.add_argument("--queue-root", default=None)
    hi.add_argument("--limit", type=int, default=30)

    pf = sub.add_parser("profile", help="Show profiles: list, show, create from a base, validate")
    pf.add_argument("action", choices=("list", "show", "create", "validate"))
    pf.add_argument("ref", nargs="?", default=None, help="Profile id or JSON file")
    pf.add_argument("--base", default=None)
    pf.add_argument("--id", default=None)
    pf.add_argument("--name", default=None)
    pf.add_argument("--set", nargs="*", default=[], help="key=value (dotted keys, JSON values), e.g. editing_rules.min_silence_sec=1.2")
    pf.add_argument("--out", default=None, help="Folder or file (default: the custom profiles folder)")
    return p


def _set_dotted(target: dict[str, Any], dotted: str, raw: str) -> None:
    try:
        value: Any = json.loads(raw)
    except ValueError:
        value = raw
    node = target
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):
            raise ValueError(f"'{dotted}' does not name a setting")
    node[parts[-1]] = value


def _cmd_profile(a: argparse.Namespace) -> int:
    from bangonpro.show_profiles.loader import (
        create_custom_profile,
        default_custom_profiles_dir,
        list_show_profiles,
        load_show_profile,
        save_custom_profile,
        user_profile_dirs,
        validate_show_profile,
    )

    if a.action == "list":
        builtin = set(list_show_profiles())
        custom = sorted(set(list_show_profiles(user_profile_dirs())) - builtin)
        return _emit({"builtin": sorted(builtin), "custom": custom, "custom_dir": str(default_custom_profiles_dir())})
    if a.action == "show":
        return _emit(load_show_profile(a.ref, custom_dirs=user_profile_dirs()))
    if a.action == "validate":
        path = Path(a.ref or "")
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else load_show_profile(a.ref, custom_dirs=user_profile_dirs())
        validate_show_profile(data)
        return _emit({"valid": True, "profile_id": data.get("profile_id")})
    if not a.id or not a.base:
        raise ValueError("profile create needs --base <profile> and --id <new_profile_id>")
    base = load_show_profile(a.base, custom_dirs=user_profile_dirs())
    for item in a.set:
        key, sep, raw = item.partition("=")
        if not sep:
            raise ValueError(f"Expected key=value, got '{item}'")
        _set_dotted(base, key.strip(), raw.strip())
    prof = create_custom_profile(profile_id=a.id, display_name=a.name or a.id.replace("_", " ").capitalize(), format_type=base.get("format_type", "custom"), base_profile=base)
    path = save_custom_profile(prof, a.out or default_custom_profiles_dir())
    return _emit({"profile_id": prof["profile_id"], "path": str(path), "valid": True})


def _cmd_queue(a: argparse.Namespace) -> int:
    q = _queue(a)
    c = a.queue_cmd
    if c == "add":
        opts = _options_from_args(a)
        options = {k: v for k, v in vars(opts).items() if k not in ("provider_config", "mode")}
        spec = {"brief": a.brief, "inputs": a.inputs or None, "profile": a.profile, "mode": a.mode, "options": options}
        for key, val in (("kdenlive_project", a.kdenlive_project), ("project_id", a.project_id), ("sequence_id", a.sequence_id)):
            if val:
                spec[key] = val
        return _emit({"queued": q.add(spec, priority=a.priority)})
    if c == "series":
        episodes = json.loads(Path(a.episodes_json).read_text(encoding="utf-8"))
        if not isinstance(episodes, list):
            raise ValueError("The episodes file must be a JSON list")
        entries = q.add_series(profile=a.profile, series_id=a.series, episodes=episodes, mode=a.mode, brief=a.brief, options={"export_video": bool(a.export)}, priority=a.priority)
        return _emit({"series_batch_id": entries[0]["series_batch_id"], "queued": entries})
    if c in ("list", "status"):
        return _emit(q.status())
    if c == "run":
        kwargs = {"max_jobs": a.max_jobs}
        if a.stale_after_sec is not None:
            kwargs["stale_after_sec"] = a.stale_after_sec
        return _emit(q.run_worker(**kwargs))
    if c == "cancel":
        return _emit({"cancelled": q.cancel(a.queue_id)})
    if c == "remove":
        return _emit({"removed": q.remove(a.queue_id)})
    return _emit({"updated": q.set_priority(a.queue_id, a.priority)})


def _cmd_learn(a: argparse.Namespace) -> int:
    from bangonpro.show_profiles.loader import load_show_profile, user_profile_dirs

    if a.action == "diff" and a.job:
        if not a.final_project:
            raise PreferenceError("learn diff --job needs --final-project <editor's finished .kdenlive>")
        return _emit(diff_job_against_final(a.job, a.final_project, prefs_root=a.prefs_root))
    if not a.profile:
        raise PreferenceError("--profile is required")
    store = ProfilePreferenceStore(a.profile, a.series, a.prefs_root)
    if a.action == "suggest":
        profile, _ = store.apply_to_profile(load_show_profile(a.profile, custom_dirs=user_profile_dirs()))
        return _emit({"suggestions": store.propose(suggest_from_decisions(profile, store.corrections()))})
    if a.action == "recurring":
        profile, _ = store.apply_to_profile(load_show_profile(a.profile, custom_dirs=user_profile_dirs()))
        found = detect_recurring_preferences(profile, store.corrections(), min_jobs=a.min_jobs)
        return _emit({"suggestions": store.propose(found) if found else [], "detected": len(found)})
    if a.action == "diff":
        if not (a.ai_project and a.final_project):
            raise PreferenceError("learn diff needs --job and --final-project, or --ai-project and --final-project")
        profile, _ = store.apply_to_profile(load_show_profile(a.profile, custom_dirs=user_profile_dirs()))
        sugg = suggest_from_project_diff(profile, shots_from_kdenlive(a.ai_project), shots_from_kdenlive(a.final_project), profile.get("camera_roles", {}))
        return _emit({"suggestions": store.propose(sugg)})
    if a.action == "export":
        if not a.out:
            raise PreferenceError("learn export needs --out <bundle.json>")
        bundle = export_preference_bundle(store, a.out, editor=a.editor, app_version=APP_VERSION)
        return _emit({"bundle": str(Path(a.out).resolve()), "items": len(bundle["items"]), "payload_sha256": bundle["manifest"]["payload_sha256"]})
    if a.action == "import":
        if not a.bundle:
            raise PreferenceError("learn import needs --bundle <bundle.json>")
        added = import_preference_bundle(store, a.bundle, allow_other_profile=a.any_profile)
        return _emit({"imported_pending_suggestions": added, "applied": False, "note": "Review and approve each suggestion; nothing changes until you do."})
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


def _cmd_measure(a: argparse.Namespace) -> int:
    log = MeasurementLog(a.root)
    if a.action == "start":
        job = ProductionJob(a.job_dir) if a.job_dir else None
        job_id = job.doc.get("job_id") if job and job.doc else None
        sid = log.start(deliverable_id=a.deliverable, kind=a.kind, editor=a.editor, job_id=job_id, evidence_class=a.evidence_class, job_dir=str(job.dir) if job_id else None, profile_id=job.doc.get("profile_id") if job_id else None)
        return _emit({"session_id": sid})
    if a.action == "activity":
        log.activity(a.session, a.category, detail=a.detail)
        return _emit({"ok": True})
    if a.action in ("pause", "resume"):
        log.activity(a.session, a.action)
        return _emit({"ok": True})
    if a.action == "link":
        job = ProductionJob(a.job_dir)
        log.link_job(a.session, job_id=job.doc["job_id"], job_dir=str(job.dir))
        return _emit({"ok": True})
    if a.action == "finish":
        log.finish(a.session, quality_rating=a.quality, notes=a.notes)
        return _emit({"ok": True})
    if a.action == "open":
        return _emit({"open_sessions": log.open_sessions()})
    if a.action == "estimate":
        return _emit(modelled_estimate(ProductionJob(a.job_dir).doc))
    job_dirs = list(a.jobs) + [s["job_dir"] for s in log.sessions() if s.get("job_dir")]
    jobs: dict[str, dict[str, Any]] = {}
    for j in job_dirs:
        if Path(j, "job.json").is_file():
            doc = ProductionJob(j).doc
            jobs[doc["job_id"]] = doc
    report = savings_report(log.sessions(), jobs, idle_cutoff_sec=a.idle_cutoff_sec)
    files = write_report_files(report, a.out_dir or Path(a.root) / "reports")
    report["files"] = files
    if a.json:
        return _emit(report)
    sys.stdout.write(render_table(report))
    sys.stdout.write(f"Report: {files['json']}\nCSV:    {files['csv']}\n")
    return 0


def _cmd_job(a: argparse.Namespace) -> int:
    job = ProductionJob(a.job_dir)
    if not job.doc:
        return _fail(FileNotFoundError(f"No production job at {a.job_dir}"))
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
    elif a.cmd == "revise":
        rec = job.revise(a.instruction, range_sec=tuple(a.range) if a.range else None, range_timeline=tuple(a.range_timeline) if a.range_timeline else None)
        if rec.get("status") == "not_understood":
            # Rules first; then a validated learned skill (no reasoning call); then the shared provider router.
            from bangonpro.ai_engine.mcp.__main__ import reasoning_fallback

            return _emit(reasoning_fallback(str(job.dir), a.instruction, rec))
        return _emit(rec)
    elif a.cmd == "lock":
        return _emit({"locked_ranges": job.lock_range(a.start, a.end, timeline=a.timeline)})
    elif a.cmd == "undo-revision":
        return _emit({"undone": job.undo_revision()})
    elif a.cmd == "rollback":
        job.rollback(a.agent)
    elif a.cmd == "decide":
        job.decide(a.agent, a.decision, rejected_op_ids=a.reject_ops, note=a.note)
    elif a.cmd == "relink":
        return _emit(job.relink(a.search, project_files=a.project, dry_run=a.dry_run))
    elif a.cmd == "promote":
        try:
            return _emit(job.promote(a.target, approve=a.approve))
        except ApprovalRequiredError as exc:
            _emit({"approval_required": True, "error": str(exc), "help": explain(exc)})
            return 3
    return _emit(_job_payload(job))


def _dispatch(a: argparse.Namespace) -> int:
    if a.cmd == "agents":
        return _emit({"agents": capability_matrix(), "modes": list(AUTOMATION_MODES)})
    if a.cmd == "run":
        job = ProductionJob.create(
            job_root=a.job_root, brief=a.brief, inputs=a.inputs or None, profile_ref=a.profile, mode=a.mode, options=_options_from_args(a),
            project_id=a.project_id, sequence_id=a.sequence_id, kdenlive_project=a.kdenlive_project, project_state=a.project_state, job_id=a.job_id,
        )
        if not a.plan_only:
            job.run()
        return _emit(_job_payload(job))
    if a.cmd == "learn":
        return _cmd_learn(a)
    if a.cmd == "measure":
        return _cmd_measure(a)
    if a.cmd == "cache":
        c = AnalysisCache(a.cache_dir)
        if a.action == "clear":
            return _emit({"removed": c.clear(a.namespace), "namespace": a.namespace})
        if a.action == "prune":
            removed = c.prune_to(int(a.max_mb * 1024 * 1024), a.namespace) if a.max_mb is not None else c.prune()
            return _emit({"removed": removed, "namespace": a.namespace, **c.stats(per_namespace=True)})
        return _emit(c.stats(per_namespace=True))
    if a.cmd == "models":
        from bangonpro.ai_engine.department.readiness import model_status

        return _emit(model_status(a.model_dir))
    if a.cmd == "readiness":
        from bangonpro.ai_engine.department.readiness import check_system

        return _emit(check_system(job_root=a.job_root, cache_dir=a.cache_dir, output_dir=a.output_dir, model_dir=a.model_dir, probe_hardware=not a.quick))
    if a.cmd == "diagnostics":
        from bangonpro.ai_engine.department.diagnostics import export_diagnostics

        res = export_diagnostics(
            a.out, job_dirs=a.job or None, job_root=a.job_root, recent_jobs=a.recent, queue_root=a.queue_root,
            cache_dir=a.cache_dir, model_dir=a.model_dir, include_media=a.include_media, probe_hardware=not a.quick,
        )
        m = res["manifest"]
        return _emit({"zip_path": res["zip_path"], "bytes": res["bytes"], "included": len(m["included"]), "excluded": len(m["excluded"]), "include_media": m["include_media"], "redactions": m["redactions"]})
    if a.cmd == "queue":
        return _cmd_queue(a)
    if a.cmd == "history":
        payload: dict[str, Any] = {"jobs": job_history(a.job_root, limit=a.limit)}
        payload["queue"] = JobQueue(a.queue_root).entries() if a.queue_root else []
        return _emit(payload)
    if a.cmd == "profile":
        return _cmd_profile(a)
    return _cmd_job(a)


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return _dispatch(a)
    except (PreferenceError, QueueError) as exc:
        return _fail(exc)
    except (ValueError, KeyError, FileNotFoundError, PermissionError, OSError) as exc:
        return _fail(exc)


if __name__ == "__main__":
    raise SystemExit(main())
