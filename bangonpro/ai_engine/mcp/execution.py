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


def context_pack(bt: Any, agents: list[str], *, max_rows: int = 60) -> dict[str, Any]:
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
            "transcript_evidence": "words" if any(s.get("text") for s in v.speech()) else "speech activity only (no words)"}


def task_action(bt: Any, args: dict[str, Any], ctx: CallContext | None) -> dict[str, Any]:
    ws = bt.ws
    act = args["action"]
    if act == "begin":
        if not args.get("instruction"):
            raise ToolError("INVALID_ARGUMENT", "begin needs instruction")
        agents = list(dict.fromkeys(["executive", *(args.get("agents") or [])]))
        tid = f"task_{uuid.uuid4().hex[:8]}"
        v = ws.view()
        baseline = check_draft(v)
        with ws.lock:
            ws.layer.reload()
            ws.layer.doc.setdefault("tasks", {})[tid] = {"task_id": tid, "instruction": args["instruction"], "agents": agents, "expect": args.get("expect") or [], "status": "open",
                                                         "started_revision": ws.revision, "baseline": [list(_key(d)) for d in baseline], "edits": 0, "corrections": 0,
                                                         "events": [{"at": _now(), "phase": "understand", "agent": "executive"}], "findings": [], "client": getattr(ctx, "client_name", None)}
            ws.layer.save()
        pack = context_pack(bt, agents[1:] or ["multicam_director", "rough_cut", "graphics"])
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


def complete_task(bt: Any, tid: str, *, auto_correct: bool = True, extra_expect: list[dict[str, Any]] | None = None) -> dict[str, Any]:
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
    return result


# ---------------------------------------------------------------------- in-process cycle
PLAN_FORMAT = (
    'Answer with JSON only: {"understanding": "...", "agents": [agent ids], "steps": [{"tool": "bop_switch_camera|bop_remove_range|bop_place_asset|bop_add_title|bop_edit|bop_lock",'
    ' "args": {...without expected_revision}}], "expect": [{"check": "camera_at|camera_share|has_clip|title_at|effect_at|transition_at|caption_contains|duration|no_defect", ...}],'
    ' "questions": []}. Each step args must include "agent" (the production agent that owns the change). Use only cameras, clip ids and times from the context.'
    ' After feedback you may add "drop_checks": [indices of failed checks] with "drop_reason" when a check was wrong; dropped checks are reported to the editor.'
)


def build_prompt(instruction: str, pack: dict[str, Any], tools_guide: dict[str, Any], feedback: dict[str, Any] | None = None) -> str:
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
    if feedback:
        parts.append("PREVIOUS ATTEMPT (fix only what failed): " + json.dumps(feedback, separators=(",", ":"), default=str))
    return "\n".join(parts)


def run_cycle(bt: Any, instruction: str, reasoner: Any, *, ctx: CallContext | None = None, max_rounds: int = MAX_PLAN_ROUNDS) -> dict[str, Any]:
    """Drive one instruction end to end with ``reasoner`` as the Executive's planner."""
    from bangonpro.ai_engine.mcp.tools import OP_GUIDE

    ctx = ctx or CallContext(request_id="cycle", client_name=f"{getattr(reasoner, 'kind', 'planner')}")
    t0 = time.perf_counter()
    trace: list[dict[str, Any]] = []
    tools = {t.name: t for t in bt.tool_list()}
    pack = context_pack(bt, ["multicam_director", "rough_cut", "graphics", "audio_engineer", "colourist", "captions", "vfx"])
    plan = reasoner.plan(build_prompt(instruction, pack, OP_GUIDE))
    trace.append({"phase": "plan", "understanding": plan.get("understanding"), "steps": len(plan.get("steps") or [])})
    if plan.get("questions") and not plan.get("steps"):
        return {"status": "needs_input", "questions": plan["questions"], "trace": trace}
    begun = task_action(bt, {"action": "begin", "instruction": instruction, "agents": plan.get("agents") or [], "expect": plan.get("expect") or []}, ctx)
    tid = begun["task_id"]
    result: dict[str, Any] = {}
    all_corrections: list[dict[str, Any]] = []
    for rnd in range(max_rounds):
        for step in plan.get("steps") or []:
            name, sargs = step.get("tool"), dict(step.get("args") or {})
            if name not in ("bop_switch_camera", "bop_remove_range", "bop_place_asset", "bop_add_title", "bop_edit", "bop_lock"):
                trace.append({"phase": "execute", "tool": name, "error": "tool not allowed in a plan"})
                continue
            sargs.pop("expected_revision", None)
            if name != "bop_lock":
                sargs["expected_revision"] = bt.ws.revision
                sargs["task_id"] = tid
            out = _direct(tools[name], sargs, ctx)
            trace.append({"phase": "execute", "tool": name, "agent": sargs.get("agent"), **out})
        result = complete_task(bt, tid)
        all_corrections += result["corrections"]
        result["corrections"] = list(all_corrections)
        trace.append({"phase": "verify", "status": result["status"], "failed": [c for c in result["checks"] if not c["ok"]], "corrections": result["corrections"]})
        if result["status"] == "verified" or rnd + 1 >= max_rounds:
            break
        # Correct: the planner sees exactly what failed on the actual draft, nothing else.
        feedback = {"failed_checks": [c for c in result["checks"] if not c["ok"]], "unresolved": result["unresolved"][:8], "errors": [t for t in trace if t.get("error")][-6:],
                    "qc_corrections": [{k: c.get(k) for k in ("defect", "result", "now")} for c in result["corrections"]]}
        try:
            plan = reasoner.plan(build_prompt(instruction, context_pack(bt, plan.get("agents") or ["multicam_director"]), OP_GUIDE, feedback))
        except Exception as exc:  # noqa: BLE001 - out of reasoning budget or client failure: report, do not retry endlessly
            trace.append({"phase": "correct", "error": str(exc)[:200]})
            break
        # Revised completion checks: passed ones are kept; a failed one is only dropped when the planner
        # names it in drop_checks with a reason, and it is then reported as superseded (never hidden).
        drop = {int(i) for i in plan.get("drop_checks") or [] if str(i).isdigit()}
        prev = result.get("checks") or []
        keep = [{k: val for k, val in c.items() if k not in ("ok", "detail")} for i, c in enumerate(prev) if c["ok"] or i not in drop]
        superseded = [{**prev[i], "reason": plan.get("drop_reason", "")} for i in sorted(drop) if i < len(prev) and not prev[i]["ok"]]
        _set_task(bt.ws, tid, {"status": "open", "expect": keep + list(plan.get("expect") or []), "superseded_checks": superseded})
        trace.append({"phase": "correct", "steps": len(plan.get("steps") or []), "superseded_checks": superseded})
    return {"status": result.get("status", "failed"), "task_id": tid, "result": result, "trace": trace, "reasoner": getattr(reasoner, "kind", "?"),
            "processing_sec": round(time.perf_counter() - t0, 3), "usage": getattr(reasoner, "last_usage", None)}


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


__all__ = ["MAX_CORRECTIONS_PER_TASK", "MAX_EDITS_PER_TASK", "build_prompt", "complete_task", "context_pack", "evaluate_expect", "run_cycle", "task_action"]
