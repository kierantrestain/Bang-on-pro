"""Editorial Quality Control checks (V1.2.2), registered with ``quality_loop.register_qc_check``.

Cheap, deterministic and safe on a partial state (each check returns nothing when the
data it needs, e.g. shots or captions, does not exist yet). Codes are stable so the
quality loop can compare defects before and after a correction; ``error`` is used only
for problems a viewer would notice as broken (a clipped word, lost words, duplicated
footage, an unavailable angle on air, captions over removed speech).

* ``CUT_INSIDE_WORD`` / ``CUT_INSIDE_SPEECH`` / ``FLASH_FRAGMENT`` - awkward cuts
* ``WORD_REMOVED`` - a word lost to a pause or interruption cut
* ``DUPLICATED_FOOTAGE`` / ``REPEATED_CONTENT`` - repeated material (possible retake)
* ``ANSWER_WITHOUT_QUESTION`` / ``CONTEXT_MAY_BE_MISSING`` - missing context after a cut
* ``CAPTION_TRANSCRIPT_MISMATCH`` / ``CAPTION_OVER_REMOVED_SPEECH`` - captions vs the edit
* ``ONSCREEN_SPEAKER_MISMATCH`` / ``LOWER_THIRD_SPEAKER_NOT_ON_SCREEN`` / ``UNAVAILABLE_ANGLE_ON_AIR``

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import re
from typing import Any

from bangonpro.ai_engine.department import editorial_text as et
from bangonpro.ai_engine.state import QCIssue

_CUTAWAY_MARKERS = ("cutaway", "reaction", "listener", "wide", "intro", "outro", "bridge", "exchange", "topic_change", "unavailable", "interjection", "padding")


def _edited(st: Any, master_sec: float) -> float:
    from bangonpro.ai_engine.agents.rough_cut_multicam import map_master_to_edited_sec

    t = map_master_to_edited_sec(master_sec, st.kept_master_intervals) if st.kept_master_intervals else None
    if t is None:
        for s in st.rough_cut_shots:
            if s.master_sync_in_sec - 1e-3 <= master_sec <= s.master_sync_out_sec + 1e-3:
                return round(s.timeline_in_sec + (master_sec - s.master_sync_in_sec), 3)
        return 0.0
    return t


def _kept_mid(st: Any, a: float, b: float) -> bool:
    mid = (a + b) / 2.0
    return any(k.start_sec - 1e-3 <= mid <= k.end_sec + 1e-3 for k in st.kept_master_intervals)


def _all_words(st: Any) -> list[dict[str, Any]]:
    out = []
    for s in st.transcript_segments:
        if s.deleted:
            continue
        for w in et.segment_words(s):
            out.append({**w, "speaker_id": s.speaker_id, "segment_id": s.segment_id})
    return sorted(out, key=lambda w: w["start_sec"])


def _declared_word_cuts(st: Any) -> list[tuple[float, float]]:
    return [(d["start_sec"], d["end_sec"]) for d in ((st.metadata.get("rough_cut_decisions") or {}).get("disfluencies") or [])]


def check_awkward_cuts(st: Any) -> list[QCIssue]:
    kept = st.kept_master_intervals or []
    if len(kept) < 2:
        return []
    from bangonpro.ai_engine.department.editorial_pacing import interior_cut_points

    words = _all_words(st)
    issues: list[QCIssue] = []
    for out_pt, in_pt in interior_cut_points(kept):
        for t in (out_pt, in_pt):
            w = et.inside_word(t, words)
            if w is not None:
                issues.append(QCIssue("CUT_INSIDE_WORD", "error", f"Cut at {t:.2f}s splits the word '{w['word']}' ({w['speaker_id']})", _edited(st, t), {"master_sec": round(t, 3), "word": w["word"]}))
                continue
            seg = next((s for s in st.transcript_segments if not s.deleted and not et.segment_words(s) and s.start_sec + 0.08 < t < s.end_sec - 0.08), None)
            if seg is not None:
                issues.append(QCIssue("CUT_INSIDE_SPEECH", "error", f"Cut at {t:.2f}s falls inside speech ({seg.segment_id}, {seg.speaker_id})", _edited(st, t), {"master_sec": round(t, 3), "segment_id": seg.segment_id}))
    for k in kept:
        if k.duration_sec < 0.3 and any(s.end_sec > k.start_sec and s.start_sec < k.end_sec for s in st.transcript_segments):
            issues.append(QCIssue("FLASH_FRAGMENT", "warning", f"{k.duration_sec:.2f}s fragment of speech between two cuts at {k.start_sec:.2f}s", _edited(st, k.start_sec), {"master_sec": round(k.start_sec, 3)}))
    declared = _declared_word_cuts(st)
    lo, hi = kept[0].start_sec, kept[-1].end_sec
    for w in words:
        mid = (w["start_sec"] + w["end_sec"]) / 2.0
        if mid < lo or mid > hi or any(a - 1e-3 <= mid <= b + 1e-3 for a, b in declared):
            continue
        if not _kept_mid(st, w["start_sec"], w["end_sec"]):
            issues.append(QCIssue("WORD_REMOVED", "error", f"'{w['word']}' ({w['speaker_id']}, {mid:.2f}s) was removed by a pause/gap cut", _edited(st, mid), {"master_sec": round(mid, 3), "word": w["word"]}))
    return issues


def check_repeated_content(st: Any) -> list[QCIssue]:
    issues: list[QCIssue] = []
    shots = sorted(st.rough_cut_shots or [], key=lambda s: s.timeline_in_sec)
    for i, a in enumerate(shots):
        for b in shots[i + 1:]:
            ov = min(a.master_sync_out_sec, b.master_sync_out_sec) - max(a.master_sync_in_sec, b.master_sync_in_sec)
            if ov > 0.1:
                issues.append(QCIssue("DUPLICATED_FOOTAGE", "error", f"{b.shot_id} repeats {ov:.1f}s of {a.shot_id}", b.timeline_in_sec, {"master_sec": round(b.master_sync_in_sec, 3)}))
    if not st.kept_master_intervals:
        return issues
    seen: dict[tuple[str, ...], float] = {}
    for s in sorted(et.spoken_segments(st.transcript_segments), key=lambda s: s.start_sec):
        if not _kept_mid(st, s.start_sec, s.end_sec):
            continue
        for sent in [x for x in re.split(r"(?<=[.!?])\s+", s.text.strip()) if x]:
            key = tuple(et.tokens(sent))
            if len(key) < 6:
                continue
            if key in seen and s.start_sec - seen[key] <= 120.0:
                issues.append(QCIssue("REPEATED_CONTENT", "warning", f"Sentence repeated {s.start_sec - seen[key]:.0f}s apart (possible retake): '{sent[:60]}'", _edited(st, s.start_sec), {"master_sec": round(s.start_sec, 3), "first_master_sec": round(seen[key], 3)}))
            seen.setdefault(key, s.start_sec)
    return issues


def check_missing_context(st: Any) -> list[QCIssue]:
    kept = st.kept_master_intervals or []
    if not kept:
        return []
    issues: list[QCIssue] = []
    spoken = sorted(et.spoken_segments(st.transcript_segments), key=lambda s: s.start_sec)
    for i, q in enumerate(spoken):
        if not et.is_question_text(et.last_sentence(q.text)):
            continue
        ans = next((s for s in spoken[i + 1:] if s.speaker_id != q.speaker_id and s.start_sec - q.end_sec <= 8.0), None)
        if ans is None:
            continue
        q_words = et.segment_words(q)[-6:] or [{"start_sec": q.start_sec, "end_sec": q.end_sec}]
        q_kept = any(_kept_mid(st, w["start_sec"], w["end_sec"]) for w in q_words)
        if not q_kept and _kept_mid(st, ans.start_sec, min(ans.end_sec, ans.start_sec + 2.0)):
            issues.append(QCIssue("ANSWER_WITHOUT_QUESTION", "warning", f"{ans.speaker_id}'s answer is kept but {q.speaker_id}'s question '{et.last_sentence(q.text)[:50]}' was cut", _edited(st, ans.start_sec), {"master_sec": round(ans.start_sec, 3)}))
    words = _all_words(st)
    declared = _declared_word_cuts(st)
    for a, b in zip(kept, kept[1:]):
        removed = [w for w in words if a.end_sec <= (w["start_sec"] + w["end_sec"]) / 2.0 <= b.start_sec]
        removed = [w for w in removed if not any(x - 1e-3 <= (w["start_sec"] + w["end_sec"]) / 2 <= y + 1e-3 for x, y in declared)]
        if not removed:
            continue
        nxt = next((w for w in words if w["start_sec"] >= b.start_sec - 1e-3), None)
        if nxt is not None and et.norm_token(nxt["word"]) in (et.ANAPHORIC_OPENERS | et.CONNECTIVE_OPENERS):
            issues.append(QCIssue("CONTEXT_MAY_BE_MISSING", "warning", f"After a cut that removed {len(removed)} word(s), the kept line opens with '{nxt['word']}'", _edited(st, b.start_sec), {"master_sec": round(b.start_sec, 3)}))
    return issues


def check_caption_alignment(st: Any) -> list[QCIssue]:
    if not st.captions or not st.kept_master_intervals:
        return []
    from bangonpro.ai_engine.department.editorial_captions import caption_transcript_agreement

    agree = caption_transcript_agreement(st)
    issues: list[QCIssue] = []
    if agree["kept_words"] and (agree["recall"] < 0.9 or agree["precision"] < 0.9):
        issues.append(QCIssue("CAPTION_TRANSCRIPT_MISMATCH", "warning", f"Captions vs kept speech: recall {agree['recall']:.0%}, precision {agree['precision']:.0%}", 0.0, {k: v for k, v in agree.items() if k != "extra_examples"}))
    for cue in agree.get("cues_over_removed", []):
        issues.append(QCIssue("CAPTION_OVER_REMOVED_SPEECH", "error", f"Caption #{cue['index']} shows words that were cut from the edit", cue["start_sec"], {"index": cue["index"]}))
    return issues


def check_speaker_on_screen(st: Any) -> list[QCIssue]:
    shots = st.rough_cut_shots or []
    if not shots or not st.transcript_segments:
        return []
    plan = st.metadata.get("camera_plan") or {}
    spk_map = plan.get("speaker_camera_map") or st.profile.get("speaker_camera_map") or {}
    cam_owner = {}
    for spk, cam in spk_map.items():
        cam_owner.setdefault(cam, spk)
    issues: list[QCIssue] = []
    for sh in shots:
        owner = cam_owner.get(sh.camera_id)
        if owner is None or any(m in sh.reason for m in _CUTAWAY_MARKERS):
            continue
        talk: dict[str, float] = {}
        for seg in st.transcript_segments:
            ov = min(seg.end_sec, sh.master_sync_out_sec) - max(seg.start_sec, sh.master_sync_in_sec)
            if ov > 0:
                talk[seg.speaker_id] = talk.get(seg.speaker_id, 0.0) + ov
        if not talk:
            continue
        top = max(talk, key=lambda k: talk[k])
        if top != owner and talk[top] - talk.get(owner, 0.0) > 1.5:
            issues.append(QCIssue("ONSCREEN_SPEAKER_MISMATCH", "warning", f"{sh.shot_id} shows {owner} ({sh.camera_id}) while {top} speaks for {talk[top]:.1f}s", sh.timeline_in_sec, {"master_sec": round(sh.master_sync_in_sec, 3), "camera_id": sh.camera_id}))
    for lt in st.lower_thirds or []:
        spk = next((s for s, v in ((st.metadata.get("editorial_context") or {}).get("speakers") or {}).items() if v.get("name") == lt.title), lt.title)
        cam = spk_map.get(spk)
        on = [sh for sh in shots if sh.timeline_in_sec < lt.start_sec + lt.duration_sec and sh.timeline_out_sec > lt.start_sec]
        if cam and on and not any(sh.camera_id == cam for sh in on):
            issues.append(QCIssue("LOWER_THIRD_SPEAKER_NOT_ON_SCREEN", "warning", f"Lower third '{lt.title}' is shown while {cam} is not on screen", lt.start_sec, {"graphic_id": lt.graphic_id}))
    from bangonpro.ai_engine.department.editorial_director import camera_availability, unavailable_overlap

    avail = camera_availability(st)
    for sh in shots:
        bad = unavailable_overlap(avail, sh.camera_id, sh.master_sync_in_sec, sh.master_sync_out_sec)
        if bad > 0.04:
            issues.append(QCIssue("UNAVAILABLE_ANGLE_ON_AIR", "error", f"{sh.shot_id} shows {sh.camera_id} for {bad:.1f}s while it is black/frozen/missing", sh.timeline_in_sec, {"master_sec": round(sh.master_sync_in_sec, 3), "camera_id": sh.camera_id}))
    return issues


CHECKS = {
    "editorial_awkward_cuts": check_awkward_cuts,
    "editorial_repeated_content": check_repeated_content,
    "editorial_missing_context": check_missing_context,
    "editorial_caption_alignment": check_caption_alignment,
    "editorial_speaker_on_screen": check_speaker_on_screen,
}

__all__ = ["CHECKS", "check_awkward_cuts", "check_caption_alignment", "check_missing_context", "check_repeated_content", "check_speaker_on_screen"]
