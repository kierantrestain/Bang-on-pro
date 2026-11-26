"""Conversational revisions for the Executive creative director (V1.3).

An editor describes a change in plain language ("tighten the opening but keep the natural
conversation", "more reaction shots during this debate", "preserve the suspense before each
reveal", "create five strong standalone vertical clips"). ``interpret_revision`` turns it,
deterministically, into a validated plan:

* a **profile overlay** of learnable keys only (bounded by the Learning Agent's guard rails),
* the **first agent to rerun** (everything before it is reused from checkpoints and caches),
* an optional **master-clock range**; outside it, and inside any locked range, the previous
  edit is kept verbatim (``apply_revision_scope``).

Anything the interpreter cannot map is reported back as not understood instead of guessed.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import re
from typing import Any

from bangonpro.ai_engine.department.registry import AGENT_ORDER

_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def _scaled(profile: dict[str, Any], section: str, key: str, factor: float, default: float) -> float:
    base = (profile.get(section) or {}).get(key) if section else profile.get(key)
    return round(float(base if isinstance(base, (int, float)) else default) * factor, 3)


def _pacing_defaults(profile: dict[str, Any]) -> dict[str, float]:
    from bangonpro.ai_engine.department.editorial_pacing import FORMAT_PACING_DEFAULTS, format_key

    fmt = FORMAT_PACING_DEFAULTS.get(format_key(profile), {})
    er = profile.get("editing_rules") or {}
    return {k: float(er.get(k, fmt.get(k, d))) for k, d in (("natural_pause_sec", 0.45), ("turn_pause_sec", 0.5), ("beat_pause_sec", 0.6), ("min_pause_saving_sec", 0.3), ("protected_pause_max_sec", 1.5), ("reaction_pause_max_sec", 4.0))}


def _range_from_text(low: str, duration: float) -> tuple[float, float] | None:
    m = re.search(r"(?:from|between)\s+(\d+):(\d{2})\s+(?:to|and|-)\s+(\d+):(\d{2})", low)
    if m:
        a = int(m.group(1)) * 60 + int(m.group(2))
        b = int(m.group(3)) * 60 + int(m.group(4))
        return (float(min(a, b)), float(max(a, b)))
    span = min(90.0, max(20.0, 0.15 * duration)) if duration > 0 else 60.0
    if re.search(r"\b(the )?(opening|intro|start|beginning|cold open)\b", low):
        return (0.0, span)
    if re.search(r"\b(the )?(ending|outro|close|closing|end)\b", low) and duration > 0:
        return (max(0.0, duration - span), duration)
    return None


def interpret_revision(text: str, profile: dict[str, Any], *, duration_sec: float = 0.0, selected_range: tuple[float, float] | None = None) -> dict[str, Any]:
    """Map a plain-language revision to ``{overlay, start_agent, range_sec, actions, not_understood}``."""
    low = (text or "").lower()
    pd = _pacing_defaults(profile)
    overlay: dict[str, Any] = {}
    agents: set[str] = set()
    actions: list[str] = []

    def er(**kv: Any) -> None:
        overlay.setdefault("editing_rules", {}).update(kv)

    tighten = re.search(r"\b(tighten|tighter|tight|faster|quicker|pick up the pace|cut (it )?down|shorten|snappier|trim)\b", low)
    natural = re.search(r"\b(keep|preserve|retain)\b[^.]*\b(natural|conversation|rhythm|flow|breath)", low) or re.search(r"\b(looser|loosen|breathe|breathing room|slower)\b", low)
    if tighten:
        f = 0.85 if natural else 0.6  # "tighten ... but keep the natural conversation" = gentle
        er(natural_pause_sec=round(pd["natural_pause_sec"] * f, 3), turn_pause_sec=round(pd["turn_pause_sec"] * f, 3),
           beat_pause_sec=round(pd["beat_pause_sec"] * (0.9 if natural else 0.7), 3), min_pause_saving_sec=round(pd["min_pause_saving_sec"] * 0.75, 3))
        agents.add("rough_cut")
        actions.append("Tighter pacing" + (", keeping conversational beats" if natural else ""))
    elif natural:
        er(pause_policy="compress", natural_pause_sec=round(pd["natural_pause_sec"] * 1.3, 3), turn_pause_sec=round(pd["turn_pause_sec"] * 1.3, 3), beat_pause_sec=round(pd["beat_pause_sec"] * 1.2, 3))
        agents.add("rough_cut")
        actions.append("Looser, more natural pacing")
    if re.search(r"\b(suspense|reveal|dramatic pause|tension|build[- ]up)\b", low):
        er(protect_punchline_pauses=True, protect_reaction_pauses=True, protected_pause_max_sec=round(min(8.0, pd["protected_pause_max_sec"] * 1.6), 3), reaction_pause_max_sec=round(min(15.0, pd["reaction_pause_max_sec"] * 1.5), 3))
        agents.add("rough_cut")
        actions.append("Longer protected beats before reveals and reactions")
    if re.search(r"\bkeep every word\b|\bdon'?t cut (any )?words\b", low):
        er(remove_false_starts=False, remove_stutters=False, remove_repeated_phrases=False, remove_fillers=False)
        agents.add("rough_cut")
        actions.append("No word-level removals")
    elif re.search(r"\bremove (the )?(fillers?|ums?|uhs?|false starts|stutters)\b", low):
        er(remove_fillers=True, remove_false_starts=True, remove_stutters=True)
        agents.add("rough_cut")
        actions.append("Remove fillers and false starts")
    more_react = re.search(r"\bmore (reaction|reactions|cutaways|listener)\b", low)
    fewer_react = re.search(r"\b(fewer|less) (reaction|reactions|cutaways)\b", low)
    if more_react or fewer_react:
        cur = float(profile.get("reaction_shot_frequency", 0.2) or 0.2)
        overlay["reaction_shot_frequency"] = round(min(1.0, cur + 0.15) if more_react else max(0.0, cur - 0.15), 3)
        agents.add("multicam_director")
        actions.append(("More" if more_react else "Fewer") + " reaction shots")
    if re.search(r"\b(fewer|less|too many) (cuts|camera (cuts|switches|changes))\b|\bstay (on|longer)\b|\bless cutting\b", low):
        overlay["min_shot_duration_sec"] = _scaled(profile, "", "min_shot_duration_sec", 1.3, 2.0)
        agents.add("multicam_director")
        actions.append("Fewer, longer shots")
    elif re.search(r"\bmore (cuts|dynamic|energy|switching)\b|\bswitch more\b", low):
        overlay["min_shot_duration_sec"] = _scaled(profile, "", "min_shot_duration_sec", 0.8, 2.0)
        agents.add("multicam_director")
        actions.append("More dynamic camera switching")
    if re.search(r"\bmore wides?\b|\bmore wide shots\b|\bwider\b", low):
        overlay["wide_shot_interval_sec"] = _scaled(profile, "", "wide_shot_interval_sec", 0.7, 30.0)
        agents.add("multicam_director")
        actions.append("More wide shots")
    if re.search(r"\b(graphics|lower thirds?|titles?|branding)\b", low):
        overlay.setdefault("graphics_rules", {})["enable_title_card"] = True
        agents.add("graphics")
        actions.append("Graphics and lower thirds from the series templates")
    if re.search(r"\bcaptions?|subtitles?\b", low):
        agents.add("captions")
        actions.append("Captions on the revised timeline")
    m = re.search(r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:strong\s+|standalone\s+|good\s+|short\s+|best\s+)*(vertical|social|short[- ]form|shorts|reels?|tiktok)", low)
    if m or re.search(r"\b(vertical|9:16|shorts|reels|tiktok) (clips?|versions?)\b", low):
        n = (int(m.group(1)) if m and m.group(1).isdigit() else _NUM.get(m.group(1), 3)) if m else 3
        overlay.setdefault("distribution_rules", {}).update({"social_versions": True, "social_clip_count": max(1, min(10, n)), "highlight_count": max(1, min(10, n))})
        agents.add("editorial_intelligence")
        actions.append(f"{n} standalone vertical clip(s)")
    rng = selected_range or (_range_from_text(low, duration_sec) if agents & {"rough_cut", "multicam_director"} else None)
    if rng and re.search(r"\b(this|selected|the selection|this section)\b", low) and selected_range is None:
        rng = None  # "this section" needs a timeline selection; never guess one
    start = next((a for a in AGENT_ORDER if a in agents), None)
    return {
        "instruction": text,
        "overlay": overlay,
        "agents": sorted(agents, key=AGENT_ORDER.index),
        "start_agent": start,
        "range_sec": [round(rng[0], 3), round(rng[1], 3)] if rng else None,
        "actions": actions,
        "not_understood": start is None,
        "needs_selection": bool(re.search(r"\b(this|selected) (section|part|bit|range|debate)\b", low)) and selected_range is None,
    }


# ----------------------------------------------------------------------------- scope splicing
def _clip_spans(spans: list[dict[str, Any]], keep: list[tuple[float, float]]) -> list[dict[str, Any]]:
    out = []
    for sp in spans:
        for a, b in keep:
            s, e = max(sp["start"], a), min(sp["end"], b)
            if e - s > 1e-3:
                out.append({**sp, "start": round(s, 4), "end": round(e, 4)})
    return out


def _complement(ranges: list[tuple[float, float]], lo: float = -1e9, hi: float = 1e9) -> list[tuple[float, float]]:
    out, cur = [], lo
    for a, b in sorted(ranges):
        if a > cur:
            out.append((cur, a))
        cur = max(cur, b)
    if cur < hi:
        out.append((cur, hi))
    return out


def apply_revision_scope(state: Any, planned: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the previous edit outside the revision range and inside locked ranges.

    ``state.metadata['revision_scope']`` = ``{"range": [a, b] | None, "locks": [[a, b], ...],
    "previous_spans": [...]}`` (master clock). It stays in the state for the whole revision run
    (the director's provisional plan and quality-loop corrections must respect it too);
    ``ProductionJob.rerun`` clears it before any later run.
    """
    scope = state.metadata.get("revision_scope")
    if not scope or not scope.get("previous_spans"):
        return planned
    prev = [dict(s) for s in scope["previous_spans"]]
    rng = scope.get("range")
    locks = [tuple(x) for x in scope.get("locks") or []]
    editable = [tuple(rng)] if rng else [(-1e9, 1e9)]
    editable = [(max(a, la), min(b, lb)) for a, b in editable for la, lb in _complement(locks) if min(b, lb) > max(a, la)]
    fixed = _complement(editable)
    merged = sorted(_clip_spans(prev, fixed) + _clip_spans(planned, editable), key=lambda s: s["start"])
    # Adjacent pieces on the same camera from either side become one shot (no cut that changes nothing)
    out: list[dict[str, Any]] = []
    for sp in merged:
        if out and out[-1]["camera_id"] == sp["camera_id"] and abs(out[-1]["end"] - sp["start"]) < 1e-3:
            out[-1]["end"] = sp["end"]
        else:
            out.append(sp)
    from bangonpro.ai_engine.state import TimeInterval

    kept: list[TimeInterval] = []
    for sp in out:
        if kept and abs(kept[-1].end_sec - sp["start"]) < 1e-3:
            kept[-1] = TimeInterval(start_sec=kept[-1].start_sec, end_sec=sp["end"], label=kept[-1].label)
        else:
            kept.append(TimeInterval(start_sec=sp["start"], end_sec=sp["end"], label=f"kept_{len(kept) + 1}"))
    state.kept_master_intervals = kept
    state.metadata["revision_scope_applied"] = {"range": rng, "locks": [list(x) for x in locks], "spans_kept_from_previous": len(_clip_spans(prev, fixed))}
    return out


def previous_spans(state: Any) -> list[dict[str, Any]]:
    """The current draft's shots as planner spans (master clock)."""
    return [
        {"start": s.master_sync_in_sec, "end": s.master_sync_out_sec, "camera_id": s.camera_id, "speaker_id": s.speaker_id,
         "confidence": s.confidence, "reason": getattr(s, "reason", "previous_edit"), "needs_review": bool(s.needs_review)}
        for s in state.rough_cut_shots
    ]


def timeline_to_master(state: Any, a: float, b: float) -> tuple[float, float] | None:
    """Map a timeline selection on the current draft to the master clock."""
    pts = []
    for t in (a, b):
        for s in state.rough_cut_shots:
            if s.timeline_in_sec <= t <= s.timeline_out_sec:
                pts.append(s.master_sync_in_sec + (t - s.timeline_in_sec))
                break
    return (min(pts), max(pts)) if len(pts) == 2 else None


__all__ = ["apply_revision_scope", "interpret_revision", "previous_spans", "timeline_to_master"]
