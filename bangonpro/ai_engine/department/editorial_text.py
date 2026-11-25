"""Shared, deterministic text helpers for the editorial agents (V1.2.2).

Pure functions only (no state, no I/O): tokenisation, sentence boundaries,
question/pronoun/connective detection and transcript word access. They are used by
pacing (Rough Cut), editorial analysis, captions, graphics and editorial QC so every
agent reads the transcript the same way.

Word-level logic only runs on genuine spoken text (``is_neural_inference=True``);
VAD placeholder segments never contribute words.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

STOPWORDS: frozenset[str] = frozenset(
    """
    a about above after again against all also am an and any are aren't as at be because been before being below
    between both but by can can't cannot could couldn't did didn't do does doesn't doing don't down during each few
    for from further get got had hadn't has hasn't have haven't having he he'd he'll he's her here here's hers herself
    him himself his how how's i i'd i'll i'm i've if in into is isn't it it's its itself just let's like me more most
    much mustn't my myself no nor not now of off on once only or other ought our ours ourselves out over own really
    right same say said shan't she she'd she'll she's should shouldn't so some such than that that's the their theirs
    them themselves then there there's these they they'd they'll they're they've thing things think this those
    through to too under until up very was wasn't we we'd we'll we're we've well were weren't what what's when
    when's where where's which while who who's whom why why's will with won't would wouldn't yeah yes you you'd
    you'll you're you've your yours yourself yourselves okay ok oh um uh erm er ah hmm mm actually going know mean
    lot kind sort way yeah gonna wanna one two also
    """.split()
)

FILLER_TOKENS: frozenset[str] = frozenset({"um", "uh", "erm", "er", "ah", "uhm", "hmm", "mm", "mhm", "uh-huh"})
INTERROGATIVE_STARTS: tuple[str, ...] = (
    "what", "why", "how", "who", "whom", "whose", "where", "when", "which",
    "do", "does", "did", "is", "are", "was", "were", "can", "could", "will", "would", "should",
    "have", "has", "had", "shall", "may", "might", "isn't", "aren't", "don't", "doesn't", "didn't",
)
# Words that point back to something said earlier; at the start of a kept fragment after a
# cut they suggest the referent may have been removed.
ANAPHORIC_OPENERS: frozenset[str] = frozenset(
    {"he", "she", "it", "they", "them", "him", "her", "that", "this", "those", "these", "there", "its", "their", "his"}
)
CONNECTIVE_OPENERS: frozenset[str] = frozenset({"and", "but", "so", "because", "which", "or", "then", "also"})
ANSWER_OPENERS: frozenset[str] = frozenset(
    {"yes", "yeah", "yep", "no", "nope", "well", "absolutely", "exactly", "definitely", "sure", "honestly", "correct", "right"}
)
_TERMINAL_RE = re.compile(r"[.!?…]['\"”’)\]]*$")
_TOKEN_STRIP_RE = re.compile(r"[^\w'’-]+", re.UNICODE)


def norm_token(word: str) -> str:
    """Lower-case a word and strip surrounding punctuation (keeps internal apostrophes)."""
    tok = _TOKEN_STRIP_RE.sub("", (word or "").strip().lower()).replace("’", "'")
    return tok.strip("-'")


def tokens(text: str) -> list[str]:
    return [t for t in (norm_token(w) for w in (text or "").split()) if t]


def content_tokens(text: str) -> list[str]:
    """Content words for lexical cohesion: stop words removed, light plural stemming."""
    out: list[str] = []
    for t in tokens(text):
        if t in STOPWORDS or len(t) < 3 or t.isdigit():
            continue
        if len(t) > 4 and t.endswith("ies"):
            t = t[:-3] + "y"
        elif len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


def ends_sentence(word_or_text: str) -> bool:
    return bool(_TERMINAL_RE.search((word_or_text or "").strip()))


def is_truncated(word: str) -> bool:
    """Whisper marks cut-off words with a trailing dash (``th-``, ``we were—``)."""
    w = (word or "").strip()
    return len(w) > 1 and w.endswith(("-", "—", "–"))


_WH = ("what", "why", "how", "who", "whom", "whose", "where", "when", "which")
_AUX = ("do", "does", "did", "is", "are", "was", "were", "can", "could", "will", "would", "should", "have", "has", "had", "shall", "may", "might", "isn't", "aren't", "don't", "doesn't", "didn't", "won't", "wouldn't", "can't", "couldn't")
_PRON = ("i", "you", "we", "they", "he", "she", "it", "that", "this", "there", "anyone", "everyone", "your", "the")


def is_question_text(text: str) -> bool:
    """True when the (last) sentence is a question.

    Explicit ``?`` always counts. Without terminal punctuation only an inverted form
    counts (``what do you ...``, ``are you ...``), so a statement such as "what we did was
    great" is never mistaken for a question.
    """
    t = (text or "").strip()
    if not t:
        return False
    if t.rstrip("\"'”’) ").endswith("?"):
        return True
    if ends_sentence(t):
        return False
    toks = tokens(last_sentence(t))
    if len(toks) < 2:
        return False
    if toks[0] in _WH and toks[1] in _AUX:
        return True
    return toks[0] in _AUX and toks[1] in _PRON


def last_sentence(text: str) -> str:
    parts = re.split(r"(?<=[.!?…])\s+", (text or "").strip())
    return parts[-1] if parts else ""


def first_sentence(text: str) -> str:
    parts = re.split(r"(?<=[.!?…])\s+", (text or "").strip())
    return parts[0] if parts else ""


def spoken_segments(segments: Iterable[Any]) -> list[Any]:
    """Segments that carry genuine spoken text (neural ASR), not VAD placeholders."""
    return [s for s in segments if getattr(s, "is_neural_inference", False) and (s.text or "").strip() and not getattr(s, "deleted", False)]


def segment_words(seg: Any) -> list[dict[str, Any]]:
    """Timed words of a spoken segment (empty for VAD placeholders)."""
    if not getattr(seg, "is_neural_inference", False):
        return []
    out = []
    for w in seg.words or []:
        txt = str(w.get("word", "")).strip()
        if not txt:
            continue
        s = float(w.get("start_sec", seg.start_sec))
        e = max(s, float(w.get("end_sec", s)))
        out.append({**w, "word": txt, "start_sec": s, "end_sec": e})
    return out


def words_have_asr_timing(seg: Any) -> bool:
    """Words carry measured ASR timestamps (not even interpolation) - required before word cuts."""
    words = seg.words or []
    return bool(words) and all(str(w.get("timing", "asr")) == "asr" for w in words)


def sentence_boundaries(segments: Iterable[Any]) -> list[float]:
    """Master-clock times where a sentence or speaker turn ends (sorted, de-duplicated).

    Neural: word ends with terminal punctuation, plus every segment end. VAD only:
    segment starts and ends (the best available approximation of a complete thought).
    """
    pts: set[float] = set()
    for seg in segments:
        pts.add(round(float(seg.start_sec), 3))
        pts.add(round(float(seg.end_sec), 3))
        for w in segment_words(seg):
            if ends_sentence(w["word"]):
                pts.add(round(float(w["end_sec"]), 3))
    return sorted(pts)


def sentence_starts(segments: Iterable[Any]) -> list[float]:
    pts: set[float] = set()
    for seg in segments:
        pts.add(round(float(seg.start_sec), 3))
        words = segment_words(seg)
        for prev, nxt in zip(words, words[1:]):
            if ends_sentence(prev["word"]):
                pts.add(round(float(nxt["start_sec"]), 3))
    return sorted(pts)


def inside_word(t: float, words: Iterable[dict[str, Any]], guard: float = 0.02) -> dict[str, Any] | None:
    for w in words:
        if w["start_sec"] + guard < t < w["end_sec"] - guard:
            return w
    return None


__all__ = [
    "ANAPHORIC_OPENERS",
    "ANSWER_OPENERS",
    "CONNECTIVE_OPENERS",
    "FILLER_TOKENS",
    "INTERROGATIVE_STARTS",
    "STOPWORDS",
    "content_tokens",
    "ends_sentence",
    "first_sentence",
    "inside_word",
    "is_question_text",
    "is_truncated",
    "last_sentence",
    "norm_token",
    "segment_words",
    "sentence_boundaries",
    "sentence_starts",
    "spoken_segments",
    "tokens",
    "words_have_asr_timing",
]
