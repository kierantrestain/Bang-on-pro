"""Visual Before/After/Split PNG Thumbnail Generator for Premiere & Native Presets.

Renders real deterministic before, after, and side-by-side split-screen preview PNGs
using `melt` + `ffmpeg` (strictly `shell=False`) with content-addressed disk caching.

SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

try:
    import numpy as np  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

try:
    from PIL import Image, ImageDraw  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]

from .catalog import DEFAULT_HEIGHT, DEFAULT_WIDTH
from .mapper import generate_mlt_filter_xml
from .models import CompatibilityReport, MappedPreset, MltFilterSpec
from .verifier import (
    _rescale_filters_for_canvas,
    generate_reference_clip,
    render_mlt_xml,
)


@dataclass
class PreviewPaths:
    """Generated or cached thumbnail PNG paths for a preset."""

    before_path: Path
    after_path: Path
    split_path: Path
    cache_hit: bool = False


def _filter_cache_key(
    filters: Sequence[MltFilterSpec],
    width: int,
    height: int,
    sample_frame: int,
) -> str:
    payload = {
        "w": width,
        "h": height,
        "frame": sample_frame,
        "filters": [
            {"service": f.service, "media_type": f.media_type, "properties": dict(sorted(f.properties.items()))}
            for f in filters
        ],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _extract_single_frame_png(
    video_path: Path,
    output_png: Path,
    frame_index: int = 4,
) -> Path:
    """Extract a deterministic single PNG frame from a video file via FFmpeg."""
    output_png.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-vf",
        f"select=eq(n\\,{max(0, int(frame_index))})",
        "-frames:v",
        "1",
        str(output_png),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if proc.returncode != 0 or not output_png.is_file() or output_png.stat().st_size == 0:
        # Fallback to frame 0 if clip was shorter than frame_index
        cmd_zero = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            str(output_png),
        ]
        proc2 = subprocess.run(cmd_zero, capture_output=True, text=True, timeout=30, check=False)
        if proc2.returncode != 0 or not output_png.is_file():
            raise RuntimeError(f"FFmpeg PNG frame extraction failed: {proc2.stderr.strip()}")
    return output_png


def compose_before_after_split_png(
    before_png: Path,
    after_png: Path,
    split_png: Path,
    *,
    label_left: str = "BEFORE",
    label_right: str = "AFTER",
) -> Path:
    """Build a crisp side-by-side split comparison PNG (Original | Effect Applied)."""
    split_png.parent.mkdir(parents=True, exist_ok=True)

    if Image is not None:
        with Image.open(before_png) as img_b, Image.open(after_png) as img_a:
            rgb_b = img_b.convert("RGB")
            rgb_a = img_a.convert("RGB")
            w, h = rgb_b.size
            if rgb_a.size != (w, h):
                rgb_a = rgb_a.resize((w, h))

            # Side-by-side layout: left half = original, right half = after, separated by bright divider
            split = Image.new("RGB", (w * 2 + 2, h), (245, 197, 24))
            split.paste(rgb_b, (0, 0))
            split.paste(rgb_a, (w + 2, 0))

            if ImageDraw is not None:
                draw = ImageDraw.Draw(split)
                # Subtle dark label pills at top-left of each half
                draw.rectangle([4, 4, 68, 20], fill=(15, 23, 42))
                draw.text((8, 6), label_left, fill=(241, 245, 249))
                draw.rectangle([w + 6, 4, w + 64, 20], fill=(15, 23, 42))
                draw.text((w + 10, 6), label_right, fill=(250, 204, 21))

            split.save(split_png, format="PNG")
            return split_png

    # Pure FFmpeg hstack fallback if Pillow is absent
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(before_png),
        "-i",
        str(after_png),
        "-filter_complex",
        "[0:v][1:v]hstack=inputs=2",
        "-frames:v",
        "1",
        str(split_png),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if proc.returncode != 0 or not split_png.is_file():
        raise RuntimeError(f"FFmpeg split PNG compose failed: {proc.stderr.strip()}")
    return split_png


def _render_audio_waveform_png(
    media_path: Path,
    output_png: Path,
    width: int,
    height: int,
    color: str,
) -> Path:
    """Render an audio waveform preview PNG via FFmpeg showwavespic for audio-only presets."""
    output_png.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(media_path),
        "-filter_complex",
        f"aformat=channel_layouts=mono,showwavespic=s={width}x{height}:colors={color}",
        "-frames:v",
        "1",
        str(output_png),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if proc.returncode != 0 or not output_png.is_file():
        raise RuntimeError(f"FFmpeg audio waveform preview failed: {proc.stderr.strip()}")
    return output_png


def generate_preset_previews(
    preset: MappedPreset,
    cache_dir: str | Path,
    *,
    width: int = 240,
    height: int = 135,
    fps: int = 25,
    duration_sec: float = 0.5,
    sample_frame: int = 6,
    reference_clip: Path | None = None,
    before_video: Path | None = None,
) -> PreviewPaths | None:
    """Generate (or reuse cached) before/after/split preview PNGs for a MappedPreset.

    Attaches `preview_before_path`, `preview_after_path`, and `preview_split_path`
    directly onto `preset`. Returns None if the preset has no executable MLT filters.
    """
    if not preset.mlt_filters:
        return None

    cdir = Path(cache_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    key = _filter_cache_key(preset.mlt_filters, width, height, sample_frame)
    safe_id = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in preset.preset_id)[:64]

    before_png = cdir / f"{safe_id}_{key}_before.png"
    after_png = cdir / f"{safe_id}_{key}_after.png"
    split_png = cdir / f"{safe_id}_{key}_split.png"

    if before_png.is_file() and after_png.is_file() and split_png.is_file():
        preset.preview_before_path = str(before_png)
        preset.preview_after_path = str(after_png)
        preset.preview_split_path = str(split_png)
        return PreviewPaths(
            before_path=before_png,
            after_path=after_png,
            split_path=split_png,
            cache_hit=True,
        )

    has_video = any(f.media_type == "video" for f in preset.mlt_filters)
    out_frame = max(sample_frame + 1, round(duration_sec * fps) - 1)

    with tempfile.TemporaryDirectory(prefix="bangonpro_preview_") as tmp:
        work_dir = Path(tmp)
        ref = reference_clip or generate_reference_clip(
            work_dir / "ref.mkv",
            duration_sec=duration_sec,
            fps=fps,
            width=width,
            height=height,
        )
        if before_video is not None and before_video.is_file():
            before_mkv = before_video
        else:
            before_xml = generate_mlt_filter_xml(
                input_media=str(ref),
                mlt_filters=[],
                out_frame=out_frame,
                fps_num=fps,
                fps_den=1,
                width=width,
                height=height,
            )
            before_mkv = render_mlt_xml(before_xml, work_dir / "before.mkv", work_dir)

        scaled_filters = _rescale_filters_for_canvas(
            preset.mlt_filters,
            target_w=width,
            target_h=height,
            source_w=DEFAULT_WIDTH,
            source_h=DEFAULT_HEIGHT,
        )
        after_xml = generate_mlt_filter_xml(
            input_media=str(ref),
            mlt_filters=scaled_filters,
            out_frame=out_frame,
            fps_num=fps,
            fps_den=1,
            width=width,
            height=height,
        )
        after_mkv = render_mlt_xml(after_xml, work_dir / "after.mkv", work_dir)

        if has_video:
            _extract_single_frame_png(before_mkv, before_png, frame_index=sample_frame)
            _extract_single_frame_png(after_mkv, after_png, frame_index=sample_frame)
        else:
            _render_audio_waveform_png(before_mkv, before_png, width, height, color="#38bdf8")
            _render_audio_waveform_png(after_mkv, after_png, width, height, color="#facc15")

        compose_before_after_split_png(before_png, after_png, split_png)

    preset.preview_before_path = str(before_png)
    preset.preview_after_path = str(after_png)
    preset.preview_split_path = str(split_png)
    return PreviewPaths(
        before_path=before_png,
        after_path=after_png,
        split_path=split_png,
        cache_hit=False,
    )


def attach_previews_to_report(
    report: CompatibilityReport,
    cache_dir: str | Path,
    *,
    width: int = 240,
    height: int = 135,
    fps: int = 25,
    duration_sec: float = 0.5,
    sample_frame: int = 6,
    max_presets: int | None = None,
) -> dict[str, PreviewPaths]:
    """Generate/cache visual thumbnails for presets in a CompatibilityReport sharing one baseline clip."""
    cdir = Path(cache_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    results: dict[str, PreviewPaths] = {}

    eligible = [p for p in report.presets if p.mlt_filters]
    if max_presets is not None:
        eligible = eligible[: max(0, int(max_presets))]
    if not eligible:
        return results

    out_frame = max(sample_frame + 1, round(duration_sec * fps) - 1)
    with tempfile.TemporaryDirectory(prefix="bangonpro_preview_batch_") as tmp:
        work_dir = Path(tmp)
        ref = generate_reference_clip(
            work_dir / "shared_ref.mkv",
            duration_sec=duration_sec,
            fps=fps,
            width=width,
            height=height,
        )
        before_xml = generate_mlt_filter_xml(
            input_media=str(ref),
            mlt_filters=[],
            out_frame=out_frame,
            fps_num=fps,
            fps_den=1,
            width=width,
            height=height,
        )
        before_mkv = render_mlt_xml(before_xml, work_dir / "shared_before.mkv", work_dir)

        for preset in eligible:
            try:
                paths = generate_preset_previews(
                    preset,
                    cache_dir=cdir,
                    width=width,
                    height=height,
                    fps=fps,
                    duration_sec=duration_sec,
                    sample_frame=sample_frame,
                    reference_clip=ref,
                    before_video=before_mkv,
                )
                if paths is not None:
                    results[preset.preset_id] = paths
            except Exception as exc:  # noqa: BLE001
                preset.notes.append(f"Preview generation skipped: {exc}")

    return results
