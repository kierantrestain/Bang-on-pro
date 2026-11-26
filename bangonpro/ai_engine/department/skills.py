"""Reusable editing skills: validated declarative workflows over the native editing tools (AWM-style).

A *skill* is what a verified task did, generalised so another production can do it again without
asking a reasoning provider. It is data, never code:

* ``steps``: calls to the compound editing tools (``bop_switch_camera``, ``bop_remove_range``,
  ``bop_place_asset``, ``bop_add_title``) or another validated skill (``{"skill": id}``, depth 2).
* Arguments are literals or *anchors* resolved on the target production's draft:
  ``{"$at": "turn", "speaker_role": "guest", "rank": "longest", "point": "mid", "offset": -1.0}``,
  ``{"$at": "program", "point": "start", "offset": 0}``, ``{"$rel": "start", "offset": 2.0}``,
  ``{"$camera": {"role": "host"}}``, ``{"$speaker": {"role": "host"}}``,
  ``{"$input": "asset", "kind": "video", "name": "broll_city.mp4"}``.
* ``expect``: the completion checks, generalised the same way, evaluated on the actual draft.
* ``applicability``: format, required camera roles, required inputs; ``triggers``: instruction words.

Lifecycle: *candidate* (acquired from one verified task) -> *validated* (verified when executed on a
different production) -> *active* (reused successfully again) -> *retired* (repeated failures or by
the editor). Every execution is verified by Quality Control; a failed one is undone and the task falls
back to reasoning.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from bangonpro.ai_engine.department.intelligence import tokens
from bangonpro.ai_engine.department.registry import AGENT_ORDER

SKILL_TOOLS: dict[str, set[str]] = {
    "bop_switch_camera": {"agent", "start", "end", "camera", "reason"},
    "bop_remove_range": {"agent", "start", "end"},
    "bop_place_asset": {"agent", "file", "media_ref", "start", "duration", "position", "fade", "source_in"},
    "bop_add_title": {"agent", "text", "subtitle", "start", "duration", "position"},
}
CHECKS = {"camera_at", "camera_share", "has_clip", "title_at", "effect_at", "transition_at", "caption_contains", "duration", "no_defect"}
TIME_KEYS = {"start", "end", "time"}
ANCHOR_KEYS = {"$at": {"$at", "speaker_role", "rank", "point", "offset", "camera_role"}, "$rel": {"$rel", "offset"}, "$camera": {"$camera"}, "$speaker": {"$speaker"},
               "$input": {"$input", "kind", "name"}}
MATCH_THRESHOLD = 0.5


class SkillError(ValueError):
    pass


class NotApplicable(SkillError):
    pass


# ---------------------------------------------------------------------- validation
def _check_value(v: Any, where: str) -> None:
    if isinstance(v, dict):
        kind = next((k for k in ANCHOR_KEYS if k in v), None)
        if kind is None or set(v) - ANCHOR_KEYS[kind]:
            raise SkillError(f"{where}: not an allowed anchor {sorted(v)[:4]}")
        if kind == "$at" and (v["$at"] not in ("turn", "program", "shot") or v.get("point", "start") not in ("start", "mid", "end")
                              or not isinstance(v.get("offset", 0), (int, float)) or abs(float(v.get("offset", 0))) > 600):
            raise SkillError(f"{where}: bad $at anchor")
        if kind == "$at" and v.get("rank") is not None and not (v["rank"] in ("longest", "first", "last") or (isinstance(v["rank"], int) and 0 <= v["rank"] < 500)):
            raise SkillError(f"{where}: bad rank")
        if kind == "$rel" and (v["$rel"] not in ("start",) or not isinstance(v.get("offset"), (int, float)) or not 0 < float(v["offset"]) <= 600):
            raise SkillError(f"{where}: bad $rel anchor")
        if kind in ("$camera", "$speaker") and (not isinstance(v[kind], dict) or set(v[kind]) - {"role"} or not re.fullmatch(r"[a-z_]{1,24}", str(v[kind].get("role", "")))):
            raise SkillError(f"{where}: bad {kind}")
        if kind == "$input" and (v["$input"] != "asset" or v.get("kind") not in ("video", "image") or (v.get("name") is not None and not re.fullmatch(r"[\w .\-]{1,80}", str(v["name"])))):
            raise SkillError(f"{where}: bad $input")
        return
    if isinstance(v, bool) or v is None or isinstance(v, (int, float)):
        return
    if isinstance(v, str):
        if len(v) > 200 or re.search(r"[/\\]|\.\.|[\x00-\x08]", v):
            raise SkillError(f"{where}: strings may not hold paths or control characters")
        return
    raise SkillError(f"{where}: unsupported value")


def validate_spec(spec: dict[str, Any], *, depth: int = 0) -> dict[str, Any]:
    """Strict structural validation: only declarative steps over allowlisted tools and checks."""
    if not isinstance(spec, dict) or set(spec) - {"name", "instruction", "triggers", "agent", "agents", "applicability", "steps", "expect", "inputs"}:
        raise SkillError("unknown skill fields")
    steps = spec.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 12:
        raise SkillError("a skill has 1 to 12 steps")
    for i, st in enumerate(steps):
        if not isinstance(st, dict):
            raise SkillError(f"step {i} must be an object")
        if "skill" in st:
            if set(st) != {"skill"} or not re.fullmatch(r"sk_[0-9a-f]{12}", str(st["skill"])) or depth >= 2:
                raise SkillError(f"step {i}: bad skill reference")
            continue
        if set(st) - {"tool", "args"} or st.get("tool") not in SKILL_TOOLS or not isinstance(st.get("args"), dict):
            raise SkillError(f"step {i}: tool must be one of {sorted(SKILL_TOOLS)}")
        extra = set(st["args"]) - SKILL_TOOLS[st["tool"]]
        if extra:
            raise SkillError(f"step {i}: arguments {sorted(extra)} are not accepted by {st['tool']}")
        if st["args"].get("agent") not in AGENT_ORDER:
            raise SkillError(f"step {i}: agent must be a production agent")
        for k, v in st["args"].items():
            _check_value(v, f"step {i}.{k}")
    for j, e in enumerate(spec.get("expect") or []):
        if not isinstance(e, dict) or e.get("check") not in CHECKS:
            raise SkillError(f"expect {j}: unknown check")
        for k, v in e.items():
            _check_value(v, f"expect {j}.{k}")
    if spec.get("agent") not in AGENT_ORDER:
        raise SkillError("skill agent must be a production agent")
    return spec


# ---------------------------------------------------------------------- context anchors on a draft
def _turns(v: Any) -> list[dict[str, Any]]:
    out = []
    for s in v.speech():
        cam = v.speaker_camera.get(s["speaker_id"])
        out.append({**s, "role": v.role_of_cam.get(cam) if cam else None, "dur": s["end"] - s["start"]})
    return out


def _speaker_names(v: Any) -> dict[str, str]:
    sp = (v.st.metadata.get("editorial_context") or {}).get("speakers")
    return {k: (val.get("name") or k) for k, val in sp.items()} if isinstance(sp, dict) else {}


def _pick(turns: list[dict[str, Any]], role: str, rank: Any) -> dict[str, Any] | None:
    mine = [t for t in turns if t["role"] == role and not t.get("overlap")]
    if not mine:
        return None
    if rank == "longest":
        return max(mine, key=lambda t: t["dur"])
    if rank == "first":
        return mine[0]
    if rank == "last":
        return mine[-1]
    return mine[int(rank)] if isinstance(rank, int) and rank < len(mine) else None


def _point(t: dict[str, Any], p: str) -> float:
    return float(t["start"]) if p == "start" else float(t["end"]) if p == "end" else (float(t["start"]) + float(t["end"])) / 2


def _generalise_time(v: Any, t: float) -> dict[str, Any]:
    turns = _turns(v)
    cands: list[tuple[float, dict[str, Any]]] = [(abs(t) + 0.5, {"$at": "program", "point": "start", "offset": round(t, 2)})]
    for role in sorted({x["role"] for x in turns if x["role"]}):
        mine = [x for x in turns if x["role"] == role and not x.get("overlap")]
        for i, tr in enumerate(mine):
            ranks: list[Any] = [r for r in ("longest", "first", "last") if _pick(turns, role, r) is tr] or [i]
            for p in ("start", "mid", "end"):
                off = t - _point(tr, p)
                inside = tr["start"] - 0.05 <= t <= tr["end"] + 0.05
                cost = abs(off) + (0.0 if inside else 1.0) + (2.0 if isinstance(ranks[0], int) else 0.0)
                cands.append((cost, {"$at": "turn", "speaker_role": role, "rank": ranks[0], "point": p, "offset": round(off, 2)}))
    return min(cands, key=lambda c: c[0])[1]


def generalise_step(v: Any, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
    if tool not in SKILL_TOOLS:
        return None
    out: dict[str, Any] = {}
    names = _speaker_names(v)
    for k, val in args.items():
        if k not in SKILL_TOOLS[tool] or k in ("media_ref",):
            continue
        if k == "start":
            out[k] = _generalise_time(v, float(val))
        elif k == "end" and "start" in args:
            out[k] = {"$rel": "start", "offset": round(float(val) - float(args["start"]), 2)}
        elif k == "camera":
            role = v.role_of_cam.get(val)
            if not role:
                return None
            out[k] = {"$camera": {"role": role}}
        elif k == "text":
            spk = next((s for s, n in names.items() if n == val), val if val in v.speaker_camera else None)
            role = v.role_of_cam.get(v.speaker_camera.get(spk or "") or "")
            out[k] = {"$speaker": {"role": role}} if role else val
        elif k == "file":
            p = Path(str(val))
            out[k] = {"$input": "asset", "kind": "image" if p.suffix.lower() in (".png", ".jpg", ".jpeg") else "video", "name": p.name}
        else:
            out[k] = val
    if tool == "bop_place_asset" and "file" not in out:
        return None
    return out


def generalise_check(v: Any, e: dict[str, Any], step_starts: list[float]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, val in e.items():
        if k in ("time", "start") and isinstance(val, (int, float)):
            out[k] = _generalise_time(v, float(val))
        elif k == "end" and isinstance(val, (int, float)) and isinstance(e.get("start"), (int, float)):
            out[k] = {"$rel": "start", "offset": max(0.01, round(float(val) - float(e["start"]), 2))}
        elif k == "camera":
            role = v.role_of_cam.get(val)
            out[k] = {"$camera": {"role": role}} if role else val
        elif k in ("ok", "detail", "reason"):
            continue
        else:
            out[k] = val
    return out


def _structure_id(spec: dict[str, Any]) -> str:
    def shape(x: Any) -> Any:
        if isinstance(x, dict):
            return {k: shape(v) for k, v in sorted(x.items()) if k not in ("offset",)}
        if isinstance(x, list):
            return [shape(i) for i in x]
        return x if isinstance(x, str) else type(x).__name__

    return "sk_" + hashlib.sha256(json.dumps([shape(s) for s in spec["steps"]], sort_keys=True).encode()).hexdigest()[:12]


def acquire_from_task(intel: Any, ctx: dict[str, Any], task: dict[str, Any], v: Any = None) -> dict[str, Any] | None:
    """A verified task's compound steps, generalised. Tasks with low-level edits are not generalised."""
    if v is None:
        return None
    steps_raw = [ev["step"] for ev in task.get("events") or [] if ev.get("step") and ev.get("edit")]
    if not steps_raw or any(s["tool"] not in SKILL_TOOLS for s in steps_raw) or len(steps_raw) > 12:
        return None
    steps = []
    for s in steps_raw:
        g = generalise_step(v, s["tool"], s.get("args") or {})
        if g is None:
            return None
        steps.append({"tool": s["tool"], "args": g})
    expect = [generalise_check(v, e, []) for e in task.get("expect") or [] if isinstance(e, dict) and e.get("check") in CHECKS]
    agents = [s["args"].get("agent") for s in steps]
    spec = {"name": "_".join(sorted(tokens(task["instruction"]), key=task["instruction"].lower().find)[:5]) or "skill", "instruction": task["instruction"][:300],
            "triggers": sorted(tokens(task["instruction"])), "agent": max(set(agents), key=agents.count), "agents": sorted(set(agents)),
            "applicability": {"format": ctx["format"], "roles": sorted({json.dumps(x) for s in steps for x in _roles(s["args"])})}, "steps": steps, "expect": expect}
    spec["applicability"]["roles"] = sorted({r for s in steps for r in _roles(s["args"])})
    try:
        validate_spec(spec)
    except SkillError:
        return None
    sid = _structure_id(spec)
    existing = [s for s in intel.skills(id=sid)]
    if existing:
        cur = existing[-1]
        if ctx["job_id"] not in cur["provenance"]:
            cur["provenance"] = [*cur["provenance"], ctx["job_id"]][-20:]
            intel.put_skill(cur)
        return cur
    skill = {"id": sid, "version": 1, "name": spec["name"], "agent": spec["agent"], "format": ctx["format"], "series_id": ctx["series"], "status": "candidate", "spec": spec,
             "provenance": [ctx["job_id"], task["task_id"]], "origin_job": ctx["job_id"], "reason": "acquired from a task verified by Quality Control"}
    intel.put_skill(skill)
    return skill


def _roles(args: dict[str, Any]) -> list[str]:
    out = []
    for v in args.values():
        if isinstance(v, dict):
            if v.get("$at") == "turn" and v.get("speaker_role"):
                out.append(v["speaker_role"])
            for k in ("$camera", "$speaker"):
                if isinstance(v.get(k), dict) and v[k].get("role"):
                    out.append(v[k]["role"])
    return out


# ---------------------------------------------------------------------- binding + matching
def _resolve(v: Any, val: Any, bound: dict[str, Any], assets: list[Path]) -> Any:
    if not isinstance(val, dict):
        return val
    if "$at" in val:
        if val["$at"] == "program":
            base = 0.0 if val.get("point", "start") == "start" else v.end_frame / v.fps
        else:
            tr = _pick(_turns(v), val.get("speaker_role", ""), val.get("rank", "first"))
            if tr is None:
                raise NotApplicable(f"no {val.get('rank')} turn for role {val.get('speaker_role')}")
            base = _point(tr, val.get("point", "start"))
        t = round(max(0.0, base + float(val.get("offset", 0.0))), 2)
        if t > v.end_frame / v.fps:
            raise NotApplicable("anchor falls after the end of the programme")
        return t
    if "$rel" in val:
        if "start" not in bound:
            raise NotApplicable("relative time without a start")
        return round(float(bound["start"]) + float(val["offset"]), 2)
    if "$camera" in val:
        cam = next((c for c, r in v.role_of_cam.items() if r == val["$camera"]["role"] and c in v.media_of_cam), None)
        if cam is None:
            raise NotApplicable(f"no {val['$camera']['role']} camera")
        return cam
    if "$speaker" in val:
        cam = next((c for c, r in v.role_of_cam.items() if r == val["$speaker"]["role"]), None)
        spk = v.camera_owner.get(cam or "")
        if not spk:
            raise NotApplicable(f"no speaker on the {val['$speaker']['role']} camera")
        return _speaker_names(v).get(spk, spk)
    if "$input" in val:
        exts = (".png", ".jpg", ".jpeg") if val.get("kind") == "image" else (".mp4", ".mov", ".mkv", ".webm", ".mxf")
        same = [a for a in assets if a.name == val.get("name")]
        kind = [a for a in assets if a.suffix.lower() in exts]
        pick = same[0] if same else kind[0] if len(kind) == 1 else None
        if pick is None:
            raise NotApplicable(f"needs a {val.get('kind')} asset{' named ' + str(val.get('name')) if val.get('name') else ''} in the approved folders")
        return str(pick)
    raise NotApplicable("unknown anchor")


def bind(intel: Any, spec: dict[str, Any], v: Any, assets: list[Path], *, depth: int = 0) -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, Any]]]:
    validate_spec(spec, depth=depth)
    steps: list[tuple[str, dict[str, Any]]] = []
    for st in spec["steps"]:
        if "skill" in st:
            sub = next((s for s in intel.latest_skills(("validated", "active")) if s["id"] == st["skill"]), None)
            if sub is None:
                raise NotApplicable(f"combined skill {st['skill']} is not validated")
            s2, _ = bind(intel, sub["spec"], v, assets, depth=depth + 1)
            steps += s2
            continue
        bound: dict[str, Any] = {}
        for k in ("start", *[k for k in st["args"] if k != "start"]):
            if k in st["args"]:
                bound[k] = _resolve(v, st["args"][k], bound, assets)
        steps.append((st["tool"], bound))
    expect = []
    for e in spec.get("expect") or []:
        b: dict[str, Any] = {}
        for k in ("start", *[k for k in e if k != "start"]):
            if k in e:
                b[k] = _resolve(v, e[k], b, assets)
        expect.append(b)
    return steps, expect


def match(intel: Any, ctx: dict[str, Any], instruction: str, v: Any, assets: list[Path]) -> dict[str, Any] | None:
    """The best applicable skill for this instruction in this series and format, or None."""
    allowed = ("candidate", "validated", "active") if intel.settings().get("trial_candidate_skills", True) else ("validated", "active")
    want = tokens(instruction)
    best = None
    for s in intel.latest_skills(allowed):
        if s["format"] not in (None, ctx["format"]) or s["series_id"] not in (None, ctx["series"]):
            continue
        if s["status"] == "candidate" and s["origin_job"] == ctx["job_id"]:
            continue  # a candidate is tried on a different production, never re-run where it was learned
        trig = set(s["spec"].get("triggers") or [])
        sim = len(want & trig) / max(1, len(want | trig))
        if sim < MATCH_THRESHOLD:
            continue
        try:
            steps, expect = bind(intel, s["spec"], v, assets)
        except SkillError:
            continue
        rank = (sim, {"active": 2, "validated": 1, "candidate": 0}[s["status"]], s["successes"])
        if best is None or rank > best[0]:
            best = (rank, s, steps, expect, sim)
    if best is None:
        return None
    return {"skill": best[1], "steps": best[2], "expect": best[3], "similarity": round(best[4], 2)}


def record_outcome(intel: Any, skill: dict[str, Any], ctx: dict[str, Any], ok: bool, *, reason: str = "") -> dict[str, Any]:
    s = dict(skill)
    s["uses"] = int(s.get("uses", 0)) + 1
    if ok:
        s["successes"] = int(s.get("successes", 0)) + 1
        if s["status"] == "candidate" and ctx["job_id"] != s.get("origin_job"):
            s["status"], s["validated_on"], s["reason"] = "validated", ctx["job_id"], "verified by Quality Control on a different production"
        elif s["status"] == "validated" and ctx["job_id"] not in (s.get("origin_job"), s.get("validated_on")):
            s["status"], s["reason"] = "active", "reused successfully"
    else:
        s["failures"] = int(s.get("failures", 0)) + 1
        if s["failures"] >= 2 and s["failures"] > s["successes"]:
            s["status"], s["reason"] = "retired", f"retired after {s['failures']} failed executions: {reason[:120]}"
    s["provenance"] = [*s.get("provenance", []), ctx["job_id"]][-20:]
    intel.put_skill(s)
    return s


def set_skill_status(intel: Any, skill_id: str, status: str, reason: str = "") -> dict[str, Any]:
    if status not in ("candidate", "validated", "active", "retired"):
        raise SkillError("bad status")
    cur = [s for s in intel.skills(id=skill_id)]
    if not cur:
        raise KeyError(skill_id)
    s = dict(cur[-1])
    s["status"], s["reason"] = status, reason or f"set to {status} by the editor"
    intel.put_skill(s)
    return s


__all__ = ["SKILL_TOOLS", "NotApplicable", "SkillError", "acquire_from_task", "bind", "generalise_step", "match", "record_outcome", "set_skill_status", "validate_spec"]
