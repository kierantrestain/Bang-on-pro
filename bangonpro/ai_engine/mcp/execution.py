"""Bounded execution cycle: understand -> inspect -> plan -> execute -> evaluate -> correct -> verify.

A *task* is one creative instruction. ``bop_task begin`` records it with the checks that will prove
it is done (``expect``), a QC baseline of the actual draft and a step budget, and hands back a
compact context pack (only the briefs of the agents involved, the series preferences, the timeline
summary and current defects) instead of a conversation history. Agents then edit through the tools
(each edit attributed and revision-checked), optionally holding leases so two agents never change
the same track range in one task. ``bop_task complete`` evaluates the actual draft: every ``expect``
check, and QC defects that were not in the baseline. High-confidence defects with a deterministic
repair are corrected by the owning agent within the correction budget, each correction is
independently re-checked and undone if it does not remove the defect or introduces a new one. The
task is ``verified`` only when every check passes and no new high-confidence defect remains.

:func:`run_cycle` drives the same steps in-process with a planner (the user's authorised CLI, or a
scripted planner in tests), for instructions typed in Bang On Pro.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from bangonpro.ai_engine.department.agent_roles import role
from bangonpro.ai_engine.department.draft_qc import (
    HIGH_CONFIDENCE,
    DraftView,
    check_draft,
    fix_ops,
)
from bangonpro.ai_engine.mcp.protocol import CallContext, ToolError

MAX_EDITS_PER_TASK = 24
MAX_CORRECTIONS_PER_TASK = 3
MAX_PLAN_ROUNDS = 2
LESSON_BUDGET_CHARS = 1600


def _key(d: dict[str, Any]) -> tuple[str, float]:
    return d["code"], round(float(d["start"]), 0)


def evaluate_expect(v: DraftView, e: dict[str, Any]) -> dict[str, Any]:
    """One completion check on the actual draft. Unknown checks fail as unverifiable (never assumed)."""
    chk = e.get("check")
    ok: bool | None = None
    detail = ""
    try:
        if chk == "camera_at":
            got = v.camera_at(v.f(float(e["time"])))
            ok, detail = got == e["camera"], f"on screen: {got}"
        elif chk == "camera_share":
            f0, f1 = v.f(float(e["start"])), v.f(float(e["end"]))
            on = sum(max(0, min(s.end, f1) - max(s.start, f0)) for s in v.program_shots() if s.camera_id == e["camera"])
            share = on / max(1, f1 - f0)
            ok, detail = share >= float(e.get("min", 0.5)), f"share {share:.2f}"
        elif chk == "has_clip":
            f0, f1 = v.f(float(e["start"])), v.f(float(e["end"]))
            lst = v.c.track_lists().get(e.get("track", "V2"), [])
            hits = [c.clip_id for c in lst if c.timeline_start < f1 and c.timeline_end_inclusive >= f0 and (not e.get("media_ref") or c.media_ref == e["media_ref"])]
            ok, detail = bool(hits), f"clips: {hits[:4]}"
        elif chk == "title_at":
            f = v.f(float(e["time"]))
            hits = [str(c.params.get("title")) for c in v.c.v2_clips if c.params.get("clip_kind") == "kdenlivetitle" and c.timeline_start <= f <= c.timeline_end_inclusive]
            ok = bool(hits) and (not e.get("text") or any(str(e["text"]).lower() in h.lower() for h in hits))
            detail = f"titles: {hits}"
        elif chk == "effect_at":
            f = v.f(float(e["time"]))
            lst = v.c.track_lists().get(e.get("track", "V1"), []) + (v.c.v1b_clips if e.get("track", "V1") == "V1" else [])
            effs = [fl.get("kdenlive_id") for c in lst if c.timeline_start <= f <= c.timeline_end_inclusive for fl in c.filters] + [fl.get("mlt_service") for c in lst if c.timeline_start <= f <= c.timeline_end_inclusive for fl in c.filters]
            ok, detail = e["effect"] in effs, f"effects: {sorted({x for x in effs if x})}"
        elif chk == "transition_at":
            f = v.f(float(e["time"]))
            ok = any(t["in_frame"] - 2 <= f <= t["out_frame"] + 2 for t in v.c.transitions)
            detail = f"{len(v.c.transitions)} transitions"
        elif chk == "caption_contains":
            ok = any(str(e["text"]).lower() in str(c.get("text", "")).lower() for c in v.c.captions)
        elif chk == "duration":
            d = v.end_frame / v.fps
            ok = (e.get("min") is None or d >= float(e["min"]) - 0.05) and (e.get("max") is None or d <= float(e["max"]) + 0.05)
            detail = f"duration {d:.2f}s"
        elif chk == "no_defect":
            ok = not any(d["code"] == e["code"] for d in check_draft(v))
        else:
            return {**e, "ok": False, "detail": f"unverifiable check '{chk}'"}
    except (KeyError, TypeError, ValueError) as exc:
        return {**e, "ok": False, "detail": f"malformed check: {exc}"}
    return {**e, "ok": bool(ok), "detail": detail}


CHECK_KEYS = {"camera_at": ({"time", "camera"}, set()), "camera_share": ({"start", "end", "camera"}, {"min"}), "has_clip": ({"start", "end"}, {"track", "media_ref"}),
              "title_at": ({"time"}, {"text"}), "effect_at": ({"time", "effect"}, {"track"}), "transition_at": ({"time"}, set()), "caption_contains": ({"text"}, set()),
              "duration": (set(), {"min", "max"}), "no_defect": ({"code"}, set())}


def check_problem(e: Any) -> str | None:
    """Why a completion check cannot be evaluated as written (None when it is well formed)."""
    if not isinstance(e, dict) or e.get("check") not in CHECK_KEYS:
        return f"unknown check {e.get('check') if isinstance(e, dict) else e!r}; use one of {sorted(CHECK_KEYS)}"
    need, opt = CHECK_KEYS[e["check"]]
    keys = set(e) - {"check"}
    missing, extra = need - keys, keys - need - opt
    if missing or extra or (e["check"] == "duration" and not keys & {"min", "max"}):
        return f"{e['check']} takes {sorted(need)}" + (f" and optionally {sorted(opt)}" if opt else "") + (f"; missing {sorted(missing)}" if missing else "") + (f"; unknown {sorted(extra)}" if extra else "")
    if any(isinstance(e.get(k), bool) or not isinstance(e.get(k), (int, float)) for k in keys & {"time", "start", "end", "min", "max"}):
        return f"{e['check']}: times and bounds must be numbers"
    return None


def split_checks(expect: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ok, bad = [], []
    for e in expect if isinstance(expect, list) else []:
        why = check_problem(e)
        (bad if why else ok).append({"check": e, "problem": why} if why else e)
    return ok, bad


def context_pack(bt: Any, agents: list[str], *, max_rows: int = 60, text: str = "") -> dict[str, Any]:
    """What a reasoning agent needs to plan, compactly: no histories, no full transcripts."""
    v = bt.ws.view()
    shots = [{"start": v.s(s.start), "end": v.s(s.end), "camera": s.camera_id, "clip_id": s.clip_id} for s in v.program_shots()][:max_rows]
    speech = [{k: s[k] for k in ("speaker_id", "start", "end", "overlap")} | ({"text": s["text"][:120]} if s.get("text") else {}) for s in v.speech()][:max_rows]
    summ = v.summary()
    briefs = {a: {"brief": role(a)["brief"], "ops": list(role(a)["ops"]), "tracks": list(role(a)["tracks"])} for a in agents}
    return {"revision": bt.ws.revision, "duration_sec": summ["duration_sec"], "size": [summ["width"], summ["height"]],
            "cameras": {c: {"role": v.role_of_cam.get(c), "speaker": v.camera_owner.get(c)} for c in v.media_of_cam}, "program_shots": shots, "speech": speech,
            "graphics": [{k: r.get(k) for k in ("id", "start", "end", "title", "media")} for r in summ["tracks"].get("V2", [])], "captions": len(summ["captions"]),
            "locked": bt.ws.locked_timeline_ranges(v), "agents": briefs, "series_preferences": bt.agents({}, None).get("series_preferences") if agents else None,
            "transcript_evidence": "words" if any(s.get("text") for s in v.speech()) else "speech activity only (no words)", **_knowledge(bt, agents, text)}


def _knowledge(bt: Any, agents: list[str], text: str) -> dict[str, Any]:
    """Learning Agent: verified lessons and active strategies for these agents (data, not instructions)."""
    try:
        from bangonpro.ai_engine.department import strategies as strat
        from bangonpro.ai_engine.department.intelligence import Intelligence, job_context

        intel = Intelligence.for_job(bt.ws.job)
        if not intel.enabled:
            return {}
        lessons = intel.retrieve(job_context(bt.ws.job), agents, text=text, budget_chars=LESSON_BUDGET_CHARS)
        st = {a: p for a in agents if (p := strat.active_params(intel, a)[1])}
        return {k: val for k, val in (("lessons", lessons), ("strategies", st)) if val}
    except Exception:  # noqa: BLE001 - knowledge is advisory; a damaged store never blocks editing
        return {}


def task_action(bt: Any, args: dict[str, Any], ctx: CallContext | None) -> dict[str, Any]:
    ws = bt.ws
    act = args["action"]
    if act == "begin":
        if not args.get("instruction"):
            raise ToolError("INVALID_ARGUMENT", "begin needs instruction")
        _, bad = split_checks(args.get("expect") or [])
        if bad:
            raise ToolError("INVALID_ARGUMENT", "; ".join(b["problem"] for b in bad[:4]), hint="Fix the completion checks; nothing was started", details={"invalid_checks": bad[:8]})
        agents = list(dict.fromkeys(["executive", *(args.get("agents") or [])]))
        tid = f"task_{uuid.uuid4().hex[:8]}"
        v = ws.view()
        baseline = check_draft(v)
        started = ws.revision  # read before editing the in-memory layer (reading it reloads from disk)
        with ws.lock:
            ws.layer.reload()
            # A retry of the same instruction supersedes the earlier attempt, so the editor sees one result.
            for old in (ws.layer.doc.get("tasks") or {}).values():
                if old.get("instruction") == args["instruction"] and old.get("status") in ("open", "needs_attention"):
                    old.update({"status": "superseded", "superseded_by": tid, "superseded_unresolved": old.pop("unresolved", [])})
            ws.layer.doc.setdefault("tasks", {})[tid] = {"task_id": tid, "instruction": args["instruction"], "agents": agents, "expect": args.get("expect") or [], "status": "open",
                                                         "started_revision": started, "baseline": [list(_key(d)) for d in baseline], "edits": 0, "corrections": 0,
                                                         "events": [{"at": _now(), "phase": "understand", "agent": "executive"}], "findings": [], "client": getattr(ctx, "client_name", None),
                                                         "source": args.get("source") or f"mcp_client:{getattr(ctx, 'client_name', None) or 'unknown'}", "skill": args.get("skill")}
            ws.layer.save()
        pack = context_pack(bt, agents[1:] or ["multicam_director", "rough_cut", "graphics"], text=args["instruction"])
        return {"task_id": tid, "phase": "inspect", "budget": {"edits": MAX_EDITS_PER_TASK, "corrections": MAX_CORRECTIONS_PER_TASK}, "context": pack,
                "qc_baseline": [{k: d[k] for k in ("code", "start", "end", "agent", "confidence", "message")} for d in baseline][:20],
                "next": "Inspect what you need (bop_frames, bop_transcript, bop_search), edit as the owning agents with task_id, then bop_task complete."}
    tid = args.get("task_id")
    with ws.lock:
        ws.layer.reload()
        task = (ws.layer.doc.get("tasks") or {}).get(tid or "")
    if task is None:
        raise ToolError("NOT_FOUND", f"Unknown task '{tid}'")
    if act == "status":
        return {k: task.get(k) for k in ("task_id", "instruction", "status", "agents", "edits", "corrections", "findings", "result", "unresolved")}
    if act == "lease":
        lease = ws.take_lease(args["agent"], tid, list(args.get("tracks") or ["V1"]), float(args.get("start", 0.0)), float(args.get("end", 1e9)))
        bt._task_event(tid, {"phase": "plan", "agent": args["agent"], "lease": lease["lease_id"]})
        return {"lease": lease}
    if act == "note":
        f = dict(args.get("finding") or {})
        f["agent"] = args.get("agent") or f.get("agent")
        with ws.lock:
            ws.layer.reload()
            ws.layer.doc["tasks"][tid].setdefault("findings", []).append(f)
            ws.layer.save()
        return {"findings": len(task.get("findings", [])) + 1}
    if act == "cancel":
        ws.release_leases(tid)
        _set_task(ws, tid, {"status": "cancelled"})
        return {"task_id": tid, "status": "cancelled"}
    if act == "complete":
        return complete_task(bt, tid, auto_correct=bool(args.get("auto_correct", True)), extra_expect=args.get("expect"))
    raise ToolError("INVALID_ARGUMENT", f"Unknown action '{act}'")


def _set_task(ws: Any, tid: str, upd: dict[str, Any]) -> None:
    with ws.lock:
        ws.layer.reload()
        ws.layer.doc["tasks"][tid].update(upd)
        ws.layer.save()


def complete_task(bt: Any, tid: str, *, auto_correct: bool = True, extra_expect: list[dict[str, Any]] | None = None, capture: bool = True) -> dict[str, Any]:
    ws = bt.ws
    ws.release_leases(tid)
    task = ws.layer.doc["tasks"][tid]
    if task.get("edits", 0) > MAX_EDITS_PER_TASK:
        raise ToolError("BUDGET", f"Task used {task['edits']} edits (budget {MAX_EDITS_PER_TASK})")
    expect = list(task.get("expect") or []) + list(extra_expect or [])
    baseline = {tuple(k) for k in task.get("baseline", [])}
    corrections: list[dict[str, Any]] = []
    rounds = 0
    while True:
        v = ws.view()
        checks = [evaluate_expect(v, e) for e in expect]
        defects = check_draft(v)
        new = [d for d in defects if _key(d) not in baseline and d["confidence"] >= HIGH_CONFIDENCE]
        fixable = [d for d in new if d.get("fix") and not any(c["defect"]["code"] == d["code"] and abs(c["defect"]["start"] - d["start"]) < 0.5 for c in corrections)]
        if not (auto_correct and fixable and len(corrections) < MAX_CORRECTIONS_PER_TASK):
            break
        rounds += 1
        d = fixable[0]
        rec = {"defect": {k: d[k] for k in ("code", "start", "end", "agent", "confidence", "message")}, "agent": d["agent"]}
        try:
            ops = fix_ops(v, d)
            if not ops:
                rec["result"] = "no deterministic repair"
                corrections.append(rec)
                continue
            res = ws.apply(ops, agent_id=d["agent"], expected_revision=ws.revision, label=f"QC correction: {d['message'][:80]}", task_id=tid)
            rec["entry_id"] = res["entry_id"]
            # Independent verification by Quality Control: the defect is gone and nothing new appeared.
            v2 = ws.view()
            after = check_draft(v2)
            still = any(x["code"] == d["code"] and abs(x["start"] - d["start"]) < 0.5 for x in after)
            before_keys = {_key(x) for x in defects}
            introduced = [x for x in after if _key(x) not in before_keys and x["severity"] == "error"]
            if still or introduced or not res["verified"]["ok"]:
                ws.undo("quality_control")
                rec["result"] = "rejected and undone" + (f": introduced {introduced[0]['code']}" if introduced else ": defect remained" if still else ": project verification failed")
            else:
                rec["result"] = "accepted (verified by quality_control)"
                rec["now"] = [{"id": cid, "track": c.track_id, "start": v2.s(c.timeline_start), "end": v2.s(c.timeline_end_inclusive + 1)}
                              for cid in res["diff"]["added"] + res["diff"]["changed"] if (c := v2.clip(cid)) is not None][:6]
        except ToolError as exc:
            rec["result"] = f"not applied: {exc.code} {exc.message[:120]}"
        corrections.append(rec)
    v = ws.view()
    checks = [evaluate_expect(v, e) for e in expect]
    defects = check_draft(v)
    unresolved = [{"agent": d["agent"], "code": d["code"], "message": f"{d['message']} ({d['start']:.1f}s)", "confidence": d["confidence"]}
                  for d in defects if _key(d) not in baseline and d["confidence"] >= HIGH_CONFIDENCE]
    failed_checks = [c for c in checks if not c["ok"]]
    status = "verified" if not failed_checks and not unresolved and (expect or task.get("edits", 0) > 0) else "needs_attention"
    unresolved += [{"agent": "executive", "code": "CHECK_FAILED", "message": f"{c.get('check')} not met ({c.get('detail')})"} for c in failed_checks]
    result = {"task_id": tid, "status": status, "checks": checks, "superseded_checks": task.get("superseded_checks") or [], "corrections": corrections, "unresolved": unresolved, "revision": ws.revision,
              "edits": task.get("edits", 0), "project": str(ws.project_path())}
    _set_task(ws, tid, {"status": status, "result": {k: result[k] for k in ("status", "revision", "edits")}, "unresolved": unresolved, "corrections": len(corrections), "completed_at": _now()})
    try:  # Learning Agent: accepted agent work becomes a recorded correction for the series
        from bangonpro.ai_engine.department.learning import ProfilePreferenceStore

        job = ws.job
        ProfilePreferenceStore(str(job.doc["profile_id"]), str(job.doc.get("series_id") or "default"), job.doc["options"].get("prefs_root")).record_correction(
            {"kind": "agent_task", "job_id": job.doc["job_id"], "instruction": task["instruction"][:300], "status": status, "edits": task.get("edits", 0), "corrections": len(corrections)})
    except Exception:  # noqa: BLE001, S110 - learning is advisory and must never fail a task
        pass
    if capture:
        result["learning"] = learn_from_task(bt, tid, result)
    return result


def learn_from_task(bt: Any, tid: str, result: dict[str, Any], *, usage: dict[str, Any] | None = None, processing_sec: float | None = None) -> dict[str, Any]:
    """Experience capture + consolidation + monitoring of promoted strategies (never fails the task)."""
    try:
        from bangonpro.ai_engine.department import strategies as strat
        from bangonpro.ai_engine.department.intelligence import Intelligence, capture_task

        ws = bt.ws
        ws.layer.reload()
        task = ws.layer.doc["tasks"][tid]
        intel = Intelligence.for_job(ws.job)
        out = capture_task(ws, task, result, source=str(task.get("source") or "unknown"), usage=usage, processing_sec=processing_sec, intel=intel)
        mons = [m for a in task.get("agents") or [] if (m := strat.monitor(intel, a)) and m.get("status") == "reverted"]
        if mons:
            out["reverted"] = mons
        bt._presence({"learning": learning_line(intel)})
        return out
    except Exception as exc:  # noqa: BLE001 - learning is advisory and must never fail a task
        return {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}


def learning_line(intel: Any) -> str:
    s = intel.summary()
    if not s["enabled"]:
        return "Learning off"
    sk = s["skills"]
    k = s["knowledge"]
    lessons = sum(n for key, n in k.items() if key.split(":")[0] in ("failure", "semantic") and key.split(":")[1] in ("validated", "active"))
    pending = len(s["awaiting_approval"]["strategies"]) + len(s["awaiting_approval"]["knowledge"])
    parts = [f"{lessons} lesson(s)", f"{sk['validated'] + sk['active']} skill(s)" + (f" + {sk['candidate']} candidate" if sk["candidate"] else ""),
             f"{len(s['active_strategies'])} strategy update(s)"]
    return "Learning: " + ", ".join(parts) + (f", {pending} awaiting approval" if pending else "")


# ---------------------------------------------------------------------- in-process cycle
PLAN_FORMAT = (
    'Answer with JSON only: {"understanding": "...", "agents": [agent ids], "steps": [{"tool": "bop_switch_camera|bop_remove_range|bop_place_asset|bop_add_title|bop_edit|bop_lock",'
    ' "args": {...without expected_revision}}], "expect": [checks, each exactly one of: {"check":"camera_at","time","camera"} {"check":"camera_share","start","end","camera","min"?}'
    ' {"check":"has_clip","start","end","track"?,"media_ref"?} {"check":"title_at","time","text"?} {"check":"effect_at","time","effect","track"?} {"check":"transition_at","time"}'
    ' {"check":"caption_contains","text"} {"check":"duration","min"?,"max"?} {"check":"no_defect","code"}; other keys are rejected],'
    ' "questions": []}. Each step args must include "agent" (the production agent that owns the change). Use only cameras, clip ids and times from the context.'
    ' After feedback you may add "drop_checks": [indices of failed checks] with "drop_reason" when a check was wrong; dropped checks are reported to the editor.'
)


def build_prompt(instruction: str, pack: dict[str, Any], tools_guide: dict[str, Any], feedback: dict[str, Any] | None = None, progress: list[dict[str, Any]] | None = None) -> str:
    parts = [
        "You are the Executive Editing Agent of Bang On Pro's production department (YouTube podcasts, gameshows, sports, news, interviews, Shorts).",
        "Turn the editor's instruction into a short plan of editing tool calls for the specialist agents. Times are timeline seconds.",
        f"INSTRUCTION: {instruction}",
        "PROJECT: " + json.dumps(pack, separators=(",", ":"), default=str),
        "TOOLS: " + json.dumps({"bop_switch_camera": "{agent, start, end, camera, reason}", "bop_remove_range": "{agent, start, end}",
                                "bop_place_asset": "{agent, file|media_ref, start, duration, position, fade}", "bop_add_title": "{agent, text, subtitle, start, duration}",
                                "bop_edit": "{agent, ops:[{type, clip_id, track, start, source_in, source_out, at, params}]}", "bop_lock": "{start, end}", "ops": tools_guide.get("ops")},
                               separators=(",", ":")),
        PLAN_FORMAT,
    ]
    if pack.get("lessons"):
        parts.append("LESSONS are verified department experience for this series; follow them unless the instruction says otherwise.")
    if progress:
        parts.append("ALREADY DONE AND VERIFIED (do not repeat): " + json.dumps(progress[-12:], separators=(",", ":"), default=str))
    if feedback:
        parts.append("PREVIOUS ATTEMPT (fix only what failed): " + json.dumps(feedback, separators=(",", ":"), default=str))
    return "\n".join(parts)


PLAN_TOOLS = ("bop_switch_camera", "bop_remove_range", "bop_place_asset", "bop_add_title", "bop_edit", "bop_lock")
_STOPPED = ("blocked", "interrupted")


def _checkpoint(ws: Any, tid: str, cyc: dict[str, Any], **upd: Any) -> None:
    """Persist the cycle's verified state: plan, steps already applied, outstanding objectives, provider history."""
    cyc["updated"] = _now()
    _set_task(ws, tid, {"cycle": json.loads(json.dumps(cyc, default=str)), **upd})


def _strategy_params(bt: Any, learning: bool) -> dict[str, dict[str, Any]]:
    if not learning:
        return {}
    try:
        from bangonpro.ai_engine.department import strategies as strat
        from bangonpro.ai_engine.department.intelligence import Intelligence

        intel = Intelligence.for_job(bt.ws.job)
        return {a: p for a in strat.STRATEGY_SPACE if (p := strat.active_params(intel, a)[1])} if intel.enabled else {}
    except Exception:  # noqa: BLE001
        return {}


def run_cycle(bt: Any, instruction: str, reasoner: Any, *, ctx: CallContext | None = None, max_rounds: int = MAX_PLAN_ROUNDS, cancel: Any = None,
              learning: bool = True, resume_task: str | None = None) -> dict[str, Any]:
    """Drive one instruction end to end: validated skill first (no reasoning), else ``reasoner`` as the Executive's planner.

    Every plan, applied step and failover is checkpointed in the task, so a provider switch, a
    cancellation or a restart resumes exactly where it stopped without repeating an edit.
    """
    from bangonpro.ai_engine.mcp.reasoning import AllProvidersUnavailable, BudgetExhausted, ReasonerError
    from bangonpro.ai_engine.mcp.tools import OP_GUIDE

    ctx = ctx or CallContext(request_id="cycle", client_name=f"{getattr(reasoner, 'kind', 'planner')}")
    t0 = time.perf_counter()
    ws = bt.ws
    tools = {t.name: t for t in bt.tool_list()}
    trace: list[dict[str, Any]] = []
    skill_note: dict[str, Any] | None = None
    if resume_task:
        ws.layer.reload()
        task = (ws.layer.doc.get("tasks") or {}).get(resume_task)
        if task is None or not task.get("cycle"):
            raise ToolError("NOT_FOUND", f"No resumable cycle for task '{resume_task}'")
        if task.get("status") not in ("open", *_STOPPED):
            return {"status": task.get("status"), "task_id": resume_task, "result": task.get("result"), "trace": [], "resumed": False}
        tid, instruction, cyc = resume_task, task["instruction"], dict(task["cycle"])
        _set_task(ws, tid, {"status": "open"})
        trace.append({"phase": "resume", "from": cyc.get("phase"), "done_steps": len(cyc.get("done_steps") or [])})
    else:
        tid = None
        cyc = {"round": 0, "phase": "plan", "plan": None, "done_steps": [], "progress": [], "providers": [], "learning": learning}
        if learning:
            out = run_skill(bt, instruction, ctx=ctx)
            if out is not None:
                if out["status"] == "verified":
                    out["processing_sec"] = round(time.perf_counter() - t0, 3)
                    return out
                skill_note = {"skill": out.get("skill_id"), "status": out["status"], "why": out.get("why")}
                trace.append({"phase": "skill", **skill_note})
    params = _strategy_params(bt, cyc.get("learning", learning))
    if hasattr(reasoner, "begin_task"):
        reasoner.begin_task(tid)  # safe boundary: the preferred provider is used again if it is available
    if hasattr(reasoner, "cancel") and cancel is not None:
        reasoner.cancel = cancel

    def on_switch(ev: dict[str, Any]) -> None:
        cyc["providers"].append(ev)
        bt._presence({"failover": f"{ev['from']} {ev['kind']} > {ev.get('to') or 'none'}", "provider": ev.get("to")})
        if tid:
            _checkpoint(ws, tid, cyc)

    if hasattr(reasoner, "on_switch"):
        reasoner.on_switch = on_switch
    result: dict[str, Any] = {}
    all_corrections: list[dict[str, Any]] = list(cyc.get("corrections") or [])
    while True:
        if cyc["phase"] == "plan":
            agents_hint = (cyc.get("plan") or {}).get("agents") or ["multicam_director", "rough_cut", "graphics", "audio_engineer", "colourist", "captions", "vfx"]
            prompt = build_prompt(instruction, context_pack(bt, agents_hint, text=instruction), OP_GUIDE, cyc.get("feedback"), cyc.get("progress"))
            try:
                plan = reasoner.plan(prompt)
            except (AllProvidersUnavailable, BudgetExhausted) as exc:
                # Nothing can reason right now: keep the verified state and stop safely; resume_cycle continues later.
                if tid is None:
                    tid = task_action(bt, {"action": "begin", "instruction": instruction, "agents": [], "expect": [], "source": _source(reasoner)}, ctx)["task_id"]
                cyc["blocked"] = {"reason": str(exc)[:300], "retry_at": getattr(exc, "retry_at", None), "kind": type(exc).__name__}
                _checkpoint(ws, tid, cyc, status="blocked")
                trace.append({"phase": "blocked", "reason": str(exc)[:200]})
                return {"status": "blocked", "task_id": tid, "result": result, "trace": trace, "reasoner": getattr(reasoner, "kind", "?"), "retry_at": cyc["blocked"]["retry_at"],
                        "processing_sec": round(time.perf_counter() - t0, 3), "usage": _usage(reasoner)}
            except ReasonerError as exc:  # unusable answer, cancelled call, or nothing authorised
                if tid is None:
                    if cyc["round"] == 0 and not resume_task:
                        raise
                trace.append({"phase": "correct", "error": str(exc)[:200]})
                if tid is not None and cancel is not None and cancel.is_set():
                    _checkpoint(ws, tid, cyc, status="interrupted")
                    return {"status": "interrupted", "task_id": tid, "result": result, "trace": trace, "reasoner": getattr(reasoner, "kind", "?"), "usage": _usage(reasoner)}
                break
            cyc.pop("blocked", None)
            plan["expect"], bad = split_checks(plan.get("expect") or [])
            if bad:  # never evaluated as failures and never hidden: reported, and fed back to the planner
                cyc.setdefault("rejected_checks", []).extend(bad)
            trace.append({"phase": "plan", "round": cyc["round"], "understanding": plan.get("understanding"), "steps": len(plan.get("steps") or []), "provider": getattr(reasoner, "provider", None),
                          **({"rejected_checks": bad} if bad else {})})
            if tid is None:
                if plan.get("questions") and not plan.get("steps"):
                    return {"status": "needs_input", "questions": plan["questions"], "trace": trace}
                tid = task_action(bt, {"action": "begin", "instruction": instruction, "agents": plan.get("agents") or [], "expect": plan.get("expect") or [], "source": _source(reasoner)}, ctx)["task_id"]
                if hasattr(reasoner, "task_id"):
                    reasoner.task_id = tid  # failover events and ledger rows name the task
            elif cyc["round"] > 0:
                # Revised completion checks: passed ones are kept; a failed one is only dropped when the planner
                # names it in drop_checks with a reason, and it is then reported as superseded (never hidden).
                drop = {int(i) for i in plan.get("drop_checks") or [] if str(i).isdigit()}
                prev = cyc.get("last_checks") or []
                keep = [{k: val for k, val in c.items() if k not in ("ok", "detail")} for i, c in enumerate(prev) if c["ok"] or i not in drop]
                superseded = [{**prev[i], "reason": plan.get("drop_reason", "")} for i in sorted(drop) if i < len(prev) and not prev[i]["ok"]]
                _set_task(ws, tid, {"status": "open", "expect": keep + list(plan.get("expect") or []), "superseded_checks": superseded})
                trace.append({"phase": "correct", "steps": len(plan.get("steps") or []), "superseded_checks": superseded})
            elif not (ws.layer.doc.get("tasks") or {}).get(tid, {}).get("expect"):
                _set_task(ws, tid, {"agents": list(dict.fromkeys(["executive", *(plan.get("agents") or [])])), "expect": plan.get("expect") or []})
            cyc.update(plan={k: plan.get(k) for k in ("understanding", "agents", "steps", "expect")}, done_steps=[], inflight=None, phase="execute")
            _checkpoint(ws, tid, cyc)
        if cyc["phase"] == "execute":
            steps = (cyc.get("plan") or {}).get("steps") or []
            inflight = cyc.get("inflight")
            if inflight is not None:  # restarted during a step: the layer says whether that edit landed
                landed = any(e.get("task_id") == tid and e["entry_id"] == f"e{inflight['revision'] + 1}" and not e.get("undone") for e in ws.layer.doc.get("entries") or [])
                if landed or inflight.get("tool") == "bop_lock":
                    cyc["done_steps"].append(inflight["step"])
                cyc["inflight"] = None
            for i, step in enumerate(steps):
                if i in cyc["done_steps"]:
                    continue
                if cancel is not None and cancel.is_set():
                    _checkpoint(ws, tid, cyc, status="interrupted")
                    trace.append({"phase": "interrupted", "done_steps": len(cyc["done_steps"])})
                    return {"status": "interrupted", "task_id": tid, "result": result, "trace": trace, "reasoner": getattr(reasoner, "kind", "?"), "usage": _usage(reasoner)}
                name, sargs = step.get("tool"), dict(step.get("args") or {})
                if name not in PLAN_TOOLS:
                    trace.append({"phase": "execute", "tool": name, "error": "tool not allowed in a plan"})
                    cyc["done_steps"].append(i)
                    continue
                sargs.pop("expected_revision", None)
                applied: list[str] = []
                if params and name in ("bop_add_title", "bop_place_asset", "bop_switch_camera"):
                    from bangonpro.ai_engine.department.strategies import apply_params

                    sargs, applied = apply_params(ws.view(), name, sargs, params)
                if name != "bop_lock":
                    sargs["expected_revision"] = ws.revision
                    sargs["task_id"] = tid
                cyc["inflight"] = {"step": i, "revision": ws.revision, "tool": name}
                _checkpoint(ws, tid, cyc)
                bt._applied_strategy = applied
                out = _direct(tools[name], sargs, ctx)
                bt._applied_strategy = []
                cyc["inflight"] = None
                cyc["done_steps"].append(i)
                if out.get("ok"):
                    cyc["progress"].append({"round": cyc["round"], "tool": name, "agent": sargs.get("agent"), "entry": out.get("entry_id"),
                                            **{k: sargs[k] for k in ("start", "end", "camera", "text", "duration") if k in sargs}})
                _checkpoint(ws, tid, cyc)
                trace.append({"phase": "execute", "tool": name, "agent": sargs.get("agent"), **out, **({"strategy": applied} if applied else {})})
            cyc["phase"] = "verify"
            _checkpoint(ws, tid, cyc)
        if cyc["phase"] == "verify":
            result = complete_task(bt, tid, capture=False)
            all_corrections += result["corrections"]
            result["corrections"] = list(all_corrections)
            cyc["corrections"] = all_corrections
            trace.append({"phase": "verify", "status": result["status"], "failed": [c for c in result["checks"] if not c["ok"]], "corrections": result["corrections"]})
            if result["status"] == "verified" or cyc["round"] + 1 >= max_rounds:
                cyc["phase"] = "done"
                _checkpoint(ws, tid, cyc)
                break
            # Correct: the planner sees exactly what failed on the actual draft, nothing else.
            cyc["feedback"] = {"failed_checks": [c for c in result["checks"] if not c["ok"]], "unresolved": result["unresolved"][:8], "errors": [t for t in trace if t.get("error")][-6:],
                               "invalid_checks": (cyc.get("rejected_checks") or [])[-6:],
                               "qc_corrections": [{k: c.get(k) for k in ("defect", "result", "now")} for c in result["corrections"]]}
            cyc["last_checks"] = result["checks"]
            cyc["round"] += 1
            cyc["phase"] = "plan"
            _checkpoint(ws, tid, cyc)
    usage = _usage(reasoner)
    if result and cyc.get("rejected_checks"):
        result["rejected_checks"] = cyc["rejected_checks"]
    if tid and result:
        result["learning"] = learn_from_task(bt, tid, result, usage=usage, processing_sec=round(time.perf_counter() - t0, 3))
    out = {"status": result.get("status", "failed"), "task_id": tid, "result": result, "trace": trace, "reasoner": getattr(reasoner, "kind", "?"),
           "processing_sec": round(time.perf_counter() - t0, 3), "usage": usage}
    if skill_note:
        out["skill_attempt"] = skill_note
    return out


def resume_cycle(bt: Any, task_id: str, reasoner: Any, **kw: Any) -> dict[str, Any]:
    """Continue a blocked or interrupted cycle (possibly in a new process) from its checkpoint."""
    return run_cycle(bt, "", reasoner, resume_task=task_id, **kw)


def _source(reasoner: Any) -> str:
    kind = getattr(reasoner, "kind", "planner")
    return "mocked_reasoning" if kind == "scripted_mock" else f"authenticated_cli:{getattr(reasoner, 'provider', None) or 'pending'}" if kind == "authenticated_cli" else str(kind)


def _usage(reasoner: Any) -> dict[str, Any] | None:
    b = getattr(reasoner, "budget", None)
    out = dict(getattr(reasoner, "last_usage", None) or {})
    if b is not None and hasattr(b, "summary"):
        out["budget"] = b.summary()
    if getattr(reasoner, "events", None):
        out["failover"] = list(reasoner.events)
    return out or None


def run_skill(bt: Any, instruction: str, *, ctx: CallContext | None = None) -> dict[str, Any] | None:
    """Execute a matching learned skill deterministically (no reasoning call); undo it if QC does not verify it."""
    from bangonpro.ai_engine.department import skills as sk
    from bangonpro.ai_engine.department.intelligence import Intelligence, job_context
    from bangonpro.ai_engine.department.strategies import apply_params

    ws = bt.ws
    try:
        intel = Intelligence.for_job(ws.job)
        if not intel.enabled:
            return None
        jctx = job_context(ws.job)
        assets = [p for r in ws.allowed_roots if r.is_dir() for p in sorted(r.iterdir()) if p.is_file()]
        m = sk.match(intel, jctx, instruction, ws.view(), assets)
    except Exception:  # noqa: BLE001 - a damaged store or odd skill never blocks the cycle
        return None
    if m is None:
        return None
    skill = m["skill"]
    ctx = ctx or CallContext(request_id="skill", client_name="skill")
    tools = {t.name: t for t in bt.tool_list()}
    params = _strategy_params(bt, True)
    agents = sorted({a.get("agent") for _, a in m["steps"] if a.get("agent")})
    tid = task_action(bt, {"action": "begin", "instruction": instruction, "agents": agents, "expect": m["expect"], "source": f"skill:{skill['id']}", "skill": skill["id"]}, ctx)["task_id"]
    trace: list[dict[str, Any]] = [{"phase": "skill", "skill": skill["id"], "version": skill["version"], "status": skill["status"], "similarity": m["similarity"]}]
    ok = True
    for name, args in m["steps"]:
        a, applied = apply_params(ws.view(), name, dict(args), params) if params else (dict(args), [])
        a.update(expected_revision=ws.revision, task_id=tid)
        bt._applied_strategy = applied
        out = _direct(tools[name], a, ctx)
        bt._applied_strategy = []
        trace.append({"phase": "execute", "tool": name, "agent": a.get("agent"), **out})
        if not out.get("ok"):
            ok = False
            break
    result = complete_task(bt, tid, capture=False) if ok else {"status": "needs_attention", "checks": [], "corrections": [], "unresolved": [], "edits": 0}
    verified = ok and result["status"] == "verified"
    if not verified:
        # A skill that does not verify leaves nothing behind: its edits are undone and reasoning takes over.
        while any(e.get("task_id") == tid and not e.get("undone") for e in ws.layer.active_entries()):
            ws.undo("learning")
        _set_task(ws, tid, {"status": "superseded", "superseded_reason": "learned skill did not verify; handed to reasoning"})
    rec = sk.record_outcome(intel, skill, jctx, verified, reason=str(result.get("unresolved") or trace[-1])[:200])
    if verified:
        result["learning"] = learn_from_task(bt, tid, result)
    return {"status": "verified" if verified else "skill_failed", "task_id": tid, "result": result, "trace": trace, "reasoner": "skill", "skill_id": skill["id"],
            "skill_status": rec["status"], "usage": {"reasoning_calls": 0}, "why": None if verified else (result.get("unresolved") or trace[-1])}


def _direct(tool: Any, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
    """Run a planned step exactly as an MCP call would: strict argument check first."""
    from jsonschema import Draft202012Validator

    errs = [e.message for e in Draft202012Validator(tool.definition()["inputSchema"]).iter_errors(args)]
    if errs:
        return {"ok": False, "error": {"code": "INVALID_ARGUMENT", "message": "; ".join(errs[:3])}}
    try:
        out = tool.handler(args, ctx)
        data = out.data if hasattr(out, "data") else out
        return {"ok": True, "revision": data.get("revision"), "entry_id": data.get("entry_id"), "verified": (data.get("verified") or {}).get("ok")}
    except ToolError as exc:
        return {"ok": False, "error": exc.to_dict()}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


__all__ = ["MAX_CORRECTIONS_PER_TASK", "MAX_EDITS_PER_TASK", "build_prompt", "complete_task", "context_pack", "evaluate_expect", "learn_from_task", "resume_cycle", "run_cycle", "run_skill", "task_action"]
