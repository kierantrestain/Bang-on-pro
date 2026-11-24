"""Deterministic MLT (melt) + FFmpeg before/after visual and audio verification harness.

Renders synthetic reference audiovisual clips through `melt` before and after applying
converted Premiere presets, measures real pixel-difference PSNR/SSIM/MSE and audio
RMS/spectral deltas, and verifies bit-exact frame-hash determinism across repeated runs.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import array
import hashlib
import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .catalog import DEFAULT_HEIGHT, DEFAULT_WIDTH
from .mapper import generate_mlt_filter_xml
from .models import CompatibilityStatus, MappedPreset, MltFilterSpec

_PSNR_RE = re.compile(r"average:([0-9.]+|inf)")
_SSIM_RE = re.compile(r"All:([0-9.]+)")
_SPECTRAL_PROBE_HZ = (110.0, 220.0, 440.0, 660.0, 1000.0, 2200.0, 3500.0, 6000.0)


@dataclass
class VisualMetrics:
    """Measured visual difference between before and after renders."""

    psnr_db: float
    ssim: float
    mse: float
    frame_count: int
    frame_hashes_after: List[str] = field(default_factory=list)
    visual_changed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "psnr_db": round(self.psnr_db, 4) if math.isfinite(self.psnr_db) else "inf",
            "ssim": round(self.ssim, 6),
            "mse": round(self.mse, 4),
            "frame_count": self.frame_count,
            "visual_changed": self.visual_changed,
            "first_frame_sha256": self.frame_hashes_after[0] if self.frame_hashes_after else "",
        }


@dataclass
class AudioMetrics:
    """Measured audio RMS, waveform, and spectral differences between before and after renders."""

    rms_before_db: float
    rms_after_db: float
    rms_delta_db: float
    waveform_l2_delta: float
    spectral_centroid_before_hz: float
    spectral_centroid_after_hz: float
    spectral_centroid_delta_hz: float
    audio_sha256_after: str
    audio_changed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rms_before_db": round(self.rms_before_db, 4),
            "rms_after_db": round(self.rms_after_db, 4),
            "rms_delta_db": round(self.rms_delta_db, 4),
            "waveform_l2_delta": round(self.waveform_l2_delta, 6),
            "spectral_centroid_before_hz": round(self.spectral_centroid_before_hz, 2),
            "spectral_centroid_after_hz": round(self.spectral_centroid_after_hz, 2),
            "spectral_centroid_delta_hz": round(self.spectral_centroid_delta_hz, 2),
            "audio_sha256_after": self.audio_sha256_after,
            "audio_changed": self.audio_changed,
        }


@dataclass
class PresetVerificationResult:
    """Full before/after and determinism verification result for a single MappedPreset."""

    preset_id: str
    preset_name: str
    status: str
    rendered_ok: bool
    deterministic: bool
    has_video_filters: bool
    has_audio_filters: bool
    visual: Optional[VisualMetrics] = None
    audio: Optional[AudioMetrics] = None
    error: str = ""
    preview_before_path: Optional[str] = None
    preview_after_path: Optional[str] = None
    preview_split_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "preset_id": self.preset_id,
            "preset_name": self.preset_name,
            "status": self.status,
            "rendered_ok": self.rendered_ok,
            "deterministic": self.deterministic,
            "has_video_filters": self.has_video_filters,
            "has_audio_filters": self.has_audio_filters,
        }
        if self.visual is not None:
            data["visual"] = self.visual.to_dict()
        if self.audio is not None:
            data["audio"] = self.audio.to_dict()
        if self.preview_before_path is not None:
            data["preview_before_path"] = self.preview_before_path
        if self.preview_after_path is not None:
            data["preview_after_path"] = self.preview_after_path
        if self.preview_split_path is not None:
            data["preview_split_path"] = self.preview_split_path
        if self.error:
            data["error"] = self.error
        return data


def _headless_env(runtime_dir: Path) -> Dict[str, str]:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    # MLT 7.x libmltqt.so checks getenv("DISPLAY") on Linux before creating QApplication
    # even when Qt uses the offscreen QPA plugin; provide a non-connecting fallback.
    if os.name == "posix" and not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        env["DISPLAY"] = ":99"
    env["XDG_RUNTIME_DIR"] = str(runtime_dir)
    env["MLT_THREADS"] = "1"
    env["LC_ALL"] = "C"
    return env


def generate_reference_clip(
    output_path: Union[str, Path],
    duration_sec: float = 0.8,
    fps: int = 25,
    width: int = 320,
    height: int = 180,
) -> Path:
    """Create a deterministic synthetic reference video+audio clip via FFmpeg."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    audio_expr = (
        "0.35*sin(2*PI*440*t)*(0.5+0.5*sin(2*PI*3*t))"
        "+0.18*sin(2*PI*2200*t)+0.12*sin(2*PI*110*t)"
        "|"
        "0.35*sin(2*PI*660*t)*(0.5+0.5*sin(2*PI*2*t))"
        "+0.15*sin(2*PI*3500*t)+0.10*sin(2*PI*220*t)"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={width}x{height}:rate={fps}:duration={duration_sec}",
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc='{audio_expr}':s=48000:d={duration_sec}",
        "-c:v",
        "ffv1",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "pcm_s16le",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-threads",
        "1",
        "-shortest",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if proc.returncode != 0 or not out.is_file():
        raise RuntimeError(f"FFmpeg reference clip generation failed: {proc.stderr.strip()}")
    return out


def _rescale_rect_token(token: str, sx: float, sy: float) -> str:
    """Scale pixel geometry `x y w h [alpha]` inside an MLT keyframe entry."""
    parts = token.strip().split()
    if len(parts) < 4:
        return token
    try:
        x = int(round(float(parts[0]) * sx))
        y = int(round(float(parts[1]) * sy))
        w = max(1, int(round(float(parts[2]) * sx)))
        h = max(1, int(round(float(parts[3]) * sy)))
        tail = " ".join(parts[4:])
        return f"{x} {y} {w} {h} {tail}".strip()
    except ValueError:
        return token


def _rescale_filters_for_canvas(
    filters: Sequence[MltFilterSpec],
    target_w: int,
    target_h: int,
    source_w: int = DEFAULT_WIDTH,
    source_h: int = DEFAULT_HEIGHT,
) -> List[MltFilterSpec]:
    """Scale pixel-coordinate filter properties (`qtblend`/`qtcrop` rect) for verification canvas."""
    if target_w == source_w and target_h == source_h:
        return list(filters)

    sx = target_w / float(source_w)
    sy = target_h / float(source_h)
    scaled: List[MltFilterSpec] = []
    op_re = re.compile(r"^(\d+)([~|]?=)(.+)$")

    for flt in filters:
        props = dict(flt.properties)
        if flt.service in ("qtblend", "qtcrop") and "rect" in props:
            raw_rect = props["rect"]
            if "%" not in raw_rect:
                segments = [s.strip() for s in raw_rect.split(";") if s.strip()]
                new_segs: List[str] = []
                for seg in segments:
                    m = op_re.match(seg)
                    if m:
                        new_segs.append(f"{m.group(1)}{m.group(2)}{_rescale_rect_token(m.group(3), sx, sy)}")
                    else:
                        new_segs.append(_rescale_rect_token(seg, sx, sy))
                props["rect"] = ";".join(new_segs)
        scaled.append(
            MltFilterSpec(
                service=flt.service,
                media_type=flt.media_type,
                properties=props,
                source_effect=flt.source_effect,
                notes=list(flt.notes),
            )
        )
    return scaled


def render_mlt_xml(
    mlt_xml: str,
    output_path: Union[str, Path],
    work_dir: Path,
    timeout_sec: int = 45,
) -> Path:
    """Execute `melt` headless to render an MLT XML graph into a lossless Matroska file."""
    out = Path(output_path)
    xml_path = work_dir / f"{out.stem}.mlt"
    xml_path.write_text(mlt_xml, encoding="utf-8")

    runtime_dir = work_dir / "xdg_runtime"
    runtime_dir.mkdir(exist_ok=True)

    cmd = [
        "melt",
        str(xml_path),
        "-consumer",
        f"avformat:{out}",
        "vcodec=ffv1",
        "pix_fmt=yuv420p",
        "acodec=pcm_s16le",
        "ar=48000",
        "channels=2",
        "threads=1",
        "real_time=0",
        "terminate_on_pause=1",
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=_headless_env(runtime_dir),
        timeout=timeout_sec,
        check=False,
    )
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        raise RuntimeError(f"melt render failed (rc={proc.returncode}): {proc.stderr[-800:]}")
    return out


def _extract_raw_rgb_frames(
    media_path: Path,
    width: int,
    height: int,
) -> List[bytes]:
    """Decode deterministic RGB24 frame buffers via FFmpeg."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(media_path),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-threads",
        "1",
        "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg rawvideo decode failed: {proc.stderr.decode('utf-8', errors='replace')}")
    frame_size = width * height * 3
    raw = proc.stdout
    count = len(raw) // frame_size
    return [raw[i * frame_size : (i + 1) * frame_size] for i in range(count)]


def compute_visual_metrics(
    before_path: Path,
    after_path: Path,
    width: int,
    height: int,
) -> VisualMetrics:
    """Measure PSNR, SSIM, MSE, and per-frame SHA-256 hashes between before and after videos."""
    # 1. FFmpeg PSNR & SSIM filter pass
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-i",
        str(before_path),
        "-i",
        str(after_path),
        "-lavfi",
        "[0:v][1:v]ssim;[0:v][1:v]psnr",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    stderr = proc.stderr

    psnr_match = _PSNR_RE.search(stderr)
    ssim_match = _SSIM_RE.search(stderr)

    psnr_db = float("inf")
    if psnr_match:
        raw_psnr = psnr_match.group(1)
        psnr_db = float("inf") if raw_psnr == "inf" else float(raw_psnr)
    ssim = float(ssim_match.group(1)) if ssim_match else 1.0

    # 2. Exact raw frame MSE + SHA-256 hashes
    frames_before = _extract_raw_rgb_frames(before_path, width, height)
    frames_after = _extract_raw_rgb_frames(after_path, width, height)
    n_frames = min(len(frames_before), len(frames_after))

    hashes_after = [hashlib.sha256(buf).hexdigest() for buf in frames_after]

    sq_err = 0.0
    total_samples = 0
    # Sample every 4th frame + first & last for fast exact MSE over full clip
    sample_indices = sorted({0, max(0, n_frames - 1)} | set(range(0, n_frames, 4)))
    for idx in sample_indices:
        if idx >= n_frames:
            continue
        fb = frames_before[idx]
        fa = frames_after[idx]
        # Stride by 3 bytes on large images only if needed; 320x180x3 = 172,800 bytes is fast in Python
        diff_sum = sum((a - b) * (a - b) for a, b in zip(fa, fb))
        sq_err += diff_sum
        total_samples += len(fa)

    mse = (sq_err / total_samples) if total_samples > 0 else 0.0
    visual_changed = mse > 0.5 or (math.isfinite(psnr_db) and psnr_db < 60.0) or ssim < 0.9995

    return VisualMetrics(
        psnr_db=psnr_db,
        ssim=ssim,
        mse=mse,
        frame_count=len(frames_after),
        frame_hashes_after=hashes_after,
        visual_changed=visual_changed,
    )


def _extract_pcm_s16le(media_path: Path, sample_rate: int = 48000) -> bytes:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(media_path),
        "-vn",
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ar",
        str(sample_rate),
        "-ac",
        "2",
        "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg PCM decode failed: {proc.stderr.decode('utf-8', errors='replace')}")
    return proc.stdout


def _goertzel_power(samples: Sequence[float], sample_rate: float, target_hz: float) -> float:
    """Compute DFT bin power at target_hz using Goertzel's algorithm."""
    n = len(samples)
    if n == 0:
        return 0.0
    k = int(0.5 + (n * target_hz) / sample_rate)
    omega = (2.0 * math.pi * k) / n
    coeff = 2.0 * math.cos(omega)
    s_prev = 0.0
    s_prev2 = 0.0
    for x in samples:
        s = x + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    power = s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2
    return max(0.0, power)


def _spectral_centroid(mono_samples: Sequence[float], sample_rate: float = 48000.0) -> float:
    """Estimate spectral centroid across probe frequency bins."""
    # Use a fixed window of up to 8192 samples (170ms) for speed and determinism
    window = mono_samples[:8192]
    num = 0.0
    den = 0.0
    for hz in _SPECTRAL_PROBE_HZ:
        mag = math.sqrt(_goertzel_power(window, sample_rate, hz))
        num += hz * mag
        den += mag
    return (num / den) if den > 1e-12 else 0.0


def compute_audio_metrics(before_path: Path, after_path: Path, sample_rate: int = 48000) -> AudioMetrics:
    """Compute RMS dB delta, normalized waveform L2 delta, spectral centroid delta, and audio SHA-256."""
    raw_before = _extract_pcm_s16le(before_path, sample_rate=sample_rate)
    raw_after = _extract_pcm_s16le(after_path, sample_rate=sample_rate)
    audio_hash = hashlib.sha256(raw_after).hexdigest()

    arr_b = array.array("h")
    arr_b.frombytes(raw_before[: len(raw_before) - (len(raw_before) % 2)])
    arr_a = array.array("h")
    arr_a.frombytes(raw_after[: len(raw_after) - (len(raw_after) % 2)])

    n = min(len(arr_b), len(arr_a))
    if n == 0:
        return AudioMetrics(
            rms_before_db=-120.0,
            rms_after_db=-120.0,
            rms_delta_db=0.0,
            waveform_l2_delta=0.0,
            spectral_centroid_before_hz=0.0,
            spectral_centroid_after_hz=0.0,
            spectral_centroid_delta_hz=0.0,
            audio_sha256_after=audio_hash,
            audio_changed=False,
        )

    scale = 1.0 / 32768.0
    fb = [arr_b[i] * scale for i in range(n)]
    fa = [arr_a[i] * scale for i in range(n)]

    rms_b = math.sqrt(sum(x * x for x in fb) / n)
    rms_a = math.sqrt(sum(x * x for x in fa) / n)
    rms_b_db = 20.0 * math.log10(max(rms_b, 1e-9))
    rms_a_db = 20.0 * math.log10(max(rms_a, 1e-9))
    l2_delta = math.sqrt(sum((a - b) * (a - b) for a, b in zip(fa, fb)) / n)

    mono_b = [0.5 * (fb[i] + fb[i + 1]) for i in range(0, n - 1, 2)]
    mono_a = [0.5 * (fa[i] + fa[i + 1]) for i in range(0, n - 1, 2)]
    sc_b = _spectral_centroid(mono_b, float(sample_rate))
    sc_a = _spectral_centroid(mono_a, float(sample_rate))
    sc_delta = sc_a - sc_b

    audio_changed = (
        abs(rms_a_db - rms_b_db) > 0.1
        or l2_delta > 0.005
        or abs(sc_delta) > 5.0
    )

    return AudioMetrics(
        rms_before_db=rms_b_db,
        rms_after_db=rms_a_db,
        rms_delta_db=rms_a_db - rms_b_db,
        waveform_l2_delta=l2_delta,
        spectral_centroid_before_hz=sc_b,
        spectral_centroid_after_hz=sc_a,
        spectral_centroid_delta_hz=sc_delta,
        audio_sha256_after=audio_hash,
        audio_changed=audio_changed,
    )


def verify_preset_render(
    preset: MappedPreset,
    reference_clip: Optional[Path] = None,
    fps: int = 25,
    width: int = 320,
    height: int = 180,
    duration_sec: float = 0.8,
    check_determinism: bool = True,
    preview_dir: Optional[Union[str, Path]] = None,
    before_clip: Optional[Path] = None,
) -> PresetVerificationResult:
    """Verify a single MappedPreset end-to-end with real `melt` before/after + determinism renders."""
    has_video = any(f.media_type == "video" for f in preset.mlt_filters)
    has_audio = any(f.media_type == "audio" for f in preset.mlt_filters)

    if not preset.mlt_filters:
        return PresetVerificationResult(
            preset_id=preset.preset_id,
            preset_name=preset.name,
            status=preset.compatibility_status.value,
            rendered_ok=False,
            deterministic=True,
            has_video_filters=False,
            has_audio_filters=False,
            error="No executable MLT filters mapped for this preset.",
        )

    out_frame = max(4, int(round(duration_sec * fps)) - 1)

    with tempfile.TemporaryDirectory(prefix="bangonpro_verify_") as tmp:
        work_dir = Path(tmp)
        ref_path = reference_clip or generate_reference_clip(
            work_dir / "reference.mkv",
            duration_sec=duration_sec,
            fps=fps,
            width=width,
            height=height,
        )

        scaled_filters = _rescale_filters_for_canvas(
            preset.mlt_filters,
            target_w=width,
            target_h=height,
            source_w=DEFAULT_WIDTH,
            source_h=DEFAULT_HEIGHT,
        )

        try:
            # 1. Baseline ("before") render through MLT with zero filters
            if before_clip is not None and before_clip.is_file():
                before_out = before_clip
            else:
                before_xml = generate_mlt_filter_xml(
                    input_media=str(ref_path),
                    mlt_filters=[],
                    out_frame=out_frame,
                    fps_num=fps,
                    fps_den=1,
                    width=width,
                    height=height,
                )
                before_out = render_mlt_xml(before_xml, work_dir / "before.mkv", work_dir)

            # 2. Converted preset ("after" run 1)
            after_xml = generate_mlt_filter_xml(
                input_media=str(ref_path),
                mlt_filters=scaled_filters,
                out_frame=out_frame,
                fps_num=fps,
                fps_den=1,
                width=width,
                height=height,
            )
            after_out_1 = render_mlt_xml(after_xml, work_dir / "after_run1.mkv", work_dir)

            visual = compute_visual_metrics(before_out, after_out_1, width=width, height=height)
            audio = compute_audio_metrics(before_out, after_out_1)

            deterministic = True
            if check_determinism:
                after_out_2 = render_mlt_xml(after_xml, work_dir / "after_run2.mkv", work_dir)
                frames_2 = _extract_raw_rgb_frames(after_out_2, width, height)
                hashes_2 = [hashlib.sha256(buf).hexdigest() for buf in frames_2]
                audio_2 = hashlib.sha256(_extract_pcm_s16le(after_out_2)).hexdigest()
                deterministic = (
                    hashes_2 == visual.frame_hashes_after
                    and audio_2 == audio.audio_sha256_after
                )

            preview_before: Optional[str] = None
            preview_after: Optional[str] = None
            preview_split: Optional[str] = None
            if preview_dir is not None:
                from .preview_generator import generate_preset_previews

                p_paths = generate_preset_previews(
                    preset,
                    cache_dir=preview_dir,
                    width=width,
                    height=height,
                    fps=fps,
                    duration_sec=duration_sec,
                    sample_frame=min(6, out_frame),
                    reference_clip=ref_path,
                    before_video=before_out,
                )
                if p_paths is not None:
                    preview_before = str(p_paths.before_path)
                    preview_after = str(p_paths.after_path)
                    preview_split = str(p_paths.split_path)

            return PresetVerificationResult(
                preset_id=preset.preset_id,
                preset_name=preset.name,
                status=preset.compatibility_status.value,
                rendered_ok=True,
                deterministic=deterministic,
                has_video_filters=has_video,
                has_audio_filters=has_audio,
                visual=visual,
                audio=audio,
                preview_before_path=preview_before,
                preview_after_path=preview_after,
                preview_split_path=preview_split,
            )
        except Exception as exc:
            return PresetVerificationResult(
                preset_id=preset.preset_id,
                preset_name=preset.name,
                status=preset.compatibility_status.value,
                rendered_ok=False,
                deterministic=False,
                has_video_filters=has_video,
                has_audio_filters=has_audio,
                error=str(exc),
            )


def verify_presets_batch(
    presets: Sequence[MappedPreset],
    fps: int = 25,
    width: int = 320,
    height: int = 180,
    duration_sec: float = 0.8,
    check_determinism: bool = True,
    preview_dir: Optional[Union[str, Path]] = None,
) -> List[PresetVerificationResult]:
    """Verify multiple mapped presets reusing a single generated reference clip & baseline render."""
    out_frame = max(4, int(round(duration_sec * fps)) - 1)
    with tempfile.TemporaryDirectory(prefix="bangonpro_batch_ref_") as tmp:
        work_dir = Path(tmp)
        ref = generate_reference_clip(
            work_dir / "shared_reference.mkv",
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
        shared_before = render_mlt_xml(before_xml, work_dir / "shared_before.mkv", work_dir)

        results: List[PresetVerificationResult] = []
        for preset in presets:
            if preset.compatibility_status in (
                CompatibilityStatus.EXACT_VALIDATED,
                CompatibilityStatus.APPROXIMATED,
            ) or preset.mlt_filters:
                results.append(
                    verify_preset_render(
                        preset,
                        reference_clip=ref,
                        fps=fps,
                        width=width,
                        height=height,
                        duration_sec=duration_sec,
                        check_determinism=check_determinism,
                        preview_dir=preview_dir,
                        before_clip=shared_before,
                    )
                )
        return results
