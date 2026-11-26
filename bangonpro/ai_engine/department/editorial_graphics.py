"""Graphics (V1.2.2): template registry, context-aware lower thirds, topic cards, safe areas.

* a small reusable **template registry** (lower thirds, news strap, gameshow bar, title
  card, topic card) with geometry used both to render ``kdenlivetitle`` XML and to
  validate it;
* **lower thirds** at a speaker's first *substantive* appearance, while that speaker is on
  their own angle, never during crosstalk, never under the title card or a topic card,
  and re-shown after a long absence; on-screen time clamped to profile min/max;
* **topic / story title cards** where the profile asks for them (default: daily news);
* **title-safe / action-safe** validation and a text-fit check for every graphic.

Profile keys (``graphics_rules``; all optional): ``lower_third_min_sec`` (3.0),
``lower_third_max_sec`` (8.0), ``lower_third_min_speech_sec`` (2.5),
``lower_third_reshow_after_sec`` (format default), ``topic_title_cards`` (news: true),
``topic_card_duration_sec`` (4.0), ``speaker_names`` ({label: name | {name, title}}).

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import re

from typing import Any

from bangonpro.ai_engine.department import editorial_text as et

TEMPLATES: dict[str, dict[str, Any]] = {
    "studio_clean_bar": {"kind": "lower_third", "box_w": 0.34, "box_h": 0.115, "title_px": 0.038, "sub_px": 0.024, "position": "bottom_left"},
    "podcast_clean_bar": {"kind": "lower_third", "box_w": 0.34, "box_h": 0.115, "title_px": 0.038, "sub_px": 0.024, "position": "bottom_left"},
    "news_strap": {"kind": "lower_third", "box_w": 0.62, "box_h": 0.10, "title_px": 0.040, "sub_px": 0.024, "position": "bottom_left"},
    "gameshow_bold": {"kind": "lower_third", "box_w": 0.40, "box_h": 0.13, "title_px": 0.046, "sub_px": 0.028, "position": "bottom_center"},
    "sports_score_bar": {"kind": "lower_third", "box_w": 0.46, "box_h": 0.11, "title_px": 0.040, "sub_px": 0.024, "position": "bottom_left"},
    "episode_opener": {"kind": "title_card", "box_w": 0.60, "box_h": 0.20, "title_px": 0.060, "sub_px": 0.030, "position": "center"},
    "topic_card": {"kind": "topic_card", "box_w": 0.50, "box_h": 0.09, "title_px": 0.036, "sub_px": 0.022, "position": "top_left"},
}
# Names used by the built-in show profiles (same geometry families)
for _alias, _base in (
    ("news_breaking_chyron", "news_strap"),
    ("gameshow_scoreboard_banner", "gameshow_bold"),
    ("sports_analyst_chyron", "sports_score_bar"),
    ("news_headline_card", "episode_opener"),
    ("gameshow_round_bumper", "episode_opener"),
    ("sports_segment_slate", "episode_opener"),
    ("podcast_episode_opener", "episode_opener"),
    ("studio_opener", "episode_opener"),
):
    TEMPLATES[_alias] = dict(TEMPLATES[_base])
_KIND_DEFAULT = {"lower_third": "studio_clean_bar", "title_card": "episode_opener", "topic_card": "topic_card"}
RESHOW_DEFAULT: dict[str, float] = {"daily_news": 300.0, "podcast": 900.0, "interview": 900.0, "gameshow": 600.0, "sports_discussion": 600.0, "social": 0.0, "youtube": 600.0}
TITLE_SAFE = 0.90
ACTION_SAFE = 0.93


def register_template(name: str, spec: dict[str, Any]) -> None:
    """Add or replace a template (geometry as fractions of the frame)."""
    base = dict(TEMPLATES.get(_KIND_DEFAULT.get(str(spec.get("kind", "lower_third")), "studio_clean_bar"), {}))
    base.update(spec)
    TEMPLATES[name] = base


def get_template(name: str | None, kind: str = "lower_third") -> tuple[str, dict[str, Any], bool]:
    """Resolve a template by name; unknown names fall back to the kind's default (reported)."""
    if name and name in TEMPLATES and TEMPLATES[name]["kind"] == kind:
        return name, TEMPLATES[name], True
    if name and name in TEMPLATES:
        return name, TEMPLATES[name], True
    if name and kind == "title_card" and name.endswith(("opener", "_card")):
        return name, TEMPLATES["episode_opener"], True
    fallback = _KIND_DEFAULT[kind]
    return fallback, TEMPLATES[fallback], False


def geometry(spec: dict[str, Any], position: str, width: int, height: int) -> tuple[int, int, int, int]:
    """(x0, y0, w, h) in pixels - identical to the layout ``generate_kdenlive_lower_third_xml`` draws."""
    w = round(width * float(spec["box_w"]))
    h = round(height * float(spec["box_h"]))
    if position == "bottom_center":
        return round((width - w) * 0.5), round(height * 0.78), w, h
    if position == "bottom_right":
        return round(width * 0.95) - w, round(height * 0.78), w, h
    if position == "top_left":
        return round(width * 0.05), round(height * 0.08), w, h
    if position == "top_right":
        return round(width * 0.95) - w, round(height * 0.08), w, h
    if position == "center":
        return round((width - w) * 0.5), round((height - h) * 0.5), w, h
    if position == "top_center":
        return round((width - w) * 0.5), round(height * 0.08), w, h
    return round(width * 0.05), round(height * 0.78), w, h


def validate_graphic(*, spec: dict[str, Any], position: str, width: int, height: int, title: str, subtitle: str, duration_sec: float, min_sec: float, max_sec: float) -> dict[str, Any]:
    x0, y0, w, h = geometry(spec, position, width, height)

    def inside(frac: float) -> bool:
        mx, my = width * (1 - frac) / 2.0, height * (1 - frac) / 2.0
        return x0 >= mx - 0.5 and y0 >= my - 0.5 and x0 + w <= width - mx + 0.5 and y0 + h <= height - my + 0.5

    bar = max(10, round(width * 0.008)) + 22
    title_px = max(22, round(height * float(spec["title_px"])))
    sub_px = max(16, round(height * float(spec["sub_px"])))
    fits = len(title) * title_px * 0.55 <= (w - bar - 12) and len(subtitle) * sub_px * 0.55 <= (w - bar - 12)
    return {
        "bbox": [x0, y0, w, h],
        "inside_title_safe": inside(TITLE_SAFE),
        "inside_action_safe": inside(ACTION_SAFE),
        "text_fits": bool(fits),
        "duration_ok": min_sec - 1e-3 <= duration_sec <= max_sec + 1e-3,
    }


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------
def _speaker_names(state: Any) -> dict[str, dict[str, Any]]:
    ctx = (state.metadata.get("editorial_context") or {}).get("speakers")
    if ctx:
        return ctx
    from bangonpro.ai_engine.department.editorial_analysis import identify_speakers

    return identify_speakers(state)


_NAME_STRAP_RE = re.compile(
    r"(?i:lower[\s-]*thirds?|name\s+straps?|name\s+titles?|chyrons?)\s*(?i:for|:|-|–|saying|reading)?\s*"
    r"(?P<name>[A-Z][\w'.-]+(?:\s+[A-Z][\w'.-]+){0,3})(?:\s*(?:,|–|-|—|\()\s*(?P<title>[^.,;()\n]{2,48}))?"
)
_PLACEHOLDER = re.compile(r"^(cam(era)?[_ ]?\w*|speaker[_ ]?\w*|spk[_ ]?\w*|host|guest|presenter|unknown|person[_ ]?\d*)$", re.IGNORECASE)


def _real_name(name: Any, speaker_id: str) -> bool:
    n = str(name or "").strip()
    return bool(n) and n != speaker_id and not _PLACEHOLDER.match(n)


def names_from_brief(brief: str, speaker_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Names the editor typed ("lower third: Jane Doe, Host"), assigned to speakers in order of first appearance."""
    found = [(m.group("name").strip(), (m.group("title") or "").strip()) for m in _NAME_STRAP_RE.finditer(brief or "")]
    order = list(dict.fromkeys(speaker_ids))
    return {spk: {"name": n, "title": t, "source": "editor_brief", "confidence": 1.0} for spk, (n, t) in zip(order, found, strict=False)}


def _to_edited(state: Any, t: float) -> float | None:
    for s in state.rough_cut_shots:
        if s.master_sync_in_sec - 1e-3 <= t <= s.master_sync_out_sec + 1e-3:
            return s.timeline_in_sec + (t - s.master_sync_in_sec)
    return None if state.rough_cut_shots else t


def _free(window: tuple[float, float], occupied: list[tuple[float, float]], gap: float = 0.5) -> bool:
    return all(window[1] + gap <= a or window[0] >= b + gap for a, b in occupied)


def plan_topic_cards(state: Any, occupied: list[tuple[float, float]]) -> list[dict[str, Any]]:
    from bangonpro.ai_engine.department.editorial_pacing import format_key

    rules = state.profile.get("graphics_rules", {}) or {}
    if not bool(rules.get("topic_title_cards", format_key(state.profile) == "daily_news")):
        return []
    dur = float(min(6.0, max(2.5, float(rules.get("topic_card_duration_sec", 4.0)))))
    cards = []
    for sec in (state.story_sections or [])[1:]:
        t = _to_edited(state, sec.start_sec)
        if t is None:
            continue
        seg = next((s for s in state.transcript_segments if s.is_neural_inference and s.start_sec >= sec.start_sec - 1e-3), None)
        headline = et.first_sentence(seg.text) if seg is not None else ""
        if len(headline) > 48:
            headline = headline[:48].rsplit(" ", 1)[0] + "…"
        title = headline or sec.label
        win = (t + 0.3, t + 0.3 + dur)
        if _free(win, occupied):
            cards.append({"start_sec": round(win[0], 3), "duration_sec": dur, "title": title, "subtitle": sec.label if headline else "", "section_id": sec.section_id})
            occupied.append(win)
    return cards


def plan_lower_thirds(state: Any, occupied: list[tuple[float, float]] | None = None) -> list[dict[str, Any]]:
    """Context-aware lower-third windows on the edited timeline (dict rows; see module doc)."""
    from bangonpro.ai_engine.department.editorial_pacing import format_key
    from bangonpro.show_profiles.loader import get_effective_profile_settings

    rules = state.profile.get("graphics_rules", {}) or {}
    target = float(rules.get("lower_third_duration_sec", 4.5))
    lt_min = float(rules.get("lower_third_min_sec", 3.0))
    lt_max = float(rules.get("lower_third_max_sec", 8.0))
    min_speech = float(rules.get("lower_third_min_speech_sec", 2.5))
    reshow = float(rules.get("lower_third_reshow_after_sec", RESHOW_DEFAULT.get(format_key(state.profile), 900.0)))
    dur_target = min(lt_max, max(lt_min, target))
    occupied = list(occupied or [])
    eff = get_effective_profile_settings(state.profile)
    spk_map = dict((state.metadata.get("camera_plan") or {}).get("speaker_camera_map") or eff.get("speaker_camera_map") or {})
    roles = {**eff.get("camera_roles", {}), **(state.profile.get("camera_roles") or {})}
    names = _speaker_names(state)
    brief_names = names_from_brief(str(getattr(state, "instruction", "") or ""), [s.speaker_id for s in (state.transcript_segments or [])])
    targets = state.intent.target_speakers
    segs = sorted((s for s in state.transcript_segments if not s.deleted), key=lambda s: s.start_sec)
    crosstalk = [(s.start_sec, s.end_sec) for s in segs if s.is_overlap]
    shots = state.rough_cut_shots
    total = shots[-1].timeline_out_sec if shots else max((s.end_sec for s in segs), default=0.0)

    def matches(spk: str) -> bool:
        low = spk.lower()
        return any(t.lower() in low or low in t.lower() for t in targets)

    # A target that names no real speaker (e.g. the job title "Host" in "lower third: Jane Doe, Host") must not
    # silently remove every graphic: the filter applies only when it matches someone in the programme.
    if targets and not any(matches(s.speaker_id) for s in segs):
        state.metadata["lower_third_target_ignored"] = list(targets)
        targets = []

    def allowed(spk: str) -> bool:
        return not targets or matches(spk)

    def windows(spk: str, seg: Any, tier: int) -> tuple[float, float] | None:
        """Edited-time window for ``spk`` during ``seg`` (tier 1 own angle, 2 own or wide, 3 any)."""
        if any(min(seg.end_sec, b) - max(seg.start_sec, a) > 0 for a, b in crosstalk):
            return None
        if not shots:
            a, b = seg.start_sec + 0.3, min(total, seg.start_sec + 0.3 + dur_target)
            return (a, b) if b - a >= lt_min - 1e-3 else None
        cam = spk_map.get(spk)
        best = None
        run_s = run_e = None
        for sh in shots:
            if sh.master_sync_out_sec <= seg.start_sec or sh.master_sync_in_sec >= seg.end_sec + dur_target:
                if run_s is not None and sh.master_sync_in_sec >= seg.end_sec + dur_target:
                    break
                continue
            good = tier == 3 or sh.camera_id == cam or (tier == 2 and roles.get(sh.camera_id) == "wide")
            if good:
                run_s = sh.timeline_in_sec if run_s is None else run_s
                run_e = sh.timeline_out_sec
            else:
                if run_s is not None:
                    break
        if run_s is not None:
            seg_t = _to_edited(state, seg.start_sec)
            a = max(run_s + 0.3, (seg_t or run_s) + 0.2)
            b = min(run_e - 0.2, a + dur_target, total - 0.1)
            if b - a >= lt_min - 1e-3:
                best = (a, b)
        return best

    out: list[dict[str, Any]] = []
    last_seen: dict[str, float] = {}
    shown_at: dict[str, float] = {}
    for tier in (1, 2, 3):
        for seg in segs:
            spk = seg.speaker_id
            if not allowed(spk):
                continue
            prev_end = last_seen.get(spk)
            due = spk not in shown_at or (reshow > 0 and prev_end is not None and seg.start_sec - prev_end >= reshow and shown_at[spk] < seg.start_sec)
            last_seen[spk] = max(last_seen.get(spk, 0.0), seg.end_sec)
            if not due or (tier == 1 and (seg.end_sec - seg.start_sec) < min_speech):
                continue
            win = windows(spk, seg, tier)
            if win is None or not _free(win, occupied):
                continue
            info = brief_names.get(spk) or names.get(spk, {})  # the editor's own words win over generic labels
            if not _real_name(info.get("name"), spk):
                # V1.6: never print an internal id ("cam_wide", "SPEAKER_00") as a person's name.
                state.metadata.setdefault("lower_thirds_withheld", [])
                if spk not in state.metadata["lower_thirds_withheld"]:
                    state.metadata["lower_thirds_withheld"].append(spk)
                continue
            out.append(
                {
                    "speaker_id": spk,
                    "title": info.get("name") or spk,
                    "subtitle": info.get("title") or "",
                    "start_sec": round(win[0], 3),
                    "duration_sec": round(win[1] - win[0], 3),
                    "master_sec": round(seg.start_sec, 3),
                    "placement": {1: "first substantive appearance on own angle", 2: "on own angle or the wide", 3: "fallback: speaker may not be on screen"}[tier] + ("" if spk not in shown_at else " (re-shown after absence)"),
                    "tier": tier,
                }
            )
            occupied.append(win)
            shown_at[spk] = seg.start_sec
        last_seen = {}
        if all(s.speaker_id in shown_at or not allowed(s.speaker_id) for s in segs):
            break
    return sorted(out, key=lambda r: r["start_sec"])


__all__ = [
    "ACTION_SAFE",
    "TEMPLATES",
    "TITLE_SAFE",
    "geometry",
    "get_template",
    "plan_lower_thirds",
    "plan_topic_cards",
    "register_template",
    "validate_graphic",
]
