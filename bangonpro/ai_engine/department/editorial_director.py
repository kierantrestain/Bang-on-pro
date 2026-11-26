"""Multicamera switching intelligence (V1.2.2) for the Director and the Rough Cut engine.

The V1.2.1 engine cut on diarised speaker turns and inserted reaction/wide cutaways at
fixed intervals. This module adds conversational context:

* **camera availability** - angles that are black, frozen or missing for a stretch
  (``st.metadata["visual_analysis"][cam]["unavailable_intervals"]`` from the media
  specialist, master clock unless ``clock == "source"``) are never put on air; the wide
  (or the next best angle) covers them;
* **natural cut points** - cutaways land on reactions (laughter, applause, listener
  interjections) or on sentence boundaries instead of a fixed timer, and go to the
  *listener* (or the person who reacted) rather than a generic angle;
* **wide shots** on crosstalk, rapid back-and-forth faster than the minimum shot, and at
  topic changes; one-word interjections never trigger a cut;
* **objective plan metrics** (cut rate, avoidable sub-minimum shots, unnecessary cuts,
  coverage, unavailable-angle time) used by the Director assessor and the evaluation.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import bisect
from collections import Counter
from typing import Any

from bangonpro.ai_engine.department import editorial_text as et

# Format cut-rate bands (camera changes per edited minute) above which a cut reads as choppy
REF_MAX_CAMERA_CUTS_PER_MIN: dict[str, float] = {"daily_news": 10.0, "podcast": 12.0, "interview": 8.0, "gameshow": 24.0, "sports_discussion": 18.0, "social": 30.0}
CUTAWAY_REASONS = ("cutaway", "reaction", "listener", "wide", "intro", "outro", "bridge", "exchange", "topic_change", "unavailable", "interjection")


def _intervals(raw: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for it in raw or []:
        if isinstance(it, dict):
            s, e = it.get("start_sec"), it.get("end_sec")
        elif isinstance(it, (list, tuple)) and len(it) >= 2:
            s, e = it[0], it[1]
        else:
            continue
        try:
            s, e = float(s), float(e)
        except (TypeError, ValueError):
            continue
        if e > s:
            out.append((s, e))
    return sorted(out)


def camera_availability(state: Any) -> dict[str, list[tuple[float, float]]]:
    """Unavailable master-clock intervals per camera (black / frozen / missing video)."""
    va = state.metadata.get("visual_analysis") or {}
    sr = state.sync_result
    out: dict[str, list[tuple[float, float]]] = {}
    for cam, info in va.items():
        if not isinstance(info, dict):
            continue
        ivs = _intervals(info.get("unavailable_intervals"))
        if str(info.get("clock", "master")) == "source" and sr is not None:
            off = float(sr.offsets_sec.get(cam, 0.0))
            ivs = [(s - off, e - off) for s, e in ivs]
        if ivs:
            out[cam] = ivs
    return out


def unavailable_overlap(avail: dict[str, list[tuple[float, float]]], cam: str, s: float, e: float) -> float:
    return sum(max(0.0, min(e, b) - max(s, a)) for a, b in avail.get(cam, []))


def build_switch_context(state: Any) -> dict[str, Any]:
    """Pre-computed context for the switching engine (cheap; no inference)."""
    from bangonpro.ai_engine.department.editorial_pacing import _moments

    segs = [s for s in state.transcript_segments if not s.deleted]
    talk: dict[str, Counter[str]] = {}
    for s in segs:
        talk.setdefault(s.speaker_id, Counter())[s.camera_id] += s.end_sec - s.start_sec
    totals = Counter({k: sum(v.values()) for k, v in talk.items()})
    moments = _moments(state)
    reactions = [m for m in moments if m["type"] in ("laughter", "applause", "reaction")]
    return {
        "boundaries": et.sentence_boundaries(segs),
        "reactions": sorted(reactions, key=lambda m: m["start_sec"]),
        "topic_starts": [sec.start_sec for sec in (state.story_sections or [])[1:]],
        "speaker_talk": dict(totals),
        "availability": camera_availability(state),
        "segments": sorted(segs, key=lambda s: s.start_sec),
    }


def listener_camera(ctx: dict[str, Any], speaker_id: str, speaker_to_cam: dict[str, str], available: list[str], base_cam: str) -> str | None:
    """The other main participant's own angle (the natural listener shot)."""
    for spk, _sec in sorted(ctx.get("speaker_talk", {}).items(), key=lambda kv: -kv[1]):
        if spk == speaker_id:
            continue
        cam = speaker_to_cam.get(spk)
        if cam and cam in available and cam != base_cam:
            return cam
    return None


def reactor_camera(moment: dict[str, Any], speaker_to_cam: dict[str, str], available: list[str], base_cam: str) -> str | None:
    spk = moment.get("speaker_id")
    cam = speaker_to_cam.get(spk) if spk else None
    return cam if cam and cam in available and cam != base_cam else None


def natural_cut_time(ctx: dict[str, Any], lo: float, hi: float, target: float) -> tuple[float | None, dict[str, Any] | None]:
    """Prefer the first reaction inside [lo, hi]; else the sentence boundary closest to target."""
    for m in ctx.get("reactions", []):
        if lo <= m["start_sec"] <= hi:
            return float(m["start_sec"]), m
        if m["start_sec"] > hi:
            break
    b = ctx.get("boundaries", [])
    i = bisect.bisect_left(b, lo)
    best, best_d = None, 1.6
    while i < len(b) and b[i] <= hi:
        d = abs(b[i] - target)
        if d < best_d:
            best, best_d = b[i], d
        i += 1
    return best, None


def rapid_exchange_indices(segs: list[Any], min_shot: float) -> set[int]:
    """Indices of segments in runs of >= 3 alternating turns each shorter than the minimum shot."""
    out: set[int] = set()
    run: list[int] = []

    def flush() -> None:
        if len(run) >= 3 and len({segs[k].speaker_id for k in run}) >= 2:
            out.update(run)

    for k, s in enumerate(segs):
        short = (s.end_sec - s.start_sec) < min_shot
        if short and (not run or (segs[run[-1]].speaker_id != s.speaker_id and s.start_sec - segs[run[-1]].end_sec < 0.6)):
            run.append(k)
            continue
        flush()
        run = [k] if short else []
    flush()
    return out


def _remerge(shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sp in shots:
        if out and out[-1]["camera_id"] == sp["camera_id"] and abs(out[-1]["end"] - sp["start"]) < 1e-3:
            if (sp["end"] - sp["start"]) > (out[-1]["end"] - out[-1]["start"]):
                out[-1]["reason"] = sp["reason"]
            out[-1]["confidence"] = round(min(out[-1]["confidence"], sp["confidence"]), 4)
            out[-1]["needs_review"] = bool(out[-1].get("needs_review") or sp.get("needs_review"))
            out[-1]["end"] = sp["end"]
        else:
            out.append(dict(sp))
    return out


def _absorb_short(shots: list[dict[str, Any]], min_shot: float, protect: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Merge avoidable sub-minimum shots into a contiguous neighbour on the same kept interval."""
    shots = [dict(s) for s in shots]
    changed = True
    while changed:
        changed = False
        for i, sp in enumerate(shots):
            dur = sp["end"] - sp["start"]
            if dur >= min_shot - 0.05 or any(sp["reason"].startswith(p) for p in protect):
                continue
            prev_ok = i > 0 and abs(shots[i - 1]["end"] - sp["start"]) < 1e-3
            next_ok = i + 1 < len(shots) and abs(shots[i + 1]["start"] - sp["end"]) < 1e-3
            if not (prev_ok or next_ok):
                continue
            if prev_ok and (not next_ok or (shots[i - 1]["end"] - shots[i - 1]["start"]) >= (shots[i + 1]["end"] - shots[i + 1]["start"])):
                shots[i - 1]["end"] = sp["end"]
            else:
                shots[i + 1]["start"] = sp["start"]
            shots.pop(i)
            changed = True
            break
    return _remerge(shots)


def cover_jump_splices(shots: list[dict[str, Any]], *, state: Any, wide_cam: str, speaker_to_cam: dict[str, str], ctx: dict[str, Any], min_shot: float, ok: Any) -> list[dict[str, Any]]:
    """Hide same-angle splices (pause compression, removed stutters) under the wide or a listener shot.

    A cut that removes material while the same close-up stays on screen is a jump cut. The
    shots around such splices are switched to a covering angle for at least the minimum shot,
    on the edited timeline, so the jump happens in the wide where it reads as a normal cut.
    """
    if len(shots) < 2:
        return shots
    times, t = [], 0.0
    for sp in shots:
        times.append((t, t + (sp["end"] - sp["start"])))
        t += sp["end"] - sp["start"]
    windows: list[list[Any]] = []
    for i in range(len(shots) - 1):
        a, b = shots[i], shots[i + 1]
        if a["camera_id"] != b["camera_id"] or abs(a["end"] - b["start"]) < 1e-3 or a["camera_id"] == wide_cam:
            continue
        spk = a.get("speaker_id") or ""
        cover = wide_cam if ok(wide_cam, a["end"] - min_shot, b["start"] + min_shot) else listener_camera(ctx, spk, speaker_to_cam, list(state.media_assets), a["camera_id"])
        if not cover:
            continue
        te = times[i][1]
        lo, hi = te - min_shot / 2.0, te + min_shot / 2.0
        if windows and lo <= windows[-1][1] + min_shot and windows[-1][2] == cover:
            windows[-1][1] = max(windows[-1][1], hi)
        else:
            windows.append([lo, hi, cover])
    if not windows:
        return shots
    out: list[dict[str, Any]] = []
    for sp, (ts, te) in zip(shots, times):
        cuts = [ts]
        for lo, hi, _c in windows:
            for x in (lo, hi):
                if ts + 1e-3 < x < te - 1e-3:
                    cuts.append(x)
        cuts = sorted(set(cuts)) + [te]
        for a, b in zip(cuts, cuts[1:]):
            mid = (a + b) / 2.0
            win = next((w for w in windows if w[0] <= mid <= w[1]), None)
            piece = {**sp, "start": round(sp["start"] + (a - ts), 4), "end": round(sp["start"] + (b - ts), 4)}
            if win is not None and win[2] != sp["camera_id"]:
                piece.update({"camera_id": win[2], "reason": "jump_cut_cover_" + ("wide" if win[2] == wide_cam else "listener"), "confidence": round(min(0.85, sp["confidence"]), 4)})
            out.append(piece)
    # Remnants shorter than the minimum shot join the covering shot next to them
    merged: list[dict[str, Any]] = []
    for sp in out:
        if merged and merged[-1]["camera_id"] == sp["camera_id"] and (sp["reason"].startswith("jump_cut_cover") or merged[-1]["reason"].startswith("jump_cut_cover")):
            merged[-1]["end_edit"] = merged[-1].get("end_edit", 0)
            merged.append(sp)
            continue
        merged.append(sp)
    final: list[dict[str, Any]] = []
    for sp in merged:
        sp.pop("end_edit", None)
        final.append(sp)
    k = 0
    while k < len(final):
        dur = final[k]["end"] - final[k]["start"]
        if dur < min_shot - 0.05 and not final[k]["reason"].startswith("jump_cut_cover"):
            nb = k - 1 if k > 0 and final[k - 1]["reason"].startswith("jump_cut_cover") else (k + 1 if k + 1 < len(final) and final[k + 1]["reason"].startswith("jump_cut_cover") else None)
            if nb is not None:
                final[k] = {**final[k], "camera_id": final[nb]["camera_id"], "reason": final[nb]["reason"]}
        k += 1
    return final


def refine_shot_plan(shots: list[dict[str, Any]], *, state: Any, wide_cam: str, speaker_to_cam: dict[str, str], ctx: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Availability substitution, topic-change wides and removal of no-op cuts (post-pass)."""
    from bangonpro.show_profiles.loader import get_effective_profile_settings

    if not shots:
        return shots
    ctx = ctx or build_switch_context(state)
    eff = get_effective_profile_settings(state.profile)
    min_shot = float(eff["min_shot_duration_sec"])
    prefs = state.profile.get("camera_preferences") or {}
    avail = ctx.get("availability") or {}
    video = [c for c, a in state.media_assets.items() if a.has_video and a.camera_role != "audio_recorder"]

    def ok(cam: str, s: float, e: float) -> bool:
        return cam in state.media_assets and unavailable_overlap(avail, cam, s, e) <= 1e-3

    out: list[dict[str, Any]] = []
    if avail:
        for sp in shots:
            bad = [(max(sp["start"], a), min(sp["end"], b)) for a, b in avail.get(sp["camera_id"], []) if min(sp["end"], b) > max(sp["start"], a)]
            if not bad:
                out.append(sp)
                continue
            cur = sp["start"]
            for a, b in bad + [(sp["end"], sp["end"])]:
                if a > cur + 1e-3:
                    out.append({**sp, "start": round(cur, 4), "end": round(a, 4)})
                if b > a:
                    choices = [wide_cam] + [speaker_to_cam.get(s) for s in ctx.get("speaker_talk", {})] + video
                    sub = next((c for c in choices if c and c != sp["camera_id"] and ok(c, a, b)), None)
                    if sub is not None:
                        out.append({**sp, "start": round(a, 4), "end": round(b, 4), "camera_id": sub, "reason": f"unavailable_{sp['camera_id']}_covered", "confidence": round(min(0.8, sp["confidence"]), 4), "needs_review": True})
                    else:
                        out.append({**sp, "start": round(a, 4), "end": round(b, 4), "needs_review": True, "reason": sp["reason"] + "_no_available_alternative"})
                cur = max(cur, b)
        shots = _absorb_short(out, min_shot)
    if bool(prefs.get("avoid_jump_cuts", True)):
        shots = cover_jump_splices(shots, state=state, wide_cam=wide_cam, speaker_to_cam=speaker_to_cam, ctx=ctx, min_shot=min_shot, ok=ok)
    if bool(prefs.get("wide_on_topic_change", True)) and wide_cam in video:
        wide_dur = max(min_shot, 2.5)
        refined: list[dict[str, Any]] = []
        topics = list(ctx.get("topic_starts", []))
        for sp in shots:
            t = next((x for x in topics if sp["start"] - 1e-3 <= x < sp["end"]), None)
            if t is None or sp["camera_id"] == wide_cam or not ok(wide_cam, t, t + wide_dur):
                refined.append(sp)
                continue
            if t - sp["start"] < min_shot:
                t = sp["start"]
            if sp["end"] - (t + wide_dur) < min_shot:
                refined.append(sp)
                continue
            if t > sp["start"]:
                refined.append({**sp, "end": round(t, 4)})
            refined.append({**sp, "start": round(t, 4), "end": round(t + wide_dur, 4), "camera_id": wide_cam, "reason": "topic_change_wide", "confidence": round(min(0.9, sp["confidence"]), 4)})
            refined.append({**sp, "start": round(t + wide_dur, 4)})
        shots = refined
    return _remerge(shots)


# ---------------------------------------------------------------------------
# Objective plan metrics (Director assessor, QC and the evaluation)
# ---------------------------------------------------------------------------
def shot_plan_metrics(state: Any, shots: list[dict[str, Any]], kept: list[Any] | None = None) -> dict[str, Any]:
    """Measure a planned shot list ({start, end, camera_id, speaker_id, reason}) on the master clock."""
    from bangonpro.ai_engine.department.editorial_pacing import format_key
    from bangonpro.show_profiles.loader import get_effective_profile_settings

    eff = get_effective_profile_settings(state.profile)
    min_shot = float(eff["min_shot_duration_sec"])
    max_shot = float(eff["max_shot_duration_sec"])
    fmt = format_key(state.profile)
    roles = dict(eff.get("camera_roles") or {})
    roles.update(state.profile.get("camera_roles") or {})
    spk_map = dict((state.metadata.get("camera_plan") or {}).get("speaker_camera_map") or eff.get("speaker_camera_map") or {})
    avail = camera_availability(state)
    segs = sorted((s for s in state.transcript_segments if not s.deleted), key=lambda s: s.start_sec)
    edited = sum(max(0.0, s["end"] - s["start"]) for s in shots)
    kept_lens = []
    for sp in shots:
        k = next((iv for iv in (kept or []) if iv.start_sec - 1e-3 <= sp["start"] and sp["end"] <= iv.end_sec + 1e-3), None)
        kept_lens.append(k.duration_sec if k is not None else float("inf"))
    camera_cuts = sum(1 for a, b in zip(shots, shots[1:]) if a["camera_id"] != b["camera_id"])
    # What the viewer sees: consecutive shots on one angle read as a single shot (a splice inside
    # it is a jump cut, counted separately), so shot length is measured on camera runs.
    runs: list[dict[str, Any]] = []
    for i, sp in enumerate(shots):
        if runs and runs[-1]["camera_id"] == sp["camera_id"]:
            runs[-1]["dur"] += sp["end"] - sp["start"]
            runs[-1]["kept_ok"] = runs[-1]["kept_ok"] or kept_lens[i] >= min_shot
        else:
            runs.append({"camera_id": sp["camera_id"], "at": sp["start"], "dur": sp["end"] - sp["start"], "kept_ok": kept_lens[i] >= min_shot})
    sub_min, noop, interjection, too_long = [], [], [], []
    for k, r in enumerate(runs):
        if r["dur"] < min_shot - 0.05 and k < len(runs) - 1 and r["kept_ok"]:
            sub_min.append({"at_sec": round(r["at"], 3), "duration_sec": round(r["dur"], 3), "camera_id": r["camera_id"]})
        if r["dur"] > max_shot * 1.5:
            too_long.append({"at_sec": round(r["at"], 3), "duration_sec": round(r["dur"], 3), "camera_id": r["camera_id"]})
    jump_cuts = [
        {"at_sec": round(b["start"], 3), "camera_id": b["camera_id"]}
        for a, b in zip(shots, shots[1:])
        if a["camera_id"] == b["camera_id"] and abs(a["end"] - b["start"]) >= 1e-3 and roles.get(b["camera_id"]) != "wide"
    ]
    for i, sp in enumerate(shots):
        dur = sp["end"] - sp["start"]
        if i and shots[i - 1]["camera_id"] == sp["camera_id"] and abs(shots[i - 1]["end"] - sp["start"]) < 1e-3:
            noop.append({"at_sec": round(sp["start"], 3), "camera_id": sp["camera_id"]})
        if 0 < i < len(shots) - 1 and shots[i - 1]["camera_id"] == shots[i + 1]["camera_id"] != sp["camera_id"] and not any(k in sp["reason"] for k in ("reaction", "listener", "cover", "wide")):
            on = [s for s in segs if s.end_sec > sp["start"] and s.start_sec < sp["end"]]
            if on and all((s.end_sec - s.start_sec) < 1.2 for s in on if spk_map.get(s.speaker_id) == sp["camera_id"]) and any(spk_map.get(s.speaker_id) == sp["camera_id"] for s in on) and dur < 1.5 * min_shot:
                interjection.append({"at_sec": round(sp["start"], 3), "camera_id": sp["camera_id"]})
    on_angle = total = overlap_total = overlap_wide = 0.0
    for seg in segs:
        want = spk_map.get(seg.speaker_id)
        for sp in shots:
            ov = min(seg.end_sec, sp["end"]) - max(seg.start_sec, sp["start"])
            if ov <= 0:
                continue
            if seg.is_overlap:
                overlap_total += ov
                if roles.get(sp["camera_id"]) == "wide":
                    overlap_wide += ov
            if want:
                total += ov
                if sp["camera_id"] == want or roles.get(sp["camera_id"]) == "wide" or "reaction" in sp["reason"] or "listener" in sp["reason"]:
                    on_angle += ov
    unavailable = sum(unavailable_overlap(avail, sp["camera_id"], sp["start"], sp["end"]) for sp in shots)
    minutes = edited / 60.0 if edited > 0 else 0.0
    return {
        "shots": len(shots),
        "edited_sec": round(edited, 3),
        "camera_cuts": camera_cuts,
        "camera_cuts_per_min": round(camera_cuts / minutes, 3) if minutes > 0 else 0.0,
        "ref_max_camera_cuts_per_min": REF_MAX_CAMERA_CUTS_PER_MIN.get(fmt, 12.0),
        "sub_minimum_shots": sub_min,
        "shots_too_long": too_long,
        "noop_cuts": noop,
        "interjection_cuts": interjection,
        "unnecessary_cuts": len(noop) + len(interjection),
        "close_up_jump_cuts": jump_cuts,
        "speaker_coverage": round(on_angle / total, 4) if total > 0 else None,
        "overlap_on_wide": round(overlap_wide / overlap_total, 4) if overlap_total > 0 else None,
        "unavailable_on_air_sec": round(unavailable, 3),
        "min_shot_sec": min_shot,
        "max_shot_sec": max_shot,
    }


def provisional_shot_plan(st: Any) -> dict[str, Any] | None:
    """Plan the rough cut's shots without emitting anything (pure; used by the Director assessor)."""
    from bangonpro.ai_engine.agents import rough_cut_multicam as rcm
    from bangonpro.ai_engine.department.editorial_pacing import compute_pacing_plan

    if st.sync_result is None or not st.media_assets or not st.transcript_segments or not st.intent.multicam_switch:
        return None
    wide_cam, reaction_cam, speaker_to_cam = rcm._resolve_camera_roles(st)
    ref = st.sync_result.reference_camera_id
    ref_dur = st.media_assets[ref].duration_sec if ref in st.media_assets else max(a.duration_sec for a in st.media_assets.values())
    common = st.sync_result.common_duration_sec or ref_dur
    kept = compute_pacing_plan(st, common).kept
    shots = rcm.plan_rough_cut_shots(
        st, kept, wide_cam=wide_cam, reaction_cam=reaction_cam, speaker_to_cam=speaker_to_cam,
        applause_times=rcm._detect_high_energy_applause_times(st, common),
    )
    return {"shots": shots, "kept": kept, "metrics": shot_plan_metrics(st, shots, kept)}


__all__ = [
    "REF_MAX_CAMERA_CUTS_PER_MIN",
    "build_switch_context",
    "camera_availability",
    "listener_camera",
    "natural_cut_time",
    "provisional_shot_plan",
    "rapid_exchange_indices",
    "reactor_camera",
    "refine_shot_plan",
    "shot_plan_metrics",
    "unavailable_overlap",
]
