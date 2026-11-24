"""Natural-language production instruction parser for Bang On Pro.

Translates studio commands such as:
    "Sync all four cameras, remove setup delays, build a first cut and add our standard graphics"
into a structured ``ProductionIntent`` controlling orchestrator stages.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import re

from bangonpro.ai_engine.state import ProductionIntent

_PROFILE_HINTS: list[tuple[str, str]] = [
    (r"\b(game\s*show|panel|contestant|quiz)\b", "gameshow_multicam"),
    (r"\b(sport|sports|match|debate|football|analyst)\b", "sports_discussion"),
    (r"\b(news|bulletin|anchor|headline|daily\s+news)\b", "daily_news"),
    (r"\b(podcast|interview|episode|talk\s*show)\b", "podcast_multicam"),
]


def parse_production_instruction(
    instruction: str,
    *,
    export_override: bool | None = None,
) -> ProductionIntent:
    """Parse a natural-language editing request into structured stage flags."""
    raw = (instruction or "").strip()
    lower = raw.lower()

    if not raw:
        intent = ProductionIntent(
            raw_instruction="",
            export_video=bool(export_override),
            parsed_actions=["default_full_production_pipeline"],
        )
        return intent

    parsed_actions: list[str] = []

    def _has(pattern: str) -> bool:
        return bool(re.search(pattern, lower))

    # Negation detection so users can say "no captions" or "skip graphics"
    no_captions = _has(r"\b(no|without|skip|disable)\s+(captions?|subtitles?)\b")
    no_graphics = _has(r"\b(no|without|skip|disable)\s+(graphics?|lower\s*thirds?|titles?)\b")
    no_silence = _has(r"\b(keep|preserve|no|without|skip)\s+(silence|dead\s*air|pauses?)\b")
    no_setup = _has(r"\b(keep|preserve|no|without|skip)\s+(setup|delays?|leader)\b")
    no_audio = _has(r"\b(no|without|skip)\s+(audio\s+restor|denoise|loudness|audio\s+clean)\b")

    sync_mentioned = _has(r"\b(sync|synchroni[sz]e|align|multicam|cameras?)\b")
    setup_mentioned = _has(r"\b(setup|delay|trim|leader|slate|intro\s+wait)\b")
    silence_mentioned = _has(r"\b(silence|dead\s*air|pauses?|tight|trim|first\s*cut|rough\s*cut)\b")
    cut_mentioned = _has(r"\b(first\s*cut|rough\s*cut|cut|edit|assemble|switch|multicam)\b")
    graphics_mentioned = _has(r"\b(graphics?|lower\s*thirds?|titles?|brand|standard\s+graphics)\b")
    captions_mentioned = _has(r"\b(captions?|subtitles?|srt|ass|transcript)\b")
    audio_mentioned = _has(r"\b(audio|sound|loudness|lufs|denoise|clean|restore|mix)\b")
    export_mentioned = _has(r"\b(export|render|mp4|deliver|master)\b")

    if sync_mentioned:
        parsed_actions.append("sync_cameras")
    if setup_mentioned and not no_setup:
        parsed_actions.append("remove_setup_delay")
    if silence_mentioned and not no_silence:
        parsed_actions.append("remove_silence")
    if cut_mentioned:
        parsed_actions.append("build_rough_cut")
    if audio_mentioned and not no_audio:
        parsed_actions.append("restore_audio")
    if captions_mentioned and not no_captions:
        parsed_actions.append("add_captions")
    if graphics_mentioned and not no_graphics:
        parsed_actions.append("add_lower_thirds")
    if export_mentioned:
        parsed_actions.append("export_video")

    # Detect profile hint if mentioned in prose
    profile_hint: str | None = None
    for pattern, profile_id in _PROFILE_HINTS:
        if _has(pattern):
            profile_hint = profile_id
            break

    # "standard graphics" implies both lower thirds and captions unless negated
    add_lower_thirds = not no_graphics and (graphics_mentioned or cut_mentioned or not parsed_actions)
    add_captions = not no_captions and (captions_mentioned or graphics_mentioned or cut_mentioned or not parsed_actions)

    export_video = export_mentioned if export_override is None else bool(export_override)

    if not parsed_actions:
        parsed_actions.append("full_autonomous_assembly")

    return ProductionIntent(
        raw_instruction=raw,
        sync_cameras=True if (sync_mentioned or cut_mentioned or not parsed_actions) else True,
        remove_setup_delay=not no_setup,
        remove_silence=not no_silence,
        build_rough_cut=True,
        multicam_switch=True,
        restore_audio=not no_audio,
        add_captions=add_captions,
        add_lower_thirds=add_lower_thirds,
        run_qc=True,
        export_video=export_video,
        profile_hint=profile_hint,
        parsed_actions=parsed_actions,
    )
