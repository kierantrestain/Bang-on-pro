"""Editorial Intelligence analysis (V1.2.2): topics, significant moments, speakers, highlights.

All analysis here is cheap and deterministic and runs **on top of** the cached
transcript/diarisation (no re-inference):

* ``segment_topics``    - topic segmentation from lexical cohesion and topic-shift cues
  when spoken text exists, from turn structure, pauses and energy when it does not;
* ``detect_moments``    - typed significant moments (laughter/humour, applause,
  reactions, interruptions/crosstalk, questions, emphatic and newsworthy statements);
* ``identify_speakers`` - display names from the profile (``graphics_rules.speaker_names``,
  non-generic speaker labels) and self-introductions in the transcript ("I'm Sarah");
* ``score_highlights``  - context-aware highlight windows that prefer complete thoughts
  with a setup and a payoff, starting and ending on sentence/turn boundaries;
* ``select_social_clips`` - per-platform social clips on the master clock.

Every result says which evidence it used (spoken text vs speech-activity only), so a
VAD-only transcript is never presented as if words were understood.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import bisect
import math
import re
from collections import Counter
from typing import Any

import numpy as np

from bangonpro.ai_engine.department import editorial_text as et

MIN_TOPIC_DEFAULT: dict[str, float] = {"daily_news": 12.0, "podcast": 45.0, "interview": 45.0, "gameshow": 30.0, "sports_discussion": 30.0, "social": 15.0}
_TOPIC_CUES = re.compile(
    r"^(?:(?:so|okay|ok|right|alright|well),?\s+)?(?:moving on|let'?s (?:talk|move|turn|get|switch) (?:about|on|to|over)|next up|turning (?:now )?to|"
    r"in other news|our next (?:story|guest|question|round|item)|first up|and now|switching gears|on to (?:the|our)|next question|"
    r"let'?s (?:start|begin|kick off) with|elsewhere|meanwhile|now,? (?:to|for)|time for|coming up|back to)\b",
    re.IGNORECASE,
)
_LAUGH_TEXT = re.compile(r"\b(?:ha(?:ha)+|he(?:he)+|lol|lmao)\b|[\[(*]\s*(?:laugh\w*|chuckl\w*|giggl\w*)\s*[\])*]", re.IGNORECASE)
_APPLAUSE_TEXT = re.compile(r"[\[(*]\s*(?:applause|clapping|cheer\w*)\s*[\])*]", re.IGNORECASE)
_EMPHATIC_WORDS = re.compile(r"\b(absolutely|incredible|unbelievable|amazing|insane|ridiculous|brilliant|outrageous|massive|huge|never|disaster|shocking|wow|goal|winner|unreal)\b", re.IGNORECASE)
_NEWS_WORDS = re.compile(
    r"\b(breaking|announc\w+|confirm\w*|exclusive\w*|reveal\w*|resign\w*|elect\w*|verdict|arrest\w*|launch\w*|record[- ]breaking|for the first time|"
    r"signed|deal|million|billion|percent|per cent|investigation|officially|charged|sacked|appointed|transfer)\b",
    re.IGNORECASE,
)
MOMENT_WEIGHT: dict[str, float] = {"humour": 1.0, "laughter": 0.95, "applause": 0.9, "newsworthy": 0.8, "reaction": 0.7, "emphatic": 0.55, "interruption": 0.35, "crosstalk": 0.3, "question": 0.25, "topic_change": 0.1}
_PAYOFF_TYPES = frozenset({"humour", "laughter", "applause", "newsworthy", "reaction", "emphatic"})
_NOT_NAMES = frozenset(
    "Not So Very Really Just Going Sure Glad Happy Sorry Here There Back Fine Good Great Okay Ok Excited Delighted Thrilled Honoured Honored "
    "Joined Joining Talking Telling Saying Thinking Looking Trying Getting Doing Making Still Also Always Never Actually Literally Pretty Quite "
    "All The A An In On At With From Like Gonna Done Ready Afraid Proud British American Irish Scottish English French German Welsh "
    "Absolutely Definitely Totally Kind Sort Only Now Then Well Yes No Yeah Today Tonight Live Coming Hoping Lucky Old New Your Our My His Her "
    "Their One Two Three Over Under Out Up Down About Off Hosting Presenting Reporting Standing Sitting Joining Calling Speaking".split()
)
_NAME = r"(?P<name>[A-Z][a-z]+(?:[ -][A-Z][a-z]+){0,2})"
_SELF_INTRO = re.compile(r"\b(?:I'm|I’m|I am|[Mm]y name is|[Mm]y name's|[Mm]y name’s)\s+" + _NAME)
_SELF_HERE = re.compile(r"^(?:[Hh]i,?\s+|[Hh]ello,?\s+)?" + _NAME + r"\s+here\b")
_OTHER_INTRO = re.compile(
    r"\b(?:[Jj]oining me (?:today |now |here |tonight )?(?:is|are)|[Mm]y guest (?:today |this week |tonight )?is|[Pp]lease welcome|[Ww]elcome(?: back)?(?: to the show)?,)\s+" + _NAME
)
_GENERIC_ROLES = {"host", "guest", "panel", "contestant", "interviewer", "subject", "presenter", "anchor", "speaker", "studio", "reporter", "caller", "co-host", "cohost"}


def _fmt(profile: dict[str, Any]) -> str:
    from bangonpro.ai_engine.department.editorial_pacing import format_key

    return format_key(profile)


def _live(segments: list[Any]) -> list[Any]:
    return sorted((s for s in segments if not getattr(s, "deleted", False)), key=lambda s: (s.start_sec, s.end_sec))


# ---------------------------------------------------------------------------
# Topic segmentation
# ---------------------------------------------------------------------------
def _cosine(a: Counter[str], b: Counter[str]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def segment_topics(segments: list[Any], profile: dict[str, Any], *, min_silence_sec: float = 0.8, interruptions: list[Any] | None = None) -> list[dict[str, Any]]:
    """Split the programme into topics (master clock). Returns dict rows (see module doc)."""
    segs = _live(segments)
    if not segs:
        return []
    fmt = _fmt(profile)
    rules = profile.get("editing_rules", {}) or {}
    min_topic = float(rules.get("min_topic_sec", MIN_TOPIC_DEFAULT.get(fmt, 30.0)))
    total = sum(s.end_sec - s.start_sec for s in segs) or 1.0
    spoken = et.spoken_segments(segs)
    lexical = sum(s.end_sec - s.start_sec for s in spoken) / total >= 0.5
    talk: Counter[str] = Counter()
    for s in segs:
        talk[s.speaker_id] += s.end_sec - s.start_sec
    main_spk = talk.most_common(1)[0][0]
    window = max(15.0, min_topic * 0.75)
    starts = [s.start_sec for s in segs]
    toks = [Counter(et.content_tokens(s.text)) if s.is_neural_inference else Counter() for s in segs]
    energies = np.array([s.energy_db for s in segs], dtype=float)
    e_sd = float(energies.std() or 1.0)
    long_gap = max(2.0, 2.5 * min_silence_sec)
    interrupt_starts = {round(float(iv.end_sec), 2) for iv in (interruptions or [])}
    cands: list[tuple[float, int, list[str]]] = []
    for i in range(1, len(segs)):
        t = segs[i].start_sec
        gap = max(0.0, t - segs[i - 1].end_sec)
        lo = bisect.bisect_left(starts, t - window)
        hi = bisect.bisect_left(starts, t + window)
        left, right = segs[lo:i], segs[i:hi]
        why: list[str] = []
        score = 0.0
        if lexical:
            lc: Counter[str] = Counter()
            rc: Counter[str] = Counter()
            for k in range(lo, i):
                lc.update(toks[k])
            for k in range(i, hi):
                rc.update(toks[k])
            cohesion = _cosine(lc, rc)
            score += 0.8 * (1.0 - cohesion)
            why.append(f"lexical cohesion {cohesion:.2f}")
            if segs[i].is_neural_inference and _TOPIC_CUES.search(segs[i].text.strip()):
                score += 0.6
                why.append(f"topic cue '{segs[i].text.strip()[:40]}'")
        else:
            lt: Counter[str] = Counter()
            rt: Counter[str] = Counter()
            for s in left:
                lt[s.speaker_id] += s.end_sec - s.start_sec
            for s in right:
                rt[s.speaker_id] += s.end_sec - s.start_sec
            lsum, rsum = sum(lt.values()) or 1.0, sum(rt.values()) or 1.0
            dissim = 0.5 * sum(abs(lt.get(k, 0) / lsum - rt.get(k, 0) / rsum) for k in set(lt) | set(rt))
            le = float(np.mean([s.energy_db for s in left])) if left else 0.0
            re_ = float(np.mean([s.energy_db for s in right])) if right else 0.0
            score += 0.4 * dissim + 0.2 * min(1.0, abs(le - re_) / (2 * e_sd))
            why.append(f"turn-structure change {dissim:.2f}")
        if gap >= long_gap:
            score += 0.3 * min(1.0, gap / (2 * long_gap)) + 0.2
            why.append(f"{gap:.1f}s pause")
        if round(t, 2) in interrupt_starts or any(abs(t - x) < 0.6 for x in interrupt_starts):
            score += 0.5
            why.append("recording restarted")
        if segs[i].speaker_id == main_spk and segs[i - 1].speaker_id != main_spk:
            score += 0.1
            why.append(f"{main_spk} takes the floor")
        thresh = 0.85 if lexical else 0.6
        if score >= thresh and (lexical or gap >= long_gap or "recording restarted" in why):
            cands.append((score, i, why))
    chosen: list[int] = []
    prog_start, prog_end = segs[0].start_sec, max(s.end_sec for s in segs)
    for _score, i, _why in sorted(cands, key=lambda c: -c[0]):
        t = segs[i].start_sec
        if t - prog_start < min_topic * 0.5 or prog_end - t < min_topic * 0.5:
            continue
        if all(abs(t - segs[j].start_sec) >= min_topic for j in chosen):
            chosen.append(i)
    chosen.sort()
    why_by_i = {i: w for _s, i, w in cands}
    score_by_i = {i: s for s, i, _w in cands}
    bounds = [0, *chosen, len(segs)]
    groups = [segs[bounds[k]:bounds[k + 1]] for k in range(len(bounds) - 1)]
    df: Counter[str] = Counter()
    group_tf = []
    for g in groups:
        tf: Counter[str] = Counter()
        for s in g:
            if s.is_neural_inference:
                tf.update(et.content_tokens(s.text))
        group_tf.append(tf)
        df.update(set(tf))
    out: list[dict[str, Any]] = []
    for k, g in enumerate(groups):
        dur: Counter[str] = Counter()
        for s in g:
            dur[s.speaker_id] += s.end_sec - s.start_sec
        kw = [w for w, _ in sorted(group_tf[k].items(), key=lambda kv: (-(kv[1] * math.log((1 + len(groups)) / (1 + df[kv[0]])) + kv[1] * 1e-3), kv[0]))[:3]] if lexical else []
        base = "Opening" if k == 0 else f"Topic {k + 1}" if lexical else f"Section {k + 1}"
        bi = bounds[k]
        out.append(
            {
                "section_id": f"sec_{k + 1:02d}",
                "label": base + (f": {', '.join(kw)}" if kw else ""),
                "start_sec": round(g[0].start_sec, 4),
                "end_sec": round(max(s.end_sec for s in g), 4),
                "dominant_speaker": dur.most_common(1)[0][0],
                "confidence": round(float(np.mean([s.confidence for s in g])) * (0.95 if lexical else 0.75), 4),
                "keywords": kw,
                "method": "lexical_cohesion" if lexical else "turn_structure_energy",
                "boundary_evidence": why_by_i.get(bi, ["programme start"]) if k else ["programme start"],
                "boundary_score": round(score_by_i.get(bi, 0.0), 3) if k else None,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Significant moments
# ---------------------------------------------------------------------------
def _span_of(seg: Any, pattern: re.Pattern[str]) -> tuple[float, float]:
    for w in et.segment_words(seg):
        if pattern.search(w["word"]):
            return w["start_sec"], w["end_sec"]
    return seg.start_sec, seg.end_sec


def detect_moments(state: Any) -> list[dict[str, Any]]:
    """Typed significant moments on the master clock (deterministic)."""
    segs = _live(state.transcript_segments)
    neural_run = bool(getattr(state.asr_backend_report, "is_neural_inference", False))
    moments: list[dict[str, Any]] = []

    def add(mtype: str, s: float, e: float, spk: str | None, conf: float, evidence: str, text: str = "") -> None:
        moments.append({"type": mtype, "start_sec": round(float(s), 3), "end_sec": round(float(max(e, s + 0.05)), 3), "speaker_id": spk, "confidence": round(conf, 3), "evidence": evidence, "text": text[:160]})

    energies = np.array([s.energy_db for s in segs], dtype=float) if segs else np.array([0.0])
    mu, sd = float(energies.mean()), float(energies.std() or 1.0)
    for s in segs:
        txt = (s.text or "").strip()
        if s.is_neural_inference and txt:
            if _LAUGH_TEXT.search(txt):
                a, b = _span_of(s, _LAUGH_TEXT)
                add("laughter", a, b, s.speaker_id, 0.85, "laughter in the transcript", txt)
            if _APPLAUSE_TEXT.search(txt):
                a, b = _span_of(s, _APPLAUSE_TEXT)
                add("applause", a, b, None, 0.85, "applause in the transcript", txt)
            last = et.last_sentence(txt)
            if et.is_question_text(last):
                words = et.segment_words(s)
                q_start = s.start_sec
                if words:
                    for prev_w, w in zip(words, words[1:]):
                        if et.ends_sentence(prev_w["word"]) and not et.ends_sentence(w["word"]):
                            q_start = w["start_sec"]
                add("question", q_start, s.end_sec, s.speaker_id, 0.8, "question", last)
            n_news = len(_NEWS_WORDS.findall(txt))
            if n_news and (n_news >= 2 or re.search(r"\d", txt)):
                add("newsworthy", s.start_sec, s.end_sec, s.speaker_id, min(0.9, 0.5 + 0.15 * n_news), "newsworthy wording", txt)
            if "!" in txt or _EMPHATIC_WORDS.search(txt):
                add("emphatic", s.start_sec, s.end_sec, s.speaker_id, 0.6, "emphatic wording", txt)
        elif neural_run and (s.end_sec - s.start_sec) <= 8.0:
            # Speech-like audio the ASR found no words in: laughter, cheering or another vocal reaction
            add("reaction", s.start_sec, s.end_sec, s.speaker_id, 0.55, "voice-band audio without words")
        z = (s.energy_db - mu) / sd
        if z >= 1.5 and (s.end_sec - s.start_sec) >= 1.0 and not (s.is_neural_inference and "!" in (s.text or "")):
            add("emphatic", s.start_sec, s.end_sec, s.speaker_id, 0.5, f"vocal energy +{z:.1f} sd")
        if s.is_overlap:
            add("crosstalk", s.start_sec, s.end_sec, s.speaker_id, 0.7, "overlapping speakers: " + " & ".join(s.overlap_speakers))
    for ev in state.metadata.get("nonspeech_activity", []) or []:
        kind = "applause" if ev.get("kind_hint") == "applause" else "reaction"
        add(kind, ev["start_sec"], ev["end_sec"], ev.get("dominant_speaker"), 0.7 if kind == "applause" else 0.5, f"non-speech {ev.get('kind_hint', 'activity')} on the microphones")
    for a, b in zip(segs, segs[1:]):
        if a.speaker_id == b.speaker_id:
            continue
        if b.start_sec < a.end_sec - 0.15 and not b.is_overlap:
            add("interruption", b.start_sec, min(a.end_sec, b.end_sec), b.speaker_id, 0.6, f"{b.speaker_id} starts while {a.speaker_id} is speaking")
        elif 0.0 <= b.start_sec - a.end_sec < 0.25 and a.is_neural_inference:
            words = et.segment_words(a)
            if words and (et.is_truncated(words[-1]["word"]) or not et.ends_sentence(words[-1]["word"])):
                add("interruption", b.start_sec, b.start_sec + 0.5, b.speaker_id, 0.55, f"{a.speaker_id} cut off mid-sentence")
    laughs = [m for m in moments if m["type"] == "laughter"]
    for m in laughs:
        setup = max((s for s in segs if s.is_neural_inference and m["start_sec"] - 2.5 <= s.end_sec <= m["start_sec"] + 0.3 and not _LAUGH_TEXT.fullmatch((s.text or "").strip())), key=lambda s: s.end_sec, default=None)
        if setup is not None:
            add("humour", setup.start_sec, m["end_sec"], setup.speaker_id, 0.75, "line followed by laughter", setup.text)
    for sec in (state.story_sections or [])[1:]:
        add("topic_change", sec.start_sec, sec.start_sec + 0.5, sec.dominant_speaker, sec.confidence, f"new topic: {sec.label}")
    moments.sort(key=lambda m: (m["start_sec"], m["type"]))
    for i, m in enumerate(moments, start=1):
        m["moment_id"] = f"mom_{i:04d}"
    return moments


# ---------------------------------------------------------------------------
# Speaker identification
# ---------------------------------------------------------------------------
def _is_generic(label: str) -> bool:
    first = re.split(r"[\s_]+", label.strip().lower())[0] if label.strip() else ""
    return first in _GENERIC_ROLES or first.startswith("cam") or bool(re.fullmatch(r"(speaker|spk)\s*_?\d*", label.strip().lower()))


def _clean_name(raw: str) -> str | None:
    parts = [p for p in re.split(r"[ ]", raw.strip()) if p]
    keep: list[str] = []
    for p in parts:
        if p in _NOT_NAMES or p.lower() in et.STOPWORDS:
            break
        keep.append(p)
    return " ".join(keep) if keep else None


def identify_speakers(state: Any) -> dict[str, dict[str, Any]]:
    """Resolve each diarised speaker to a display name, title and camera, with the evidence used."""
    segs = _live(state.transcript_segments)
    prof = state.profile
    names_cfg = (prof.get("graphics_rules", {}) or {}).get("speaker_names") or {}
    spk_map = dict(prof.get("speaker_camera_map") or {})
    roles = prof.get("camera_roles") or {}
    out: dict[str, dict[str, Any]] = {}
    for s in segs:
        r = out.setdefault(s.speaker_id, {"speaker_id": s.speaker_id, "talk_time_sec": 0.0, "turns": 0, "first_sec": s.start_sec, "last_sec": s.end_sec, "camera_id": spk_map.get(s.speaker_id, s.camera_id)})
        r["talk_time_sec"] += s.end_sec - s.start_sec
        r["last_sec"] = max(r["last_sec"], s.end_sec)
    prev_spk = None
    for s in segs:
        if s.speaker_id != prev_spk:
            out[s.speaker_id]["turns"] += 1
        prev_spk = s.speaker_id
    found: dict[str, list[tuple[float, str, str]]] = {}
    for i, s in enumerate(segs):
        if not s.is_neural_inference:
            continue
        txt = s.text or ""
        for rx in (_SELF_INTRO, _SELF_HERE):
            m = rx.search(txt)
            if m:
                nm = _clean_name(m.group("name"))
                if nm:
                    found.setdefault(s.speaker_id, []).append((0.85, nm, f"self-introduction '{m.group(0)[:50]}'"))
        m = _OTHER_INTRO.search(txt)
        if m:
            nm = _clean_name(m.group("name"))
            nxt = next((o for o in segs[i + 1:] if o.speaker_id != s.speaker_id and o.start_sec - s.end_sec <= 30.0), None)
            if nm and nxt is not None:
                found.setdefault(nxt.speaker_id, []).append((0.7, nm, f"introduced by {s.speaker_id}: '{m.group(0)[:50]}'"))
    for spk, r in out.items():
        cfg = names_cfg.get(spk)
        title = ""
        if isinstance(cfg, dict):
            name, title, src, conf = str(cfg.get("name") or spk), str(cfg.get("title") or ""), "profile.graphics_rules.speaker_names", 1.0
        elif isinstance(cfg, str) and cfg.strip():
            name, src, conf = cfg.strip(), "profile.graphics_rules.speaker_names", 1.0
        elif not _is_generic(spk):
            name, src, conf = spk, "profile speaker label", 0.95
        elif spk in found:
            conf, name, src = sorted(found[spk], key=lambda f: -f[0])[0]
        else:
            name, src, conf = spk, "role label only", 0.4
        cam = r["camera_id"]
        role = str(roles.get(cam, "")) if cam else ""
        r.update(
            {
                "name": name,
                "title": title or (spk if (_is_generic(spk) and name != spk) else (role.capitalize() if role and role not in ("wide", "reaction", "broll") else "")),
                "name_source": src,
                "name_confidence": round(conf, 3),
                "talk_time_sec": round(r["talk_time_sec"], 3),
                "candidates": [{"name": n, "confidence": c, "evidence": e} for c, n, e in found.get(spk, [])],
            }
        )
    return out


# ---------------------------------------------------------------------------
# Highlights and social clips
# ---------------------------------------------------------------------------
def sentence_units(segments: list[Any]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for seg in _live(segments):
        words = et.segment_words(seg)
        if words:
            cur: list[dict[str, Any]] = []
            for k, w in enumerate(words):
                cur.append(w)
                if et.ends_sentence(w["word"]) or k == len(words) - 1:
                    text = " ".join(x["word"] for x in cur)
                    units.append({"start": cur[0]["start_sec"], "end": cur[-1]["end_sec"], "text": text, "speaker": seg.speaker_id, "segment_id": seg.segment_id, "ends_sentence": et.ends_sentence(w["word"]) or k == len(words) - 1, "is_question": et.is_question_text(text), "energy": seg.energy_db, "overlap": seg.is_overlap, "neural": True})
                    cur = []
        else:
            units.append({"start": seg.start_sec, "end": seg.end_sec, "text": seg.text if seg.is_neural_inference else "", "speaker": seg.speaker_id, "segment_id": seg.segment_id, "ends_sentence": True, "is_question": seg.is_neural_inference and et.is_question_text(seg.text), "energy": seg.energy_db, "overlap": seg.is_overlap, "neural": bool(seg.is_neural_inference)})
    units.sort(key=lambda u: u["start"])
    for i, u in enumerate(units):
        prev = units[i - 1] if i else None
        u["starts_turn"] = prev is None or prev["speaker"] != u["speaker"] or (u["start"] - prev["end"]) >= 0.6
        u["sentence_start"] = prev is None or prev["ends_sentence"]
    return units


def _window_candidates(state: Any, units: list[dict[str, Any]], moments: list[dict[str, Any]], *, min_len: float, max_len: float, target_len: float) -> list[dict[str, Any]]:
    if not units:
        return []
    energies = np.array([u["energy"] for u in units], dtype=float)
    mu, sd = float(energies.mean()), float(energies.std() or 1.0)
    z = (energies - mu) / sd
    zc = np.concatenate([[0.0], np.cumsum(z)])
    turns = np.concatenate([[0], np.cumsum([1 if (i and units[i]["speaker"] != units[i - 1]["speaker"]) else 0 for i in range(len(units))])])
    payoff = sorted((m for m in moments if m["type"] in _PAYOFF_TYPES), key=lambda m: m["start_sec"])
    p_starts = [m["start_sec"] for m in payoff]
    topic_starts = [s.start_sec for s in (state.story_sections or [])[1:]]
    out: list[dict[str, Any]] = []
    programme = units[-1]["end"] - units[0]["start"]
    if programme < min_len:
        min_len = max(1.0, programme * 0.5)
    for i, u in enumerate(units):
        if not (u["starts_turn"] or u["sentence_start"]):
            continue
        first_tok = (et.tokens(u["text"]) or [""])[0]
        for j in range(i, len(units)):
            dur = units[j]["end"] - u["start"]
            if dur > max_len:
                break
            if dur < min_len or not units[j]["ends_sentence"]:
                continue
            start, end = u["start"], units[j]["end"]
            lo = bisect.bisect_left(p_starts, start - 0.5)
            hi = bisect.bisect_right(p_starts, end + 2.0)
            pay, pay_types, ext_end = 0.0, [], end
            for m in payoff[lo:hi]:
                w = MOMENT_WEIGHT.get(m["type"], 0.3) * float(m.get("confidence", 0.6))
                if m["start_sec"] >= end - 0.05:  # reaction right after the window: include it
                    if m["end_sec"] + 0.3 - start <= max_len + 4.0:
                        ext_end = max(ext_end, m["end_sec"] + 0.3)
                    else:
                        continue
                pay = max(pay, w)
                pay_types.append(m["type"])
            setup = 0.0
            if u["is_question"] or any(units[k]["is_question"] and k + 1 <= j and units[k + 1]["speaker"] != units[k]["speaker"] for k in range(i, j)):
                setup = 0.6
            elif u["starts_turn"]:
                setup = 0.3
            if any(abs(start - t) <= 2.0 for t in topic_starts):
                setup += 0.2
            start_ok = 1.0 if u["starts_turn"] else 0.7
            end_ok = 1.0 if units[j]["ends_sentence"] else 0.5
            opener_pen = 0.0
            if u["neural"] and not u["starts_turn"]:
                if first_tok in et.ANAPHORIC_OPENERS:
                    opener_pen = 0.4
                elif first_tok in et.CONNECTIVE_OPENERS:
                    opener_pen = 0.3
            unanswered = 0.4 if units[j]["is_question"] else 0.0
            completeness = (start_ok + end_ok) / 2.0 - opener_pen - unanswered
            n = j - i + 1
            eng = 0.5 * float(np.clip((zc[j + 1] - zc[i]) / n, -2, 2)) / 2.0 + 0.5 * min(1.0, float(turns[j + 1] - turns[i + 1]) / max(1.0, dur / 10.0))
            lenpref = max(0.0, 1.0 - abs(dur - target_len) / target_len)
            crosses = sum(1 for t in topic_starts if start + 1.0 < t < end - 1.0)
            score = 1.5 * pay + 0.6 * setup + 1.0 * completeness + 0.5 * eng + 0.2 * lenpref - 0.3 * crosses
            reasons = []
            if pay_types:
                reasons.append("payoff: " + ", ".join(sorted(set(pay_types))))
            if setup >= 0.6:
                reasons.append("setup: question and answer")
            elif setup >= 0.3:
                reasons.append("starts at a turn")
            reasons.append("complete thought (sentence boundaries)" if completeness >= 0.85 else "partial thought")
            if eng > 0.4:
                reasons.append("lively exchange / high energy")
            spk = Counter()
            for k in range(i, j + 1):
                spk[units[k]["speaker"]] += units[k]["end"] - units[k]["start"]
            out.append(
                {
                    "start_sec": round(max(0.0, start - 0.15), 3),
                    "end_sec": round(ext_end + 0.15, 3),
                    "score": round(float(score), 4),
                    "speaker_id": spk.most_common(1)[0][0],
                    "segment_id": u["segment_id"],
                    "reasons": reasons,
                    "text_available": bool(u["neural"]),
                    "moment_types": sorted(set(pay_types)),
                    "boundary": {"start": "turn_start" if u["starts_turn"] else "sentence_start", "end": "sentence_end" if units[j]["ends_sentence"] else "segment_end"},
                    "completeness": round(completeness, 3),
                    "text": " ".join(units[k]["text"] for k in range(i, j + 1) if units[k]["text"])[:240],
                    "_first_unit": i,
                }
            )
    return out


def _select(cands: list[dict[str, Any]], max_items: int, topics: list[Any]) -> list[dict[str, Any]]:
    tstarts = [t.start_sec for t in topics] or [0.0]
    per_topic_cap = max(1, math.ceil(max_items / max(1, len(tstarts)))) + 1
    used: Counter[int] = Counter()
    chosen: list[dict[str, Any]] = []
    for c in sorted(cands, key=lambda c: (-c["score"], c["start_sec"])):
        if any(not (c["end_sec"] <= h["start_sec"] - 0.5 or c["start_sec"] >= h["end_sec"] + 0.5) for h in chosen):
            continue
        topic = max(0, bisect.bisect_right(tstarts, c["start_sec"]) - 1)
        if used[topic] >= per_topic_cap:
            continue
        used[topic] += 1
        c = dict(c)
        c["topic_index"] = topic
        c.pop("_first_unit", None)
        chosen.append(c)
        if len(chosen) >= max_items:
            break
    return sorted(chosen, key=lambda h: h["start_sec"])


def score_highlights(state: Any, *, max_items: int = 8, min_len: float | None = None, max_len: float | None = None, moments: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Context-aware highlight windows (complete thoughts, setup + payoff, sentence boundaries)."""
    rules = state.profile.get("distribution_rules", {}) or {}
    min_len = float(min_len if min_len is not None else rules.get("highlight_min_sec", 6.0))
    max_len = float(max_len if max_len is not None else rules.get("highlight_max_sec", 30.0))
    units = sentence_units(state.transcript_segments)
    if moments is None:
        moments = detect_moments(state)
    cands = _window_candidates(state, units, moments, min_len=min_len, max_len=max_len, target_len=min(max_len, max(min_len, 18.0)))
    return _select(cands, max_items, state.story_sections or [])


PLATFORM_LIMITS: dict[str, tuple[float, float]] = {
    "vertical_9x16": (12.0, 60.0),
    "youtube_shorts": (12.0, 60.0),
    "instagram_reels": (12.0, 90.0),
    "tiktok": (12.0, 90.0),
    "x_video": (8.0, 140.0),
}


def _title(clip: dict[str, Any], speakers: dict[str, dict[str, Any]]) -> str:
    text = (clip.get("text") or "").strip()
    if text:
        first = et.first_sentence(text)
        if len(first) > 60:
            first = first[:60].rsplit(" ", 1)[0] + "…"
        return first
    name = speakers.get(clip.get("speaker_id") or "", {}).get("name") or clip.get("speaker_id") or "Highlight"
    m, s = divmod(int(clip["start_sec"]), 60)
    return f"{name} at {m}:{s:02d}"


def select_social_clips(state: Any, *, moments: list[dict[str, Any]] | None = None, speakers: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    rules = state.profile.get("distribution_rules", {}) or {}
    platforms = rules.get("social_platforms") or ["vertical_9x16"]
    count = int(rules.get("social_clip_count", 3))
    if moments is None:
        moments = detect_moments(state)
    speakers = speakers if speakers is not None else identify_speakers(state)
    units = sentence_units(state.transcript_segments)
    out: list[dict[str, Any]] = []
    for plat in platforms:
        lo, hi = PLATFORM_LIMITS.get(str(plat), (12.0, 60.0))
        lo = float(rules.get("social_clip_min_sec", lo))
        hi = float(min(hi, float(rules.get("social_clip_max_sec", hi))))
        cands = _window_candidates(state, units, moments, min_len=lo, max_len=hi, target_len=min(hi, max(lo, 30.0)))
        for c in _select(cands, count, state.story_sections or []):
            out.append(
                {
                    "start_sec": c["start_sec"],
                    "end_sec": c["end_sec"],
                    "platform": str(plat),
                    "aspect": str(rules.get("social_aspect", "9:16")),
                    "title": _title(c, speakers),
                    "reasons": c["reasons"],
                    "score": c["score"],
                    "speaker_id": c["speaker_id"],
                    "moment_types": c["moment_types"],
                    "text_available": c["text_available"],
                }
            )
    return out


# ---------------------------------------------------------------------------
# Shared editorial context
# ---------------------------------------------------------------------------
def build_editorial_context(state: Any) -> dict[str, Any]:
    from bangonpro.ai_engine.department.editorial_pacing import pause_inventory

    moments = detect_moments(state)
    speakers = identify_speakers(state)
    pauses = pause_inventory(state, moments=moments)
    neural = bool(getattr(state.asr_backend_report, "is_neural_inference", False))
    topics = [
        {"section_id": s.section_id, "label": s.label, "start_sec": s.start_sec, "end_sec": s.end_sec, "dominant_speaker": s.dominant_speaker, "confidence": s.confidence}
        for s in state.story_sections
    ]
    return {
        "version": "1.2.2",
        "evidence": "spoken transcript (neural ASR)" if neural else "speech activity only (VAD): no words; text-based typing disabled",
        "topics": topics,
        "topic_method": (state.metadata.get("topic_segmentation") or {}).get("method"),
        "moments": moments,
        "moment_counts": dict(Counter(m["type"] for m in moments)),
        "speakers": speakers,
        "protected_pauses": [p for p in pauses if p["protected"]],
        "pause_inventory_counts": dict(Counter(p["kind"] for p in pauses)),
    }


__all__ = [
    "MOMENT_WEIGHT",
    "PLATFORM_LIMITS",
    "build_editorial_context",
    "detect_moments",
    "identify_speakers",
    "score_highlights",
    "segment_topics",
    "select_social_clips",
    "sentence_units",
]
