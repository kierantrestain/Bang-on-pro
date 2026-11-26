"""Captions (V1.2.2): word-level cues on the edited timeline, terminology, readability.

* cues are built from **kept words** (each word exactly once, mapped through the shots to
  the edited timeline), split on speaker change, sentence and clause boundaries and the
  profile's readability limits (characters per line and per second, minimum/maximum
  duration, minimum gap); single-word orphans are merged;
* text repair: custom terminology and proper nouns (``caption_style.terminology`` plus
  speaker names), ``i`` -> ``I``, sentence-initial capitals, terminal punctuation at the
  end of a turn, stray spacing and duplicate punctuation;
* speaker attribution on speaker change (``caption_style.speaker_change_marker``:
  ``chevron`` ">> ", ``dash`` "- ", ``name`` "SARAH: " or ``none``);
* optional sound tags ([LAUGHTER], [APPLAUSE]) for typed moments in kept content;
* ``retime_captions_to_shots`` re-times captions to any shot list (the rough cut, or one
  read back from an edited Kdenlive project with ``shots_from_kdenlive``), and
  ``caption_transcript_agreement`` re-checks continuity after timeline edits.

Captions are only ever built from genuine spoken text (neural ASR); VAD placeholders
are never turned into words.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from bangonpro.ai_engine.department import editorial_text as et
from bangonpro.ai_engine.state import CaptionCue

_COMMON_WORD_NAMES = frozenset(
    "will mark bill rose grace hope joy may june april august frank art pat sue rob jack bob dawn faith summer amber ruby holly ivy daisy "
    "penny sky river storm rich chase hunter drew bo guy max sunny happy cash king major victor earl dean chance page lane bay "
    "gene ray rod sandy dot jean".split()
)
_I_FORMS = {"i": "I", "i'm": "I'm", "i've": "I've", "i'll": "I'll", "i'd": "I'd", "i’m": "I’m", "i’ve": "I’ve", "i’ll": "I’ll", "i’d": "I’d"}
_NO_LINE_END = frozenset({"a", "an", "the", "to", "of", "and", "in", "on", "at", "for", "with", "but", "or", "my", "your", "our", "his", "her", "their", "its", "this", "that", "very"})
_SOUND_TAG = {"laughter": "[LAUGHTER]", "applause": "[APPLAUSE]"}


def caption_rules(profile: dict[str, Any]) -> dict[str, Any]:
    from bangonpro.show_profiles.loader import get_effective_profile_settings

    style = dict(get_effective_profile_settings(profile)["caption_style"])
    fps = float(profile["video_spec"]["fps_num"]) / float(profile["video_spec"]["fps_den"]) if profile.get("video_spec") else 25.0
    return {
        "max_chars_per_line": int(style.get("max_chars_per_line", 42)),
        "max_lines": int(style.get("max_lines", 2)),
        "max_chars_per_sec": float(style.get("max_chars_per_sec", 20.0)),
        "min_duration_sec": float(style.get("min_duration_sec", 1.0)),
        "max_duration_sec": float(style.get("max_duration_sec", 6.0)),
        "min_gap_sec": float(style.get("min_gap_sec", round(2.0 / fps, 3))),
        "speaker_change_marker": str(style.get("speaker_change_marker", "chevron")),
        "sound_tags": bool(style.get("sound_tags", True)),
        "terminology": style.get("terminology") or [],
        "uppercase": bool(style.get("uppercase", False)),
    }


# ---------------------------------------------------------------------------
# Terminology and text repair
# ---------------------------------------------------------------------------
def _term_pattern(canonical: str) -> re.Pattern[str]:
    parts = [re.escape(p) for p in re.split(r"[\s-]+", canonical.strip()) if p]
    return re.compile(r"(?<![\w'])" + r"[\s-]*".join(parts) + r"(?![\w'])", re.IGNORECASE)


def terminology_rules(terminology: Any, names: Iterable[str] = ()) -> list[tuple[re.Pattern[str], str]]:
    """Compile ``caption_style.terminology`` plus speaker names into (pattern, replacement) rules.

    Accepted shapes: ``["Bang On Pro", ...]`` (canonical spellings), ``{"Kdenlive": ["k den live"]}``
    (canonical -> variants) or ``{"k den live": "Kdenlive"}`` (variant -> canonical).
    """
    rules: list[tuple[re.Pattern[str], str]] = []
    canon: list[str] = []
    if isinstance(terminology, dict):
        for k, v in terminology.items():
            if isinstance(v, (list, tuple)):
                canon.append(str(k))
                rules.extend((_term_pattern(str(var)), str(k)) for var in v if str(var).strip())
            elif isinstance(v, str) and v.strip():
                rules.append((_term_pattern(str(k)), v))
                canon.append(v)
    elif isinstance(terminology, (list, tuple)):
        canon.extend(str(t) for t in terminology if str(t).strip())
    for name in names:
        for part in [name, *name.split()]:
            if len(part) >= 3 and part.lower() not in _COMMON_WORD_NAMES and part.lower() not in et.STOPWORDS:
                canon.append(part)
    for c in sorted(set(canon), key=lambda x: -len(x)):
        rules.append((_term_pattern(c), c))
    return rules


def _edit1(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1 or a == b:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    return any(a == b[:i] + b[i + 1:] for i in range(len(b)))


def apply_terminology(text: str, rules: list[tuple[re.Pattern[str], str]], names: Iterable[str] = ()) -> str:
    out = text
    for pat, rep in rules:
        out = pat.sub(rep, out)
    long_names = [n for name in names for n in name.split() if len(n) >= 6]
    if long_names:
        def fix(m: re.Match[str]) -> str:
            tok = m.group(0)
            for n in long_names:
                if tok[0].isupper() and _edit1(tok.lower(), n.lower()):
                    return n
            return tok

        out = re.sub(r"\b[A-Z][a-z]{4,}\b", fix, out)
    return out


def repair_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Casing and punctuation repair over a time-ordered word stream (in place copy)."""
    out = [dict(w) for w in words]
    for i, w in enumerate(out):
        txt = re.sub(r"\s+", " ", str(w["word"]).strip())
        low = txt.lower().strip(".,!?;:")
        if low in _I_FORMS:
            txt = txt.lower().replace(low, _I_FORMS[low], 1)
        prev = out[i - 1] if i else None
        nxt = out[i + 1] if i + 1 < len(out) else None
        starts = prev is None or prev["speaker_id"] != w["speaker_id"] or et.ends_sentence(prev["word"]) or (w["start"] - prev["end"]) >= 1.5
        if starts:
            k = next((j for j, ch in enumerate(txt) if ch.isalpha()), None)
            if k is not None:
                txt = txt[:k] + txt[k].upper() + txt[k + 1:]
        end_turn = nxt is None or nxt["speaker_id"] != w["speaker_id"] or (nxt["start"] - w["end"]) >= 1.5
        if end_turn and not et.ends_sentence(txt) and not et.is_truncated(txt):
            j = i
            while j > 0 and not et.ends_sentence(out[j - 1]["word"]) and out[j - 1]["speaker_id"] == w["speaker_id"]:
                j -= 1
            sentence = " ".join(str(x["word"]) for x in out[j:i]) + " " + txt
            txt = txt.rstrip(",;:") + ("?" if et.is_question_text(sentence) else ".")
        txt = re.sub(r"([,!?;:])\1+", r"\1", txt)
        txt = re.sub(r"(?<!\.)\.\.(?!\.)", ".", txt)
        w["word"] = txt
    return out


def balanced_wrap(text: str, max_chars: int, max_lines: int = 2) -> list[str]:
    """Split into <= max_lines lines of <= max_chars, as even as possible, never ending a line on an article."""
    text = " ".join(text.split())
    if len(text) <= max_chars or max_chars <= 0:
        return [text]
    toks = text.split(" ")
    if max_lines >= 2:
        best, best_cost = None, None
        for k in range(1, len(toks)):
            a, b = " ".join(toks[:k]), " ".join(toks[k:])
            if len(a) > max_chars or len(b) > max_chars:
                continue
            cost = abs(len(a) - len(b)) + (25 if et.norm_token(toks[k - 1]) in _NO_LINE_END else 0) - (8 if re.search(r"[,.;:!?]$", toks[k - 1]) else 0)
            if best_cost is None or cost < best_cost:
                best, best_cost = [a, b], cost
        if best:
            return best
    import textwrap

    return textwrap.wrap(text, width=max_chars)


# ---------------------------------------------------------------------------
# Edited-timeline words and cue building
# ---------------------------------------------------------------------------
def _shot_rows(shots: Iterable[Any]) -> list[dict[str, float]]:
    rows = []
    for s in shots:
        g = s.get if isinstance(s, dict) else (lambda k, _s=s: getattr(_s, k))
        rows.append({"m_in": float(g("master_sync_in_sec")), "m_out": float(g("master_sync_out_sec")), "t_in": float(g("timeline_in_sec")), "t_out": float(g("timeline_out_sec"))})
    return sorted(rows, key=lambda r: r["t_in"])


def edited_words(segments: Iterable[Any], shots: Iterable[Any] | None, *, exclude: list[tuple[float, float]] | None = None) -> list[dict[str, Any]]:
    """Spoken words mapped onto the edited timeline through the shots (master clock if no shots)."""
    rows = _shot_rows(shots) if shots is not None else None
    out: list[dict[str, Any]] = []
    for seg in et.spoken_segments(segments):
        for w in et.segment_words(seg):
            mid = (w["start_sec"] + w["end_sec"]) / 2.0
            if exclude and any(a - 1e-3 <= mid <= b + 1e-3 for a, b in exclude):
                continue
            if rows is None:
                out.append({"word": w["word"], "start": w["start_sec"], "end": w["end_sec"], "speaker_id": seg.speaker_id, "master": mid})
                continue
            for r in rows:
                if r["m_in"] - 1e-3 <= mid <= r["m_out"] + 1e-3:
                    s = r["t_in"] + max(0.0, w["start_sec"] - r["m_in"])
                    e = r["t_in"] + min(r["m_out"], w["end_sec"]) - r["m_in"]
                    out.append({"word": w["word"], "start": round(s, 4), "end": round(max(s + 0.02, min(e, r["t_out"])), 4), "speaker_id": seg.speaker_id, "master": mid})
                    break
    out.sort(key=lambda x: x["start"])
    return out


def _marker(style: str, name: str) -> str:
    if style == "chevron":
        return ">> "
    if style == "dash":
        return "- "
    if style == "name":
        return f"{name.upper()}: "
    return ""


def build_cues(words: list[dict[str, Any]], rules: dict[str, Any], *, names: dict[str, str] | None = None, term_rules: list[tuple[re.Pattern[str], str]] | None = None, sound_events: list[dict[str, Any]] | None = None, timeline_end: float | None = None) -> list[CaptionCue]:
    names = names or {}
    max_chars = rules["max_chars_per_line"] * max(1, rules["max_lines"])
    words = repair_words(words)
    groups: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []

    def text_of(ws: list[dict[str, Any]]) -> str:
        return " ".join(w["word"] for w in ws)

    for w in words:
        if cur:
            gap = w["start"] - cur[-1]["end"]
            too_long = len(text_of(cur + [w])) > max_chars - 4 or (w["end"] - cur[0]["start"]) > rules["max_duration_sec"]
            if w["speaker_id"] != cur[-1]["speaker_id"] or gap > 1.0 or too_long:
                groups.append(cur)
                cur = []
        cur.append(w)
        n = len(text_of(cur))
        if et.ends_sentence(w["word"]) and n >= rules["max_chars_per_line"] * 0.6:
            groups.append(cur)
            cur = []
        elif w["word"].endswith((",", ";", ":")) and n >= rules["max_chars_per_line"] * 1.2:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    # Orphans: a lone word joins its neighbour from the same speaker when it fits
    merged: list[list[dict[str, Any]]] = []
    for g in groups:
        if merged and (len(g) == 1 or len(merged[-1]) == 1) and g[0]["speaker_id"] == merged[-1][-1]["speaker_id"] and len(text_of(merged[-1] + g)) <= max_chars and g[0]["start"] - merged[-1][-1]["end"] <= 1.0:
            merged[-1] = merged[-1] + g
        else:
            merged.append(g)
    cues: list[dict[str, Any]] = [{"start": g[0]["start"], "end": g[-1]["end"], "speaker_id": g[0]["speaker_id"], "text": text_of(g)} for g in merged]
    for ev in sound_events or []:
        tag = _SOUND_TAG.get(ev["type"])
        if tag and not any(c["start"] < ev["end"] and c["end"] > ev["start"] for c in cues):
            cues.append({"start": ev["start"], "end": ev["end"], "speaker_id": "SFX", "text": tag})
    cues.sort(key=lambda c: c["start"])
    # Timing: minimum duration and reading speed, without overlapping the next cue
    gap = rules["min_gap_sec"]
    for i, c in enumerate(cues):
        nxt_start = cues[i + 1]["start"] - gap if i + 1 < len(cues) else (timeline_end if timeline_end is not None else c["end"] + rules["max_duration_sec"])
        need = max(rules["min_duration_sec"], len(c["text"]) / max(1.0, rules["max_chars_per_sec"]))
        if c["end"] - c["start"] < need:
            c["end"] = round(min(max(c["end"], c["start"] + need), max(c["end"], nxt_start), c["start"] + rules["max_duration_sec"]), 4)
        if i and c["start"] < cues[i - 1]["end"] + gap:
            cues[i - 1]["end"] = round(max(cues[i - 1]["start"] + 0.2, c["start"] - gap), 4)
    out: list[CaptionCue] = []
    prev_spk = None
    for c in cues:
        text = c["text"]
        if term_rules:
            text = apply_terminology(text, term_rules, names.values())
        if c["speaker_id"] != "SFX" and prev_spk is not None and c["speaker_id"] != prev_spk:
            text = _marker(rules["speaker_change_marker"], names.get(c["speaker_id"], c["speaker_id"])) + text
        elif c["speaker_id"] != "SFX" and prev_spk is None and rules["speaker_change_marker"] == "name":
            text = _marker("name", names.get(c["speaker_id"], c["speaker_id"])) + text
        if c["speaker_id"] != "SFX":
            prev_spk = c["speaker_id"]
        out.append(CaptionCue(index=len(out) + 1, start_sec=round(c["start"], 3), end_sec=round(c["end"], 3), speaker_id=c["speaker_id"], text=text, confidence=0.9))
    return out


def _names(state: Any) -> dict[str, str]:
    sp = (state.metadata.get("editorial_context") or {}).get("speakers") or {}
    out = {k: v.get("name") or k for k, v in sp.items()}
    for s in state.transcript_segments:
        out.setdefault(s.speaker_id, s.speaker_name or s.speaker_id)
    return out


def _sound_events(state: Any, shots: Any) -> list[dict[str, Any]]:
    moments = (state.metadata.get("editorial_context") or {}).get("moments") or []
    rows = _shot_rows(shots) if shots else None
    out = []
    for m in moments:
        if m["type"] not in _SOUND_TAG or float(m.get("confidence", 0)) < 0.6 or m["end_sec"] - m["start_sec"] < 0.8:
            continue
        if rows is None:
            out.append({"type": m["type"], "start": m["start_sec"], "end": m["end_sec"]})
            continue
        for r in rows:
            if r["m_in"] <= m["start_sec"] < r["m_out"]:
                s = r["t_in"] + (m["start_sec"] - r["m_in"])
                out.append({"type": m["type"], "start": round(s, 4), "end": round(min(r["t_out"], s + (m["end_sec"] - m["start_sec"])), 4)})
                break
    return out


def retime_captions_to_shots(segments: Iterable[Any], shots: Iterable[Any] | None, *, profile: dict[str, Any], names: dict[str, str] | None = None, exclude: list[tuple[float, float]] | None = None, sound_events: list[dict[str, Any]] | None = None) -> list[CaptionCue]:
    """Pure: captions for any shot list (``EditShot``s or dicts with master/timeline in/out)."""
    rules = caption_rules(profile)
    shot_list = list(shots) if shots is not None else None
    words = edited_words(segments, shot_list, exclude=exclude)
    names = names or {}
    tl_end = max((r["t_out"] for r in _shot_rows(shot_list)), default=None) if shot_list else None
    return build_cues(words, rules, names=names, term_rules=terminology_rules(rules["terminology"], names.values()), sound_events=sound_events, timeline_end=tl_end)


def build_captions_v122(state: Any) -> list[CaptionCue]:
    exclude = [(d["start_sec"], d["end_sec"]) for d in ((state.metadata.get("rough_cut_decisions") or {}).get("disfluencies") or [])]
    shots = state.rough_cut_shots or None
    cues = retime_captions_to_shots(
        [s for s in state.transcript_segments if _speaker_ok(state, s.speaker_id)],
        shots,
        profile=state.profile,
        names=_names(state),
        exclude=exclude,
        sound_events=_sound_events(state, shots) if caption_rules(state.profile)["sound_tags"] else None,
    )
    tr = state.intent.time_range_sec
    if tr is not None and not shots:
        cues = [c for c in cues if c.end_sec > tr[0] and c.start_sec < tr[1]]
        for i, c in enumerate(cues, start=1):
            c.index = i
    return cues


def _speaker_ok(state: Any, spk: str) -> bool:
    targets = state.intent.target_speakers
    if not targets:
        return True
    low = spk.strip().lower()
    return any(t.strip().lower() in low or low in t.strip().lower() for t in targets)


# ---------------------------------------------------------------------------
# Readability and continuity
# ---------------------------------------------------------------------------
def caption_readability(captions: list[Any], profile: dict[str, Any]) -> dict[str, Any]:
    rules = caption_rules(profile)
    if not captions:
        return {"cues": 0}
    n = len(captions)
    cps_ok = lines_ok = dur_ok = 0
    orphans = overlaps = 0
    longest = 0.0
    for i, c in enumerate(captions):
        body = re.sub(r"^(>> |- |[A-Z][A-Z .'-]*: )", "", c.text)
        dur = max(0.01, c.end_sec - c.start_sec)
        longest = max(longest, dur)
        cps_ok += len(body) / dur <= rules["max_chars_per_sec"] + 1e-6
        lines = balanced_wrap(body, rules["max_chars_per_line"], rules["max_lines"])
        lines_ok += len(lines) <= rules["max_lines"] and all(len(x) <= rules["max_chars_per_line"] for x in lines)
        dur_ok += dur >= rules["min_duration_sec"] - 0.05
        orphans += len(body.split()) == 1 and not body.startswith("[")
        overlaps += bool(i and c.start_sec < captions[i - 1].end_sec - 0.01)
    return {
        "cues": n,
        "within_reading_speed": round(cps_ok / n, 4),
        "within_line_limits": round(lines_ok / n, 4),
        "meets_min_duration": round(dur_ok / n, 4),
        "single_word_orphans": orphans,
        "overlapping_cues": overlaps,
        "longest_cue_sec": round(longest, 3),
        "rules": {k: rules[k] for k in ("max_chars_per_line", "max_lines", "max_chars_per_sec", "min_duration_sec", "max_duration_sec")},
    }


def caption_transcript_agreement(state: Any) -> dict[str, Any]:
    """Re-check captions against the kept transcript words after timeline edits."""
    exclude = [(d["start_sec"], d["end_sec"]) for d in ((state.metadata.get("rough_cut_decisions") or {}).get("disfluencies") or [])]
    kept_words = edited_words(state.transcript_segments, state.rough_cut_shots or None, exclude=exclude)
    kept = Counter(et.norm_token(w["word"]) for w in kept_words)
    kept.pop("", None)
    cap_tokens: Counter[str] = Counter()
    for c in state.captions:
        body = re.sub(r"^(>> |- |[A-Z][A-Z .'-]*: )", "", c.text)
        if body.startswith("[") and body.endswith("]"):
            continue
        cap_tokens.update(t for t in et.tokens(body))
    matched = sum((kept & cap_tokens).values())
    removed_words = []
    if state.kept_master_intervals:
        for s in et.spoken_segments(state.transcript_segments):
            for w in et.segment_words(s):
                mid = (w["start_sec"] + w["end_sec"]) / 2.0
                if not any(k.start_sec - 1e-3 <= mid <= k.end_sec + 1e-3 for k in state.kept_master_intervals):
                    removed_words.append(et.norm_token(w["word"]))
    removed = Counter(removed_words)
    over_removed = []
    for c in state.captions:
        toks = Counter(et.tokens(re.sub(r"^(>> |- |[A-Z][A-Z .'-]*: )", "", c.text)))
        if not toks or not removed:
            continue
        near = Counter(et.norm_token(w["word"]) for w in kept_words if w["start"] < c.end_sec + 0.5 and w["end"] > c.start_sec - 0.5)
        unexplained = toks - near
        if sum(unexplained.values()) >= max(2, 0.5 * sum(toks.values())) and sum((unexplained & removed).values()) >= 0.5 * sum(unexplained.values()):
            over_removed.append({"index": c.index, "start_sec": c.start_sec})
    total_kept = sum(kept.values())
    total_cap = sum(cap_tokens.values())
    return {
        "kept_words": total_kept,
        "caption_words": total_cap,
        "recall": round(matched / total_kept, 4) if total_kept else 1.0,
        "precision": round(matched / total_cap, 4) if total_cap else 1.0,
        "cues_over_removed": over_removed,
    }


# ---------------------------------------------------------------------------
# Kdenlive program track -> shots (for re-timing after a human edit)
# ---------------------------------------------------------------------------
def shots_from_kdenlive(path: str | Path, offsets_sec: dict[str, float], *, resource_to_camera: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Read the program video track of a .kdenlive/MLT file back as master-clock shots.

    ``offsets_sec`` are the sync offsets (source = master + offset). Clock drift is not
    applied here (documented limitation). Entries whose clip cannot be mapped to a camera are
    skipped; blanks advance the timeline.
    """
    import defusedxml.ElementTree as DET

    root = DET.parse(str(path)).getroot()
    prof = root.find("profile")
    fps = (float(prof.get("frame_rate_num", 25)) / float(prof.get("frame_rate_den", 1))) if prof is not None else 25.0
    res_map = {str(k): v for k, v in (resource_to_camera or {}).items()}

    def props(el: Any) -> dict[str, str]:
        return {p.get("name"): (p.text or "") for p in el.findall("property")}

    prod_cam: dict[str, str] = {}
    for el in list(root.iter("chain")) + list(root.iter("producer")):
        pr = props(el)
        cam = pr.get("kdenlive:clipname") if pr.get("kdenlive:clipname") in offsets_sec else None
        if cam is None:
            res = pr.get("resource", "")
            cam = res_map.get(res) or res_map.get(Path(res).name) or next((c for c in offsets_sec if Path(res).stem == c), None)
        if cam:
            prod_cam[el.get("id")] = cam
    playlists = {pl.get("id"): pl for pl in root.iter("playlist")}
    track_lists: list[tuple[str, Any]] = []
    for tr in root.iter("tractor"):
        pr = props(tr)
        if "kdenlive:track_name" not in pr or pr.get("kdenlive:audio_track") == "1":
            continue
        for t in tr.findall("track"):
            pl = playlists.get(t.get("producer"))
            if pl is not None and t.get("hide") != "video":
                track_lists.append((pr["kdenlive:track_name"], pl))
                break
    scored = []
    for name, pl in track_lists:
        n = sum(1 for e in pl.findall("entry") if e.get("producer") in prod_cam)
        if n:
            scored.append((("program" in name.lower() or "v1" in name.lower()), n, name, pl))
    if not scored:
        return []
    best = max(scored, key=lambda x: (x[0], x[1]))[2:]
    shots: list[dict[str, Any]] = []
    cursor = 0
    for el in best[1]:
        if el.tag == "blank":
            cursor += int(el.get("length", "0"))
            continue
        if el.tag != "entry":
            continue
        a, b = int(el.get("in", "0")), int(el.get("out", "0"))
        n = b - a + 1
        cam = prod_cam.get(el.get("producer"))
        if cam is not None and n > 0:
            src_in = a / fps
            m_in = src_in - float(offsets_sec.get(cam, 0.0))
            shots.append({"camera_id": cam, "master_sync_in_sec": round(m_in, 4), "master_sync_out_sec": round(m_in + n / fps, 4), "timeline_in_sec": round(cursor / fps, 4), "timeline_out_sec": round((cursor + n) / fps, 4)})
        cursor += max(0, n)
    return shots


__all__ = [
    "apply_terminology",
    "balanced_wrap",
    "build_captions_v122",
    "build_cues",
    "caption_readability",
    "caption_rules",
    "caption_transcript_agreement",
    "edited_words",
    "repair_words",
    "retime_captions_to_shots",
    "shots_from_kdenlive",
    "terminology_rules",
]
