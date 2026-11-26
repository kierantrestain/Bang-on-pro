"""Reference-video style learning (V1.6).

Analyses one or multiple reference videos locally using existing media tools
(FFmpeg/FFprobe, PySceneDetect, ``media_visual``, ``numpy``/``scipy``), caches
per-video observations by content hash in ``AnalysisCache``, optionally consults
an authorised subscription through the shared ``CliReasoner`` router when
semantic naming/synthesis is useful, and builds an editable, schema-validated
Show Profile stored in the standard custom-profiles directory.

Every mapped style dimension feeds directly into existing production agents so
selecting a learned style produces measurable differences in pacing, cuts,
transitions, colour, loudness, captions, graphics and framing on different
source footage.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

from bangonpro.ai_engine.department.cache import AnalysisCache
from bangonpro.ai_engine.department.media_visual import THUMB_W, analyse_camera
from bangonpro.show_profiles.loader import (
    default_custom_profiles_dir,
    list_show_profiles,
    load_show_profile,
    save_custom_profile,
    user_profile_dirs,
    validate_show_profile,
)

REF_STYLE_VERSION = "1.6.0-refstyle-1"
CACHE_NAMESPACE = "reference_style"


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return slug[:48] if slug else "reference_style"


def _probe_video(path: Path) -> dict[str, Any]:
    """Read container/stream metadata via ffprobe (no full decode)."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    if res.returncode != 0 or not res.stdout.strip():
        raise ValueError(f"Could not inspect reference video '{path.name}': {res.stderr.strip()[:200]}")
    data = json.loads(res.stdout)
    streams = data.get("streams") or []
    v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    a_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v_stream is None:
        raise ValueError(f"Reference file '{path.name}' contains no video stream")
    fmt = data.get("format") or {}
    dur = float(fmt.get("duration") or v_stream.get("duration") or 0.0)
    w = int(v_stream.get("width") or 1920)
    h = int(v_stream.get("height") or 1080)
    fps_raw = str(v_stream.get("r_frame_rate") or v_stream.get("avg_frame_rate") or "25/1")
    fps_num, fps_den = 25, 1
    if "/" in fps_raw:
        a, b = fps_raw.split("/", 1)
        try:
            fps_num, fps_den = max(1, int(a)), max(1, int(b))
        except ValueError:
            fps_num, fps_den = 25, 1
    return {
        "duration_sec": max(0.1, dur),
        "width": w,
        "height": h,
        "fps": round(fps_num / fps_den, 3),
        "fps_num": fps_num,
        "fps_den": fps_den,
        "has_audio": a_stream is not None,
        "aspect": "vertical" if h > w else ("square" if w == h else "landscape"),
    }


def _detect_scene_cuts(path: Path, duration: float, visual_cuts: list[float]) -> list[float]:
    """Detect shot cuts with PySceneDetect ContentDetector, merged with visual cuts."""
    cuts: list[float] = []
    try:
        from scenedetect import ContentDetector, detect

        scenes = detect(str(path), ContentDetector(threshold=24.0, min_scene_len=8))
        for start, _end in scenes[1:]:
            t = float(start.seconds)
            if 0.25 < t < duration - 0.25:
                cuts.append(round(t, 3))
    except Exception:
        cuts = []

    merged = sorted(cuts + [float(t) for t in visual_cuts if 0.25 < float(t) < duration - 0.25])
    deduped: list[float] = []
    for t in merged:
        if not deduped or t - deduped[-1] >= 0.35:
            deduped.append(round(t, 3))
    return deduped


def _analyse_audio_ebur128(path: Path, duration: float) -> dict[str, Any]:
    """Measure integrated loudness (LUFS), LRA, true peak and silence pauses via FFmpeg."""
    # Bound analysis window to at most 180 s so multi-minute references stay fast.
    limit = min(duration, 180.0)
    cmd = [
        "ffmpeg",
        "-v",
        "info",
        "-nostats",
        "-t",
        f"{limit:.3f}",
        "-i",
        str(path),
        "-vn",
        "-af",
        "ebur128=peak=true,silencedetect=noise=-38dB:d=0.25",
        "-f",
        "null",
        "-",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)
    stderr = res.stderr or ""
    lufs = None
    lra = None
    peak = None
    m_i = re.findall(r"I:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*LUFS", stderr)
    if m_i:
        lufs = float(m_i[-1])
    m_lra = re.findall(r"LRA:\s*([0-9]+(?:\.[0-9]+)?)\s*LU", stderr)
    if m_lra:
        lra = float(m_lra[-1])
    m_pk = re.findall(r"Peak:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*dBFS", stderr)
    if m_pk:
        peak = float(m_pk[-1])
    sil_durs = [float(x) for x in re.findall(r"silence_duration:\s*([0-9]+(?:\.[0-9]+)?)", stderr)]
    sil_starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9]+(?:\.[0-9]+)?)", stderr)]
    sil_ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)", stderr)]
    total_sil = sum(sil_durs)
    speech_onset_sec = 0.0
    if sil_starts and sil_starts[0] < 0.25 and sil_ends:
        speech_onset_sec = round(min(5.0, sil_ends[0]), 3)
    return {
        "integrated_lufs": round(lufs, 2) if lufs is not None else -15.0,
        "loudness_range_lu": round(lra, 2) if lra is not None else 6.0,
        "true_peak_dbtp": round(min(-0.5, max(-6.0, peak)), 2) if peak is not None else -1.2,
        "silence_count": len(sil_durs),
        "median_pause_sec": round(float(np.median(sil_durs)), 3) if sil_durs else 0.45,
        "silence_ratio": round(min(1.0, total_sil / max(0.5, limit)), 4),
        "speech_onset_sec": speech_onset_sec,
        "measured": lufs is not None,
    }


def _sample_overlay_and_colour_frames(path: Path, duration: float, width: int, height: int, max_frames: int = 18) -> dict[str, Any]:
    """Sample bounded 160px-wide frames to inspect captions, graphics, transitions, zoom and colour."""
    sample_w = 160
    sample_h = max(32, int(round(sample_w * height / max(1, width))) & ~1)
    n_samples = max(4, min(max_frames, int(max(4, min(duration, 180.0) / 1.0))))
    interval = max(0.25, min(duration, 180.0) / max(1, n_samples))
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-nostats",
        "-t",
        f"{min(duration, 180.0):.3f}",
        "-i",
        str(path),
        "-an",
        "-vf",
        f"fps=1/{interval:.3f},scale={sample_w}:{sample_h}:flags=area",
        "-frames:v",
        str(n_samples),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]
    res = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    frame_bytes = sample_w * sample_h * 3
    if res.returncode != 0 or len(res.stdout) < frame_bytes:
        return {}
    count = len(res.stdout) // frame_bytes
    frames = np.frombuffer(res.stdout[: count * frame_bytes], dtype=np.uint8).reshape(count, sample_h, sample_w, 3)
    f01 = frames.astype(np.float64) / 255.0

    # 1. Colour characteristics: luma, contrast, saturation, warm/cool balance, accent colour
    luma = 0.2126 * f01[..., 0] + 0.7152 * f01[..., 1] + 0.0722 * f01[..., 2]
    mean_rgb = f01.mean(axis=(0, 1, 2))
    cmax = f01.max(axis=-1)
    cmin = f01.min(axis=-1)
    sat = np.where(cmax > 1e-4, (cmax - cmin) / np.maximum(cmax, 1e-4), 0.0)
    median_luma = float(np.median(luma.mean(axis=(1, 2))))
    contrast_span = float(np.median(np.percentile(luma, 95, axis=(1, 2)) - np.percentile(luma, 5, axis=(1, 2))))
    mean_sat = float(np.mean(sat))
    warmth = float((mean_rgb[0] - mean_rgb[2]) / max(0.05, float(mean_rgb.mean())))

    # Extract dominant saturated accent colour (e.g. from lower thirds, graphics or brand palette)
    vivid_mask = (sat > 0.45) & (luma > 0.18) & (luma < 0.92)
    accent_hex = "#FF5A1F"
    if int(vivid_mask.sum()) >= 20:
        vivid_pixels = frames[vivid_mask]
        # Quantise to 16-level buckets to find the most prominent brand accent
        q = (vivid_pixels // 16) * 16 + 8
        uniq, counts = np.unique(q.reshape(-1, 3), axis=0, return_counts=True)
        dom = uniq[int(np.argmax(counts))]
        accent_hex = f"#{int(dom[0]):02X}{int(dom[1]):02X}{int(dom[2]):02X}"

    # 2. Opening & closing fade detection
    frame_lumas = luma.mean(axis=(1, 2))
    fade_in_sec = 0.0
    if len(frame_lumas) >= 3 and frame_lumas[0] < 0.12 and frame_lumas[1] > frame_lumas[0] + 0.08:
        fade_in_sec = round(min(1.5, interval), 2)
    fade_out_sec = 0.0
    if len(frame_lumas) >= 3 and frame_lumas[-1] < 0.12 and frame_lumas[-2] > frame_lumas[-1] + 0.08:
        fade_out_sec = round(min(1.5, interval), 2)

    # 3. Caption band detection (high horizontal edge density + bright/high-contrast glyphs in bottom-center or center band)
    gx = np.abs(np.diff(luma, axis=2))
    gy = np.abs(np.diff(luma, axis=1))
    bot_slice = slice(int(sample_h * 0.72), int(sample_h * 0.94))
    mid_slice = slice(int(sample_h * 0.42), int(sample_h * 0.65))
    top_slice = slice(int(sample_h * 0.04), int(sample_h * 0.20))
    cx_slice = slice(int(sample_w * 0.18), int(sample_w * 0.82))
    left_slice = slice(int(sample_w * 0.04), int(sample_w * 0.46))
    right_slice = slice(int(sample_w * 0.54), int(sample_w * 0.96))

    bot_text_energy = float(np.mean(gx[:, bot_slice, cx_slice] > 0.16))
    mid_text_energy = float(np.mean(gx[:, mid_slice, cx_slice] > 0.16))
    bot_left_overlay = float(np.mean((sat[:, bot_slice, left_slice] > 0.38) | (gx[:, bot_slice, left_slice] > 0.18)))
    bot_right_overlay = float(np.mean((sat[:, bot_slice, right_slice] > 0.38) | (gx[:, bot_slice, right_slice] > 0.18)))
    top_left_overlay = float(np.mean((sat[:, top_slice, left_slice] > 0.38) | (gx[:, top_slice, left_slice] > 0.18)))
    top_right_overlay = float(np.mean((sat[:, top_slice, right_slice] > 0.38) | (gx[:, top_slice, right_slice] > 0.18)))

    caption_detected = (bot_text_energy > 0.035) or (mid_text_energy > 0.055)
    caption_position = "center" if (mid_text_energy > bot_text_energy * 1.35 and mid_text_energy > 0.05) else "bottom_center"

    # Graphics placement preference from strongest overlay region
    zone_scores = {
        "bottom_left": bot_left_overlay,
        "bottom_right": bot_right_overlay,
        "top_left": top_left_overlay,
        "top_right": top_right_overlay,
    }
    best_zone = max(zone_scores, key=lambda k: zone_scores[k])
    graphics_detected = zone_scores[best_zone] > 0.045

    # Temporal variance of lower-third band -> intermittent graphics vs persistent bug
    lt_per_frame = (gx[:, bot_slice, left_slice] > 0.16).mean(axis=(1, 2))
    graphics_changes = int(np.sum(np.abs(np.diff(lt_per_frame)) > 0.03)) if len(lt_per_frame) > 1 else 0

    # 4. Dissolve / soft transition ratio vs hard cuts from inter-frame luma ramps
    diffs = np.mean(np.abs(np.diff(f01, axis=0)), axis=(1, 2, 3))
    hard_jumps = int(np.sum(diffs > 0.22))
    soft_ramps = int(np.sum((diffs > 0.07) & (diffs <= 0.22)))
    dissolve_ratio = round(soft_ramps / max(1, hard_jumps + soft_ramps), 3)

    # 5. Zoom / punch-in / camera movement proxy: radial edge-gradient scale shifts between consecutive frames
    edge_mag = np.sqrt(gx[:, :-1, :] ** 2 + gy[:, :, :-1] ** 2)
    cy, cx = edge_mag.shape[1] // 2, edge_mag.shape[2] // 2
    center_energy = edge_mag[:, cy // 2 : cy + cy // 2, cx // 2 : cx + cx // 2].mean(axis=(1, 2))
    outer_energy = edge_mag.mean(axis=(1, 2))
    center_ratio = center_energy / np.maximum(outer_energy, 1e-4)
    zoom_changes = int(np.sum(np.abs(np.diff(center_ratio)) > 0.14)) if len(center_ratio) > 1 else 0

    return {
        "median_luma": round(median_luma, 4),
        "contrast_span": round(contrast_span, 4),
        "mean_saturation": round(mean_sat, 4),
        "warmth_rb": round(warmth, 4),
        "mean_rgb": [round(float(x), 4) for x in mean_rgb],
        "accent_hex": accent_hex,
        "fade_in_sec": fade_in_sec,
        "fade_out_sec": fade_out_sec,
        "caption_detected": caption_detected,
        "caption_position": caption_position,
        "bot_text_energy": round(bot_text_energy, 4),
        "graphics_detected": graphics_detected,
        "graphics_zone": best_zone,
        "graphics_changes": graphics_changes,
        "dissolve_ratio": dissolve_ratio,
        "zoom_changes": zoom_changes,
        "frames_sampled": count,
    }


def analyse_reference_video(
    video_path: str | Path,
    *,
    cache: AnalysisCache | None = None,
) -> dict[str, Any]:
    """Analyse one reference video locally and return structured observations (cached by content hash)."""
    path = Path(video_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Reference video not found: {path}")

    cache = cache or AnalysisCache()
    content_hash = _file_sha256(path)
    cached = cache.get(CACHE_NAMESPACE, REF_STYLE_VERSION, content_hash)
    if isinstance(cached, dict) and cached.get("content_hash") == content_hash:
        out = copy.deepcopy(cached)
        out["cache_hit"] = True
        out["source_path"] = str(path)
        return out

    probe = _probe_video(path)
    duration = probe["duration_sec"]
    visual = analyse_camera(
        str(path),
        duration=duration,
        width=probe["width"],
        height=probe["height"],
        interval_sec=min(1.0, max(0.35, duration / 120.0)),
    )
    cuts = _detect_scene_cuts(path, duration, visual.get("scene_cuts_sec") or [])
    boundaries = [0.0, *cuts, duration]
    shot_durs = [round(b - a, 3) for a, b in zip(boundaries[:-1], boundaries[1:]) if b - a >= 0.2]
    if not shot_durs:
        shot_durs = [round(duration, 3)]

    median_shot = float(np.median(shot_durs))
    p25_shot = float(np.percentile(shot_durs, 25))
    p75_shot = float(np.percentile(shot_durs, 75))
    cuts_per_min = round(len(cuts) / max(1.0 / 60.0, duration / 60.0), 2)

    audio = _analyse_audio_ebur128(path, duration) if probe["has_audio"] else {
        "integrated_lufs": -15.0,
        "loudness_range_lu": 6.0,
        "true_peak_dbtp": -1.2,
        "silence_count": 0,
        "median_pause_sec": 0.45,
        "silence_ratio": 0.0,
        "speech_onset_sec": 0.0,
        "measured": False,
    }
    overlay = _sample_overlay_and_colour_frames(path, duration, probe["width"], probe["height"])

    # Framing, zoom and B-roll pattern observations from visual samples
    samples = visual.get("samples") or {}
    boxes = [b for b in (samples.get("box") or []) if b]
    skin_shares = [float(x) for x in (samples.get("skin_share") or [])]
    motions = [float(m) for m in (samples.get("motion") or []) if m is not None]
    box_areas = [(b[2] - b[0]) * (b[3] - b[1]) for b in boxes]
    area_std = float(np.std(box_areas)) if len(box_areas) >= 3 else 0.0
    mean_motion = float(np.mean(motions)) if motions else 0.0
    # Non-skin-dominant frames in a video that otherwise has a presenter indicate B-roll / cutaway shots
    has_presenter = bool(skin_shares and max(skin_shares) >= 0.03)
    broll_share = (
        round(float(np.mean([1.0 if s < 0.015 else 0.0 for s in skin_shares])), 3)
        if has_presenter and len(skin_shares) >= 4
        else 0.0
    )

    first_shot_sec = shot_durs[0]
    hook_opening = bool(first_shot_sec <= 6.0 and audio["speech_onset_sec"] <= 1.2)
    if cuts_per_min >= 14.0 or median_shot <= 3.5:
        pacing_tier = "fast"
    elif cuts_per_min >= 6.0 or median_shot <= 8.0:
        pacing_tier = "balanced"
    else:
        pacing_tier = "measured"

    warmth = float(overlay.get("warmth_rb", 0.0))
    contrast_span = float(overlay.get("contrast_span", 0.55))
    mean_sat = float(overlay.get("mean_saturation", 0.25))
    if contrast_span >= 0.65 and mean_sat >= 0.30:
        colour_treatment = "crisp_high_contrast"
    elif warmth >= 0.07:
        colour_treatment = "warm_cinematic"
    elif warmth <= -0.07:
        colour_treatment = "cool_editorial"
    else:
        colour_treatment = "natural_studio"

    result: dict[str, Any] = {
        "schema_version": "1.0",
        "version": REF_STYLE_VERSION,
        "content_hash": content_hash,
        "source_path": str(path),
        "file_name": path.name,
        "cache_hit": False,
        "media": probe,
        "pacing": {
            "shot_count": len(shot_durs),
            "cut_count": len(cuts),
            "cuts_per_minute": cuts_per_min,
            "median_shot_sec": round(median_shot, 3),
            "p25_shot_sec": round(p25_shot, 3),
            "p75_shot_sec": round(p75_shot, 3),
            "min_shot_sec": round(min(shot_durs), 3),
            "max_shot_sec": round(max(shot_durs), 3),
            "pacing_tier": pacing_tier,
        },
        "transitions": {
            "hard_cut_share": round(1.0 - float(overlay.get("dissolve_ratio", 0.0)), 3),
            "dissolve_ratio": float(overlay.get("dissolve_ratio", 0.0)),
            "fade_in_sec": float(overlay.get("fade_in_sec", 0.0)),
            "fade_out_sec": float(overlay.get("fade_out_sec", 0.0)),
        },
        "opening": {
            "first_shot_sec": round(first_shot_sec, 3),
            "speech_onset_sec": audio["speech_onset_sec"],
            "hook_opening": hook_opening,
            "opening_fade_in_sec": float(overlay.get("fade_in_sec", 0.0)),
        },
        "camera_framing": {
            "mean_motion_energy": round(mean_motion, 4),
            "framing_area_std": round(area_std, 4),
            "zoom_punch_events": int(overlay.get("zoom_changes", 0)),
            "dynamic_zoom_style": bool(area_std > 0.04 or int(overlay.get("zoom_changes", 0)) >= 3),
        },
        "captions": {
            "detected": bool(overlay.get("caption_detected", False)),
            "position": str(overlay.get("caption_position", "bottom_center")),
            "text_band_energy": float(overlay.get("bot_text_energy", 0.0)),
        },
        "graphics": {
            "detected": bool(overlay.get("graphics_detected", False)),
            "preferred_position": str(overlay.get("graphics_zone", "bottom_left")),
            "accent_hex": str(overlay.get("accent_hex", "#FF5A1F")),
            "overlay_transitions": int(overlay.get("graphics_changes", 0)),
        },
        "colour": {
            "median_luma": float(overlay.get("median_luma", 0.45)),
            "contrast_span": contrast_span,
            "mean_saturation": mean_sat,
            "warmth_rb": warmth,
            "mean_rgb": overlay.get("mean_rgb", [0.45, 0.45, 0.45]),
            "treatment": colour_treatment,
        },
        "audio": audio,
        "broll": {
            "presenter_detected": has_presenter,
            "cutaway_share": broll_share,
            "frequent_broll": bool(broll_share >= 0.20),
        },
    }
    cache.put(CACHE_NAMESPACE, REF_STYLE_VERSION, content_hash, result)
    return result


def _aggregate_observations(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine observations from one or more reference videos into one consensus analysis."""
    if not items:
        raise ValueError("At least one reference video is required")

    def med(vals: list[float], default: float = 0.0) -> float:
        return round(float(np.median(vals)), 3) if vals else default

    aspects = [x["media"]["aspect"] for x in items]
    vertical_majority = aspects.count("vertical") > len(aspects) / 2
    cuts_per_min = med([x["pacing"]["cuts_per_minute"] for x in items], 8.0)
    median_shot = med([x["pacing"]["median_shot_sec"] for x in items], 5.0)
    p25_shot = med([x["pacing"]["p25_shot_sec"] for x in items], 2.0)
    p75_shot = med([x["pacing"]["p75_shot_sec"] for x in items], 10.0)
    dissolve_ratio = med([x["transitions"]["dissolve_ratio"] for x in items], 0.0)
    fade_in_sec = med([x["transitions"]["fade_in_sec"] for x in items], 0.0)
    fade_out_sec = med([x["transitions"]["fade_out_sec"] for x in items], 0.3)
    first_shot_sec = med([x["opening"]["first_shot_sec"] for x in items], 4.0)
    hook_opening = sum(1 for x in items if x["opening"]["hook_opening"]) >= max(1, len(items) // 2)
    dynamic_zoom = any(x["camera_framing"]["dynamic_zoom_style"] for x in items)

    cap_detected = any(x["captions"]["detected"] for x in items)
    cap_positions = [x["captions"]["position"] for x in items]
    cap_pos = max(set(cap_positions), key=cap_positions.count)

    gfx_detected = any(x["graphics"]["detected"] for x in items)
    gfx_positions = [x["graphics"]["preferred_position"] for x in items]
    gfx_pos = max(set(gfx_positions), key=gfx_positions.count)
    accent_hex = items[0]["graphics"]["accent_hex"]

    median_luma = med([x["colour"]["median_luma"] for x in items], 0.45)
    contrast_span = med([x["colour"]["contrast_span"] for x in items], 0.55)
    mean_sat = med([x["colour"]["mean_saturation"] for x in items], 0.25)
    warmth_rb = med([x["colour"]["warmth_rb"] for x in items], 0.0)
    mean_rgb = [
        med([x["colour"]["mean_rgb"][c] for x in items], 0.45) for c in range(3)
    ]
    treatments = [x["colour"]["treatment"] for x in items]
    treatment = max(set(treatments), key=treatments.count)

    lufs = med([x["audio"]["integrated_lufs"] for x in items], -14.0)
    lufs = round(min(-9.0, max(-24.0, lufs)), 1)
    true_peak = med([x["audio"]["true_peak_dbtp"] for x in items], -1.0)
    true_peak = round(min(-0.5, max(-3.0, true_peak)), 1)
    median_pause = med([x["audio"]["median_pause_sec"] for x in items], 0.45)
    silence_ratio = med([x["audio"]["silence_ratio"] for x in items], 0.1)
    broll_share = med([x["broll"]["cutaway_share"] for x in items], 0.0)

    if cuts_per_min >= 14.0 or median_shot <= 3.5:
        pacing_tier = "fast"
    elif cuts_per_min >= 6.0 or median_shot <= 8.0:
        pacing_tier = "balanced"
    else:
        pacing_tier = "measured"

    return {
        "video_count": len(items),
        "sources": [
            {
                "file_name": x["file_name"],
                "source_path": x["source_path"],
                "content_hash": x["content_hash"],
                "duration_sec": x["media"]["duration_sec"],
                "cache_hit": x["cache_hit"],
            }
            for x in items
        ],
        "aspect": "vertical" if vertical_majority else "landscape",
        "pacing": {
            "cuts_per_minute": cuts_per_min,
            "median_shot_sec": median_shot,
            "p25_shot_sec": p25_shot,
            "p75_shot_sec": p75_shot,
            "pacing_tier": pacing_tier,
        },
        "transitions": {
            "dissolve_ratio": dissolve_ratio,
            "fade_in_sec": fade_in_sec,
            "fade_out_sec": fade_out_sec,
        },
        "opening": {
            "first_shot_sec": first_shot_sec,
            "hook_opening": hook_opening,
        },
        "camera_framing": {
            "dynamic_zoom_style": dynamic_zoom,
        },
        "captions": {
            "detected": cap_detected,
            "position": cap_pos,
        },
        "graphics": {
            "detected": gfx_detected,
            "preferred_position": gfx_pos,
            "accent_hex": accent_hex,
        },
        "colour": {
            "median_luma": median_luma,
            "contrast_span": contrast_span,
            "mean_saturation": mean_sat,
            "warmth_rb": warmth_rb,
            "mean_rgb": mean_rgb,
            "treatment": treatment,
        },
        "audio": {
            "integrated_lufs": lufs,
            "true_peak_dbtp": true_peak,
            "median_pause_sec": median_pause,
            "silence_ratio": silence_ratio,
        },
        "broll": {
            "cutaway_share": broll_share,
        },
    }


def _optional_semantic_synthesis(summary: dict[str, Any], *, allow_reasoning: bool = True) -> dict[str, Any]:
    """Use authorised subscription reasoning selectively for semantic style notes; never fake."""
    if not allow_reasoning:
        return {"used": False, "provider": None, "reason": "Local-only analysis requested"}
    try:
        from bangonpro.ai_engine.mcp.reasoning import CliReasoner, enabled, load_config

        if not enabled(load_config()):
            return {"used": False, "provider": None, "reason": "Subscription reasoning is switched off (Local Only)"}
        router = CliReasoner()
        if not router.authorised():
            return {"used": False, "provider": None, "reason": "No subscription connected"}
        router.begin_task("reference_style_analysis")

        def prompt(_policy: dict[str, Any]) -> str:
            compact = {
                "pacing": summary["pacing"],
                "opening": summary["opening"],
                "colour": summary["colour"],
                "audio": summary["audio"],
                "captions": summary["captions"],
                "graphics": summary["graphics"],
                "broll": summary["broll"],
            }
            return (
                "Summarise this measured reference-video editing style for a video editor. "
                "Reply with ONE JSON object and nothing else: "
                "{\"summary\": <1-2 plain sentences>, \"suggested_name\": <short style name>}.\n"
                f"Metrics: {json.dumps(compact)}"
            )

        ans = router.plan(prompt, purpose="reference_style_summary", agent="learning")
        return {
            "used": True,
            "provider": router.provider,
            "summary": str(ans.get("summary") or "")[:300],
            "suggested_name": str(ans.get("suggested_name") or "")[:64],
        }
    except Exception as exc:  # noqa: BLE001 - local style learning always completes cleanly
        return {"used": False, "provider": None, "reason": f"Local analysis only ({str(exc)[:120]})"}


def style_to_show_profile(
    summary: dict[str, Any],
    *,
    name: str,
    profile_id: str | None = None,
    base_profile: str | None = None,
    overrides: dict[str, Any] | None = None,
    reasoning_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert aggregated reference-video observations into a validated Show Profile v1."""
    if base_profile:
        base_id = base_profile
    elif summary.get("aspect") == "vertical":
        base_id = "social_short"
    else:
        base_id = "youtube_talking_head"

    prof = copy.deepcopy(load_show_profile(base_id, custom_dirs=user_profile_dirs()))
    pid = _slugify(profile_id or name)
    if pid in set(list_show_profiles()):
        pid = f"ref_{pid}"
    prof["profile_id"] = pid
    prof["display_name"] = name.strip() or pid.replace("_", " ").title()

    pacing = summary["pacing"]
    audio = summary["audio"]
    colour = summary["colour"]
    captions = summary["captions"]
    graphics = summary["graphics"]
    transitions = summary["transitions"]
    opening = summary["opening"]
    broll = summary["broll"]

    # 1. Pacing & editing rules derived directly from measured shot durations & pauses
    min_shot = round(min(4.0, max(0.8, pacing["p25_shot_sec"] * 0.85)), 2)
    max_shot = round(min(30.0, max(min_shot + 2.0, pacing["p75_shot_sec"] * 1.4)), 2)
    if pacing["pacing_tier"] == "fast":
        min_silence = round(min(0.45, max(0.25, audio["median_pause_sec"] * 0.7)), 2)
        natural_pause = round(min(0.32, max(0.18, min_silence * 0.7)), 2)
        turn_pause = round(min(0.38, max(0.22, min_silence * 0.85)), 2)
        keep_pad = 0.08
    elif pacing["pacing_tier"] == "measured":
        min_silence = round(min(1.4, max(0.65, audio["median_pause_sec"] * 1.15)), 2)
        natural_pause = round(min(0.85, max(0.48, min_silence * 0.75)), 2)
        turn_pause = round(min(0.95, max(0.55, min_silence * 0.85)), 2)
        keep_pad = 0.20
    else:
        min_silence = round(min(0.85, max(0.40, audio["median_pause_sec"] * 0.95)), 2)
        natural_pause = round(min(0.55, max(0.30, min_silence * 0.75)), 2)
        turn_pause = round(min(0.62, max(0.36, min_silence * 0.85)), 2)
        keep_pad = 0.14

    reaction_freq = round(min(0.6, max(0.05, broll["cutaway_share"] * 0.8 + (0.15 if pacing["pacing_tier"] == "fast" else 0.0))), 2)
    wide_interval = round(min(90.0, max(8.0, pacing["median_shot_sec"] * 4.0)), 1)

    prof["min_shot_duration_sec"] = min_shot
    prof["max_shot_duration_sec"] = max_shot
    prof["reaction_shot_frequency"] = reaction_freq
    prof["wide_shot_interval_sec"] = wide_interval
    prof["loudness_target_lufs"] = float(audio["integrated_lufs"])

    er = prof.setdefault("editing_rules", {})
    er.update(
        {
            "min_shot_sec": min_shot,
            "max_shot_sec": max_shot,
            "min_shot_duration_sec": min_shot,
            "max_shot_duration_sec": max_shot,
            "min_silence_sec": min_silence,
            "natural_pause_sec": natural_pause,
            "turn_pause_sec": turn_pause,
            "beat_pause_sec": round(turn_pause * 1.15, 2),
            "keep_padding_sec": keep_pad,
            "reaction_shot_frequency": reaction_freq,
            "wide_shot_interval_sec": wide_interval,
            "pause_policy": "tight" if pacing["pacing_tier"] == "fast" else ("natural" if pacing["pacing_tier"] == "measured" else "compress"),
            "punch_in_zoom": bool(summary["camera_framing"]["dynamic_zoom_style"]),
        }
    )

    # 2. Transitions & opening structure
    io = prof.setdefault("intro_outro_rules", {})
    io["fade_in_sec"] = float(transitions["fade_in_sec"])
    io["fade_out_sec"] = float(transitions["fade_out_sec"])
    io["open_on_wide"] = not bool(opening["hook_opening"])
    io["opening_wide_sec"] = 0.0 if opening["hook_opening"] else round(min(4.0, opening["first_shot_sec"]), 2)

    # 3. Audio loudness & mixing
    ar = prof.setdefault("audio_rules", {})
    ar["target_lufs"] = float(audio["integrated_lufs"])
    ar["loudness_target_lufs"] = float(audio["integrated_lufs"])
    ar["true_peak_dbtp"] = float(audio["true_peak_dbtp"])
    if "delivery_spec" in prof:
        prof["delivery_spec"]["target_lufs"] = float(audio["integrated_lufs"])
        prof["delivery_spec"]["true_peak_dbtp"] = float(audio["true_peak_dbtp"])

    # 4. Colour characteristics (actually applied by Colourist when match_reference_look=True)
    mean_rgb = [max(0.05, float(x)) for x in colour["mean_rgb"]]
    rgb_avg = sum(mean_rgb) / 3.0
    # Subtle, safe channel tint derived from reference's measured colour balance (clamped to +/- 12%)
    tint_rgb = [round(min(1.12, max(0.88, c / max(0.05, rgb_avg))), 4) for c in mean_rgb]
    target_luma = round(min(0.75, max(0.22, float(colour["median_luma"]))), 3)
    contrast_boost = round(min(1.22, max(0.88, 0.85 + float(colour["contrast_span"]) * 0.45)), 3)
    sat_scale = round(min(1.25, max(0.80, 0.82 + float(colour["mean_saturation"]) * 0.75)), 3)

    cr = prof.setdefault("colour_rules", {})
    cr.update(
        {
            "enabled": True,
            "correct_exposure": True,
            "match_reference_look": True,
            "target_luma": target_luma,
            "treatment": colour["treatment"],
            "style_tint_rgb": tint_rgb,
            "style_contrast": contrast_boost,
            "style_saturation": sat_scale,
        }
    )

    # 5. Graphics & Captions
    accent = str(graphics["accent_hex"])
    lt_pos = str(graphics["preferred_position"])
    if lt_pos not in ("bottom_left", "bottom_center", "bottom_right", "top_left", "top_right"):
        lt_pos = "bottom_left"
    gr = prof.setdefault("graphics_rules", {})
    gr.update(
        {
            "enable_captions": True,
            "enable_lower_thirds": True,
            "brand_accent_hex": accent,
            "lower_third_duration_sec": 3.5 if pacing["pacing_tier"] == "fast" else (5.5 if pacing["pacing_tier"] == "measured" else 4.2),
            "enable_title_card": bool(not opening["hook_opening"] or graphics["detected"]),
            "animation_style": "slide_left" if pacing["pacing_tier"] == "fast" else "fade_pop",
        }
    )
    gt = prof.setdefault("graphics_templates", {})
    gt["lower_third_position"] = lt_pos
    gt["accent_hex"] = accent

    cs = prof.setdefault("caption_style", {})
    cs["highlight_color_hex"] = accent
    cs["alignment"] = "bottom_center" if captions["position"] not in ("bottom_left", "bottom_center", "bottom_right", "top_center") else captions["position"]
    cs["uppercase"] = bool(pacing["pacing_tier"] == "fast" and summary.get("aspect") == "vertical")
    cs["max_chars_per_line"] = 24 if pacing["pacing_tier"] == "fast" else 42
    cs["burn_in"] = bool(captions["detected"] or summary.get("aspect") == "vertical")

    # Apply any user-edited overrides from the preview/modify UI
    if overrides:
        for dotted_key, val in overrides.items():
            parts = str(dotted_key).split(".")
            node: Any = prof
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = val
        # Keep mirrored top-level fields in sync
        if "editing_rules.min_shot_duration_sec" in overrides:
            prof["min_shot_duration_sec"] = float(overrides["editing_rules.min_shot_duration_sec"])
            prof["editing_rules"]["min_shot_sec"] = float(overrides["editing_rules.min_shot_duration_sec"])
        if "editing_rules.max_shot_duration_sec" in overrides:
            prof["max_shot_duration_sec"] = float(overrides["editing_rules.max_shot_duration_sec"])
            prof["editing_rules"]["max_shot_sec"] = float(overrides["editing_rules.max_shot_duration_sec"])
        if "audio_rules.target_lufs" in overrides:
            lufs_val = float(overrides["audio_rules.target_lufs"])
            prof["loudness_target_lufs"] = lufs_val
            prof["audio_rules"]["loudness_target_lufs"] = lufs_val
            if "delivery_spec" in prof:
                prof["delivery_spec"]["target_lufs"] = lufs_val
        if "graphics_rules.brand_accent_hex" in overrides:
            hex_val = str(overrides["graphics_rules.brand_accent_hex"])
            prof.setdefault("graphics_templates", {})["accent_hex"] = hex_val
            prof.setdefault("caption_style", {})["highlight_color_hex"] = hex_val

    auto_desc = (
        f"Learned from {summary['video_count']} reference video(s): "
        f"{pacing['pacing_tier']} pacing ({pacing['cuts_per_minute']:.1f} cuts/min, "
        f"{pacing['median_shot_sec']:.1f}s median shot), {audio['integrated_lufs']:.1f} LUFS, "
        f"{colour['treatment'].replace('_', ' ')} look, accent {accent}."
    )
    if reasoning_info and reasoning_info.get("used") and reasoning_info.get("summary"):
        prof["description"] = f"{reasoning_info['summary']} ({auto_desc})"
    else:
        prof["description"] = auto_desc

    prof["reference_style"] = {
        "learned": True,
        "version": REF_STYLE_VERSION,
        "base_profile": base_id,
        "observations": summary,
        "reasoning": reasoning_info or {"used": False, "provider": None, "reason": "Local analysis"},
    }
    validate_show_profile(prof)
    return prof


def learn_style_from_references(
    video_paths: list[str | Path],
    *,
    name: str,
    profile_id: str | None = None,
    base_profile: str | None = None,
    overrides: dict[str, Any] | None = None,
    save: bool = False,
    output_dir: str | Path | None = None,
    cache: AnalysisCache | None = None,
    allow_reasoning: bool = True,
) -> dict[str, Any]:
    """Full reference-video style learning workflow: analyse, aggregate, build profile, optionally save."""
    if not video_paths:
        raise ValueError("Provide at least one reference video")
    cache = cache or AnalysisCache()
    per_video = [analyse_reference_video(p, cache=cache) for p in video_paths]
    summary = _aggregate_observations(per_video)
    reasoning_info = _optional_semantic_synthesis(summary, allow_reasoning=allow_reasoning)
    style_name = name.strip() or reasoning_info.get("suggested_name") or Path(video_paths[0]).stem.replace("_", " ").title()
    profile = style_to_show_profile(
        summary,
        name=style_name,
        profile_id=profile_id,
        base_profile=base_profile,
        overrides=overrides,
        reasoning_info=reasoning_info,
    )
    saved_path: str | None = None
    if save:
        target_dir = Path(output_dir).expanduser().resolve() if output_dir else default_custom_profiles_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        out_file = target_dir / f"{profile['profile_id']}.json"
        save_custom_profile(profile, out_file)
        saved_path = str(out_file)
    return {
        "profile_id": profile["profile_id"],
        "display_name": profile["display_name"],
        "description": profile["description"],
        "saved_path": saved_path,
        "observations": summary,
        "per_video": per_video,
        "reasoning": reasoning_info,
        "profile": profile,
    }


def list_reference_styles(custom_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """List saved custom style profiles (marking those learned from reference videos)."""
    folder = Path(custom_dir).expanduser().resolve() if custom_dir else default_custom_profiles_dir()
    if not folder.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for f in sorted(folder.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        pid = str(data.get("profile_id") or f.stem)
        ref_meta = data.get("reference_style") if isinstance(data.get("reference_style"), dict) else None
        out.append(
            {
                "profile_id": pid,
                "display_name": str(data.get("display_name") or pid),
                "description": str(data.get("description") or ""),
                "path": str(f),
                "learned_from_reference": bool(ref_meta and ref_meta.get("learned")),
                "source_count": len((ref_meta.get("observations") or {}).get("sources") or []) if ref_meta else 0,
                "pacing_tier": ((ref_meta.get("observations") or {}).get("pacing") or {}).get("pacing_tier") if ref_meta else None,
                "target_lufs": (data.get("audio_rules") or {}).get("target_lufs"),
                "accent_hex": (data.get("graphics_rules") or {}).get("brand_accent_hex"),
            }
        )
    return out


def delete_reference_style(profile_id: str, custom_dir: str | Path | None = None) -> bool:
    """Remove a saved custom style profile by id (never removes built-in profiles)."""
    folder = Path(custom_dir).expanduser().resolve() if custom_dir else default_custom_profiles_dir()
    if not folder.is_dir():
        return False
    slug = _slugify(profile_id)
    for cand in (folder / f"{profile_id}.json", folder / f"{slug}.json"):
        if cand.is_file():
            cand.unlink()
            return True
    for f in folder.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if str(data.get("profile_id")) == profile_id:
                f.unlink()
                return True
        except Exception:
            continue
    return False


__all__ = [
    "CACHE_NAMESPACE",
    "REF_STYLE_VERSION",
    "analyse_reference_video",
    "delete_reference_style",
    "learn_style_from_references",
    "list_reference_styles",
    "style_to_show_profile",
]
