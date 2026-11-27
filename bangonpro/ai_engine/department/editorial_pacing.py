"""Rough Cut pacing (V1.2.2): pause compression, protected beats and disfluency removal.

V1.2.1 excised **every** interior silence longer than the threshold (leaving
``2 x keep_padding_sec``). V1.2.2 never removes meaningful content merely because it
contains silence:

* each pause is **classified** (dead air, turn gap, after a question, before a
  punchline, next to laughter/applause/reaction) from the transcript, timed words and
  the non-speech activity measured by the Editorial Intelligence agent;
* unprotected pauses are **compressed** to a natural, show-specific length (daily news
  is tight, podcasts keep conversational breath) and only when the saving is worth an
  edit point; protected pauses keep their timing up to a profile maximum;
* cut points never fall inside speech evidence (segments, timed words, non-speech
  activity), so no word or transcript segment is ever split by a pause cut;
* with genuine word timestamps, false starts, stutters and immediately repeated phrases
  are removed on word boundaries (never when the brief asks to keep every word).

Everything here is deterministic and pure (state in, plan out), so the quality-loop
assessor can recompute it without re-running inference.

Profile keys (all optional, inside ``editing_rules``; format defaults in
``FORMAT_PACING_DEFAULTS``): ``pause_policy`` (compress | remove | keep),
``natural_pause_sec``, ``turn_pause_sec``, ``protected_pause_max_sec``,
``reaction_pause_max_sec``, ``min_pause_saving_sec``, ``protect_question_pauses``,
``protect_punchline_pauses``, ``protect_reaction_pauses``, ``remove_false_starts``,
``remove_stutters``, ``remove_repeated_phrases``, ``remove_fillers``, ``edge_guard_sec``.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import bisect
from dataclasses import asdict, dataclass, field
from typing import Any

from bangonpro.ai_engine.department import editorial_text as et
from bangonpro.ai_engine.state import ProjectAnalysisState, TimeInterval

# Show-specific pacing defaults (seconds). Daily news is tight; podcasts and interviews
# keep conversational breath; gameshows protect suspense and long audience reactions.
FORMAT_PACING_DEFAULTS: dict[str, dict[str, Any]] = {
    "daily_news": {"beat_pause_sec": 0.5, "natural_pause_sec": 0.35, "turn_pause_sec": 0.45, "protected_pause_max_sec": 0.8, "reaction_pause_max_sec": 2.0, "min_pause_saving_sec": 0.25, "remove_fillers": True},
    "podcast": {"beat_pause_sec": 0.85, "natural_pause_sec": 0.6, "turn_pause_sec": 0.7, "protected_pause_max_sec": 1.5, "reaction_pause_max_sec": 4.0, "min_pause_saving_sec": 0.4, "remove_fillers": False},
    "interview": {"beat_pause_sec": 0.9, "natural_pause_sec": 0.7, "turn_pause_sec": 0.8, "protected_pause_max_sec": 1.8, "reaction_pause_max_sec": 4.0, "min_pause_saving_sec": 0.4, "remove_fillers": False},
    "gameshow": {"beat_pause_sec": 0.75, "natural_pause_sec": 0.45, "turn_pause_sec": 0.5, "protected_pause_max_sec": 3.0, "reaction_pause_max_sec": 8.0, "min_pause_saving_sec": 0.3, "remove_fillers": False},
    "sports_discussion": {"beat_pause_sec": 0.75, "natural_pause_sec": 0.45, "turn_pause_sec": 0.5, "protected_pause_max_sec": 1.2, "reaction_pause_max_sec": 4.0, "min_pause_saving_sec": 0.3, "remove_fillers": False},
    # V1.6 single-presenter YouTube: jump cuts between thoughts, breath kept only where it lands a point.
    "youtube": {"beat_pause_sec": 0.45, "natural_pause_sec": 0.3, "turn_pause_sec": 0.35, "protected_pause_max_sec": 0.8, "reaction_pause_max_sec": 2.0, "min_pause_saving_sec": 0.2, "remove_fillers": True},
    "social": {"beat_pause_sec": 0.35, "natural_pause_sec": 0.25, "turn_pause_sec": 0.3, "protected_pause_max_sec": 0.6, "reaction_pause_max_sec": 1.5, "min_pause_saving_sec": 0.2, "remove_fillers": True},
}
_GENERIC_DEFAULTS: dict[str, Any] = {
    "pause_policy": "compress",
    "protect_question_pauses": True,
    "protect_punchline_pauses": True,
    "protect_reaction_pauses": True,
    "remove_false_starts": True,
    "remove_stutters": True,
    "remove_repeated_phrases": True,
    "edge_guard_sec": 0.06,
}
# Safe ranges applied when reading (learned overlays are also clamped by the Learning Agent)
_BOUNDS: dict[str, tuple[float, float]] = {
    "natural_pause_sec": (0.1, 3.0),
    "beat_pause_sec": (0.1, 3.0),
    "turn_pause_sec": (0.1, 3.0),
    "protected_pause_max_sec": (0.2, 8.0),
    "reaction_pause_max_sec": (0.3, 15.0),
    "min_pause_saving_sec": (0.1, 3.0),
    "edge_guard_sec": (0.02, 0.3),
}
TURN_BEAT_MAX_SEC = 2.0  # V1.3: longer pauses inside a turn are dead air
PROTECTED_KINDS: frozenset[str] = frozenset({"after_question", "before_punchline", "reaction_beat"})
# Audible reactions only ("humour" spans the setup line itself, so it is not a reaction time)
REACTION_MOMENTS: frozenset[str] = frozenset({"laughter", "applause", "reaction"})

# Word-level disfluency vocabularies (conservative: only words that commonly stutter)
_STUTTER_PRONE: frozenset[str] = frozenset(
    {"i", "i'm", "i've", "i'd", "we", "we're", "you", "you're", "he", "she", "they", "they're", "it", "it's", "the", "a", "an",
     "and", "but", "or", "so", "to", "of", "in", "on", "at", "for", "with", "my", "our", "your", "this", "what", "when", "if", "because", "like"}
)
_EMPHATIC_OK: frozenset[str] = frozenset(
    {"no", "yes", "very", "really", "go", "bye", "ha", "oh", "far", "more", "much", "many", "ok", "okay", "yeah", "right", "now", "again", "never", "hey", "wow", "please", "come", "well"}
)


def format_key(profile: dict[str, Any]) -> str:
    ft = str(profile.get("format_type") or "").lower()
    if ft in FORMAT_PACING_DEFAULTS:
        return ft
    pid = str(profile.get("profile_id") or "").lower()
    for key in FORMAT_PACING_DEFAULTS:
        if key.split("_")[0] in pid:
            return key
    return "podcast"


def resolve_pacing_rules(state: ProjectAnalysisState) -> dict[str, Any]:
    """Merge format defaults, profile ``editing_rules`` and brief directives (in that order)."""
    from bangonpro.ai_engine.department.editorial_brief import interpret_brief

    fmt = format_key(state.profile)
    rules: dict[str, Any] = {**_GENERIC_DEFAULTS, **FORMAT_PACING_DEFAULTS[fmt]}
    er = state.profile.get("editing_rules", {}) or {}
    for k in list(rules) + ["keep_padding_sec"]:
        if k in er:
            rules[k] = er[k]
    rules.setdefault("keep_padding_sec", float(er.get("keep_padding_sec", 0.2)))
    brief = interpret_brief(state.instruction or state.intent.raw_instruction or "")
    pacing = brief["directives"].get("pacing", "natural")
    rules["brief_pacing"] = pacing
    rules["keep_every_word"] = bool(brief["directives"].get("keep_every_word"))
    if pacing == "tight":
        for k in ("natural_pause_sec", "turn_pause_sec"):
            rules[k] = float(rules[k]) * 0.6
        rules["protected_pause_max_sec"] = float(rules["protected_pause_max_sec"]) * 0.75
        rules["min_pause_saving_sec"] = float(rules["min_pause_saving_sec"]) * 0.75
    elif pacing == "loose":
        for k in ("natural_pause_sec", "turn_pause_sec", "protected_pause_max_sec"):
            rules[k] = float(rules[k]) * 1.5
    if rules["keep_every_word"]:
        rules.update({"remove_false_starts": False, "remove_stutters": False, "remove_repeated_phrases": False, "remove_fillers": False})
    for k, (lo, hi) in _BOUNDS.items():
        try:
            rules[k] = float(min(hi, max(lo, float(rules[k]))))
        except (TypeError, ValueError):
            rules[k] = float(FORMAT_PACING_DEFAULTS[fmt].get(k, _GENERIC_DEFAULTS.get(k, lo)))
    if str(rules.get("pause_policy")) not in ("compress", "remove", "keep"):
        rules["pause_policy"] = "compress"
    rules["format"] = fmt
    return rules


@dataclass
class PacingPlan:
    kept: list[TimeInterval]
    cuts: list[dict[str, Any]] = field(default_factory=list)
    pauses: list[dict[str, Any]] = field(default_factory=list)
    disfluencies: list[dict[str, Any]] = field(default_factory=list)
    rules: dict[str, Any] = field(default_factory=dict)
    start_bound: float = 0.0
    end_bound: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        removed = sum(c["end_sec"] - c["start_sec"] for c in self.cuts)
        prot = [p for p in self.pauses if p["protected"]]
        return {
            "method": "v1.2.2 pause classification + compression; word-boundary disfluency removal",
            "rules": self.rules,
            "start_bound_sec": round(self.start_bound, 4),
            "end_bound_sec": round(self.end_bound, 4),
            "kept_intervals": [asdict(k) for k in self.kept],
            "cuts": self.cuts,
            "pauses": self.pauses,
            "disfluencies": self.disfluencies,
            "stats": {
                "interior_cuts": len(self.cuts),
                "removed_sec": round(removed, 3),
                "pauses_classified": len(self.pauses),
                "protected_pauses": len(prot),
                "protected_kept_sec": round(sum(p["kept_sec"] for p in prot), 3),
                "disfluencies_removed": len(self.disfluencies),
            },
        }


# ---------------------------------------------------------------------------
# Speech evidence: everything a cut must never land inside
# ---------------------------------------------------------------------------
# A neural segment's own start/end are evidence only close to its timed words. faster-whisper's VAD
# mode transcribes the speech chunks joined together and maps timestamps back, so one segment can
# straddle the dead air between two chunks while its words stay accurate; trusting the segment span
# kept every such gap (and defeated the setup-delay trim) in a 10-minute neural-ASR production.
WORD_GAP_SPLIT_SEC = 1.0
SEGMENT_EDGE_SLACK_SEC = 0.3


def _segment_spans_from_words(seg: Any, words: list[dict[str, Any]]) -> list[tuple[float, float]]:
    ws = sorted(words, key=lambda w: w["start_sec"])
    spans: list[list[float]] = [[ws[0]["start_sec"], ws[0]["end_sec"]]]
    for w in ws[1:]:
        if w["start_sec"] - spans[-1][1] > WORD_GAP_SPLIT_SEC:
            spans.append([w["start_sec"], w["end_sec"]])
        else:
            spans[-1][1] = max(spans[-1][1], w["end_sec"])
    spans[0][0] = max(float(seg.start_sec), spans[0][0] - SEGMENT_EDGE_SLACK_SEC) if seg.start_sec < spans[0][0] else spans[0][0]
    spans[-1][1] = min(float(seg.end_sec), spans[-1][1] + SEGMENT_EDGE_SLACK_SEC) if seg.end_sec > spans[-1][1] else spans[-1][1]
    return [(float(a), float(b)) for a, b in spans]


def speech_evidence(state: ProjectAnalysisState) -> list[tuple[float, float, str]]:
    """Sorted, merged regions of speech or reaction audio: segments, timed words, activity."""
    regs: list[tuple[float, float, str]] = []
    for s in state.transcript_segments:
        words = [w for w in et.segment_words(s) if w["end_sec"] > w["start_sec"]]
        if words:
            regs.extend((a, b, "segment") for a, b in _segment_spans_from_words(s, words))
        elif s.end_sec > s.start_sec:
            regs.append((float(s.start_sec), float(s.end_sec), "segment"))
        for w in words:
            regs.append((w["start_sec"], w["end_sec"], "word"))
    for ev in state.metadata.get("nonspeech_activity", []) or []:
        if float(ev.get("end_sec", 0)) > float(ev.get("start_sec", 0)):
            regs.append((float(ev["start_sec"]), float(ev["end_sec"]), "activity"))
    regs.sort()
    merged: list[list[Any]] = []
    for s, e, k in regs:
        if merged and s <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], e)
            if k != "segment":
                merged[-1][2] = merged[-1][2] if merged[-1][2] == "activity" else k
        else:
            merged.append([s, e, k])
    return [(float(a), float(b), str(c)) for a, b, c in merged]


def _silent_subgaps(gap: tuple[float, float], evidence: list[tuple[float, float, str]]) -> list[tuple[float, float]]:
    s0, e0 = gap
    out: list[tuple[float, float]] = []
    cur = s0
    for es, ee, _ in evidence:
        if ee <= cur or es >= e0:
            continue
        if es > cur:
            out.append((cur, es))
        cur = max(cur, ee)
    if e0 > cur:
        out.append((cur, e0))
    return [g for g in out if g[1] - g[0] > 1e-3]


def _moments(state: ProjectAnalysisState) -> list[dict[str, Any]]:
    ctx = state.metadata.get("editorial_context") or {}
    if ctx.get("moments") is not None:
        return list(ctx["moments"])
    from bangonpro.ai_engine.department.editorial_analysis import detect_moments

    return detect_moments(state)


# ---------------------------------------------------------------------------
# Pause classification
# ---------------------------------------------------------------------------
class SegmentIndex:
    """Bisect index over transcript segments for O(log n) neighbour lookups (built per call)."""

    def __init__(self, segments: list[Any]) -> None:
        self.by_end = sorted(segments, key=lambda s: s.end_sec)
        self.by_start = sorted(segments, key=lambda s: s.start_sec)
        self.ends = [s.end_sec for s in self.by_end]
        self.starts = [s.start_sec for s in self.by_start]

    def neighbours(self, s0: float, e0: float) -> tuple[Any, Any]:
        i = bisect.bisect_right(self.ends, s0 + 0.05) - 1
        j = bisect.bisect_left(self.starts, e0 - 0.05)
        return (self.by_end[i] if i >= 0 else None), (self.by_start[j] if j < len(self.by_start) else None)


def classify_gap(
    gap: tuple[float, float],
    state: ProjectAnalysisState,
    moments: list[dict[str, Any]],
    rules: dict[str, Any],
    index: SegmentIndex | None = None,
) -> tuple[str, list[str]]:
    """Return ``(kind, reasons)`` for one silent gap between two pieces of speech evidence."""
    s0, e0 = gap
    prev, nxt = (index or SegmentIndex(state.transcript_segments)).neighbours(s0, e0)
    reasons: list[str] = []
    reactions = [m for m in moments if m["type"] in REACTION_MOMENTS]
    if rules.get("protect_reaction_pauses", True):
        for m in reactions:
            if m["start_sec"] - 0.35 <= e0 and m["end_sec"] + 0.35 >= s0:
                reasons.append(f"next to {m['type']} at {m['start_sec']:.1f}s")
                return "reaction_beat", reasons
        for ev in state.metadata.get("nonspeech_activity", []) or []:
            if float(ev["start_sec"]) - 0.35 <= e0 and float(ev["end_sec"]) + 0.35 >= s0:
                reasons.append(f"non-speech {ev.get('kind_hint', 'activity')} on the mics")
                return "reaction_beat", reasons
    if rules.get("protect_question_pauses", True) and prev is not None and prev.is_neural_inference:
        if et.is_question_text(prev.text):
            reasons.append(f"after question by {prev.speaker_id}: '{et.last_sentence(prev.text)[-60:]}'")
            return "after_question", reasons
    if rules.get("protect_punchline_pauses", True) and nxt is not None and (nxt.end_sec - nxt.start_sec) <= 6.0:
        for m in reactions:
            if nxt.end_sec - 0.3 <= m["start_sec"] <= nxt.end_sec + 1.2:
                reasons.append(f"beat before a line that gets a {m['type']}")
                return "before_punchline", reasons
    if prev is not None and nxt is not None and prev.speaker_id != nxt.speaker_id:
        reasons.append(f"turn change {prev.speaker_id} -> {nxt.speaker_id}")
        return "turn_gap", reasons
    if prev is not None and nxt is not None and prev.speaker_id == nxt.speaker_id and (gap[1] - gap[0]) <= TURN_BEAT_MAX_SEC:
        # V1.3: a short pause inside one speaker's turn is a thinking beat, not dead air. Without
        # words it cannot be told apart from hesitation, so it keeps the format's beat length.
        reasons.append("beat inside one speaker's turn")
        return "turn_beat", reasons
    reasons.append("same-speaker pause" if prev is not None and nxt is not None else "pause")
    return "dead_air", reasons


def _target_keep(kind: str, rules: dict[str, Any], dur: float) -> float:
    if rules["pause_policy"] == "keep":
        return dur
    if rules["pause_policy"] == "remove":
        return min(dur, 2.0 * float(rules.get("keep_padding_sec", 0.2)))
    if kind in ("after_question", "before_punchline"):
        return min(dur, float(rules["protected_pause_max_sec"]))
    if kind == "reaction_beat":
        return min(dur, max(float(rules["protected_pause_max_sec"]), float(rules["natural_pause_sec"])))
    if kind == "turn_gap":
        return min(dur, float(rules["turn_pause_sec"]))
    if kind == "turn_beat":
        return min(dur, max(float(rules["natural_pause_sec"]), float(rules.get("beat_pause_sec", rules["natural_pause_sec"]))))
    return min(dur, float(rules["natural_pause_sec"]))


def pause_inventory(state: ProjectAnalysisState, *, moments: list[dict[str, Any]] | None = None, rules: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every interior pause (silent sub-gap) with its editorial kind and target kept length."""
    rules = rules or resolve_pacing_rules(state)
    moments = moments if moments is not None else _moments(state)
    evidence = speech_evidence(state)
    index = SegmentIndex(state.transcript_segments)
    out: list[dict[str, Any]] = []
    for iv in state.silence_intervals:
        if iv.label != "silence":
            continue
        for g in _silent_subgaps((iv.start_sec, iv.end_sec), evidence):
            if g[1] - g[0] < 0.15:
                continue
            kind, reasons = classify_gap(g, state, moments, rules, index)
            out.append(
                {
                    "start_sec": round(g[0], 4),
                    "end_sec": round(g[1], 4),
                    "kind": kind,
                    "protected": kind in PROTECTED_KINDS,
                    "original_sec": round(g[1] - g[0], 3),
                    "target_keep_sec": round(_target_keep(kind, {**rules, "pause_policy": "compress"}, g[1] - g[0]), 3),
                    "reasons": reasons,
                }
            )
    return out


# ---------------------------------------------------------------------------
# Disfluencies (word level; needs genuine ASR word timestamps)
# ---------------------------------------------------------------------------
def find_disfluencies(state: ProjectAnalysisState, rules: dict[str, Any]) -> list[dict[str, Any]]:
    if not any(rules.get(k) for k in ("remove_false_starts", "remove_stutters", "remove_repeated_phrases", "remove_fillers")):
        return []
    found: list[dict[str, Any]] = []
    for seg in et.spoken_segments(state.transcript_segments):
        if not et.words_have_asr_timing(seg):
            continue
        words = et.segment_words(seg)
        if len(words) < 2:
            continue
        norm = [et.norm_token(w["word"]) for w in words]
        remove: dict[int, str] = {}
        n = len(words)
        i = 0
        while i < n:
            # 1. Truncated word: partial-word stutter ("th- the") or a false start ("we were- we went")
            if rules.get("remove_false_starts", True) and et.is_truncated(words[i]["word"]) and i + 1 < n:
                frag = norm[i]
                if frag and norm[i + 1].startswith(frag) and len(frag) <= 6:
                    remove[i] = "partial_word"
                    i += 1
                    continue
                c = i
                while c > 0 and not et.ends_sentence(words[c - 1]["word"]) and words[c]["start_sec"] - words[c - 1]["end_sec"] < 0.3 and i - c < 6:
                    c -= 1
                if (i - c + 1) <= 6 and (norm[i + 1] == norm[c] or (i - c + 1) <= 3):
                    for k in range(c, i + 1):
                        remove[k] = "false_start"
                    i += 1
                    continue
            # 2. Immediately repeated phrase / stutter: remove the first copy
            matched = False
            for size in (4, 3, 2, 1):
                if i + 2 * size > n or any(k in remove for k in range(i, i + 2 * size)):
                    continue
                a, b = norm[i:i + size], norm[i + size:i + 2 * size]
                if a != b or not all(a):
                    continue
                gap = words[i + size]["start_sec"] - words[i + size - 1]["end_sec"]
                if gap > 1.0 or any(et.ends_sentence(words[k]["word"]) for k in range(i, i + size)):
                    continue
                if size == 1:
                    if not rules.get("remove_stutters", True) or a[0] not in _STUTTER_PRONE or a[0] in _EMPHATIC_OK:
                        continue
                    kind = "stutter"
                else:
                    if not rules.get("remove_repeated_phrases", True) or all(t in _EMPHATIC_OK for t in a):
                        continue
                    kind = "repeated_phrase"
                for k in range(i, i + size):
                    remove[k] = kind
                i += size
                matched = True
                break
            if matched:
                continue
            if rules.get("remove_fillers") and norm[i] in et.FILLER_TOKENS:
                remove[i] = "filler"
            i += 1
        if len(remove) >= n:  # never empty a whole segment
            continue
        # Group consecutive removed indices into word-boundary cut spans
        idxs = sorted(remove)
        runs: list[list[int]] = []
        for k in idxs:
            if runs and k == runs[-1][-1] + 1 and remove[k] == remove[runs[-1][-1]]:
                runs[-1].append(k)
            else:
                runs.append([k])
        for run in runs:
            first, last = run[0], run[-1]
            prev_end = words[first - 1]["end_sec"] if first > 0 else None
            next_start = words[last + 1]["start_sec"] if last + 1 < n else None
            if next_start is None:
                continue  # trailing disfluency: the pause logic handles what follows
            start = words[first]["start_sec"]
            start = max(prev_end, start - 0.03) if prev_end is not None else start - 0.03
            end = next_start - min(0.03, max(0.0, next_start - words[last]["end_sec"]) / 2.0)
            if end - start < 0.12:
                continue
            found.append(
                {
                    "start_sec": round(start, 4),
                    "end_sec": round(end, 4),
                    "kind": remove[first],
                    "text": " ".join(words[k]["word"] for k in run),
                    "segment_id": seg.segment_id,
                    "speaker_id": seg.speaker_id,
                    "word_count": len(run),
                }
            )
    return found


# ---------------------------------------------------------------------------
# Kept intervals
# ---------------------------------------------------------------------------
def compute_pacing_plan(state: ProjectAnalysisState, common_duration: float, rules: dict[str, Any] | None = None) -> PacingPlan:
    """Kept master-clock intervals after setup-delay trim, pause compression and disfluency removal."""
    rules = dict(rules or resolve_pacing_rules(state))
    er = state.profile.get("editing_rules", {}) or {}
    keep_pad = float(rules.get("keep_padding_sec", er.get("keep_padding_sec", 0.2)))
    guard = float(rules["edge_guard_sec"])
    trim_setup = bool(er.get("trim_setup_delay", True)) and state.intent.remove_setup_delay
    remove_silence = bool(state.intent.remove_silence)
    excise_interruptions = bool(er.get("excise_interruption_gaps", True)) and (state.intent.remove_silence or state.intent.build_rough_cut)
    evidence = speech_evidence(state)

    range_start, range_end = 0.0, float(common_duration)
    if state.intent.time_range_sec is not None:
        r0, r1 = state.intent.time_range_sec
        range_start = max(0.0, min(common_duration, float(r0)))
        range_end = max(range_start, min(common_duration, float(r1)))
    # Trim bounds follow words/segments only: crew noise in a setup delay must not defeat the trim
    ev_in = [e for e in evidence if e[1] > range_start and e[0] < range_end and e[2] != "activity"]

    start_bound = range_start
    if trim_setup and state.setup_delay_sec >= 0.40:
        start_bound = max(range_start, state.setup_delay_sec - keep_pad)
        if ev_in:
            start_bound = min(start_bound, max(range_start, ev_in[0][0] - max(guard, keep_pad * 0.5)))
    end_bound = range_end
    if remove_silence:
        for iv in state.silence_intervals:
            if iv.label == "trailing_silence":
                cand = min(range_end, iv.start_sec + keep_pad)
                if ev_in:
                    cand = max(cand, min(range_end, ev_in[-1][1] + max(guard, keep_pad * 0.5)))
                if cand > start_bound + 0.5:
                    end_bound = cand
    plan = PacingPlan(kept=[], rules=rules, start_bound=start_bound, end_bound=end_bound)
    if end_bound <= start_bound + 0.25:
        plan.kept = [TimeInterval(round(range_start, 4), round(max(range_start + 0.5, range_end), 4), "kept_full")]
        return plan

    moments = _moments(state)
    index = SegmentIndex(state.transcript_segments)
    cuts: list[dict[str, Any]] = []
    if remove_silence:
        for iv in state.silence_intervals:
            if iv.label != "silence":
                continue
            lo, hi = max(start_bound, iv.start_sec), min(end_bound, iv.end_sec)
            if hi - lo <= 0.15:
                continue
            for g in _silent_subgaps((lo, hi), evidence):
                dur = g[1] - g[0]
                kind, reasons = classify_gap(g, state, moments, rules, index)
                keep = _target_keep(kind, rules, dur)
                if rules["pause_policy"] == "remove":
                    min_saving = 0.15
                else:
                    min_saving = float(rules["min_pause_saving_sec"])
                lead = max(guard, keep * 0.55)
                tail = max(guard, keep - lead, guard)
                cut_s, cut_e = g[0] + lead, g[1] - tail
                protected = kind in PROTECTED_KINDS and rules["pause_policy"] != "remove"
                decided_cut = (cut_e - cut_s) >= min_saving and rules["pause_policy"] != "keep"
                plan.pauses.append(
                    {
                        "start_sec": round(g[0], 4),
                        "end_sec": round(g[1], 4),
                        "kind": kind,
                        "protected": protected,
                        "original_sec": round(dur, 3),
                        "kept_sec": round(dur - (cut_e - cut_s) if decided_cut else dur, 3),
                        "reasons": reasons,
                    }
                )
                if decided_cut:
                    cuts.append({"start_sec": round(cut_s, 4), "end_sec": round(cut_e, 4), "kind": f"pause:{kind}", "reason": "; ".join(reasons)})
    if excise_interruptions:
        for iv in state.interruption_intervals or []:
            lo = max(start_bound, iv.start_sec + keep_pad)
            hi = min(end_bound, iv.end_sec - keep_pad)
            for g in _silent_subgaps((lo, hi), evidence):
                if g[1] - g[0] >= 0.25:
                    cuts.append({"start_sec": round(g[0], 4), "end_sec": round(g[1], 4), "kind": "interruption_gap", "reason": f"recording interruption {iv.duration_sec:.1f}s"})

    disfl = find_disfluencies(state, rules) if (state.intent.build_rough_cut or remove_silence) else []
    disfl = [d for d in disfl if d["start_sec"] >= start_bound and d["end_sec"] <= end_bound]
    for _attempt in range(3):
        all_cuts = sorted(cuts + [{"start_sec": d["start_sec"], "end_sec": d["end_sec"], "kind": f"disfluency:{d['kind']}", "reason": f"removed {d['kind'].replace('_', ' ')} '{d['text']}'"} for d in disfl], key=lambda c: c["start_sec"])
        kept, merged = _kept_from_cuts(start_bound, end_bound, all_cuts)
        # A sliver of silence between two cuts is a flash of room tone: drop it
        if len(kept) > 1:
            slivers = [k for k in kept if k.duration_sec < 0.3 and not any(e[0] < k.end_sec and e[1] > k.start_sec for e in evidence)]
            if slivers and len(slivers) < len(kept):
                extra = [{"start_sec": k.start_sec - 0.001, "end_sec": k.end_sec + 0.001, "kind": "sliver", "reason": "silent sliver between cuts"} for k in slivers]
                kept, merged = _kept_from_cuts(start_bound, end_bound, all_cuts + extra)
        # A disfluency cut must not strand a sliver of speech (flash frame); cancel it instead
        bad = set()
        for k in kept:
            if k.duration_sec < 0.3 and any(e[0] < k.end_sec and e[1] > k.start_sec for e in evidence):
                for d in disfl:
                    if abs(d["end_sec"] - k.start_sec) < 1e-3 or abs(d["start_sec"] - k.end_sec) < 1e-3:
                        bad.add(id(d))
        if not bad:
            break
        disfl = [d for d in disfl if id(d) not in bad]
    plan.kept = kept
    plan.cuts = merged
    plan.disfluencies = disfl
    return plan


def _kept_from_cuts(start: float, end: float, cuts: list[dict[str, Any]]) -> tuple[list[TimeInterval], list[dict[str, Any]]]:
    merged: list[dict[str, Any]] = []
    for c in sorted(cuts, key=lambda c: c["start_sec"]):
        cs, ce = max(start, c["start_sec"]), min(end, c["end_sec"])
        if ce - cs < 0.05:
            continue
        if merged and cs <= merged[-1]["end_sec"] + 0.05:
            merged[-1]["end_sec"] = round(max(merged[-1]["end_sec"], ce), 4)
            merged[-1]["kind"] += "+" + c["kind"]
            merged[-1]["reason"] += "; " + c["reason"]
        else:
            merged.append({**c, "start_sec": round(cs, 4), "end_sec": round(ce, 4)})
    kept: list[TimeInterval] = []
    cursor = start
    for c in merged:
        if c["start_sec"] > cursor + 0.10:
            kept.append(TimeInterval(round(cursor, 4), round(c["start_sec"], 4), f"kept_{len(kept) + 1}"))
        cursor = max(cursor, c["end_sec"])
    if end > cursor + 0.10:
        kept.append(TimeInterval(round(cursor, 4), round(end, 4), f"kept_{len(kept) + 1}"))
    if not kept:
        kept.append(TimeInterval(round(start, 4), round(end, 4), "kept_1"))
    for c in merged:
        c["saved_sec"] = round(c["end_sec"] - c["start_sec"], 4)
    return kept, merged


# ---------------------------------------------------------------------------
# Objective pacing metrics (used by the Rough Cut assessor, QC and the evaluation)
# ---------------------------------------------------------------------------
# Reference values are fixed per format (NOT read from the editable rules) so a correction
# cannot move the goalposts it is measured against.
REF_PROTECTED_FLOOR: dict[str, float] = {"daily_news": 0.5, "podcast": 0.8, "interview": 0.9, "gameshow": 1.2, "sports_discussion": 0.6, "social": 0.4, "youtube": 0.45}
REF_NATURAL_CAP: dict[str, float] = {"daily_news": 0.6, "podcast": 1.0, "interview": 1.1, "gameshow": 0.8, "sports_discussion": 0.8, "social": 0.45, "youtube": 0.55}
REF_MAX_EDITS_PER_MIN: dict[str, float] = {"daily_news": 16.0, "podcast": 8.0, "interview": 6.0, "gameshow": 14.0, "sports_discussion": 12.0, "social": 20.0, "youtube": 18.0}
_REF_RULES: dict[str, Any] = {"protect_question_pauses": True, "protect_punchline_pauses": True, "protect_reaction_pauses": True}


def _overlap(a: tuple[float, float], kept: list[TimeInterval]) -> float:
    return sum(max(0.0, min(a[1], k.end_sec) - max(a[0], k.start_sec)) for k in kept)


def interior_cut_points(kept: list[TimeInterval]) -> list[tuple[float, float]]:
    """(out_point, in_point) pairs on the master clock for every interior splice."""
    return [(kept[i].end_sec, kept[i + 1].start_sec) for i in range(len(kept) - 1)]


def pacing_metrics(state: ProjectAnalysisState, kept: list[TimeInterval], *, declared_disfluencies: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Measure pause handling and cut safety of a kept-interval list against fixed references."""
    fmt = format_key(state.profile)
    floor = REF_PROTECTED_FLOOR[fmt]
    cap = REF_NATURAL_CAP[fmt]
    evidence = speech_evidence(state)
    moments = _moments(state)
    index = SegmentIndex(state.transcript_segments)
    protected, unprot = [], []
    for iv in state.silence_intervals:
        if iv.label != "silence":
            continue
        for g in _silent_subgaps((iv.start_sec, iv.end_sec), evidence):
            if g[1] - g[0] < 0.15:
                continue
            kind, _ = classify_gap(g, state, moments, _REF_RULES, index)
            if kind == "turn_beat":
                continue  # V1.3: kept at a bounded beat length (< 1 s); neither dead air nor protected
            row = {"start_sec": round(g[0], 3), "end_sec": round(g[1], 3), "kind": kind, "original_sec": round(g[1] - g[0], 3), "kept_sec": round(_overlap(g, kept), 3)}
            (protected if kind in PROTECTED_KINDS else unprot).append(row)
    retained = [p for p in protected if p["kept_sec"] >= min(p["original_sec"], floor) - 0.05]
    # Dead air counts only where trimming would save at least 0.3 s (worth an editor's edit point)
    worth = [p for p in unprot if p["original_sec"] - cap >= 0.3]
    excess_possible = sum(p["original_sec"] - cap for p in worth)
    excess = sum(max(0.0, p["kept_sec"] - cap) for p in worth)
    dead_air_kept = [p for p in worth if p["kept_sec"] - cap >= 0.3]
    # Cut safety
    words = [w for s in state.transcript_segments for w in et.segment_words(s)]
    declared = [(d["start_sec"], d["end_sec"]) for d in (declared_disfluencies or [])]
    inside_word, inside_segment = [], []
    for out_pt, in_pt in interior_cut_points(kept):
        for t in (out_pt, in_pt):
            w = et.inside_word(t, words)
            if w is not None:
                inside_word.append({"at_sec": round(t, 3), "word": w["word"], "word_start": round(w["start_sec"], 3)})
                continue
            for s in state.transcript_segments:
                if s.start_sec + 0.08 < t < s.end_sec - 0.08 and not et.segment_words(s):
                    inside_segment.append({"at_sec": round(t, 3), "segment_id": s.segment_id})
                    break
    lost = []
    for w in words:
        mid = (w["start_sec"] + w["end_sec"]) / 2.0
        if any(a - 1e-3 <= mid <= b + 1e-3 for a, b in declared):
            continue
        if mid < kept[0].start_sec - 1e-3 or mid > kept[-1].end_sec + 1e-3:
            continue  # setup/trailing trims are bounded separately
        if not any(k.start_sec - 1e-3 <= mid <= k.end_sec + 1e-3 for k in kept):
            lost.append({"word": w["word"], "at_sec": round(mid, 3), "word_start": round(w["start_sec"], 3)})
    edited = sum(k.duration_sec for k in kept)
    n_cuts = max(0, len(kept) - 1)
    return {
        "format": fmt,
        "protected_pauses": len(protected),
        "protected_retained": len(retained),
        "protected_retention": round(len(retained) / len(protected), 4) if protected else 1.0,
        "protected_detail": protected,
        "unprotected_pauses": len(unprot),
        "dead_air_excess_possible_sec": round(excess_possible, 3),
        "dead_air_excess_kept_sec": round(excess, 3),
        "dead_air_removed_ratio": round(1.0 - excess / excess_possible, 4) if excess_possible > 1e-6 else 1.0,
        "dead_air_kept": dead_air_kept,
        "protected_lost": [p for p in protected if p not in retained],
        "flash_fragments": [{"at_sec": round(k.start_sec, 3), "duration_sec": round(k.duration_sec, 3)} for k in kept if k.duration_sec < 0.3 and any(e[0] < k.end_sec and e[1] > k.start_sec for e in evidence)],
        "cuts_inside_words": inside_word,
        "cuts_inside_segments": inside_segment,
        "words_lost": lost,
        "interior_cuts": n_cuts,
        "edited_sec": round(edited, 3),
        "edits_per_min": round(n_cuts / (edited / 60.0), 3) if edited > 1 else 0.0,
        "ref_max_edits_per_min": REF_MAX_EDITS_PER_MIN[fmt],
    }


__all__ = [
    "FORMAT_PACING_DEFAULTS",
    "PROTECTED_KINDS",
    "PacingPlan",
    "SegmentIndex",
    "classify_gap",
    "compute_pacing_plan",
    "find_disfluencies",
    "format_key",
    "interior_cut_points",
    "pacing_metrics",
    "pause_inventory",
    "resolve_pacing_rules",
    "speech_evidence",
]
