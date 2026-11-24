"""Natural-language production instruction parser for Bang On Pro.

Translates studio commands into a strictly scoped ``ProductionIntent`` controlling
orchestrator stages and AI command planning. Never enables unsolicited mutating
stages: unknown or ambiguous prompts return ``needs_clarification=True`` with all
mutating flags set to ``False``.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import re

from dataclasses import dataclass

from bangonpro.ai_engine.state import ProductionIntent


@dataclass(frozen=True)
class _EditorialTargets:
    target_clips: list[str]
    target_tracks: list[str]
    time_range_sec: tuple[float, float] | None
    target_speakers: list[str]
    loudness_target_lufs: float | None
    silence_threshold_db: float | None
    min_pause_sec: float | None


_PROFILE_HINTS: list[tuple[str, str]] = [
    (r"\b(game\s*show|panel|contestant|quiz)\b", "gameshow_multicam"),
    (r"\b(sport|sports|match|debate|football|analyst)\b", "sports_discussion"),
    (r"\b(news|bulletin|anchor|headline|daily\s+news)\b", "daily_news"),
    (r"\b(podcast|interview|episode|talk\s*show)\b", "podcast_multicam"),
]

_SUPPORTED_ACTIONS_HELP = (
    "Supported editing actions include: "
    "sync cameras, build a multicam rough/first cut, remove setup delays, "
    "remove silence/dead air (with optional dB threshold & minimum pause duration), "
    "clean up/denoise/normalize audio (e.g. -16 LUFS), transcribe speakers, "
    "generate captions/subtitles, add lower-third graphics, run technical QC, "
    "or export/render video."
)

_TIME_TOKEN_RE = r"(?:\d{1,2}:\d{2}:\d{2}(?:\.\d+)?|\d{1,2}:\d{2}(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:m\s*\d+(?:\.\d+)?\s*s?|min(?:ute)?s?|s|sec(?:ond)?s?))"


def _parse_time_token(token: str) -> float | None:
    """Parse ``HH:MM:SS``, ``MM:SS``, ``1m30s``, or ``45.5s`` into seconds."""
    cleaned = token.strip().lower()
    if not cleaned:
        return None
    if ":" in cleaned:
        parts = cleaned.split(":")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return None
        if len(nums) == 2:
            return nums[0] * 60.0 + nums[1]
        if len(nums) == 3:
            return nums[0] * 3600.0 + nums[1] * 60.0 + nums[2]
        return None

    m_min_sec = re.fullmatch(r"(\d+(?:\.\d+)?)\s*m\s*(\d+(?:\.\d+)?)\s*s?", cleaned)
    if m_min_sec:
        return float(m_min_sec.group(1)) * 60.0 + float(m_min_sec.group(2))

    m_min = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:m|min|mins|minute|minutes)", cleaned)
    if m_min:
        return float(m_min.group(1)) * 60.0

    m_sec = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)?", cleaned)
    if m_sec:
        return float(m_sec.group(1))

    return None


def _dedupe_preserve(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _extract_editorial_targets(raw: str) -> _EditorialTargets:
    """Extract structured editorial targets and numeric thresholds from prose."""
    lower = raw.lower()

    # 1. Target tracks: "V1", "V2", "A1", "A2", "video track 1", "audio track 2"
    tracks: list[str] = []
    for m in re.finditer(r"\b([VvAa]\d+)\b", raw):
        tracks.append(m.group(1).upper())
    for m in re.finditer(r"\bvideo\s+track\s+(\d+)\b", lower):
        tracks.append(f"V{m.group(1)}")
    for m in re.finditer(r"\baudio\s+track\s+(\d+)\b", lower):
        tracks.append(f"A{m.group(1)}")
    target_tracks = _dedupe_preserve(tracks)

    # 2. Target clips: explicit clip/shot/cam IDs or media filenames
    clips: list[str] = []
    for m in re.finditer(r"\bclip[_\s]+['\"]?([A-Za-z0-9_.\-]+)['\"]?", raw, re.IGNORECASE):
        cand = m.group(1)
        if cand.lower() not in {"on", "in", "to", "from", "and", "the", "a", "for"}:
            clips.append(cand)
    for m in re.finditer(r"\b(shot_\d+|cam_[A-Za-z0-9_]+|clip_[A-Za-z0-9_]+)\b", raw, re.IGNORECASE):
        clips.append(m.group(1))
    for m in re.finditer(
        r"\b([A-Za-z0-9_\-]+\.(?:mp4|mov|mxf|mkv|m4v|wav|mp3|m4a|flac|aac))\b",
        raw,
        re.IGNORECASE,
    ):
        clips.append(m.group(1))
    for m in re.finditer(r"\bcamera\s+(\d+)\b", lower):
        clips.append(f"camera_{m.group(1)}")
    target_clips = _dedupe_preserve(clips)

    # 3. Time range: "from 0:30 to 1:15", "between 10s and 45s", "0:30-1:15"
    time_range_sec: tuple[float, float] | None = None
    range_patterns = [
        rf"\bfrom\s+({_TIME_TOKEN_RE})\s+(?:to|until|through|-|–)\s+({_TIME_TOKEN_RE})\b",
        rf"\bbetween\s+({_TIME_TOKEN_RE})\s+and\s+({_TIME_TOKEN_RE})\b",
        r"\b(\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?)\s*(?:-|–|to)\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?)\b",
    ]
    for pat in range_patterns:
        range_match = re.search(pat, lower)
        if range_match:
            t0 = _parse_time_token(range_match.group(1))
            t1 = _parse_time_token(range_match.group(2))
            if t0 is not None and t1 is not None and t1 > t0:
                time_range_sec = (round(t0, 3), round(t1, 3))
                break

    # 4. Target speakers: "Speaker 1", "Speaker A", "Host", "Guest", "Panel", "Contestant", "Anchor"
    speakers: list[str] = []
    for m in re.finditer(r"\b(speaker\s+[A-Za-z0-9]+)\b", raw, re.IGNORECASE):
        parts = m.group(1).split()
        speakers.append(f"Speaker {parts[1].upper() if len(parts[1]) == 1 else parts[1]}")
    for role_word, canonical in (
        ("host", "Host"),
        ("guest", "Guest"),
        ("panelist", "Panel"),
        ("panel", "Panel"),
        ("contestant", "Contestant"),
        ("anchor", "Anchor"),
    ):
        if re.search(rf"\b{role_word}\b", lower):
            speakers.append(canonical)
    target_speakers = _dedupe_preserve(speakers)

    # 5. Loudness target in LUFS (e.g., "-16 LUFS", "-14.0 lufs", "-23 LUFS")
    loudness_target_lufs: float | None = None
    m_lufs = re.search(r"(-?\d+(?:\.\d+)?)\s*lufs\b", lower)
    if m_lufs:
        val = float(m_lufs.group(1))
        if val > 0:
            val = -val
        if -40.0 <= val <= -6.0:
            loudness_target_lufs = round(val, 2)

    # 6. Silence threshold in dB / dBFS (e.g., "below -40 dB", "silence threshold -38 dBFS")
    silence_threshold_db: float | None = None
    for pat in (
        r"(?:silence\s+(?:threshold|below|under|at)|below|under|threshold(?:\s+of|\s+at)?)\s*(-?\d+(?:\.\d+)?)\s*d(?:b|bfs)\b",
        r"(-?\d+(?:\.\d+)?)\s*d(?:b|bfs)\s+(?:silence|threshold|gate)",
    ):
        m_db = re.search(pat, lower)
        if m_db:
            val = float(m_db.group(1))
            if val > 0:
                val = -val
            if -90.0 <= val <= 0.0:
                silence_threshold_db = round(val, 2)
                break

    # 7. Minimum pause / silence duration in seconds
    min_pause_sec: float | None = None
    for pat in (
        r"(?:pauses?|silences?|gaps?|dead\s*air)\s+(?:longer\s+than|over|greater\s+than|exceeding|above|>=?|>)\s*(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)\b",
        r"min(?:imum)?\s+(?:pause|silence|gap)(?:\s+duration)?\s*(?:of|=|:)?\s*(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)\b",
    ):
        m_pause = re.search(pat, lower)
        if m_pause:
            val = float(m_pause.group(1))
            if 0.05 <= val <= 60.0:
                min_pause_sec = round(val, 3)
                break

    return _EditorialTargets(
        target_clips=target_clips,
        target_tracks=target_tracks,
        time_range_sec=time_range_sec,
        target_speakers=target_speakers,
        loudness_target_lufs=loudness_target_lufs,
        silence_threshold_db=silence_threshold_db,
        min_pause_sec=min_pause_sec,
    )


def _classify_scope(
    *,
    sync_cameras: bool,
    transcribe: bool,
    remove_setup_delay: bool,
    remove_silence: bool,
    build_rough_cut: bool,
    multicam_switch: bool,
    restore_audio: bool,
    add_captions: bool,
    add_lower_thirds: bool,
    run_qc: bool,
    export_video: bool,
) -> str:
    if build_rough_cut or multicam_switch:
        return "full_production"
    active = {
        name
        for name, flag in (
            ("sync", sync_cameras),
            ("setup", remove_setup_delay),
            ("silence", remove_silence),
            ("audio", restore_audio),
            ("captions", add_captions),
            ("graphics", add_lower_thirds),
            ("transcribe", transcribe and not add_captions),
            ("qc", run_qc),
            ("export", export_video),
        )
        if flag
    }
    if active == {"audio"}:
        return "audio_only"
    if active == {"captions"}:
        return "captions_only"
    if active == {"silence"}:
        return "silence_only"
    if active == {"setup"}:
        return "setup_trim_only"
    if active == {"graphics"}:
        return "graphics_only"
    if active == {"sync"}:
        return "sync_only"
    if active == {"transcribe"}:
        return "transcribe_only"
    if active == {"qc"}:
        return "qc_only"
    if active == {"export"}:
        return "export_only"
    return "targeted_edit"


def parse_production_instruction(
    instruction: str,
    *,
    export_override: bool | None = None,
    allow_default_empty: bool = False,
) -> ProductionIntent:
    """Parse a natural-language editing request into strictly scoped stage flags.

    - Empty instructions mark ``needs_clarification=True`` with all mutating flags
      ``False`` unless ``allow_default_empty=True`` is explicitly passed.
    - Unrecognized or ambiguous instructions (e.g. ``"make it pop"``, ``"asdfgh"``)
      mark ``needs_clarification=True`` and leave ALL mutating flags ``False``.
    - Scoped instructions (audio-only, captions-only, silence-only, graphics-only)
      enable ONLY the requested operations and never trigger unsolicited edits.
    """
    raw = (instruction or "").strip()
    lower = raw.lower()

    if not raw:
        if allow_default_empty:
            return ProductionIntent.full_production(
                raw_instruction="",
                export_video=bool(export_override),
                parsed_actions=["default_full_production_pipeline"],
            )
        return ProductionIntent(
            raw_instruction="",
            needs_clarification=True,
            clarification_question=(
                "No editing instruction was provided. What would you like Bang On Pro to do? "
                + _SUPPORTED_ACTIONS_HELP
            ),
            parsed_actions=[],
            intent_confidence=0.0,
            scope="clarification",
        )

    def _has(pattern: str) -> bool:
        return bool(re.search(pattern, lower))

    targets = _extract_editorial_targets(raw)

    # Detect profile hint if mentioned in prose
    profile_hint: str | None = None
    for pattern, profile_id in _PROFILE_HINTS:
        if _has(pattern):
            profile_hint = profile_id
            break

    # Negation detection so users can explicitly skip stages
    no_captions = _has(
        r"\b(no|without|skip|disable|omit|don'?t\s+add)\s+(captions?|subtitles?|srt|ass)\b"
    )
    no_graphics = _has(
        r"\b(no|without|skip|disable|omit|don'?t\s+add)\s+(graphics?|lower[\s-]*thirds?|titles?|overlays?)\b"
    )
    no_silence = _has(
        r"\b(keep|preserve|no|without|skip|don'?t\s+(?:remove|trim|cut))\s+(silence|dead\s*air|pauses?)\b"
    )
    no_setup = _has(
        r"\b(keep|preserve|no|without|skip|don'?t\s+(?:remove|trim))\s+(setup(?:\s+delays?)?|delays?|leader|slate)\b"
    )
    no_audio = _has(
        r"\b(no|without|skip|disable|don'?t\s+(?:clean|restore|normalize|denoise|process))\s+"
        r"(audio(?:\s+restor\w*|\s+clean\w*|\s+process\w*)?|denoise|loudness|normalization|audio\s+mix)\b"
    )
    no_multicam = _has(
        r"\b(no|without|skip|disable|single[\s-]*cam(?:era)?)\s+(multicam|camera\s+switch\w*|angle\s+switch\w*)\b"
    )
    no_sync = _has(r"\b(no|without|skip|don'?t)\s+(sync|synchroni[sz]e|camera\s+sync)\b")

    # Positive explicit action detection (strictly avoiding over-broad triggers)
    sync_mentioned = _has(
        r"\b(sync|synchroni[sz]e|align\s+(?:all\s+)?(?:the\s+)?(?:cameras?|angles?|clips?|audio|tracks?))\b"
    )
    setup_mentioned = _has(
        r"\b(setup\s*delays?|pre[\s-]*roll|leader|slate|intro\s+wait|"
        r"(?:trim|remove)\s+(?:the\s+)?setup)\b"
    )
    silence_mentioned = _has(
        r"\b(silence|dead\s*air|long\s+pauses?|pauses?|tighten\s+(?:pacing|pauses)|"
        r"(?:remove|trim|cut)\s+(?:the\s+)?(?:silence|dead\s*air|pauses?|gaps?))\b"
    )
    cut_mentioned = _has(
        r"\b(first\s*cut|rough\s*cut|broadcast\s+cut|"
        r"(?:build|create|make|assemble)\s+(?:a\s+|an\s+|the\s+)?(?:balanced\s+|daily\s+|podcast\s+|gameshow\s+|sports\s+|multicam\s+)*(?:first\s*cut|rough\s*cut|broadcast\s+cut|cut|edit|bulletin|sequence|episode)|"
        r"assemble\b|full\s+(?:production|assembly|edit)|cut\s+fast\s+reactions?)\b"
    )
    multicam_mentioned = _has(
        r"\b(multicam|multi[\s-]*cam(?:era)?|camera\s+switch\w*|angle\s+switch\w*|"
        r"switch\s+(?:cameras?|angles?)|cut\s+between\s+cameras?|fast\s+reactions?)\b"
    )
    standard_graphics_mentioned = _has(r"\bstandard\s+graphics\b")
    graphics_mentioned = _has(
        r"\b(lower[\s-]*thirds?|graphics?|speaker\s+titles?|name\s+titles?|chyrons?|overlays?)\b"
    )
    captions_mentioned = _has(r"\b(captions?|subtitles?|srt|ass)\b")
    transcribe_mentioned = _has(r"\b(transcrib\w+|transcript|diaris\w+|diariz\w+)\b")
    audio_mentioned = _has(
        r"\b(clean\s*(?:up)?\s+(?:the\s+)?audio|restore\s+(?:the\s+)?audio|"
        r"master\s+(?:the\s+)?audio|audio\s+(?:clean\w*|restor\w*|mix\w*|level\w*|master\w*|norm\w*)|"
        r"denoise|de[\s-]*noise|noise\s+reduc\w*|remove\s+(?:background\s+|room\s+)?(?:noise|hum|rumble)|"
        r"normali[sz]e|loudness|lufs|compress(?:or|ion)?|duck\s+music|level\s+(?:the\s+)?(?:audio|mics?|microphones?))\b"
    )
    qc_mentioned = _has(
        r"\b(qc|quality\s+control|quality\s+check|check\s+(?:for\s+)?(?:black\s+gaps?|clipping|sync|loudness))\b"
    )
    export_mentioned = _has(r"\b(export|render|mp4|deliver|render\s+master)\b")

    # Compute strictly scoped boolean flags
    build_rough_cut = bool(cut_mentioned)
    multicam_switch = bool(
        (multicam_mentioned or build_rough_cut) and not no_multicam
    )
    sync_cameras = bool(
        (sync_mentioned or (build_rough_cut and not no_sync) or (multicam_mentioned and not no_sync))
        and not no_sync
    )
    remove_setup_delay = bool((setup_mentioned or build_rough_cut) and not no_setup)
    remove_silence = bool((silence_mentioned or build_rough_cut) and not no_silence)
    restore_audio = bool((audio_mentioned or build_rough_cut) and not no_audio)
    add_lower_thirds = bool(
        (graphics_mentioned or standard_graphics_mentioned) and not no_graphics
    )
    add_captions = bool(
        (captions_mentioned or (standard_graphics_mentioned and build_rough_cut))
        and not no_captions
    )
    transcribe = bool(
        transcribe_mentioned or add_captions or build_rough_cut or multicam_switch
    )
    run_qc = bool(qc_mentioned or build_rough_cut)
    export_video = export_mentioned if export_override is None else bool(export_override)

    # Check if ANY concrete recognized action was requested in the instruction text
    any_recognized_action = any(
        (
            sync_mentioned and not no_sync,
            setup_mentioned and not no_setup,
            silence_mentioned and not no_silence,
            cut_mentioned,
            multicam_mentioned and not no_multicam,
            audio_mentioned and not no_audio,
            captions_mentioned and not no_captions,
            transcribe_mentioned,
            (graphics_mentioned or standard_graphics_mentioned) and not no_graphics,
            qc_mentioned,
            export_mentioned,
        )
    )

    if not any_recognized_action:
        return ProductionIntent(
            raw_instruction=raw,
            needs_clarification=True,
            clarification_question=(
                f"Could not map '{raw}' to a specific editing action without guessing. "
                + _SUPPORTED_ACTIONS_HELP
            ),
            profile_hint=profile_hint,
            parsed_actions=[],
            target_clips=list(targets.target_clips),
            target_tracks=list(targets.target_tracks),
            time_range_sec=targets.time_range_sec,
            target_speakers=list(targets.target_speakers),
            loudness_target_lufs=targets.loudness_target_lufs,
            silence_threshold_db=targets.silence_threshold_db,
            min_pause_sec=targets.min_pause_sec,
            intent_confidence=0.0,
            scope="clarification",
        )

    parsed_actions: list[str] = []
    if sync_cameras:
        parsed_actions.append("sync_cameras")
    if transcribe:
        parsed_actions.append("transcribe")
    if remove_setup_delay:
        parsed_actions.append("remove_setup_delay")
    if remove_silence:
        parsed_actions.append("remove_silence")
    if build_rough_cut:
        parsed_actions.append("build_rough_cut")
    if multicam_switch:
        parsed_actions.append("multicam_switch")
    if restore_audio:
        parsed_actions.append("restore_audio")
    if add_captions:
        parsed_actions.append("add_captions")
    if add_lower_thirds:
        parsed_actions.append("add_lower_thirds")
    if run_qc:
        parsed_actions.append("run_qc")
    if export_video:
        parsed_actions.append("export_video")

    scope = _classify_scope(
        sync_cameras=sync_cameras,
        transcribe=transcribe,
        remove_setup_delay=remove_setup_delay,
        remove_silence=remove_silence,
        build_rough_cut=build_rough_cut,
        multicam_switch=multicam_switch,
        restore_audio=restore_audio,
        add_captions=add_captions,
        add_lower_thirds=add_lower_thirds,
        run_qc=run_qc,
        export_video=export_video,
    )

    return ProductionIntent(
        raw_instruction=raw,
        sync_cameras=sync_cameras,
        transcribe=transcribe,
        remove_setup_delay=remove_setup_delay,
        remove_silence=remove_silence,
        build_rough_cut=build_rough_cut,
        multicam_switch=multicam_switch,
        restore_audio=restore_audio,
        add_captions=add_captions,
        add_lower_thirds=add_lower_thirds,
        run_qc=run_qc,
        export_video=export_video,
        needs_clarification=False,
        clarification_question="",
        profile_hint=profile_hint,
        parsed_actions=parsed_actions,
        target_clips=list(targets.target_clips),
        target_tracks=list(targets.target_tracks),
        time_range_sec=targets.time_range_sec,
        target_speakers=list(targets.target_speakers),
        loudness_target_lufs=targets.loudness_target_lufs,
        silence_threshold_db=targets.silence_threshold_db,
        min_pause_sec=targets.min_pause_sec,
        intent_confidence=0.95,
        scope=scope,
    )
