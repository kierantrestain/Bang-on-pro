"""Audio Restoration, Captions (.srt/.ass), Lower-Third Graphics & Technical QC Agent.

Builds real MLT + FFmpeg audio restoration filter chains honoring Show Profile
``audio_rules``, ``loudness_target_lufs``, and user intent overrides; maps
transcripts onto timelines to emit ``.srt`` and ``.ass`` captions honoring
``caption_style``; generates Kdenlive lower-third title XML clips honoring
``graphics_templates``; runs technical QC including ``delivery_spec`` checks;
and emits validated Timeline Command API v1 batches strictly scoped to user intent.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import itertools
import json
import textwrap
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any
from xml.sax import saxutils

import numpy as np

from bangonpro.ai_engine.commands import TimelineCommandBuilder
from bangonpro.ai_engine.orchestrator import CancellationToken
from bangonpro.ai_engine.state import (
    AudioFilterSpec,
    CaptionCue,
    LowerThirdSpec,
    ProjectAnalysisState,
    QCIssue,
    QCReport,
)
from bangonpro.show_profiles.loader import get_effective_profile_settings

_ASS_ALIGNMENT_MAP: dict[str, int] = {
    "bottom_left": 1,
    "bottom_center": 2,
    "bottom_right": 3,
    "top_center": 8,
}


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    cleaned = hex_color.strip().lstrip("#")
    if len(cleaned) != 6:
        return (255, 90, 31)
    try:
        return (int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16))
    except ValueError:
        return (255, 90, 31)


def _hex_to_ass_color(hex_color: str, alpha: int = 0) -> str:
    """Convert ``#RRGGBB`` to ASS ``&HAABBGGRR`` format."""
    r, g, b = _hex_to_rgb(hex_color)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _format_srt_timestamp(seconds: float) -> str:
    ms_total = max(0, round(seconds * 1000.0))
    hours, rem = divmod(ms_total, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _format_ass_timestamp(seconds: float) -> str:
    cs_total = max(0, round(seconds * 100.0))
    hours, rem = divmod(cs_total, 3600_00)
    minutes, rem = divmod(rem, 60_00)
    secs, cs = divmod(rem, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _wrap_caption_text(
    text: str,
    *,
    max_chars_per_line: int = 42,
    uppercase: bool = False,
) -> str:
    cleaned = " ".join((text or "").strip().split())
    if uppercase:
        cleaned = cleaned.upper()
    if max_chars_per_line > 0 and len(cleaned) > max_chars_per_line:
        if len(cleaned) <= 2 * max_chars_per_line:
            # V1.2.2: balanced two-line break (even lengths, never ending a line on an article)
            from bangonpro.ai_engine.department.editorial_captions import balanced_wrap

            return "\n".join(balanced_wrap(cleaned, max_chars_per_line, 2))
        return "\n".join(textwrap.wrap(cleaned, width=max_chars_per_line))
    return cleaned


def build_audio_restoration_chain(
    audio_rules: dict[str, Any],
    *,
    rnnoise_model_path: str | None = None,
    target_track_id: str = "A1",
    loudness_override_lufs: float | None = None,
    include_music_duck: bool = True,
    measured: dict[str, Any] | None = None,
) -> tuple[list[AudioFilterSpec], str]:
    """Construct real MLT + FFmpeg audio restoration chain from Show Profile ``audio_rules``.

    ``measured`` (V1.2.2 Audio Engineer, optional) carries the measured compressor threshold/ratio
    so dynamics follow the material; without it the V1.1 fixed chain is produced unchanged.
    """
    target_lufs = float(
        loudness_override_lufs
        if loudness_override_lufs is not None
        else audio_rules.get(
            "loudness_target_lufs", audio_rules.get("target_lufs", -16.0)
        )
    )
    true_peak = float(audio_rules.get("true_peak_dbtp", -1.5))
    highpass_hz = float(audio_rules.get("highpass_hz", 80.0))
    enable_denoise = bool(audio_rules.get("enable_denoise", True))
    enable_compressor = bool(audio_rules.get("enable_compressor", True))
    music_duck_db = float(audio_rules.get("music_duck_db", -18.0))
    track_slug = target_track_id.lower()

    filters: list[AudioFilterSpec] = [
        AudioFilterSpec(
            filter_id=f"flt_{track_slug}_highpass",
            mlt_service="avfilter.highpass",
            mlt_params={"av.f": highpass_hz},
            ffmpeg_filter=f"highpass=f={int(highpass_hz) if highpass_hz.is_integer() else highpass_hz}",
            track_id=target_track_id,
            enabled=True,
            rationale=(
                f"Remove low-frequency studio rumble and mic stand handling noise (<{highpass_hz:.0f} Hz)"
            ),
        )
    ]

    if enable_denoise:
        if rnnoise_model_path and Path(rnnoise_model_path).is_file():
            model_abs = str(Path(rnnoise_model_path).resolve())
            filters.append(
                AudioFilterSpec(
                    filter_id=f"flt_{track_slug}_rnnoise",
                    mlt_service="avfilter.arnndn",
                    mlt_params={"av.model": model_abs},
                    ffmpeg_filter=f"arnndn=m='{model_abs}'",
                    track_id=target_track_id,
                    enabled=True,
                    rationale="Apply RNNoise neural voice denoiser using configured model",
                )
            )
        else:
            filters.append(
                AudioFilterSpec(
                    filter_id=f"flt_{track_slug}_denoise",
                    # V1.2: spectral avfilter.afftdn is excluded by Kdenlive and avfilter.afwtdn buffers frames
                    # (MLT "Unexpected return format"). Without an RNNoise model we use a frame-accurate
                    # downward expander (avfilter.compand) that lowers the room-noise floor between words.
                    mlt_service="avfilter.compand",
                    mlt_params={
                        "av.attacks": 0.005,
                        "av.decays": 0.25,
                        "av.points": "-90/-110|-55/-75|-45/-45|0/0",
                        "av.soft-knee": 6.0,
                        "av.gain": 0.0,
                    },
                    ffmpeg_filter="compand=attacks=0.005:decays=0.25:points=-90/-110|-55/-75|-45/-45|0/0:soft-knee=6",
                    track_id=target_track_id,
                    enabled=True,
                    rationale="Reduce room-noise floor between words with a downward expander (non-neural; configure an RNNoise model for neural denoise)",
                )
            )

    if enable_compressor and measured and measured.get("compressor_points"):
        pts = str(measured["compressor_points"])
        gain = float(measured.get("compressor_gain_db", 0.0))
        filters.append(
            AudioFilterSpec(
                filter_id=f"flt_{track_slug}_compressor",
                mlt_service="avfilter.compand",
                mlt_params={"av.attacks": 0.01, "av.decays": 0.15, "av.points": pts, "av.soft-knee": 6.0, "av.gain": gain},
                ffmpeg_filter=f"compand=attacks=0.01:decays=0.15:points={pts}:soft-knee=6:gain={gain:g}",
                track_id=target_track_id,
                enabled=True,
                rationale=str(measured.get("compressor_rationale") or "Measured dialogue compression"),
            )
        )
    elif enable_compressor:
        filters.append(
            AudioFilterSpec(
                filter_id=f"flt_{track_slug}_compressor",
                # V1.2: avfilter.acompressor is excluded by Kdenlive; avfilter.compand is supported & editable.
                # ~3:1 above -24 dBFS with +3 dB makeup, 10 ms attack / 120 ms release.
                mlt_service="avfilter.compand",
                mlt_params={
                    "av.attacks": 0.01,
                    "av.decays": 0.12,
                    "av.points": "-80/-80|-24/-24|0/-16",
                    "av.soft-knee": 6.0,
                    "av.gain": 3.0,
                },
                ffmpeg_filter="compand=attacks=0.01:decays=0.12:points=-80/-80|-24/-24|0/-16:soft-knee=6:gain=3",
                track_id=target_track_id,
                enabled=True,
                rationale="Level studio vocal dynamics across multiple microphones",
            )
        )

    filters.append(
        AudioFilterSpec(
            filter_id=f"flt_{track_slug}_loudnorm",
            # V1.2: avfilter.loudnorm is excluded by Kdenlive; MLT dynamic_loudness is the supported,
            # editable EBU R128 equivalent. Distribution verifies the rendered master and applies a
            # measured second-pass gain when needed.
            mlt_service="dynamic_loudness",
            mlt_params={
                "target_loudness": target_lufs,
                "window": 3.0,
                "max_gain": 15.0,
                "min_gain": -15.0,
                "max_rate": 3.0,
            },
            ffmpeg_filter=f"loudnorm=I={target_lufs:.1f}:TP={true_peak:.1f}:LRA=7",
            track_id=target_track_id,
            enabled=True,
            rationale=f"EBU R128 broadcast loudness normalization to {target_lufs:.1f} LUFS / {true_peak:.1f} dBTP",
        )
    )

    if include_music_duck and target_track_id == "A1":
        filters.append(
            AudioFilterSpec(
                filter_id="flt_a2_music_duck",
                mlt_service="volume",
                mlt_params={"level": music_duck_db, "gain": f"{music_duck_db:.1f}dB"},
                ffmpeg_filter=f"volume={music_duck_db:.1f}dB",
                track_id="A2",
                enabled=True,
                rationale=f"Duck background music bed to {music_duck_db:.1f} dB beneath dialogue",
            )
        )

    primary_ffmpeg_chain = ",".join(
        f.ffmpeg_filter for f in filters if f.track_id == target_track_id and f.enabled
    )
    return filters, primary_ffmpeg_chain


def generate_srt_subtitles(
    captions: list[CaptionCue],
    *,
    caption_style: dict[str, Any] | None = None,
) -> str:
    """Render edited-timeline captions as a strictly valid SubRip (.srt) document."""
    style = caption_style or {}
    max_chars = int(style.get("max_chars_per_line", 42))
    uppercase = bool(style.get("uppercase", False))

    blocks: list[str] = []
    for idx, cue in enumerate(captions, start=1):
        start_ts = _format_srt_timestamp(cue.start_sec)
        end_ts = _format_srt_timestamp(max(cue.end_sec, cue.start_sec + 0.1))
        text = _wrap_caption_text(
            cue.text.replace("\r\n", "\n"),
            max_chars_per_line=max_chars,
            uppercase=uppercase,
        )
        blocks.append(f"{idx}\n{start_ts} --> {end_ts}\n{text}\n")
    return "\n".join(blocks)


def generate_ass_subtitles(
    captions: list[CaptionCue],
    *,
    width: int = 1920,
    height: int = 1080,
    brand_accent_hex: str = "#FF5A1F",
    caption_style: dict[str, Any] | None = None,
) -> str:
    """Render edited-timeline captions as a strictly valid SubStation Alpha v4+ (.ass) document."""
    style = caption_style or {}
    font_family = str(style.get("font_family", "DejaVu Sans"))
    font_size = int(style.get("font_size_px", max(24, round(height * 0.044))))
    margin_v = max(28, round(height * 0.05))
    margin_h = max(36, round(width * 0.04))
    primary_ass = _hex_to_ass_color(str(style.get("primary_color_hex", "#FFFFFF")), alpha=0)
    accent_ass = _hex_to_ass_color(
        str(style.get("highlight_color_hex", brand_accent_hex)), alpha=0
    )
    outline_ass = _hex_to_ass_color(str(style.get("outline_color_hex", "#121212")), alpha=0)
    outline_w = float(style.get("outline_width", 3.0))
    align_code = _ASS_ALIGNMENT_MAP.get(str(style.get("alignment", "bottom_center")), 2)
    max_chars = int(style.get("max_chars_per_line", 42))
    uppercase = bool(style.get("uppercase", False))

    lines = [
        "[Script Info]",
        "; Generated by Bang On Pro AI Production Engine",
        "ScriptType: v4.00+",
        "Collisions: Normal",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font_family},{font_size},{primary_ass},{accent_ass},{outline_ass},&H90000000,1,0,0,0,100,100,0,0,1,{outline_w:g},1,{align_code},{margin_h},{margin_h},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    animated = bool(style.get("animated", False))
    for cue in captions:
        start_ts = _format_ass_timestamp(cue.start_sec)
        end_ts = _format_ass_timestamp(max(cue.end_sec, cue.start_sec + 0.1))
        safe_speaker = cue.speaker_id.replace(",", " ")
        wrapped = _wrap_caption_text(
            cue.text, max_chars_per_line=max_chars, uppercase=uppercase
        )
        safe_text = wrapped.replace("\n", "\\N")
        if animated:
            safe_text = f"{{\\fad(120,100)\\fscx92\\fscy92\\t(0,120,\\fscx100\\fscy100)}}{safe_text}"
        lines.append(
            f"Dialogue: 0,{start_ts},{end_ts},Default,{safe_speaker},0,0,0,,{safe_text}"
        )

    return "\n".join(lines) + "\n"


def generate_kdenlive_lower_third_xml(
    *,
    title: str,
    subtitle: str,
    width: int = 1920,
    height: int = 1080,
    accent_hex: str = "#FF5A1F",
    duration_frames: int = 100,
    position: str = "bottom_left",
    font_family: str = "DejaVu Sans",
    box_w_frac: float = 0.34,
    box_h_frac: float = 0.115,
    title_px_frac: float = 0.038,
    sub_px_frac: float = 0.024,
) -> str:
    """Generate native Kdenlive ``kdenlivetitle`` XML for a broadcast lower-third graphic.

    V1.2.2: geometry comes from the template registry (``editorial_graphics.TEMPLATES``) and
    ``center`` / ``top_center`` positions are supported (V1.2.1 drew a "center" title card in the
    bottom-left corner).
    """
    r, g, b = _hex_to_rgb(accent_hex)
    bar_w = max(10, round(width * 0.008))
    box_w = round(width * box_w_frac)
    box_h = round(height * box_h_frac)

    if position == "bottom_center":
        x0 = round((width - box_w) * 0.5)
        y0 = round(height * 0.78)
    elif position == "bottom_right":
        x0 = round(width * 0.95) - box_w
        y0 = round(height * 0.78)
    elif position == "top_left":
        x0 = round(width * 0.05)
        y0 = round(height * 0.08)
    elif position == "top_right":
        x0 = round(width * 0.95) - box_w
        y0 = round(height * 0.08)
    elif position == "center":
        x0 = round((width - box_w) * 0.5)
        y0 = round((height - box_h) * 0.5)
    elif position == "top_center":
        x0 = round((width - box_w) * 0.5)
        y0 = round(height * 0.08)
    else:
        x0 = round(width * 0.05)
        y0 = round(height * 0.78)

    title_px = max(22, round(height * title_px_frac))
    sub_px = max(16, round(height * sub_px_frac))

    esc_title = saxutils.escape(title)
    esc_sub = saxutils.escape(subtitle)
    esc_font = saxutils.escape(font_family)

    return (
        f'<kdenlivetitle LC_NUMERIC="C" duration="{duration_frames}" '
        f'out="{max(0, duration_frames - 1)}" width="{width}" height="{height}">'
        f'<item type="QGraphicsRectItem" z-index="0">'
        f'<position x="{x0}" y="{y0}"><transform>1,0,0,0,1,0,0,0,1</transform></position>'
        f'<content rect="0,0,{box_w},{box_h}" brushcolor="18,20,26,215" pencolor="0,0,0,0" penwidth="0"/>'
        f"</item>"
        f'<item type="QGraphicsRectItem" z-index="1">'
        f'<position x="{x0}" y="{y0}"><transform>1,0,0,0,1,0,0,0,1</transform></position>'
        f'<content rect="0,0,{bar_w},{box_h}" brushcolor="{r},{g},{b},255" pencolor="0,0,0,0" penwidth="0"/>'
        f"</item>"
        f'<item type="QGraphicsTextItem" z-index="2">'
        f'<position x="{x0 + bar_w + 22}" y="{y0 + int(box_h * 0.14)}">'
        f"<transform>1,0,0,0,1,0,0,0,1</transform></position>"
        f'<content font="{esc_font}" font-pixel-size="{title_px}" font-weight="700" '
        f'font-color="255,255,255,255">{esc_title}</content>'
        f"</item>"
        f'<item type="QGraphicsTextItem" z-index="3">'
        f'<position x="{x0 + bar_w + 22}" y="{y0 + int(box_h * 0.58)}">'
        f"<transform>1,0,0,0,1,0,0,0,1</transform></position>"
        f'<content font="{esc_font}" font-pixel-size="{sub_px}" font-weight="400" '
        f'font-color="{r},{g},{b},255">{esc_sub}</content>'
        f"</item>"
        f"</kdenlivetitle>"
    )


def _speaker_matches_filter(speaker: str, target_speakers: list[str]) -> bool:
    if not target_speakers:
        return True
    spk_low = speaker.strip().lower()
    return any(t.strip().lower() in spk_low or spk_low in t.strip().lower() for t in target_speakers)


def _split_segment_text_by_intervals(seg: Any, intervals: list[Any]) -> dict[int, str]:
    """Distribute a segment's words over the kept intervals it overlaps (each word exactly once).

    V1.2 fix: when the rough cut removed a pause inside a sentence, the full sentence was
    emitted once per kept interval (duplicated captions). Word timestamps (neural ASR) assign
    each word to the interval containing its midpoint; without them words are split in
    proportion to each interval's overlap.
    """
    overlaps: dict[int, float] = {}
    for idx, iv in enumerate(intervals):
        ov = min(seg.end_sec, iv.end_sec) - max(seg.start_sec, iv.start_sec)
        if ov >= 0.20:
            overlaps[idx] = ov
    if not overlaps:
        return {}
    if len(overlaps) == 1:
        return {next(iter(overlaps)): seg.text}
    buckets: dict[int, list[str]] = {idx: [] for idx in overlaps}
    words = [w for w in (seg.words or []) if str(w.get("word", "")).strip()]
    if words:
        for w in words:
            mid = (float(w.get("start_sec", seg.start_sec)) + float(w.get("end_sec", seg.end_sec))) / 2.0
            best = min(
                overlaps,
                key=lambda i: 0.0 if intervals[i].start_sec <= mid <= intervals[i].end_sec else min(abs(mid - intervals[i].start_sec), abs(mid - intervals[i].end_sec)),
            )
            buckets[best].append(str(w["word"]).strip())
    else:
        tokens = seg.text.split()
        total = sum(overlaps.values()) or 1.0
        pos = 0
        ordered = sorted(overlaps)
        for n, idx in enumerate(ordered):
            take = len(tokens) - pos if n == len(ordered) - 1 else round(len(tokens) * overlaps[idx] / total)
            buckets[idx] = tokens[pos: pos + take]
            pos += take
    return {idx: " ".join(ws) for idx, ws in buckets.items() if ws}


def _build_edited_captions(state: ProjectAnalysisState) -> list[CaptionCue]:
    """Map diarised transcript segments onto the edited rough-cut or source master timeline.

    V1.2.2: with a spoken (neural) transcript, cues are built word by word on the edited
    timeline with terminology, punctuation/casing repair, speaker-change markers and the
    profile readability limits (``editorial_captions``). Speech-like audio the ASR found no
    words in is never captioned as speech. VAD-only transcripts keep the V1.2 placeholder path.
    """
    if any(s.is_neural_inference and (s.text or "").strip() for s in state.transcript_segments):
        from bangonpro.ai_engine.department.editorial_captions import build_captions_v122

        return build_captions_v122(state)
    target_speakers = state.intent.target_speakers
    time_range = state.intent.time_range_sec
    cues: list[CaptionCue] = []

    if state.rough_cut_shots and state.kept_master_intervals:
        edited_duration = state.rough_cut_shots[-1].timeline_out_sec
        for seg in state.transcript_segments:
            if not _speaker_matches_filter(seg.speaker_id, target_speakers):
                continue
            cursor = 0.0
            pieces = _split_segment_text_by_intervals(seg, state.kept_master_intervals)
            for iv_idx, iv in enumerate(state.kept_master_intervals):
                ov_start = max(seg.start_sec, iv.start_sec)
                ov_end = min(seg.end_sec, iv.end_sec)
                if ov_end - ov_start >= 0.20:
                    ed_start = cursor + (ov_start - iv.start_sec)
                    ed_end = min(edited_duration, cursor + (ov_end - iv.start_sec))
                    if time_range is not None:
                        ed_start = max(ed_start, time_range[0])
                        ed_end = min(ed_end, time_range[1])
                    piece = pieces.get(iv_idx, "")
                    if ed_end - ed_start >= 0.15 and piece:
                        if cues and ed_start < cues[-1].end_sec:
                            ed_start = cues[-1].end_sec
                        if ed_end > ed_start + 0.10:
                            cues.append(
                                CaptionCue(
                                    index=len(cues) + 1,
                                    start_sec=round(ed_start, 3),
                                    end_sec=round(ed_end, 3),
                                    speaker_id=seg.speaker_id,
                                    text=piece,
                                    confidence=seg.confidence,
                                )
                            )
                cursor += iv.duration_sec
    else:
        # Scoped captions-only workflow without altering timeline cuts
        for seg in state.transcript_segments:
            if not _speaker_matches_filter(seg.speaker_id, target_speakers):
                continue
            s_t = seg.start_sec
            e_t = seg.end_sec
            if time_range is not None:
                s_t = max(s_t, time_range[0])
                e_t = min(e_t, time_range[1])
            if e_t - s_t >= 0.15:
                if cues and s_t < cues[-1].end_sec:
                    s_t = cues[-1].end_sec
                if e_t > s_t + 0.10:
                    cues.append(
                        CaptionCue(
                            index=len(cues) + 1,
                            start_sec=round(s_t, 3),
                            end_sec=round(e_t, 3),
                            speaker_id=seg.speaker_id,
                            text=seg.text,
                            confidence=seg.confidence,
                        )
                    )

    return cues


def _build_lower_thirds(state: ProjectAnalysisState) -> list[LowerThirdSpec]:
    """Create non-overlapping lower-third graphics honoring ``graphics_templates`` & intent targets."""
    graphics_rules = state.profile.get("graphics_rules", {})
    if not graphics_rules.get("enable_lower_thirds", True) or not state.intent.add_lower_thirds:
        return []

    effective = get_effective_profile_settings(state.profile)
    templates = effective["graphics_templates"]
    fps = state.fps
    spec = state.profile["video_spec"]
    width = int(spec["width"])
    height = int(spec["height"])
    accent_hex = str(
        templates.get("accent_hex", graphics_rules.get("brand_accent_hex", "#FF5A1F"))
    )
    font_family = str(templates.get("font_family", "DejaVu Sans"))
    template_name = str(templates.get("lower_third_template", "studio_clean_bar"))
    position = str(templates.get("lower_third_position", "bottom_left"))
    target_dur = float(graphics_rules.get("lower_third_duration_sec", 4.5))
    show_title = str(state.profile.get("display_name", "Bang On Pro"))
    target_speakers = state.intent.target_speakers

    if state.transcript_segments:
        # V1.2.2: context-aware placement (first substantive appearance on the speaker's own angle,
        # never during crosstalk or under another graphic, re-shown after a long absence)
        from bangonpro.ai_engine.department.editorial_graphics import get_template, plan_lower_thirds

        t_name, t_spec, _known = get_template(template_name, "lower_third")
        rows = plan_lower_thirds(state, list(state.metadata.get("graphics_reserved") or []))
        planned: list[LowerThirdSpec] = []
        for row in rows:
            start_f = state.sec_to_frame(row["start_sec"])
            dur_frames = max(1, round(row["duration_sec"] * fps))
            subtitle = row["subtitle"] or show_title
            xml = generate_kdenlive_lower_third_xml(
                title=row["title"], subtitle=subtitle, width=width, height=height, accent_hex=accent_hex,
                duration_frames=dur_frames, position=position, font_family=font_family,
                box_w_frac=float(t_spec["box_w"]), box_h_frac=float(t_spec["box_h"]),
                title_px_frac=float(t_spec["title_px"]), sub_px_frac=float(t_spec["sub_px"]),
            )
            planned.append(
                LowerThirdSpec(
                    graphic_id=f"lt_{len(planned) + 1:02d}_{row['speaker_id'].lower().replace(' ', '_')}",
                    title=row["title"],
                    subtitle=subtitle,
                    start_sec=round(start_f / fps, 4),
                    duration_sec=round(dur_frames / fps, 4),
                    start_frame=start_f,
                    out_frame=start_f + dur_frames - 1,
                    accent_hex=accent_hex,
                    kdenlive_title_xml=xml,
                    template=template_name,
                    position=position,
                )
            )
        state.metadata["lower_third_plan"] = rows
        return planned

    lower_thirds: list[LowerThirdSpec] = []
    seen_speakers: set[str] = set()
    occupied_until_frame = 0

    if state.rough_cut_shots:
        total_timeline_frames = state.rough_cut_shots[-1].timeline_out_frame + 1
        candidates = [(shot.speaker_id, shot.timeline_in_frame) for shot in state.rough_cut_shots]
    else:
        # Scoped graphics-only workflow without mutating timeline cuts
        total_dur = (
            state.sync_result.common_duration_sec
            if state.sync_result and state.sync_result.common_duration_sec > 0
            else max((a.duration_sec for a in state.media_assets.values()), default=15.0)
        )
        total_timeline_frames = max(round(target_dur * fps) + 1, state.sec_to_frame(total_dur))
        if state.transcript_segments:
            candidates = [
                (seg.speaker_id, state.sec_to_frame(seg.start_sec))
                for seg in state.transcript_segments
            ]
        elif target_speakers:
            candidates = [
                (spk, round(idx * (target_dur + 1.0) * fps))
                for idx, spk in enumerate(target_speakers)
            ]
        else:
            spk_names = [
                a.speaker_label
                for a in state.media_assets.values()
                if a.speaker_label
            ] or ["Host"]
            candidates = [
                (spk, round(idx * (target_dur + 1.0) * fps))
                for idx, spk in enumerate(spk_names)
            ]

    for spk, candidate_start_f in candidates:
        if not spk or spk in seen_speakers or not _speaker_matches_filter(spk, target_speakers):
            continue
        start_f = max(candidate_start_f, occupied_until_frame + round(0.5 * fps))
        if start_f >= total_timeline_frames - round(0.8 * fps):
            continue
        dur_frames = min(
            max(1, round(target_dur * fps)),
            total_timeline_frames - start_f,
        )
        if dur_frames < round(0.8 * fps):
            continue

        out_f = start_f + dur_frames - 1
        xml = generate_kdenlive_lower_third_xml(
            title=spk,
            subtitle=show_title,
            width=width,
            height=height,
            accent_hex=accent_hex,
            duration_frames=dur_frames,
            position=position,
            font_family=font_family,
        )
        lower_thirds.append(
            LowerThirdSpec(
                graphic_id=f"lt_{len(lower_thirds) + 1:02d}_{spk.lower().replace(' ', '_')}",
                title=spk,
                subtitle=show_title,
                start_sec=round(start_f / fps, 4),
                duration_sec=round(dur_frames / fps, 4),
                start_frame=start_f,
                out_frame=out_f,
                accent_hex=accent_hex,
                kdenlive_title_xml=xml,
                template=template_name,
                position=position,
            )
        )
        seen_speakers.add(spk)
        occupied_until_frame = out_f + 1

    return lower_thirds


def run_technical_qc(state: ProjectAnalysisState) -> QCReport:
    """Execute deterministic technical QC checks over sync, contiguity, continuity, silence, loudness & delivery_spec."""
    issues: list[QCIssue] = []
    metrics: dict[str, Any] = {}
    effective = get_effective_profile_settings(state.profile)
    audio_rules = state.profile.get("audio_rules", {})
    delivery_spec = effective["delivery_spec"]
    target_lufs = float(
        state.intent.loudness_target_lufs
        if state.intent.loudness_target_lufs is not None
        else effective["loudness_target_lufs"]
    )
    metrics["target_lufs"] = target_lufs
    metrics["delivery_spec"] = delivery_spec

    shots = state.rough_cut_shots
    if not shots:
        if state.intent.build_rough_cut or state.intent.multicam_switch:
            issues.append(
                QCIssue(
                    code="EMPTY_TIMELINE",
                    severity="error",
                    message="Rough cut produced zero shots.",
                )
            )
            return QCReport(passed=False, issues=issues, metrics=metrics)
        # Scoped non-cut pass (e.g. audio-only / captions-only / graphics-only)
        metrics["shot_count"] = 0
        metrics["scope"] = state.intent.scope
        return QCReport(passed=True, issues=issues, metrics=metrics)

    # 1. Sync confidence & drift check
    if state.sync_result is not None:
        min_sync_conf = (
            min(state.sync_result.confidences.values())
            if state.sync_result.confidences
            else 1.0
        )
        max_offset = max(
            (abs(v) for v in state.sync_result.offsets_sec.values()), default=0.0
        )
        metrics["min_sync_confidence"] = round(min_sync_conf, 4)
        metrics["max_camera_offset_sec"] = round(max_offset, 4)
        for cid, conf in state.sync_result.confidences.items():
            if conf < 0.50:
                issues.append(
                    QCIssue(
                        code="LOW_SYNC_CONFIDENCE",
                        severity="warning",
                        message=f"Camera '{cid}' GCC-PHAT sync confidence is low ({conf:.2f}).",
                        details={"camera_id": cid, "confidence": conf},
                    )
                )

    # 2. Black-frame / timeline gap & source-bounds check
    timeline_gaps = 0
    out_of_bounds = 0
    expected_tl_frame = 0
    media_by_ref = {a.media_ref: a for a in state.media_assets.values()}

    for shot in shots:
        if shot.timeline_in_frame != expected_tl_frame:
            timeline_gaps += 1
            issues.append(
                QCIssue(
                    code="TIMELINE_BLACK_GAP",
                    severity="error",
                    message=(
                        f"Gap detected before {shot.shot_id}: expected frame "
                        f"{expected_tl_frame}, got {shot.timeline_in_frame}"
                    ),
                    timeline_sec=shot.timeline_in_sec,
                )
            )
        expected_tl_frame = shot.timeline_out_frame + 1

        asset = media_by_ref.get(shot.media_ref)
        if asset is not None:
            max_frame = state.sec_to_frame(asset.duration_sec)
            if shot.source_in_frame < 0 or shot.source_out_frame > max_frame:
                out_of_bounds += 1
                issues.append(
                    QCIssue(
                        code="SOURCE_OUT_OF_BOUNDS",
                        severity="error",
                        message=f"Shot {shot.shot_id} exceeds source media duration for {shot.camera_id}.",
                        timeline_sec=shot.timeline_in_sec,
                    )
                )

    metrics["timeline_gap_count"] = timeline_gaps
    metrics["source_out_of_bounds_count"] = out_of_bounds
    metrics["edited_duration_sec"] = round(shots[-1].timeline_out_sec, 4)
    metrics["shot_count"] = len(shots)

    # 3. Continuity / jump-cut check
    jump_cuts = 0
    for prev_s, curr_s in itertools.pairwise(shots):
        if prev_s.camera_id == curr_s.camera_id:
            src_jump = abs(curr_s.source_in_sec - prev_s.source_out_sec)
            if src_jump > 0.15:
                jump_cuts += 1
                issues.append(
                    QCIssue(
                        code="JUMP_CUT_DETECTED",
                        severity="warning",
                        message=(
                            f"Same-camera jump cut on {curr_s.camera_id} between "
                            f"{prev_s.shot_id} and {curr_s.shot_id} (source jump {src_jump:.2f}s)."
                        ),
                        timeline_sec=curr_s.timeline_in_sec,
                        details={
                            "camera_id": curr_s.camera_id,
                            "source_jump_sec": round(src_jump, 3),
                        },
                    )
                )
    metrics["jump_cut_count"] = jump_cuts

    # 4. Audio peak loudness & residual silence check over stitched edited PCM
    sr = state.audio_sample_rate
    stitched_chunks: list[np.ndarray] = []
    cam_by_ref = {a.media_ref: a.camera_id for a in state.media_assets.values()}
    for shot in shots:
        a_cid = cam_by_ref.get(shot.audio_media_ref)
        pcm = state.audio_pcm_cache.get(a_cid) if a_cid else None
        if pcm is not None and len(pcm) > 0:
            s_idx = max(0, round(shot.audio_source_in_sec * sr))
            e_idx = min(len(pcm), round(shot.audio_source_out_sec * sr))
            if e_idx > s_idx:
                stitched_chunks.append(pcm[s_idx:e_idx])

    if stitched_chunks:
        edited_pcm = np.concatenate(stitched_chunks).astype(np.float64)
        peak_amp = float(np.max(np.abs(edited_pcm))) if edited_pcm.size else 0.0
        rms_amp = float(np.sqrt(np.mean(edited_pcm * edited_pcm) + 1e-18))
        peak_dbfs = round(float(20.0 * np.log10(peak_amp + 1e-12)), 2)
        rms_dbfs = round(float(20.0 * np.log10(rms_amp + 1e-12)), 2)
        metrics["pre_loudnorm_peak_dbfs"] = peak_dbfs
        metrics["pre_loudnorm_rms_dbfs"] = rms_dbfs

        if peak_dbfs > -0.2:
            issues.append(
                QCIssue(
                    code="AUDIO_CLIPPING_RISK",
                    severity="warning",
                    message=f"Edited audio peak reaches {peak_dbfs:.2f} dBFS (clipping risk prior to limiter).",
                )
            )
        if rms_dbfs < -60.0:
            issues.append(
                QCIssue(
                    code="AUDIO_NEAR_SILENT",
                    severity="error",
                    message=f"Edited audio appears silent ({rms_dbfs:.2f} dBFS RMS).",
                )
            )

        win = max(1, int(0.1 * sr))
        n_wins = edited_pcm.size // win
        max_sil_sec = 0.0
        if n_wins > 0:
            blocks = edited_pcm[: n_wins * win].reshape(n_wins, win)
            win_db = 20.0 * np.log10(np.sqrt(np.mean(blocks * blocks, axis=1) + 1e-18) + 1e-12)
            sil_thresh = float(
                state.intent.silence_threshold_db
                if state.intent.silence_threshold_db is not None
                else effective["silence_threshold_db"]
            )
            run = 0
            for db_val in win_db:
                if db_val < sil_thresh:
                    run += 1
                    max_sil_sec = max(max_sil_sec, run * 0.1)
                else:
                    run = 0
        metrics["max_residual_silence_sec"] = round(max_sil_sec, 2)
        allowed_sil = max(
            2.0, float(state.profile["editing_rules"].get("min_silence_sec", 0.8)) * 2.5
        )
        if max_sil_sec > allowed_sil:
            issues.append(
                QCIssue(
                    code="RESIDUAL_SILENCE_GAP",
                    severity="warning",
                    message=(
                        f"Residual silence gap of {max_sil_sec:.2f}s exceeds "
                        f"threshold ({allowed_sil:.2f}s)."
                    ),
                )
            )

    # 5. Per-camera microphone level consistency analysis & automatic gain staging
    enable_mic_match = bool(audio_rules.get("enable_mic_level_matching", True))
    max_mic_trim = float(audio_rules.get("max_mic_trim_db", 24.0))
    cam_rms_db: dict[str, float] = {}
    for cam_id, pcm in (state.audio_pcm_cache or {}).items():
        arr = np.asarray(pcm, dtype=np.float32)
        if arr.size > 0:
            active = arr[np.abs(arr) > 1e-4]
            if active.size > 0:
                rms_val = float(np.sqrt(np.mean(active * active) + 1e-18))
                cam_rms_db[cam_id] = round(20.0 * float(np.log10(rms_val + 1e-12)), 2)

    per_cam_trim_db: dict[str, float] = {}
    if enable_mic_match and len(cam_rms_db) >= 2:
        median_db = float(np.median(list(cam_rms_db.values())))
        spread_db = max(cam_rms_db.values()) - min(cam_rms_db.values())
        metrics["mic_level_spread_db"] = round(spread_db, 2)
        metrics["per_camera_rms_db"] = dict(cam_rms_db)
        if spread_db >= 4.0:
            issues.append(
                QCIssue(
                    code="MIC_LEVEL_INCONSISTENCY_CORRECTED",
                    severity="warning",
                    message=(
                        f"Source microphones varied by {spread_db:.1f} dB; applied automatic "
                        f"per-camera gain staging toward {median_db:.1f} dBFS median before mastering."
                    ),
                )
            )
        for cam_id, rms_db in cam_rms_db.items():
            delta = max(-max_mic_trim, min(max_mic_trim, round(median_db - rms_db, 2)))
            if abs(delta) >= 2.5:
                per_cam_trim_db[cam_id] = delta
    state.metadata["per_camera_gain_trim_db"] = per_cam_trim_db

    # 6. Mixed frame-rate (video cameras only) & delivery_spec checks
    source_fps_map = {
        cam_id: round(asset.fps, 4)
        for cam_id, asset in state.media_assets.items()
        if asset.has_video
    }
    unique_fps = {round(f, 2) for f in source_fps_map.values()}
    metrics["sequence_fps"] = round(state.fps, 4)
    metrics["source_frame_rates"] = source_fps_map
    metrics["mixed_frame_rates_normalized"] = len(unique_fps) > 1 or any(
        abs(f - state.fps) > 0.05 for f in source_fps_map.values()
    )
    if metrics["mixed_frame_rates_normalized"]:
        issues.append(
            QCIssue(
                code="MIXED_FRAME_RATES_NORMALIZED",
                severity="warning",
                message=(
                    f"Normalized mixed source frame rates {source_fps_map} into "
                    f"{state.fps:.3f} fps sequence timebase."
                ),
            )
        )

    spec = state.profile.get("video_spec", {})
    if delivery_spec.get("width") and int(spec.get("width", 0)) != int(delivery_spec["width"]):
        issues.append(
            QCIssue(
                code="DELIVERY_SPEC_WIDTH_MISMATCH",
                severity="warning",
                message=(
                    f"Sequence width {spec.get('width')} differs from delivery_spec width "
                    f"{delivery_spec['width']}."
                ),
            )
        )

    missing_media = list(getattr(state, "missing_media", []) or [])
    metrics["missing_media_count"] = len(missing_media)
    for mm in missing_media:
        issues.append(
            QCIssue(
                code="MISSING_MEDIA_SKIPPED",
                severity="warning",
                message=(
                    f"Offline/missing media skipped for camera '{mm.get('camera_id', 'unknown')}': "
                    f"{mm.get('file_path', '')} ({mm.get('reason', 'missing')})"
                ),
            )
        )

    has_error = any(i.severity == "error" for i in issues)
    return QCReport(passed=not has_error, issues=issues, metrics=metrics)


def _audio_target_tracks(state: ProjectAnalysisState) -> list[str]:
    return [t for t in state.intent.target_tracks if t.startswith("A")] or ["A1"]


def _loudness_target(state: ProjectAnalysisState) -> float:
    effective = get_effective_profile_settings(state.profile)
    return (
        state.intent.loudness_target_lufs
        if state.intent.loudness_target_lufs is not None
        else float(effective["loudness_target_lufs"])
    )


def prepare_audio_restoration(state: ProjectAnalysisState) -> list[AudioFilterSpec]:
    """Audio Engineer: build the MLT/FFmpeg restoration chain (only when ``restore_audio``)."""
    audio_rules = state.profile.get("audio_rules", {})
    audio_target_tracks = _audio_target_tracks(state)
    loudness_override = _loudness_target(state)
    audio_filters: list[AudioFilterSpec] = []
    ffmpeg_chains: list[str] = []
    if state.intent.restore_audio:
        for trk in audio_target_tracks:
            flts, chain_str = build_audio_restoration_chain(
                audio_rules,
                rnnoise_model_path=state.rnnoise_model_path,
                target_track_id=trk,
                loudness_override_lufs=loudness_override,
                include_music_duck=(trk == "A1" and len(audio_target_tracks) == 1),
                measured=(state.metadata.get("audio_plan") or {}).get("chain"),
            )
            audio_filters.extend(flts)
            if chain_str:
                ffmpeg_chains.append(chain_str)
    state.audio_filters = audio_filters
    state.ffmpeg_audio_filtergraph = ";".join(ffmpeg_chains)
    return audio_filters


def emit_audio_ops(state: ProjectAnalysisState, builder: TimelineCommandBuilder) -> int:
    """Emit audio restoration, microphone level-matching and music-duck operations."""
    if not state.intent.restore_audio:
        return 0
    before = builder.operation_count
    audio_rules = state.profile.get("audio_rules", {})
    audio_target_tracks = _audio_target_tracks(state)
    loudness_override = _loudness_target(state)
    for flt in state.audio_filters:
        if not flt.enabled:
            continue
        builder.add_operation(
            "add_audio_filter",
            track_id=flt.track_id,
            params={
                "filter_id": flt.filter_id,
                "mlt_service": flt.mlt_service,
                "mlt_params": flt.mlt_params,
                "ffmpeg_filter": flt.ffmpeg_filter,
            },
            confidence=0.98,
            rationale=flt.rationale,
        )

    plan = state.metadata.get("audio_plan") or {}
    per_cam_trim_db = {} if plan.get("clip_ops") is not None else state.metadata.get("per_camera_gain_trim_db", {})
    for cop in plan.get("clip_ops") or []:
        # V1.2.2 measured per-clip matching (volume / shelf EQ / bleed expander) as native clip effects;
        # they replace the V1.1 set_audio_level trims, which never reached the draft project.
        builder.add_operation(
            "add_effect",
            clip_id=cop["clip_id"],
            track_id=audio_target_tracks[0],
            params={"effect_id": cop["mlt_service"], "mlt_service": cop["mlt_service"], "kdenlive_id": cop["mlt_service"], "parameters": cop["properties"], "properties": cop["properties"], "agent_id": "audio_engineer"},
            confidence=float(cop.get("confidence", 0.9)),
            rationale=cop["rationale"],
        )
    if per_cam_trim_db and state.rough_cut_shots:
        for shot in state.rough_cut_shots:
            src_cam = shot.camera_id
            for cid, asset in state.media_assets.items():
                if asset.media_ref == shot.audio_media_ref:
                    src_cam = cid
                    break
            trim_db = per_cam_trim_db.get(src_cam)
            if trim_db is not None and abs(trim_db) >= 2.5:
                builder.add_operation(
                    "set_audio_level",
                    clip_id=f"{shot.shot_id}_a",
                    track_id=audio_target_tracks[0],
                    params={
                        "gain_db": float(trim_db),
                        "camera_id": src_cam,
                        "level_matching": True,
                    },
                    confidence=0.92,
                    rationale=f"Level-match microphone '{src_cam}' by {trim_db:+.1f} dB prior to master loudnorm",
                )

    if "A1" in audio_target_tracks and len(audio_target_tracks) == 1:
        builder.add_operation(
            "set_audio_level",
            track_id="A2",
            params={
                "gain_db": float(audio_rules.get("music_duck_db", -18.0)),
                "target_lufs": loudness_override,
                "true_peak_dbtp": float(audio_rules.get("true_peak_dbtp", -1.5)),
                "duck_under_dialogue": True,
            },
            confidence=0.96,
            rationale="Configure music bed ducking level relative to dialogue master A1",
        )
    return builder.operation_count - before


def prepare_captions(state: ProjectAnalysisState) -> list[CaptionCue]:
    """Captions Agent: build edited caption cues and write ``.srt`` / ``.ass`` sidecars."""
    out_dir = Path(state.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    effective = get_effective_profile_settings(state.profile)
    graphics_rules = state.profile.get("graphics_rules", {})
    caption_style = effective["caption_style"]
    enable_captions = bool(graphics_rules.get("enable_captions", True)) and state.intent.add_captions
    captions = _build_edited_captions(state) if enable_captions else []
    state.captions = captions

    if enable_captions and captions:
        spec = state.profile["video_spec"]
        srt_text = generate_srt_subtitles(captions, caption_style=caption_style)
        ass_text = generate_ass_subtitles(
            captions,
            width=int(spec["width"]),
            height=int(spec["height"]),
            brand_accent_hex=str(graphics_rules.get("brand_accent_hex", "#FF5A1F")),
            caption_style=caption_style,
        )
        srt_path = out_dir / "captions.srt"
        ass_path = out_dir / "captions.ass"
        srt_path.write_text(srt_text, encoding="utf-8")
        ass_path.write_text(ass_text, encoding="utf-8")
        state.artifacts["captions_srt"] = str(srt_path)
        state.artifacts["captions_ass"] = str(ass_path)
    if captions:
        from bangonpro.ai_engine.department.editorial_captions import caption_readability

        state.metadata["caption_report"] = caption_readability(captions, state.profile)
    return captions


def emit_caption_ops(state: ProjectAnalysisState, builder: TimelineCommandBuilder) -> int:
    before = builder.operation_count
    caption_style = get_effective_profile_settings(state.profile)["caption_style"]
    for cue in state.captions:
        in_f = state.sec_to_frame(cue.start_sec)
        out_f = max(in_f, state.sec_to_frame(cue.end_sec))
        builder.add_operation(
            "add_caption",
            track_id="V2",
            in_frame=in_f,
            out_frame=out_f,
            timeline_start=in_f,
            params={
                "index": cue.index,
                "speaker_id": cue.speaker_id,
                "text": cue.text,
                "srt_path": state.artifacts.get("captions_srt", ""),
                "ass_path": state.artifacts.get("captions_ass", ""),
                "caption_style": caption_style,
            },
            confidence=cue.confidence,
            rationale=f"Add caption cue #{cue.index} ({cue.speaker_id})",
        )
    return builder.operation_count - before


def prepare_lower_thirds(state: ProjectAnalysisState) -> list[LowerThirdSpec]:
    """Graphics Agent: build branded lower-third specifications (scoped by intent)."""
    state.lower_thirds = _build_lower_thirds(state)
    return state.lower_thirds


def emit_lower_third_ops(state: ProjectAnalysisState, builder: TimelineCommandBuilder) -> int:
    before = builder.operation_count
    for lt in state.lower_thirds:
        builder.add_operation(
            "place_clip",
            clip_id=lt.graphic_id,
            track_id="V2",
            in_frame=0,
            out_frame=max(0, lt.out_frame - lt.start_frame),
            timeline_start=lt.start_frame,
            params={
                "clip_kind": "kdenlivetitle",
                "title": lt.title,
                "subtitle": lt.subtitle,
                "accent_hex": lt.accent_hex,
                "template": lt.template,
                "position": lt.position,
                "kdenlive_title_xml": lt.kdenlive_title_xml,
            },
            confidence=0.95,
            rationale=f"Place lower-third graphic for '{lt.title}' ({lt.template}) on V2",
        )
    return builder.operation_count - before


def prepare_qc(state: ProjectAnalysisState) -> QCReport:
    """Quality Control Agent: run technical QC (only when ``run_qc``) and persist the report."""
    qc_report = run_technical_qc(state) if state.intent.run_qc else QCReport(passed=True)
    state.qc_report = qc_report
    if state.intent.run_qc:
        out_dir = Path(state.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        qc_path = out_dir / "qc_report.json"
        qc_path.write_text(json.dumps(asdict(qc_report), indent=2), encoding="utf-8")
        state.artifacts["qc_report_json"] = str(qc_path)
    return qc_report


def emit_qc_ops(
    state: ProjectAnalysisState,
    builder: TimelineCommandBuilder,
    issues: list[QCIssue] | None = None,
) -> int:
    before = builder.operation_count
    for issue in state.qc_report.issues if issues is None else issues:
        cat = "qc_error" if issue.severity == "error" else "qc_warning"
        marker = state.add_review_marker(
            timeline_sec=issue.timeline_sec,
            label=f"QC · {issue.message[:80]}",
            category=cat,
            confidence=0.95,
            rationale=issue.message,
        )
        builder.add_operation(
            "add_marker",
            track_id="V1",
            in_frame=marker.timeline_frame,
            out_frame=marker.timeline_frame,
            timeline_start=marker.timeline_frame,
            params={
                "marker_id": marker.marker_id,
                "category": cat,
                "code": issue.code,
                "severity": issue.severity,
                "label": marker.label,
            },
            confidence=0.95,
            rationale=issue.message,
        )
    return builder.operation_count - before


def new_agent_builder(
    state: ProjectAnalysisState,
    *,
    agent_id: str,
    agent_role: str,
    transaction_label: str,
    requires_approval: bool = False,
) -> TimelineCommandBuilder:
    """Create a Timeline Command API v1 builder bound to the state's current revision."""
    return TimelineCommandBuilder(
        project_id=state.project_id,
        sequence_id=state.sequence_id,
        expected_revision=state.revision,
        agent_id=agent_id,
        agent_role=agent_role,
        transaction_label=transaction_label,
        requires_approval=requires_approval,
        dry_run=state.dry_run,
        fps_num=state.fps_num,
        fps_den=state.fps_den,
    )


def run_audio_graphics_qc_agent(
    state: ProjectAnalysisState,
    token: CancellationToken,
    report_progress: Callable[[float, str], None],
) -> dict[str, Any] | None:
    """Generate audio restoration chain, captions, lower-thirds, and run technical QC strictly per intent."""
    token.check("audio_graphics_qc start")

    needs_stage = any(
        (
            state.intent.restore_audio,
            state.intent.add_captions,
            state.intent.add_lower_thirds,
            state.intent.run_qc,
        )
    )
    if not needs_stage:
        report_progress(1.0, "Skipping audio/graphics/QC (not requested by instruction scope)")
        return None

    Path(state.output_dir).mkdir(parents=True, exist_ok=True)

    report_progress(0.15, "Configuring MLT/FFmpeg audio restoration chain")
    prepare_audio_restoration(state)

    report_progress(0.40, "Generating .srt and .ass captions")
    prepare_captions(state)

    token.check("generate lower thirds")
    report_progress(0.60, "Generating Kdenlive lower-third graphics")
    prepare_lower_thirds(state)

    token.check("run technical qc")
    report_progress(0.78, "Running technical QC checks")
    qc_report = prepare_qc(state)

    report_progress(0.90, "Building Timeline Command API v1 batch for audio, graphics & QC")
    builder = new_agent_builder(
        state,
        agent_id="audio_graphics_qc_agent",
        agent_role="audio_finishing_graphics_and_qc",
        transaction_label="Apply Audio Restoration, Captions, Lower Thirds & QC",
    )
    emit_audio_ops(state, builder)
    emit_caption_ops(state, builder)
    emit_lower_third_ops(state, builder)
    emit_qc_ops(state, builder)

    if builder.operation_count == 0:
        report_progress(0.98, "No audio/graphics/QC operations emitted for current scope")
        return None

    report_progress(
        0.98,
        f"QC {'PASSED' if qc_report.passed else 'FAILED'} ({len(qc_report.issues)} findings)",
    )
    return builder.build()
