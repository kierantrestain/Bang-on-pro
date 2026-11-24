"""Audio Restoration, Captions (.srt/.ass), Lower-Third Graphics & Technical QC Agent.

Builds real MLT + FFmpeg audio restoration filter chains, maps transcripts onto
the edited rough-cut timeline to emit valid ``.srt`` and ``.ass`` captions,
generates Kdenlive lower-third title XML clips, runs technical QC checks, and
emits validated Timeline Command API v1 batches.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import xml.sax.saxutils as saxutils
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

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
    ms_total = max(0, int(round(seconds * 1000.0)))
    hours, rem = divmod(ms_total, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _format_ass_timestamp(seconds: float) -> str:
    cs_total = max(0, int(round(seconds * 100.0)))
    hours, rem = divmod(cs_total, 3600_00)
    minutes, rem = divmod(rem, 60_00)
    secs, cs = divmod(rem, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{cs:02d}"


def build_audio_restoration_chain(
    audio_rules: dict[str, Any],
    *,
    rnnoise_model_path: str | None = None,
) -> tuple[list[AudioFilterSpec], str]:
    """Construct real MLT + FFmpeg audio restoration chain from Show Profile ``audio_rules``."""
    target_lufs = float(audio_rules.get("target_lufs", -16.0))
    true_peak = float(audio_rules.get("true_peak_dbtp", -1.5))
    enable_denoise = bool(audio_rules.get("enable_denoise", True))
    enable_compressor = bool(audio_rules.get("enable_compressor", True))
    music_duck_db = float(audio_rules.get("music_duck_db", -18.0))

    filters: list[AudioFilterSpec] = [
        AudioFilterSpec(
            filter_id="flt_a1_highpass",
            mlt_service="avfilter.highpass",
            mlt_params={"av.frequency": 80.0},
            ffmpeg_filter="highpass=f=80",
            track_id="A1",
            enabled=True,
            rationale="Remove low-frequency studio rumble and mic stand handling noise (<80 Hz)",
        )
    ]

    if enable_denoise:
        if rnnoise_model_path and Path(rnnoise_model_path).is_file():
            model_abs = str(Path(rnnoise_model_path).resolve())
            filters.append(
                AudioFilterSpec(
                    filter_id="flt_a1_rnnoise",
                    mlt_service="avfilter.arnndn",
                    mlt_params={"av.model": model_abs},
                    ffmpeg_filter=f"arnndn=m='{model_abs}'",
                    track_id="A1",
                    enabled=True,
                    rationale="Apply RNNoise neural voice denoiser using configured model",
                )
            )
        else:
            filters.append(
                AudioFilterSpec(
                    filter_id="flt_a1_afftdn",
                    mlt_service="avfilter.afftdn",
                    mlt_params={"av.noise_reduction": 10.0, "av.nf": -35.0},
                    ffmpeg_filter="afftdn=nr=10:nf=-35",
                    track_id="A1",
                    enabled=True,
                    rationale="Apply FFT spectral noise reduction (deterministic CPU fallback for RNNoise)",
                )
            )

    if enable_compressor:
        filters.append(
            AudioFilterSpec(
                filter_id="flt_a1_compressor",
                mlt_service="avfilter.acompressor",
                mlt_params={
                    "av.threshold": 0.125,
                    "av.ratio": 3.0,
                    "av.attack": 10.0,
                    "av.release": 120.0,
                    "av.makeup": 1.5,
                },
                ffmpeg_filter="acompressor=threshold=0.125:ratio=3:attack=10:release=120:makeup=1.5",
                track_id="A1",
                enabled=True,
                rationale="Level studio vocal dynamics across multiple microphones",
            )
        )

    filters.append(
        AudioFilterSpec(
            filter_id="flt_a1_loudnorm",
            mlt_service="avfilter.loudnorm",
            mlt_params={
                "av.I": target_lufs,
                "av.TP": true_peak,
                "av.LRA": 7.0,
            },
            ffmpeg_filter=f"loudnorm=I={target_lufs:.1f}:TP={true_peak:.1f}:LRA=7",
            track_id="A1",
            enabled=True,
            rationale=f"EBU R128 broadcast loudness normalization to {target_lufs:.1f} LUFS / {true_peak:.1f} dBTP",
        )
    )

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

    a1_ffmpeg_chain = ",".join(f.ffmpeg_filter for f in filters if f.track_id == "A1" and f.enabled)
    return filters, a1_ffmpeg_chain


def generate_srt_subtitles(captions: list[CaptionCue]) -> str:
    """Render edited-timeline captions as a strictly valid SubRip (.srt) document."""
    blocks: list[str] = []
    for idx, cue in enumerate(captions, start=1):
        start_ts = _format_srt_timestamp(cue.start_sec)
        end_ts = _format_srt_timestamp(max(cue.end_sec, cue.start_sec + 0.1))
        text = cue.text.strip().replace("\r\n", "\n")
        blocks.append(f"{idx}\n{start_ts} --> {end_ts}\n{text}\n")
    return "\n".join(blocks)


def generate_ass_subtitles(
    captions: list[CaptionCue],
    *,
    width: int = 1920,
    height: int = 1080,
    brand_accent_hex: str = "#FF5A1F",
) -> str:
    """Render edited-timeline captions as a strictly valid SubStation Alpha v4+ (.ass) document."""
    font_size = max(24, int(round(height * 0.044)))
    margin_v = max(28, int(round(height * 0.05)))
    margin_h = max(36, int(round(width * 0.04)))
    accent_ass = _hex_to_ass_color(brand_accent_hex, alpha=0)

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
        f"Style: Default,DejaVu Sans,{font_size},&H00FFFFFF,{accent_ass},&H00121212,&H90000000,1,0,0,0,100,100,0,0,1,3,1,2,{margin_h},{margin_h},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for cue in captions:
        start_ts = _format_ass_timestamp(cue.start_sec)
        end_ts = _format_ass_timestamp(max(cue.end_sec, cue.start_sec + 0.1))
        safe_speaker = cue.speaker_id.replace(",", " ")
        safe_text = cue.text.strip().replace("\n", "\\N")
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
) -> str:
    """Generate native Kdenlive ``kdenlivetitle`` XML for a broadcast lower-third graphic."""
    r, g, b = _hex_to_rgb(accent_hex)
    x0 = int(round(width * 0.05))
    y0 = int(round(height * 0.78))
    bar_w = max(10, int(round(width * 0.008)))
    box_w = int(round(width * 0.34))
    box_h = int(round(height * 0.115))
    title_px = max(22, int(round(height * 0.038)))
    sub_px = max(16, int(round(height * 0.024)))

    esc_title = saxutils.escape(title)
    esc_sub = saxutils.escape(subtitle)

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
        f'<content font="DejaVu Sans" font-pixel-size="{title_px}" font-weight="700" '
        f'font-color="255,255,255,255">{esc_title}</content>'
        f"</item>"
        f'<item type="QGraphicsTextItem" z-index="3">'
        f'<position x="{x0 + bar_w + 22}" y="{y0 + int(box_h * 0.58)}">'
        f"<transform>1,0,0,0,1,0,0,0,1</transform></position>"
        f'<content font="DejaVu Sans" font-pixel-size="{sub_px}" font-weight="400" '
        f'font-color="{r},{g},{b},255">{esc_sub}</content>'
        f"</item>"
        f"</kdenlivetitle>"
    )


def _build_edited_captions(state: ProjectAnalysisState) -> list[CaptionCue]:
    """Map diarised transcript segments onto the edited rough-cut timeline."""
    if not state.rough_cut_shots:
        return []

    edited_duration = state.rough_cut_shots[-1].timeline_out_sec
    cues: list[CaptionCue] = []

    for seg in state.transcript_segments:
        # Map segment intersection across kept master intervals
        cursor = 0.0
        for iv in state.kept_master_intervals:
            ov_start = max(seg.start_sec, iv.start_sec)
            ov_end = min(seg.end_sec, iv.end_sec)
            if ov_end - ov_start >= 0.20:
                ed_start = cursor + (ov_start - iv.start_sec)
                ed_end = min(edited_duration, cursor + (ov_end - iv.start_sec))
                if ed_end - ed_start >= 0.15:
                    if cues and ed_start < cues[-1].end_sec:
                        ed_start = cues[-1].end_sec
                    if ed_end > ed_start + 0.10:
                        cues.append(
                            CaptionCue(
                                index=len(cues) + 1,
                                start_sec=round(ed_start, 3),
                                end_sec=round(ed_end, 3),
                                speaker_id=seg.speaker_id,
                                text=seg.text,
                                confidence=seg.confidence,
                            )
                        )
            cursor += iv.duration_sec

    if not cues and state.rough_cut_shots:
        first_shot = state.rough_cut_shots[0]
        cues.append(
            CaptionCue(
                index=1,
                start_sec=0.0,
                end_sec=round(min(edited_duration, max(1.0, first_shot.timeline_out_sec)), 3),
                speaker_id=first_shot.speaker_id,
                text=f"{state.profile['display_name']} — Studio Program",
                confidence=0.9,
            )
        )

    return cues


def _build_lower_thirds(state: ProjectAnalysisState) -> list[LowerThirdSpec]:
    """Create non-overlapping lower-third graphics for each speaker's first on-screen turn."""
    graphics_rules = state.profile.get("graphics_rules", {})
    if not graphics_rules.get("enable_lower_thirds", True) or not state.intent.add_lower_thirds:
        return []
    if not state.rough_cut_shots:
        return []

    fps = state.fps
    spec = state.profile["video_spec"]
    width = int(spec["width"])
    height = int(spec["height"])
    accent_hex = str(graphics_rules.get("brand_accent_hex", "#FF5A1F"))
    target_dur = float(graphics_rules.get("lower_third_duration_sec", 4.5))
    total_timeline_frames = state.rough_cut_shots[-1].timeline_out_frame + 1
    show_title = str(state.profile.get("display_name", "Bang On Pro"))

    lower_thirds: list[LowerThirdSpec] = []
    seen_speakers: set[str] = set()
    occupied_until_frame = 0

    for shot in state.rough_cut_shots:
        spk = shot.speaker_id
        if not spk or spk in seen_speakers:
            continue
        start_f = max(shot.timeline_in_frame, occupied_until_frame + int(round(0.5 * fps)))
        if start_f >= total_timeline_frames - int(round(0.8 * fps)):
            continue
        dur_frames = min(
            max(1, int(round(target_dur * fps))),
            total_timeline_frames - start_f,
        )
        if dur_frames < int(round(0.8 * fps)):
            continue

        out_f = start_f + dur_frames - 1
        xml = generate_kdenlive_lower_third_xml(
            title=spk,
            subtitle=show_title,
            width=width,
            height=height,
            accent_hex=accent_hex,
            duration_frames=dur_frames,
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
            )
        )
        seen_speakers.add(spk)
        occupied_until_frame = out_f + 1

    return lower_thirds


def run_technical_qc(state: ProjectAnalysisState) -> QCReport:
    """Execute deterministic technical QC checks over sync, contiguity, continuity, silence & loudness."""
    issues: list[QCIssue] = []
    metrics: dict[str, Any] = {}

    shots = state.rough_cut_shots
    if not shots:
        issues.append(
            QCIssue(
                code="EMPTY_TIMELINE",
                severity="error",
                message="Rough cut produced zero shots.",
            )
        )
        return QCReport(passed=False, issues=issues, metrics=metrics)

    # 1. Sync confidence & drift check
    if state.sync_result is not None:
        min_sync_conf = min(state.sync_result.confidences.values()) if state.sync_result.confidences else 1.0
        max_offset = max((abs(v) for v in state.sync_result.offsets_sec.values()), default=0.0)
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
    for prev_s, curr_s in zip(shots, shots[1:]):
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
                        details={"camera_id": curr_s.camera_id, "source_jump_sec": round(src_jump, 3)},
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
            s_idx = max(0, int(round(shot.audio_source_in_sec * sr)))
            e_idx = min(len(pcm), int(round(shot.audio_source_out_sec * sr)))
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
        metrics["target_lufs"] = float(state.profile["audio_rules"].get("target_lufs", -16.0))

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

        # Residual long silence scan (100 ms windows)
        win = max(1, int(0.1 * sr))
        n_wins = edited_pcm.size // win
        max_sil_sec = 0.0
        if n_wins > 0:
            blocks = edited_pcm[: n_wins * win].reshape(n_wins, win)
            win_db = 20.0 * np.log10(np.sqrt(np.mean(blocks * blocks, axis=1) + 1e-18) + 1e-12)
            sil_thresh = float(state.profile["editing_rules"].get("silence_threshold_db", -36.0))
            run = 0
            for db_val in win_db:
                if db_val < sil_thresh:
                    run += 1
                    max_sil_sec = max(max_sil_sec, run * 0.1)
                else:
                    run = 0
        metrics["max_residual_silence_sec"] = round(max_sil_sec, 2)
        allowed_sil = max(2.0, float(state.profile["editing_rules"].get("min_silence_sec", 0.8)) * 2.5)
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

    # Per-camera microphone level consistency analysis & automatic gain staging
    cam_rms_db: dict[str, float] = {}
    for cam_id, pcm in (state.audio_pcm_cache or {}).items():
        arr = np.asarray(pcm, dtype=np.float32)
        if arr.size > 0:
            active = arr[np.abs(arr) > 1e-4]
            if active.size > 0:
                rms_val = float(np.sqrt(np.mean(active * active) + 1e-18))
                cam_rms_db[cam_id] = round(20.0 * float(np.log10(rms_val + 1e-12)), 2)

    per_cam_trim_db: dict[str, float] = {}
    if len(cam_rms_db) >= 2:
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
            delta = max(-24.0, min(24.0, round(median_db - rms_db, 2)))
            if abs(delta) >= 2.5:
                per_cam_trim_db[cam_id] = delta
    state.metadata["per_camera_gain_trim_db"] = per_cam_trim_db

    # Mixed frame-rate & missing-media QC metrics
    source_fps_map = {cam_id: round(asset.fps, 4) for cam_id, asset in state.media_assets.items()}
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


def run_audio_graphics_qc_agent(
    state: ProjectAnalysisState,
    token: CancellationToken,
    report_progress: Callable[[float, str], None],
) -> dict[str, Any]:
    """Generate audio restoration chain, captions, lower-thirds, and run technical QC."""
    token.check("audio_graphics_qc start")
    if not state.rough_cut_shots:
        raise RuntimeError("rough_cut_multicam must complete before audio_graphics_qc")

    out_dir = Path(state.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Build audio restoration filter chain
    report_progress(0.15, "Configuring MLT/FFmpeg audio restoration chain")
    audio_rules = state.profile.get("audio_rules", {})
    audio_filters, ffmpeg_chain = build_audio_restoration_chain(
        audio_rules,
        rnnoise_model_path=state.rnnoise_model_path,
    )
    if not state.intent.restore_audio:
        for flt in audio_filters:
            flt.enabled = False
    state.audio_filters = audio_filters
    state.ffmpeg_audio_filtergraph = ffmpeg_chain

    # 2. Generate .srt & .ass captions
    report_progress(0.40, "Generating .srt and .ass captions")
    graphics_rules = state.profile.get("graphics_rules", {})
    enable_captions = bool(graphics_rules.get("enable_captions", True)) and state.intent.add_captions
    captions = _build_edited_captions(state) if enable_captions else []
    state.captions = captions

    if enable_captions and captions:
        spec = state.profile["video_spec"]
        srt_text = generate_srt_subtitles(captions)
        ass_text = generate_ass_subtitles(
            captions,
            width=int(spec["width"]),
            height=int(spec["height"]),
            brand_accent_hex=str(graphics_rules.get("brand_accent_hex", "#FF5A1F")),
        )
        srt_path = out_dir / "captions.srt"
        ass_path = out_dir / "captions.ass"
        srt_path.write_text(srt_text, encoding="utf-8")
        ass_path.write_text(ass_text, encoding="utf-8")
        state.artifacts["captions_srt"] = str(srt_path)
        state.artifacts["captions_ass"] = str(ass_path)

    # 3. Generate lower-third graphics
    token.check("generate lower thirds")
    report_progress(0.60, "Generating Kdenlive lower-third graphics")
    lower_thirds = _build_lower_thirds(state)
    state.lower_thirds = lower_thirds

    # 4. Run technical QC
    token.check("run technical qc")
    report_progress(0.78, "Running technical QC checks")
    qc_report = run_technical_qc(state)
    state.qc_report = qc_report

    qc_path = out_dir / "qc_report.json"
    qc_path.write_text(json.dumps(asdict(qc_report), indent=2), encoding="utf-8")
    state.artifacts["qc_report_json"] = str(qc_path)

    # 5. Emit validated Timeline Command API v1 batch
    report_progress(0.90, "Building Timeline Command API v1 batch for audio, graphics & QC")
    builder = TimelineCommandBuilder(
        project_id=state.project_id,
        sequence_id=state.sequence_id,
        expected_revision=state.revision,
        agent_id="audio_graphics_qc_agent",
        agent_role="audio_finishing_graphics_and_qc",
        transaction_label="Apply Audio Restoration, Captions, Lower Thirds & QC",
        requires_approval=False,
        dry_run=state.dry_run,
        fps_num=state.fps_num,
        fps_den=state.fps_den,
    )

    for flt in audio_filters:
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

    # Emit per-clip gain trims when microphones had inconsistent input levels
    per_cam_trim_db = state.metadata.get("per_camera_gain_trim_db", {})
    if per_cam_trim_db:
        for shot in state.rough_cut_shots:
            src_cam = shot.camera_id
            # Match audio source camera if available
            for cid, asset in state.media_assets.items():
                if asset.media_ref == shot.audio_media_ref:
                    src_cam = cid
                    break
            trim_db = per_cam_trim_db.get(src_cam)
            if trim_db is not None and abs(trim_db) >= 2.5:
                builder.add_operation(
                    "set_audio_level",
                    clip_id=f"{shot.shot_id}_a",
                    track_id="A1",
                    params={
                        "gain_db": float(trim_db),
                        "camera_id": src_cam,
                        "level_matching": True,
                    },
                    confidence=0.92,
                    rationale=f"Level-match microphone '{src_cam}' by {trim_db:+.1f} dB prior to master loudnorm",
                )

    builder.add_operation(
        "set_audio_level",
        track_id="A2",
        params={
            "gain_db": float(audio_rules.get("music_duck_db", -18.0)),
            "target_lufs": float(audio_rules.get("target_lufs", -16.0)),
            "true_peak_dbtp": float(audio_rules.get("true_peak_dbtp", -1.5)),
            "duck_under_dialogue": True,
        },
        confidence=0.96,
        rationale="Configure music bed ducking level relative to dialogue master A1",
    )

    for cue in captions:
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
            },
            confidence=cue.confidence,
            rationale=f"Add caption cue #{cue.index} ({cue.speaker_id})",
        )

    for lt in lower_thirds:
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
                "kdenlive_title_xml": lt.kdenlive_title_xml,
            },
            confidence=0.95,
            rationale=f"Place lower-third graphic for '{lt.title}' on V2",
        )

    for issue in qc_report.issues:
        cat = "qc_error" if issue.severity == "error" else "qc_warning"
        marker = state.add_review_marker(
            timeline_sec=issue.timeline_sec,
            label=f"QC [{issue.code}]: {issue.message[:80]}",
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

    report_progress(0.98, f"QC {'PASSED' if qc_report.passed else 'FAILED'} ({len(qc_report.issues)} findings)")
    return builder.build()
