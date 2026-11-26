"""Professional Rotoscoping, AI Segmentation, Mask Refinement & Tracking Engine.

Supports:
- SAM-family local segmentation evaluation & runtime backend selection
  (SAM 2.1 Hiera Tiny/Small/Base+ Apache-2.0 on Apple Silicon MPS/CoreML,
  MobileSAM Apache-2.0 ONNX, OpenCV GrabCut + Guided Edge Filter CPU fallback,
  and deterministic NumPy/Pillow polygon/Bézier rasterization).
- Positive (+) and negative (-) point/box selection prompts.
- Manual polygon, cubic Bézier, freehand, and paint/erase brush masks.
- Feathering (Gaussian), morphological expansion/contraction, hair/edge refinement.
- Multiple masks per clip with inversion and boolean combinations (add/subtract/intersect/xor).
- Forward and backward mask propagation with optical-flow tracking, occlusion detection,
  temporal EMA smoothing, and manual frame-level keyframe overrides.
- Preview rendering (mask overlay, isolated alpha, before/after split, final composite)
  plus native Kdenlive/MLT matte & rotoscoping spline export via Timeline Command API v1.

SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import platform
import shutil
import subprocess
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from scipy import ndimage

from bangonpro.ai_engine.commands import TimelineCommandBuilder

from .models import MaskShape, RotoSession

logger = logging.getLogger(__name__)

_SAM2_WEIGHT_CANDIDATES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "sam2_hiera_tiny": (
        ("sam2.1_hiera_tiny.pt", "sam2_hiera_tiny.pt"),
        ("configs/sam2.1/sam2.1_hiera_t.yaml", "sam2.1_hiera_t.yaml", "sam2_hiera_t.yaml"),
    ),
    "sam2_hiera_small": (
        ("sam2.1_hiera_small.pt", "sam2_hiera_small.pt"),
        ("configs/sam2.1/sam2.1_hiera_s.yaml", "sam2.1_hiera_s.yaml", "sam2_hiera_s.yaml"),
    ),
}

_ONNX_PAIRED_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("mobile_sam_image_encoder.onnx", "sam_mask_decoder_single.onnx"),
    ("mobile_sam_image_encoder.onnx", "mobile_sam_mask_decoder.onnx"),
    ("mobile_sam_encoder.onnx", "mobile_sam_decoder.onnx"),
    ("mobilesam_encoder.onnx", "mobilesam_decoder.onnx"),
    ("edge_sam_encoder.onnx", "edge_sam_decoder.onnx"),
    ("edgesam_encoder.onnx", "edgesam_decoder.onnx"),
    ("sam_vit_t_encoder.onnx", "sam_vit_t_decoder.onnx"),
)

_ONNX_SINGLE_CANDIDATES: tuple[str, ...] = (
    "mobile_sam.onnx",
    "mobilesam.onnx",
    "edge_sam.onnx",
    "edgesam.onnx",
)


def resolve_model_dir(model_dir: str | Path | None = None) -> Path:
    """Resolve the Bang On Pro AI model directory honoring ``BANGONPRO_MODEL_DIR``."""
    if model_dir is not None:
        return Path(model_dir).expanduser()
    env_dir = os.environ.get("BANGONPRO_MODEL_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser()
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "BangOnPro" / "models"
    return Path.home() / ".local" / "share" / "bangonpro" / "models"


def _candidate_model_subdirs(root: Path) -> list[Path]:
    return [
        root,
        root / "mobilesam-onnx",
        root / "sam2.1-hiera-tiny",
        root / "sam2.1-hiera-small",
        root / "sam2",
        root / "sam",
        root / "mobile_sam",
        root / "roto",
    ]


def _find_sam2_weights(variant: str, model_dir: Path) -> tuple[Path | None, str]:
    """Locate SAM 2.1 checkpoint weights and config in ``model_dir``."""
    weight_names, config_names = _SAM2_WEIGHT_CANDIDATES.get(
        variant,
        _SAM2_WEIGHT_CANDIDATES["sam2_hiera_tiny"],
    )
    found_ckpt: Path | None = None
    for subdir in _candidate_model_subdirs(model_dir):
        if not subdir.is_dir():
            continue
        for fname in weight_names:
            candidate = subdir / fname
            if candidate.is_file() and candidate.stat().st_size > 0:
                found_ckpt = candidate
                break
        if found_ckpt is not None:
            break

    config_ref = config_names[0]
    if found_ckpt is not None:
        for cfg in config_names:
            local_cfg = found_ckpt.parent / Path(cfg).name
            if local_cfg.is_file():
                config_ref = str(local_cfg)
                break
    return found_ckpt, config_ref


def _find_mobile_sam_onnx_weights(model_dir: Path) -> tuple[Path | None, Path | None]:
    """Locate MobileSAM / EdgeSAM ONNX encoder+decoder pair or single end-to-end model."""
    for subdir in _candidate_model_subdirs(model_dir):
        if not subdir.is_dir():
            continue
        for enc_name, dec_name in _ONNX_PAIRED_CANDIDATES:
            enc_p = subdir / enc_name
            dec_p = subdir / dec_name
            if (
                enc_p.is_file()
                and enc_p.stat().st_size > 0
                and dec_p.is_file()
                and dec_p.stat().st_size > 0
            ):
                return enc_p, dec_p
        for single_name in _ONNX_SINGLE_CANDIDATES:
            single_p = subdir / single_name
            if single_p.is_file() and single_p.stat().st_size > 0:
                return single_p, None
    return None, None


@dataclass(frozen=True)
class SegmentationBackendInfo:
    """Evaluated segmentation/tracking backend metadata (compatibility, license, hardware)."""

    backend_id: str
    display_name: str
    license: str
    gpl_compatible: bool
    macos_arm64_acceleration: str
    min_ram_gb: float
    approx_model_mb: int
    available_now: bool
    notes: str
    is_neural: bool = False
    runtime_installed: bool = False
    weights_present: bool = False
    weights_path: str | None = None


@dataclass
class SegmentationResult:
    """Honest, rich segmentation result distinguishing neural SAM from heuristic fallbacks."""

    alpha_mask: np.ndarray
    backend_id: str
    is_neural_inference: bool
    fallback_used: bool
    fallback_reason: str | None
    confidence: float
    quality_status: str  # "neural_ok" | "fallback_heuristic" | "degraded_box_fallback"
    warnings: list[str] = field(default_factory=list)
    device: str = "cpu"
    model_path: str | None = None

    def to_metadata_dict(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "is_neural_inference": self.is_neural_inference,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "confidence": round(float(self.confidence), 4),
            "quality_status": self.quality_status,
            "warnings": list(self.warnings),
            "device": self.device,
            "model_path": self.model_path,
        }


@dataclass(frozen=True)
class ClipTimingAlignment:
    """Source-frame-to-timeline alignment parameters (trims, speed, FPS, export resolution)."""

    timeline_start_frame: int = 0
    clip_trim_in_frame: int = 0
    clip_trim_out_frame: int | None = None
    clip_speed: float = 1.0
    source_fps: float = 25.0
    timeline_fps: float = 25.0
    source_width: int = 1920
    source_height: int = 1080
    export_width: int = 1920
    export_height: int = 1080
    reverse: bool = False


class FrameMaskCache:
    """Bounded LRU frame-mask cache keyed by frame, clip trim, speed, and target resolution."""

    def __init__(self, *, max_entries: int = 128, max_bytes: int = 256 * 1024 * 1024) -> None:
        self.max_entries = max(1, int(max_entries))
        self.max_bytes = max(1024, int(max_bytes))
        self._store: OrderedDict[tuple[Any, ...], np.ndarray] = OrderedDict()
        self._bytes_used = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    @staticmethod
    def make_key(
        *,
        frame_index: int,
        clip_trim: tuple[int, int | None] = (0, None),
        clip_speed: float = 1.0,
        target_resolution: tuple[int, int] = (1920, 1080),
        shape_signature: str = "",
    ) -> tuple[Any, ...]:
        return (
            int(frame_index),
            int(clip_trim[0]),
            int(clip_trim[1]) if clip_trim[1] is not None else None,
            round(float(clip_speed), 6),
            int(target_resolution[0]),
            int(target_resolution[1]),
            str(shape_signature),
        )

    def get(
        self,
        *,
        frame_index: int,
        clip_trim: tuple[int, int | None] = (0, None),
        clip_speed: float = 1.0,
        target_resolution: tuple[int, int] = (1920, 1080),
        shape_signature: str = "",
    ) -> np.ndarray | None:
        key = self.make_key(
            frame_index=frame_index,
            clip_trim=clip_trim,
            clip_speed=clip_speed,
            target_resolution=target_resolution,
            shape_signature=shape_signature,
        )
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None
        self._store.move_to_end(key)
        self._hits += 1
        return entry.copy()

    def put(
        self,
        alpha_mask: np.ndarray,
        *,
        frame_index: int,
        clip_trim: tuple[int, int | None] = (0, None),
        clip_speed: float = 1.0,
        target_resolution: tuple[int, int] = (1920, 1080),
        shape_signature: str = "",
    ) -> None:
        arr = np.asarray(alpha_mask, dtype=np.float32).copy()
        nbytes = int(arr.nbytes)
        if nbytes > self.max_bytes:
            return
        key = self.make_key(
            frame_index=frame_index,
            clip_trim=clip_trim,
            clip_speed=clip_speed,
            target_resolution=target_resolution,
            shape_signature=shape_signature,
        )
        if key in self._store:
            old = self._store.pop(key)
            self._bytes_used -= int(old.nbytes)

        while self._store and (
            len(self._store) >= self.max_entries or self._bytes_used + nbytes > self.max_bytes
        ):
            _, evicted = self._store.popitem(last=False)
            self._bytes_used -= int(evicted.nbytes)
            self._evictions += 1

        self._store[key] = arr
        self._bytes_used += nbytes

    def invalidate(self) -> None:
        self._store.clear()
        self._bytes_used = 0

    def stats(self) -> dict[str, int]:
        return {
            "entries": len(self._store),
            "bytes_used": self._bytes_used,
            "max_entries": self.max_entries,
            "max_bytes": self.max_bytes,
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
        }


def timeline_frame_to_source_frame(timeline_frame: int, alignment: ClipTimingAlignment) -> int:
    """Map a timeline frame index to the corresponding source clip frame under trim, speed & FPS."""
    rel_tl = int(timeline_frame) - int(alignment.timeline_start_frame)
    fps_ratio = max(1e-6, float(alignment.source_fps)) / max(1e-6, float(alignment.timeline_fps))
    speed = abs(float(alignment.clip_speed))
    if speed < 1e-6:
        speed = 1.0
    is_reversed = bool(alignment.reverse) or float(alignment.clip_speed) < 0.0
    delta_src = round(rel_tl * speed * fps_ratio)

    if is_reversed and alignment.clip_trim_out_frame is not None:
        src_frame = int(alignment.clip_trim_out_frame) - delta_src
    else:
        src_frame = int(alignment.clip_trim_in_frame) + delta_src

    lo = max(0, int(alignment.clip_trim_in_frame))
    if alignment.clip_trim_out_frame is not None:
        hi = max(lo, int(alignment.clip_trim_out_frame))
        return max(lo, min(hi, src_frame))
    return max(lo, src_frame)


def source_frame_to_timeline_frame(source_frame: int, alignment: ClipTimingAlignment) -> int:
    """Map a source clip frame index to the corresponding timeline frame under trim, speed & FPS."""
    fps_ratio = max(1e-6, float(alignment.timeline_fps)) / max(1e-6, float(alignment.source_fps))
    speed = abs(float(alignment.clip_speed))
    if speed < 1e-6:
        speed = 1.0
    is_reversed = bool(alignment.reverse) or float(alignment.clip_speed) < 0.0
    if is_reversed and alignment.clip_trim_out_frame is not None:
        delta_src = int(alignment.clip_trim_out_frame) - int(source_frame)
    else:
        delta_src = int(source_frame) - int(alignment.clip_trim_in_frame)
    return int(alignment.timeline_start_frame) + round((delta_src / speed) * fps_ratio)


def scale_mask_to_resolution(
    alpha_mask: np.ndarray,
    target_width: int,
    target_height: int,
) -> np.ndarray:
    """Scale a float32 ``[0, 1]`` alpha matte cleanly to ``(target_height, target_width)``."""
    a = np.clip(np.asarray(alpha_mask, dtype=np.float32), 0.0, 1.0)
    tw = max(1, int(target_width))
    th = max(1, int(target_height))
    if a.shape == (th, tw):
        return a
    img = Image.fromarray(np.clip(a * 255.0, 0.0, 255.0).astype(np.uint8), mode="L")
    resample = getattr(Image, "Resampling", Image).BILINEAR
    resized = img.resize((tw, th), resample)
    return np.clip(np.asarray(resized, dtype=np.float32) / 255.0, 0.0, 1.0).astype(np.float32)


def align_mask_sequence_to_timeline(
    source_masks: dict[int, np.ndarray] | Sequence[np.ndarray],
    alignment: ClipTimingAlignment,
    *,
    timeline_frame_count: int | None = None,
    cache: FrameMaskCache | None = None,
    shape_signature: str = "",
) -> list[np.ndarray]:
    """Produce timeline-aligned, export-scaled alpha mattes honoring clip trims & speed changes."""
    if isinstance(source_masks, dict):
        src_map = {int(k): v for k, v in source_masks.items()}
    else:
        src_map = {idx: m for idx, m in enumerate(source_masks)}

    if not src_map:
        return []

    max_src = max(src_map.keys())
    trim_in = max(0, int(alignment.clip_trim_in_frame))
    trim_out = (
        min(max_src, int(alignment.clip_trim_out_frame))
        if alignment.clip_trim_out_frame is not None
        else max_src
    )
    eff_alignment = ClipTimingAlignment(
        timeline_start_frame=alignment.timeline_start_frame,
        clip_trim_in_frame=trim_in,
        clip_trim_out_frame=max(trim_in, trim_out),
        clip_speed=alignment.clip_speed,
        source_fps=alignment.source_fps,
        timeline_fps=alignment.timeline_fps,
        source_width=alignment.source_width,
        source_height=alignment.source_height,
        export_width=alignment.export_width,
        export_height=alignment.export_height,
        reverse=alignment.reverse,
    )

    if timeline_frame_count is None:
        src_span = max(1, (eff_alignment.clip_trim_out_frame or 0) - trim_in + 1)
        speed = max(1e-6, abs(float(eff_alignment.clip_speed)))
        fps_ratio = max(1e-6, float(eff_alignment.timeline_fps)) / max(1e-6, float(eff_alignment.source_fps))
        timeline_frame_count = max(1, round((src_span / speed) * fps_ratio))

    sorted_keys = sorted(src_map.keys())
    target_res = (max(1, int(eff_alignment.export_width)), max(1, int(eff_alignment.export_height)))
    trim_tuple = (eff_alignment.clip_trim_in_frame, eff_alignment.clip_trim_out_frame)
    aligned: list[np.ndarray] = []

    for tl_offset in range(max(0, int(timeline_frame_count))):
        tl_frame = eff_alignment.timeline_start_frame + tl_offset
        if cache is not None:
            cached = cache.get(
                frame_index=tl_frame,
                clip_trim=trim_tuple,
                clip_speed=eff_alignment.clip_speed,
                target_resolution=target_res,
                shape_signature=shape_signature,
            )
            if cached is not None:
                aligned.append(cached)
                continue

        src_frame = timeline_frame_to_source_frame(tl_frame, eff_alignment)
        if src_frame in src_map:
            raw_mask = src_map[src_frame]
        else:
            nearest = min(sorted_keys, key=lambda k: abs(k - src_frame))
            raw_mask = src_map[nearest]

        scaled = scale_mask_to_resolution(raw_mask, target_res[0], target_res[1])
        if cache is not None:
            cache.put(
                scaled,
                frame_index=tl_frame,
                clip_trim=trim_tuple,
                clip_speed=eff_alignment.clip_speed,
                target_resolution=target_res,
                shape_signature=shape_signature,
            )
        aligned.append(scaled)

    return aligned


@dataclass
class PropagationFrameResult:
    """Tracked mask result for a single frame during bidirectional propagation."""

    frame_index: int
    alpha_mask: np.ndarray
    confidence: float
    occluded: bool = False
    manual_override: bool = False
    mean_flow_dx: float = 0.0
    mean_flow_dy: float = 0.0
    tracked_vertices: list[Any] = field(default_factory=list)


def evaluate_segmentation_backends(
    model_dir: str | Path | None = None,
) -> list[SegmentationBackendInfo]:
    """Evaluate local SAM-family, ONNX, and deterministic CPU segmentation backends honestly."""
    resolved_dir = resolve_model_dir(model_dir)
    has_torch = importlib.util.find_spec("torch") is not None
    has_sam2 = importlib.util.find_spec("sam2") is not None
    has_onnx = importlib.util.find_spec("onnxruntime") is not None
    has_cv2 = importlib.util.find_spec("cv2") is not None
    is_apple_silicon = platform.system() == "Darwin" and platform.machine() == "arm64"

    mps_ready = False
    if has_torch:
        try:
            import torch

            mps_ready = bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())
        except Exception as exc:  # noqa: BLE001
            logger.debug("PyTorch MPS probe failed: %s", exc)
            mps_ready = False

    coreml_ready = False
    if has_onnx:
        try:
            import onnxruntime as ort

            coreml_ready = "CoreMLExecutionProvider" in ort.get_available_providers()
        except Exception as exc:  # noqa: BLE001
            logger.debug("ONNX Runtime provider probe failed: %s", exc)
            coreml_ready = False

    sam2_tiny_ckpt, _ = _find_sam2_weights("sam2_hiera_tiny", resolved_dir)
    sam2_small_ckpt, _ = _find_sam2_weights("sam2_hiera_small", resolved_dir)
    onnx_primary, onnx_decoder = _find_mobile_sam_onnx_weights(resolved_dir)

    sam2_runtime = bool(has_sam2 and has_torch)
    sam2_tiny_ready = bool(sam2_runtime and sam2_tiny_ckpt is not None)
    sam2_small_ready = bool(sam2_runtime and sam2_small_ckpt is not None)
    onnx_ready = bool(has_onnx and onnx_primary is not None)

    return [
        SegmentationBackendInfo(
            backend_id="sam2_hiera_tiny",
            display_name="SAM 2.1 Hiera Tiny (Video & Image Segmentation)",
            license="Apache-2.0",
            gpl_compatible=True,
            macos_arm64_acceleration="Metal / MPS (PyTorch) with safe CPU fallback",
            min_ram_gb=8.0,
            approx_model_mb=155,
            available_now=sam2_tiny_ready,
            notes=(
                "Recommended default SAM-family neural model for 8-16 GB Apple Silicon Macs; "
                f"runtime_installed={sam2_runtime}, weights_present={sam2_tiny_ckpt is not None}, "
                f"MPS active={mps_ready}, Apple Silicon host={is_apple_silicon}, model_dir={resolved_dir}."
            ),
            is_neural=True,
            runtime_installed=sam2_runtime,
            weights_present=sam2_tiny_ckpt is not None,
            weights_path=str(sam2_tiny_ckpt) if sam2_tiny_ckpt is not None else None,
        ),
        SegmentationBackendInfo(
            backend_id="sam2_hiera_small",
            display_name="SAM 2.1 Hiera Small (Higher Hair/Edge Precision)",
            license="Apache-2.0",
            gpl_compatible=True,
            macos_arm64_acceleration="Metal / MPS (PyTorch) with safe CPU fallback",
            min_ram_gb=16.0,
            approx_model_mb=185,
            available_now=sam2_small_ready,
            notes=(
                "Higher edge fidelity neural model for 16 GB+ Apple Silicon Macs; "
                f"runtime_installed={sam2_runtime}, weights_present={sam2_small_ckpt is not None}, "
                f"model_dir={resolved_dir}."
            ),
            is_neural=True,
            runtime_installed=sam2_runtime,
            weights_present=sam2_small_ckpt is not None,
            weights_path=str(sam2_small_ckpt) if sam2_small_ckpt is not None else None,
        ),
        SegmentationBackendInfo(
            backend_id="mobile_sam_onnx",
            display_name="MobileSAM / EdgeSAM (CoreML / ONNX Runtime)",
            license="Apache-2.0",
            gpl_compatible=True,
            macos_arm64_acceleration="CoreML Execution Provider / CPU INT8",
            min_ram_gb=8.0,
            approx_model_mb=40,
            available_now=onnx_ready,
            notes=(
                "Ultra-compact ~40 MB neural segmentation model for low-storage Macs; "
                f"runtime_installed={has_onnx}, weights_present={onnx_primary is not None}, "
                f"paired_decoder={onnx_decoder is not None}, CoreML={coreml_ready}, model_dir={resolved_dir}."
            ),
            is_neural=True,
            runtime_installed=has_onnx,
            weights_present=onnx_primary is not None,
            weights_path=str(onnx_primary) if onnx_primary is not None else None,
        ),
        SegmentationBackendInfo(
            backend_id="opencv_grabcut_guided",
            display_name="OpenCV GrabCut + Guided Edge Refinement (Non-Neural Fallback)",
            license="Apache-2.0",
            gpl_compatible=True,
            macos_arm64_acceleration="Accelerate / NEON CPU (zero model download)",
            min_ram_gb=4.0,
            approx_model_mb=0,
            available_now=bool(has_cv2),
            notes=(
                "Non-neural deterministic heuristic fallback; requires zero external weights "
                "and handles +/- point prompts, but is NOT neural SAM inference."
            ),
            is_neural=False,
            runtime_installed=bool(has_cv2),
            weights_present=True,
            weights_path=None,
        ),
        SegmentationBackendInfo(
            backend_id="numpy_spline_raster",
            display_name="Native Polygon / Cubic Bézier / Brush & Color-Cue Fallback Engine",
            license="GPL-3.0-only",
            gpl_compatible=True,
            macos_arm64_acceleration="NumPy / SciPy / Pillow vectorized CPU",
            min_ram_gb=2.0,
            approx_model_mb=0,
            available_now=True,
            notes=(
                "Always available; exact mathematical polygon/Bézier evaluation, feathering, "
                "expand/contract, and non-neural color-cue fallback when OpenCV & SAM are unavailable."
            ),
            is_neural=False,
            runtime_installed=True,
            weights_present=True,
            weights_path=None,
        ),
    ]


def _vertex_xy(v: Any) -> tuple[float, float]:
    if isinstance(v, dict):
        return float(v.get("x", 0.0)), float(v.get("y", 0.0))
    return float(v[0]), float(v[1])


def sample_bezier_polygon(vertices: list[Any], *, steps_per_segment: int = 12) -> list[tuple[float, float]]:
    """Sample a closed polygon or cubic Bézier spline into dense 2D contour points."""
    if len(vertices) < 3:
        return [_vertex_xy(v) for v in vertices]

    has_handles = any(
        isinstance(v, dict) and ("in_x" in v or "out_x" in v)
        for v in vertices
    )
    if not has_handles:
        return [_vertex_xy(v) for v in vertices]

    sampled: list[tuple[float, float]] = []
    n = len(vertices)
    steps = max(4, int(steps_per_segment))
    for i in range(n):
        v0 = vertices[i]
        v1 = vertices[(i + 1) % n]
        p0 = _vertex_xy(v0)
        p3 = _vertex_xy(v1)
        if isinstance(v0, dict) and "out_x" in v0 and "out_y" in v0:
            p1 = (float(v0["out_x"]), float(v0["out_y"]))
        else:
            p1 = (p0[0] + (p3[0] - p0[0]) / 3.0, p0[1] + (p3[1] - p0[1]) / 3.0)
        if isinstance(v1, dict) and "in_x" in v1 and "in_y" in v1:
            p2 = (float(v1["in_x"]), float(v1["in_y"]))
        else:
            p2 = (p0[0] + 2.0 * (p3[0] - p0[0]) / 3.0, p0[1] + 2.0 * (p3[1] - p0[1]) / 3.0)

        for s in range(steps):
            t = s / float(steps)
            mt = 1.0 - t
            x = (
                (mt ** 3) * p0[0]
                + 3.0 * (mt ** 2) * t * p1[0]
                + 3.0 * mt * (t ** 2) * p2[0]
                + (t ** 3) * p3[0]
            )
            y = (
                (mt ** 3) * p0[1]
                + 3.0 * (mt ** 2) * t * p1[1]
                + 3.0 * mt * (t ** 2) * p2[1]
                + (t ** 3) * p3[1]
            )
            sampled.append((x, y))
    return sampled


def interpolate_keyframed_vertices(shape: MaskShape, frame_idx: int) -> list[Any]:
    """Interpolate keyframed mask vertices for ``frame_idx`` (honoring exact keyframes)."""
    if not shape.keyframed_paths:
        return list(shape.vertices)

    if frame_idx in shape.keyframed_paths:
        return list(shape.keyframed_paths[frame_idx])

    kf_frames = sorted(shape.keyframed_paths.keys())
    if frame_idx <= kf_frames[0]:
        return list(shape.keyframed_paths[kf_frames[0]])
    if frame_idx >= kf_frames[-1]:
        return list(shape.keyframed_paths[kf_frames[-1]])

    prev_f = kf_frames[0]
    next_f = kf_frames[-1]
    for f in kf_frames:
        if f < frame_idx:
            prev_f = f
        elif f > frame_idx:
            next_f = f
            break

    v_prev = shape.keyframed_paths[prev_f]
    v_next = shape.keyframed_paths[next_f]
    if len(v_prev) != len(v_next) or next_f == prev_f:
        return list(v_prev)

    alpha = float(frame_idx - prev_f) / float(next_f - prev_f)
    interp: list[Any] = []
    for a, b in zip(v_prev, v_next):
        if isinstance(a, dict) and isinstance(b, dict):
            item: dict[str, float] = {}
            for key in ("x", "y", "in_x", "in_y", "out_x", "out_y"):
                if key in a and key in b:
                    item[key] = float(a[key]) * (1.0 - alpha) + float(b[key]) * alpha
            interp.append(item)
        else:
            ax, ay = _vertex_xy(a)
            bx, by = _vertex_xy(b)
            interp.append([ax * (1.0 - alpha) + bx * alpha, ay * (1.0 - alpha) + by * alpha])
    return interp


def _normalize_neural_mask(raw_mask: np.ndarray, width: int, height: int) -> np.ndarray:
    """Convert neural model mask/logits output into ``(H, W)`` float32 alpha in ``[0.0, 1.0]``."""
    arr = np.asarray(raw_mask, dtype=np.float32)
    while arr.ndim > 2:
        arr = arr[0]
    if arr.size == 0:
        return np.zeros((height, width), dtype=np.float32)
    min_v = float(np.min(arr))
    max_v = float(np.max(arr))
    if min_v < 0.0 or max_v > 1.0:
        # Convert raw logits cleanly via numerically stable sigmoid
        arr = 1.0 / (1.0 + np.exp(-np.clip(arr, -30.0, 30.0)))
    arr = np.clip(arr, 0.0, 1.0).astype(np.float32)
    if arr.shape != (height, width):
        arr = scale_mask_to_resolution(arr, width, height)
    return arr


# Loaded segmentation models are reused across frames: rasterising an AI mask over a shot used to
# rebuild the ONNX Runtime session (or the SAM 2 network) for every frame. Bounded so at most a couple
# of models stay resident; keyed by file identity so replaced weights are reloaded.
_MODEL_CACHE: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
_MODEL_CACHE_MAX = 3


def _cached_model(kind: str, path: Path, extra: tuple[Any, ...], factory: Any) -> Any:
    try:
        st = path.stat()
        ident: tuple[Any, ...] = (kind, str(path.resolve()), st.st_mtime_ns, st.st_size, *extra)
    except OSError:
        return factory()
    hit = _MODEL_CACHE.get(ident)
    if hit is not None:
        _MODEL_CACHE.move_to_end(ident)
        return hit
    obj = factory()
    _MODEL_CACHE[ident] = obj
    while len(_MODEL_CACHE) > _MODEL_CACHE_MAX:
        _MODEL_CACHE.popitem(last=False)
    return obj


def clear_model_cache() -> None:
    """Release every resident segmentation model (Model Manager removal, memory pressure, tests)."""
    _MODEL_CACHE.clear()


def _try_sam2_segmentation(
    arr: np.ndarray,
    pos_pts: list[tuple[float, float]],
    neg_pts: list[tuple[float, float]],
    bbox: tuple[float, float, float, float] | None,
    variant: str,
    model_dir: Path,
) -> tuple[SegmentationResult | None, str | None]:
    """Attempt genuine SAM 2.1 PyTorch segmentation (MPS/CUDA/CPU) if runtime + weights exist."""
    if importlib.util.find_spec("torch") is None or importlib.util.find_spec("sam2") is None:
        return None, f"{variant}: PyTorch/sam2 runtime not installed"

    ckpt_path, config_ref = _find_sam2_weights(variant, model_dir)
    if ckpt_path is None:
        return None, f"{variant}: checkpoint weights not found in {model_dir}"

    try:
        import torch
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor

        h, w = arr.shape[:2]
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            device = "mps"
        elif torch.cuda.is_available():
            device = "cuda"
        else:
            device = "cpu"

        predictor = _cached_model("sam2", Path(ckpt_path), (str(config_ref), device),
                                  lambda: SAM2ImagePredictor(build_sam2(config_ref, str(ckpt_path), device=device)))
        predictor.set_image(arr)

        point_coords: np.ndarray | None = None
        point_labels: np.ndarray | None = None
        all_pts = [(float(x), float(y)) for x, y in pos_pts] + [(float(x), float(y)) for x, y in neg_pts]
        if all_pts:
            point_coords = np.array(all_pts, dtype=np.float32)
            point_labels = np.array([1] * len(pos_pts) + [0] * len(neg_pts), dtype=np.int32)

        box_arr: np.ndarray | None = None
        if bbox is not None:
            bx, by, bw, bh = bbox
            box_arr = np.array([bx, by, bx + bw, by + bh], dtype=np.float32)
        elif point_coords is None:
            point_coords = np.array([[w * 0.5, h * 0.5]], dtype=np.float32)
            point_labels = np.array([1], dtype=np.int32)

        with torch.inference_mode():
            masks, scores, _ = predictor.predict(
                point_coords=point_coords,
                point_labels=point_labels,
                box=box_arr,
                multimask_output=True,
            )

        masks_np = np.asarray(masks)
        scores_np = np.asarray(scores, dtype=np.float32).reshape(-1)
        best_idx = int(np.argmax(scores_np)) if scores_np.size > 0 else 0
        best_score = float(np.clip(scores_np[best_idx], 0.0, 1.0)) if scores_np.size > 0 else 0.85
        alpha = _normalize_neural_mask(masks_np[best_idx] if masks_np.ndim >= 3 else masks_np, w, h)

        return (
            SegmentationResult(
                alpha_mask=alpha,
                backend_id=variant,
                is_neural_inference=True,
                fallback_used=False,
                fallback_reason=None,
                confidence=round(best_score, 4),
                quality_status="neural_ok",
                warnings=[],
                device=device,
                model_path=str(ckpt_path),
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("SAM 2.1 execution failed (%s): %s", variant, exc)
        return None, f"{variant}: runtime error ({exc})"


def _prepare_sam_onnx_points(
    pos_pts: list[tuple[float, float]],
    neg_pts: list[tuple[float, float]],
    bbox: tuple[float, float, float, float] | None,
    w: int,
    h: int,
    scale_x: float,
    scale_y: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Encode SAM ONNX point_coords (1, N, 2) and point_labels (1, N) including box corners."""
    coords: list[list[float]] = []
    labels: list[float] = []
    for px, py in pos_pts:
        coords.append([float(px) * scale_x, float(py) * scale_y])
        labels.append(1.0)
    for nx, ny in neg_pts:
        coords.append([float(nx) * scale_x, float(ny) * scale_y])
        labels.append(0.0)
    if bbox is not None:
        bx, by, bw, bh = bbox
        coords.append([float(bx) * scale_x, float(by) * scale_y])
        labels.append(2.0)  # SAM ONNX top-left box corner
        coords.append([float(bx + bw) * scale_x, float(by + bh) * scale_y])
        labels.append(3.0)  # SAM ONNX bottom-right box corner
    if not coords:
        coords.append([w * 0.5 * scale_x, h * 0.5 * scale_y])
        labels.append(1.0)
    # SAM ONNX decoder expects a trailing padding point (label -1) when no box is supplied
    if bbox is None:
        coords.append([0.0, 0.0])
        labels.append(-1.0)
    return (
        np.array([coords], dtype=np.float32),
        np.array([labels], dtype=np.float32),
    )


def _try_mobile_sam_onnx_segmentation(
    arr: np.ndarray,
    pos_pts: list[tuple[float, float]],
    neg_pts: list[tuple[float, float]],
    bbox: tuple[float, float, float, float] | None,
    model_dir: Path,
) -> tuple[SegmentationResult | None, str | None]:
    """Attempt genuine MobileSAM / EdgeSAM ONNX Runtime segmentation (CoreML / CPU)."""
    if importlib.util.find_spec("onnxruntime") is None:
        return None, "mobile_sam_onnx: onnxruntime not installed"

    primary_path, decoder_path = _find_mobile_sam_onnx_weights(model_dir)
    if primary_path is None:
        return None, f"mobile_sam_onnx: ONNX weights not found in {model_dir}"

    try:
        import onnxruntime as ort

        h, w = arr.shape[:2]
        available = list(ort.get_available_providers())
        providers: list[str] = []
        if "CoreMLExecutionProvider" in available:
            providers.append("CoreMLExecutionProvider")
        if "CPUExecutionProvider" in available or not providers:
            providers.append("CPUExecutionProvider")

        resample = getattr(Image, "Resampling", Image).BILINEAR

        def _preprocess_encoder_input(target_h: int, target_w: int) -> tuple[np.ndarray, float, float]:
            resized = np.asarray(Image.fromarray(arr, mode="RGB").resize((target_w, target_h), resample), dtype=np.float32)
            mean = np.array([123.675, 116.28, 103.53], dtype=np.float32)
            std = np.array([58.395, 57.12, 57.375], dtype=np.float32)
            norm = (resized - mean) / std
            chw = np.transpose(norm, (2, 0, 1))[None, ...].astype(np.float32)
            return chw, float(target_w) / float(max(1, w)), float(target_h) / float(max(1, h))

        if decoder_path is not None:
            enc_sess = _cached_model("ort", Path(primary_path), tuple(providers), lambda: ort.InferenceSession(str(primary_path), providers=providers))
            dec_sess = _cached_model("ort", Path(decoder_path), tuple(providers), lambda: ort.InferenceSession(str(decoder_path), providers=providers))
            enc_in = enc_sess.get_inputs()[0]
            enc_shape = getattr(enc_in, "shape", None) or [1, 3, 1024, 1024]
            if len(enc_shape) == 3 and (enc_shape[2] == 3 or str(enc_shape[2]) == "3"):
                # Rank-3 HWC MobileSAM encoder (e.g. Acly/MobileSAM) expects aspect-preserving
                # longest-side-1024 float32 RGB HWC input and handles padding + normalization internally.
                longest_scale = 1024.0 / float(max(1, h, w))
                new_w = max(1, int(round(w * longest_scale)))
                new_h = max(1, int(round(h * longest_scale)))
                img_tensor = np.asarray(
                    Image.fromarray(arr, mode="RGB").resize((new_w, new_h), resample),
                    dtype=np.float32,
                )
                sx = float(new_w) / float(max(1, w))
                sy = float(new_h) / float(max(1, h))
            else:
                target_h = int(enc_shape[2]) if len(enc_shape) >= 4 and isinstance(enc_shape[2], int) and enc_shape[2] > 0 else 1024
                target_w = int(enc_shape[3]) if len(enc_shape) >= 4 and isinstance(enc_shape[3], int) and enc_shape[3] > 0 else 1024
                img_tensor, sx, sy = _preprocess_encoder_input(target_h, target_w)
            enc_outputs = enc_sess.run(None, {enc_in.name: img_tensor})
            image_embeddings = np.asarray(enc_outputs[0], dtype=np.float32)

            point_coords, point_labels = _prepare_sam_onnx_points(pos_pts, neg_pts, bbox, w, h, sx, sy)
            dec_feed: dict[str, Any] = {}
            for inp in dec_sess.get_inputs():
                name = inp.name
                lname = name.lower()
                if "embed" in lname:
                    dec_feed[name] = image_embeddings
                elif "coord" in lname:
                    dec_feed[name] = point_coords
                elif "label" in lname:
                    dec_feed[name] = point_labels
                elif "has_mask" in lname:
                    dec_feed[name] = np.zeros(1, dtype=np.float32)
                elif "mask_input" in lname:
                    dec_feed[name] = np.zeros((1, 1, 256, 256), dtype=np.float32)
                elif "orig_im_size" in lname or "size" in lname:
                    dec_feed[name] = np.array([float(h), float(w)], dtype=np.float32)
            outputs = dec_sess.run(None, dec_feed)
            active_providers = list(dec_sess.get_providers()) if hasattr(dec_sess, "get_providers") else providers
        else:
            sess = _cached_model("ort", Path(primary_path), tuple(providers), lambda: ort.InferenceSession(str(primary_path), providers=providers))
            inputs = sess.get_inputs()
            img_h, img_w = 1024, 1024
            for inp in inputs:
                if "image" in inp.name.lower():
                    sh = getattr(inp, "shape", None) or []
                    if len(sh) >= 4 and isinstance(sh[2], int) and sh[2] > 0:
                        img_h = int(sh[2])
                    if len(sh) >= 4 and isinstance(sh[3], int) and sh[3] > 0:
                        img_w = int(sh[3])
            img_tensor, sx, sy = _preprocess_encoder_input(img_h, img_w)
            point_coords, point_labels = _prepare_sam_onnx_points(pos_pts, neg_pts, bbox, w, h, sx, sy)
            feed: dict[str, Any] = {}
            for inp in inputs:
                lname = inp.name.lower()
                if "image" in lname:
                    feed[inp.name] = img_tensor
                elif "coord" in lname:
                    feed[inp.name] = point_coords
                elif "label" in lname:
                    feed[inp.name] = point_labels
                elif "has_mask" in lname:
                    feed[inp.name] = np.zeros(1, dtype=np.float32)
                elif "mask_input" in lname:
                    feed[inp.name] = np.zeros((1, 1, 256, 256), dtype=np.float32)
                elif "orig_im_size" in lname or "size" in lname:
                    feed[inp.name] = np.array([float(h), float(w)], dtype=np.float32)
            outputs = sess.run(None, feed)
            active_providers = list(sess.get_providers()) if hasattr(sess, "get_providers") else providers

        masks_out = np.asarray(outputs[0], dtype=np.float32)
        score = 0.86
        if len(outputs) >= 2:
            iou_preds = np.asarray(outputs[1], dtype=np.float32).reshape(-1)
            if iou_preds.size > 0:
                best_idx = int(np.argmax(iou_preds))
                score = float(np.clip(iou_preds[best_idx], 0.0, 1.0))
                if masks_out.ndim == 4 and masks_out.shape[1] > best_idx:
                    masks_out = masks_out[0, best_idx]
                elif masks_out.ndim == 3 and masks_out.shape[0] > best_idx:
                    masks_out = masks_out[best_idx]

        alpha = _normalize_neural_mask(masks_out, w, h)
        device_label = "coreml" if "CoreMLExecutionProvider" in active_providers else "cpu"
        return (
            SegmentationResult(
                alpha_mask=alpha,
                backend_id="mobile_sam_onnx",
                is_neural_inference=True,
                fallback_used=False,
                fallback_reason=None,
                confidence=round(score, 4),
                quality_status="neural_ok",
                warnings=[],
                device=device_label,
                model_path=str(primary_path),
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("MobileSAM ONNX execution failed: %s", exc)
        return None, f"mobile_sam_onnx: runtime error ({exc})"


def _assess_heuristic_quality(
    alpha: np.ndarray,
    frame_rgb: np.ndarray,
    bbox: tuple[float, float, float, float] | None,
) -> tuple[str, float, list[str]]:
    """Honest quality check detecting empty, full-frame, or degenerate rectangular box fallbacks."""
    h, w = alpha.shape[:2]
    binary = alpha > 0.5
    fg_ratio = float(np.mean(binary))
    warnings: list[str] = []

    if fg_ratio < 0.002 or fg_ratio > 0.98:
        warnings.append(
            f"Degenerate mask coverage ({fg_ratio * 100.0:.1f}%); subject separation failed — "
            "install SAM 2.1 or MobileSAM weights or refine +/- points."
        )
        return "degraded_box_fallback", 0.25, warnings

    # Check whether output collapsed to an unsegmented rectangular bounding box
    if bbox is not None:
        bx, by, bw, bh = [round(v) for v in bbox]
        box_mask = np.zeros((h, w), dtype=bool)
        x0 = max(0, min(w - 1, bx))
        y0 = max(0, min(h - 1, by))
        x1 = max(x0 + 1, min(w, bx + max(1, bw)))
        y1 = max(y0 + 1, min(h, by + max(1, bh)))
        box_mask[y0:y1, x0:x1] = True
        inter = int(np.sum(binary & box_mask))
        union = int(np.sum(binary | box_mask))
        box_iou = float(inter) / float(max(1, union))
        frame_std = float(np.std(frame_rgb.astype(np.float32)))
        if box_iou >= 0.96 and (frame_std < 6.0 or float(np.std(frame_rgb[box_mask].astype(np.float32))) < 4.0):
            warnings.append(
                "Degenerate rectangular bounding-box fallback detected (insufficient colour contrast "
                "for non-neural separation). Install neural SAM weights for true subject segmentation."
            )
            return "degraded_box_fallback", 0.30, warnings

    # Measure foreground/background colour separation for honest heuristic confidence
    fg_pixels = frame_rgb[binary].astype(np.float32)
    bg_pixels = frame_rgb[~binary].astype(np.float32)
    color_sep = float(np.linalg.norm(np.mean(fg_pixels, axis=0) - np.mean(bg_pixels, axis=0))) / 441.67
    if color_sep < 0.04:
        warnings.append(
            "Low foreground/background colour separation under non-neural heuristic fallback; "
            "mask edges may approximate a coarse region."
        )
        return "degraded_box_fallback", 0.32, warnings

    honest_conf = float(np.clip(0.45 + color_sep * 0.45, 0.45, 0.72))
    return "fallback_heuristic", round(honest_conf, 4), warnings


def segment_frame_detailed(
    frame_rgb: np.ndarray,
    *,
    positive_points: list[tuple[float, float]] | None = None,
    negative_points: list[tuple[float, float]] | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    backend: str = "auto",
    model_dir: str | Path | None = None,
) -> SegmentationResult:
    """Segment a subject/object and return full, honest neural vs fallback metadata.

    Execution priority for ``backend="auto"``:
      1. SAM 2.1 Hiera Tiny (PyTorch MPS / CUDA / CPU) when runtime + weights exist
      2. SAM 2.1 Hiera Small (PyTorch MPS / CUDA / CPU) when runtime + weights exist
      3. MobileSAM / EdgeSAM ONNX Runtime (CoreML / CPU) when runtime + weights exist
      4. OpenCV GrabCut + guided fallback (explicitly marked non-neural fallback)
      5. Pure-NumPy color-cue fallback (explicitly marked non-neural fallback)
    """
    arr = np.asarray(frame_rgb, dtype=np.uint8)
    h, w = arr.shape[:2]
    pos_pts = list(positive_points or [])
    neg_pts = list(negative_points or [])
    resolved_dir = resolve_model_dir(model_dir)
    fallback_reasons: list[str] = []

    # 1. Genuine Neural SAM 2.1 / MobileSAM ONNX paths
    neural_order: list[str] = []
    if backend == "auto":
        neural_order = ["sam2_hiera_tiny", "sam2_hiera_small", "mobile_sam_onnx"]
    elif backend in ("sam2", "sam2_hiera_tiny", "sam2_hiera_small", "mobile_sam_onnx"):
        neural_order = ["sam2_hiera_tiny"] if backend == "sam2" else [backend]

    for candidate in neural_order:
        if candidate in ("sam2_hiera_tiny", "sam2_hiera_small"):
            res, reason = _try_sam2_segmentation(arr, pos_pts, neg_pts, bbox, candidate, resolved_dir)
        else:
            res, reason = _try_mobile_sam_onnx_segmentation(arr, pos_pts, neg_pts, bbox, resolved_dir)
        if res is not None:
            return res
        if reason:
            fallback_reasons.append(reason)

    # 2. OpenCV GrabCut heuristic fallback (NEVER labeled as neural SAM!)
    if backend not in ("color_cue_fallback", "numpy_color_cue") and importlib.util.find_spec("cv2") is not None:
        try:
            import cv2

            bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            gc_mask = np.full((h, w), cv2.GC_PR_BGD, dtype=np.uint8)

            if bbox is not None:
                bx, by, bw, bh = [round(v) for v in bbox]
                bx = int(np.clip(bx, 0, max(0, w - 2)))
                by = int(np.clip(by, 0, max(0, h - 2)))
                bw = int(np.clip(bw, 2, w - bx))
                bh = int(np.clip(bh, 2, h - by))
                gc_mask[by : by + bh, bx : bx + bw] = cv2.GC_PR_FGD
            elif pos_pts:
                xs = [p[0] for p in pos_pts]
                ys = [p[1] for p in pos_pts]
                pad_x = max(12, int(w * 0.12))
                pad_y = max(12, int(h * 0.12))
                x0 = max(0, int(min(xs)) - pad_x)
                y0 = max(0, int(min(ys)) - pad_y)
                x1 = min(w, int(max(xs)) + pad_x)
                y1 = min(h, int(max(ys)) + pad_y)
                gc_mask[y0:y1, x0:x1] = cv2.GC_PR_FGD
            else:
                gc_mask[h // 6 : 5 * h // 6, w // 6 : 5 * w // 6] = cv2.GC_PR_FGD

            radius = max(3, min(w, h) // 48)
            for px, py in pos_pts:
                cv2.circle(gc_mask, (round(px), round(py)), radius, int(cv2.GC_FGD), -1)
            for nx, ny in neg_pts:
                cv2.circle(gc_mask, (round(nx), round(ny)), radius, int(cv2.GC_BGD), -1)

            if not np.any((gc_mask == cv2.GC_FGD) | (gc_mask == cv2.GC_PR_FGD)):
                gc_mask[h // 4 : 3 * h // 4, w // 4 : 3 * w // 4] = cv2.GC_PR_FGD
            if not np.any((gc_mask == cv2.GC_BGD) | (gc_mask == cv2.GC_PR_BGD)):
                gc_mask[0:2, :] = cv2.GC_BGD

            bgd_model = np.zeros((1, 65), np.float64)
            fgd_model = np.zeros((1, 65), np.float64)
            cv2.grabCut(bgr, gc_mask, None, bgd_model, fgd_model, 3, cv2.GC_INIT_WITH_MASK)
            alpha = np.where(
                (gc_mask == cv2.GC_FGD) | (gc_mask == cv2.GC_PR_FGD),
                1.0,
                0.0,
            ).astype(np.float32)

            for px, py in pos_pts:
                cv2.circle(alpha, (round(px), round(py)), max(2, radius // 2), 1.0, -1)
            for nx, ny in neg_pts:
                cv2.circle(alpha, (round(nx), round(ny)), max(3, radius), 0.0, -1)

            alpha = np.clip(alpha, 0.0, 1.0).astype(np.float32)
            quality_status, conf, quality_warnings = _assess_heuristic_quality(alpha, arr, bbox)
            reason_text = (
                "; ".join(fallback_reasons)
                if fallback_reasons
                else "OpenCV GrabCut non-neural heuristic backend selected"
            )
            warnings = [
                f"Using non-neural OpenCV GrabCut fallback ({reason_text}).",
                *quality_warnings,
            ]
            return SegmentationResult(
                alpha_mask=alpha,
                backend_id="opencv_grabcut_guided",
                is_neural_inference=False,
                fallback_used=True,
                fallback_reason=reason_text,
                confidence=conf,
                quality_status=quality_status,
                warnings=warnings,
                device="cpu",
                model_path=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("OpenCV GrabCut fallback failed: %s", exc)
            fallback_reasons.append(f"opencv_grabcut_guided failed ({exc})")

    # 3. Pure-NumPy color-distance + spatial fallback (NEVER labeled as neural SAM!)
    arr_f = arr.astype(np.float32)
    if pos_pts:
        fg_samples = np.array(
            [arr_f[int(np.clip(py, 0, h - 1)), int(np.clip(px, 0, w - 1))] for px, py in pos_pts],
            dtype=np.float32,
        )
    elif bbox is not None:
        bx, by, bw, bh = [round(v) for v in bbox]
        cx = int(np.clip(bx + bw // 2, 0, w - 1))
        cy = int(np.clip(by + bh // 2, 0, h - 1))
        fg_samples = arr_f[cy : cy + 1, cx : cx + 1].reshape(-1, 3)
    else:
        fg_samples = arr_f[h // 2 : h // 2 + 1, w // 2 : w // 2 + 1].reshape(-1, 3)

    if neg_pts:
        bg_samples = np.array(
            [arr_f[int(np.clip(ny, 0, h - 1)), int(np.clip(nx, 0, w - 1))] for nx, ny in neg_pts],
            dtype=np.float32,
        )
    else:
        bg_samples = np.array(
            [arr_f[0, 0], arr_f[0, w - 1], arr_f[h - 1, 0], arr_f[h - 1, w - 1]],
            dtype=np.float32,
        )

    fg_mean = np.mean(fg_samples, axis=0)
    bg_mean = np.mean(bg_samples, axis=0)
    d_fg = np.linalg.norm(arr_f - fg_mean, axis=2)
    d_bg = np.linalg.norm(arr_f - bg_mean, axis=2)

    degraded_box = False
    if float(np.linalg.norm(fg_mean - bg_mean)) < 3.0 and bbox is not None:
        # Degenerate zero-contrast case: only bounding box prior exists
        bx, by, bw, bh = [round(v) for v in bbox]
        alpha = np.zeros((h, w), dtype=np.float32)
        alpha[max(0, by) : min(h, by + bh), max(0, bx) : min(w, bx + bw)] = 1.0
        degraded_box = True
    else:
        alpha = (d_fg < d_bg).astype(np.float32)
        if bbox is not None:
            bx, by, bw, bh = [round(v) for v in bbox]
            box_mask = np.zeros((h, w), dtype=np.float32)
            box_mask[max(0, by) : min(h, by + bh), max(0, bx) : min(w, bx + bw)] = 1.0
            alpha *= box_mask

    alpha = np.clip(alpha, 0.0, 1.0).astype(np.float32)
    quality_status, conf, quality_warnings = _assess_heuristic_quality(alpha, arr, bbox)
    if degraded_box:
        quality_status = "degraded_box_fallback"
        conf = min(conf, 0.25)
    else:
        conf = min(conf, 0.62)

    reason_text = (
        "; ".join(fallback_reasons)
        if fallback_reasons
        else "Color-cue non-neural fallback selected"
    )
    warnings = [
        f"Using non-neural color-cue heuristic fallback ({reason_text}).",
        *quality_warnings,
    ]
    return SegmentationResult(
        alpha_mask=alpha,
        backend_id="color_cue_fallback",
        is_neural_inference=False,
        fallback_used=True,
        fallback_reason=reason_text,
        confidence=round(conf, 4),
        quality_status=quality_status,
        warnings=warnings,
        device="cpu",
        model_path=None,
    )


def segment_frame(
    frame_rgb: np.ndarray,
    *,
    positive_points: list[tuple[float, float]] | None = None,
    negative_points: list[tuple[float, float]] | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    backend: str = "auto",
    model_dir: str | Path | None = None,
) -> tuple[np.ndarray, str, float]:
    """Segment a subject/object using positive/negative points and/or bounding box.

    Backward-compatible tuple wrapper returning ``(alpha_float32_hw, backend_used, confidence)``.
    Prefer :func:`segment_frame_detailed` for honest neural vs fallback inspection.
    """
    result = segment_frame_detailed(
        frame_rgb,
        positive_points=positive_points,
        negative_points=negative_points,
        bbox=bbox,
        backend=backend,
        model_dir=model_dir,
    )
    return result.alpha_mask, result.backend_id, result.confidence


def _guided_edge_refine(alpha: np.ndarray, frame_rgb: np.ndarray, strength: float) -> np.ndarray:
    """Edge-preserving guided filter refinement for hair & fine-contour boundaries."""
    s = float(np.clip(strength, 0.0, 1.0))
    if s <= 1e-4:
        return alpha

    guide = (
        0.299 * frame_rgb[..., 0].astype(np.float32)
        + 0.587 * frame_rgb[..., 1].astype(np.float32)
        + 0.114 * frame_rgb[..., 2].astype(np.float32)
    ) / 255.0
    radius = max(3, round(3 + s * 6))
    win = 2 * radius + 1
    eps = 1e-3 + (1.0 - s) * 1e-2

    mean_i = ndimage.uniform_filter(guide, size=win)
    mean_p = ndimage.uniform_filter(alpha, size=win)
    mean_ip = ndimage.uniform_filter(guide * alpha, size=win)
    cov_ip = mean_ip - mean_i * mean_p
    mean_ii = ndimage.uniform_filter(guide * guide, size=win)
    var_i = np.maximum(0.0, mean_ii - mean_i * mean_i)

    a = cov_ip / (var_i + eps)
    b = mean_p - a * mean_i
    mean_a = ndimage.uniform_filter(a, size=win)
    mean_b = ndimage.uniform_filter(b, size=win)
    refined = np.clip(mean_a * guide + mean_b, 0.0, 1.0)

    edge_band = ndimage.binary_dilation(alpha > 0.05, iterations=radius) & ~ndimage.binary_erosion(
        alpha > 0.95, iterations=radius
    )
    out = alpha.copy()
    out[edge_band] = (1.0 - s) * alpha[edge_band] + s * refined[edge_band]
    return np.clip(out, 0.0, 1.0).astype(np.float32)


def _shape_cache_signature(shape: MaskShape) -> str:
    raw = json.dumps(shape.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def rasterize_mask_shape(
    shape: MaskShape,
    width: int,
    height: int,
    *,
    frame_idx: int = 0,
    frame_rgb: np.ndarray | None = None,
    cache: FrameMaskCache | None = None,
    alignment: ClipTimingAlignment | None = None,
    model_dir: str | Path | None = None,
) -> np.ndarray:
    """Rasterize a single ``MaskShape`` to a float32 ``[0.0, 1.0]`` matte of shape ``(H, W)``."""
    w = max(1, int(width))
    h = max(1, int(height))

    trim_tuple = (
        (alignment.clip_trim_in_frame, alignment.clip_trim_out_frame)
        if alignment is not None
        else (0, None)
    )
    speed_val = alignment.clip_speed if alignment is not None else 1.0
    sig = _shape_cache_signature(shape) if cache is not None else ""

    if cache is not None:
        cached = cache.get(
            frame_index=frame_idx,
            clip_trim=trim_tuple,
            clip_speed=speed_val,
            target_resolution=(w, h),
            shape_signature=sig,
        )
        if cached is not None:
            return cached

    if shape.tool == "ai_sam" and frame_rgb is not None and (shape.positive_points or shape.bbox is not None):
        seg = segment_frame_detailed(
            frame_rgb,
            positive_points=shape.positive_points,
            negative_points=shape.negative_points,
            bbox=shape.bbox,
            model_dir=model_dir,
        )
        alpha = scale_mask_to_resolution(seg.alpha_mask, w, h)
    else:
        img = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(img)

        verts = interpolate_keyframed_vertices(shape, frame_idx)
        if shape.tool in ("polygon", "bezier", "freehand") and len(verts) >= 3:
            pts = sample_bezier_polygon(verts) if shape.tool == "bezier" else [_vertex_xy(v) for v in verts]
            draw.polygon([(float(x), float(y)) for x, y in pts], fill=255)
        elif shape.bbox is not None and not shape.brush_strokes:
            bx, by, bw, bh = shape.bbox
            draw.rectangle([bx, by, bx + bw, by + bh], fill=255)

        for stroke in shape.brush_strokes:
            if not stroke.points:
                continue
            r = max(1.0, float(stroke.radius))
            fill_val = 0 if stroke.erase else 255
            pts = [(float(x), float(y)) for x, y in stroke.points]
            if len(pts) >= 2:
                draw.line(pts, fill=fill_val, width=max(1, round(r * 2.0)))
            for px, py in pts:
                draw.ellipse([px - r, py - r, px + r, py + r], fill=fill_val)

        alpha = np.asarray(img, dtype=np.float32) / 255.0

    # Morphological expansion (>0) or contraction (<0)
    if abs(shape.expand_px) >= 0.5:
        iters = max(1, round(abs(shape.expand_px)))
        binary = alpha >= 0.5
        if shape.expand_px > 0:
            alpha = ndimage.binary_dilation(binary, iterations=iters).astype(np.float32)
        else:
            alpha = ndimage.binary_erosion(binary, iterations=iters).astype(np.float32)

    # Edge / hair refinement when source frame is provided
    if shape.edge_refine_strength > 0.0 and frame_rgb is not None:
        guide_rgb = frame_rgb
        if guide_rgb.shape[:2] != (h, w):
            resample = getattr(Image, "Resampling", Image).BILINEAR
            guide_rgb = np.asarray(Image.fromarray(guide_rgb, mode="RGB").resize((w, h), resample), dtype=np.uint8)
        alpha = _guided_edge_refine(alpha, guide_rgb, shape.edge_refine_strength)

    # Gaussian feathering
    if shape.feather_px > 0.0:
        sigma = float(shape.feather_px) * 0.5
        alpha = ndimage.gaussian_filter(alpha, sigma=sigma)

    if shape.inverted:
        alpha = 1.0 - alpha

    alpha = np.clip(alpha * float(np.clip(shape.opacity, 0.0, 1.0)), 0.0, 1.0).astype(np.float32)
    if cache is not None:
        cache.put(
            alpha,
            frame_index=frame_idx,
            clip_trim=trim_tuple,
            clip_speed=speed_val,
            target_resolution=(w, h),
            shape_signature=sig,
        )
    return alpha


def combine_masks(
    shapes: list[MaskShape],
    width: int,
    height: int,
    *,
    frame_idx: int = 0,
    frame_rgb: np.ndarray | None = None,
    cache: FrameMaskCache | None = None,
    alignment: ClipTimingAlignment | None = None,
    model_dir: str | Path | None = None,
) -> np.ndarray:
    """Combine multiple masks with additive, subtractive, intersect, and XOR semantics."""
    h = max(1, int(height))
    w = max(1, int(width))
    if not shapes:
        return np.zeros((h, w), dtype=np.float32)

    combined = np.zeros((h, w), dtype=np.float32)
    initialized = False
    for shape in shapes:
        matte = rasterize_mask_shape(
            shape,
            w,
            h,
            frame_idx=frame_idx,
            frame_rgb=frame_rgb,
            cache=cache,
            alignment=alignment,
            model_dir=model_dir,
        )
        mode = shape.combine_mode
        if not initialized:
            combined = matte.copy()
            initialized = True
            continue
        if mode == "add":
            combined = np.maximum(combined, matte)
        elif mode == "subtract":
            combined = np.clip(combined * (1.0 - matte), 0.0, 1.0)
        elif mode == "intersect":
            combined = np.minimum(combined, matte)
        elif mode == "xor":
            combined = np.clip(combined + matte - 2.0 * combined * matte, 0.0, 1.0)
        else:
            combined = np.maximum(combined, matte)

    return np.clip(combined, 0.0, 1.0).astype(np.float32)


def _estimate_frame_flow(
    prev_rgb: np.ndarray,
    next_rgb: np.ndarray,
    prev_mask: np.ndarray,
) -> tuple[float, float, float]:
    """Estimate median (dx, dy) motion of masked region & confidence between adjacent frames."""
    h, w = prev_rgb.shape[:2]
    ys, xs = np.where(prev_mask > 0.5)
    if xs.size < 4:
        return 0.0, 0.0, 0.25

    if importlib.util.find_spec("cv2") is not None:
        try:
            import cv2

            g0 = cv2.cvtColor(prev_rgb, cv2.COLOR_RGB2GRAY)
            g1 = cv2.cvtColor(next_rgb, cv2.COLOR_RGB2GRAY)
            flow = cv2.calcOpticalFlowFarneback(
                g0,
                g1,
                None,
                pyr_scale=0.5,
                levels=2,
                winsize=13,
                iterations=3,
                poly_n=5,
                poly_sigma=1.1,
                flags=0,
            )
            roi_dx = flow[ys, xs, 0]
            roi_dy = flow[ys, xs, 1]
            dx = float(np.median(roi_dx))
            dy = float(np.median(roi_dy))
            spread = float(np.std(roi_dx) + np.std(roi_dy))
            conf = float(np.clip(1.0 - spread / max(8.0, min(w, h) * 0.1), 0.25, 0.98))
            return dx, dy, conf
        except Exception as exc:  # noqa: BLE001
            logger.debug("OpenCV Farneback optical flow failed, falling back to NumPy block search: %s", exc)

    # Deterministic NumPy phase-correlation fallback over masked bounding box
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    g0 = np.mean(prev_rgb, axis=2)
    g1 = np.mean(next_rgb, axis=2)
    tpl = g0[y0:y1, x0:x1]
    best_dx, best_dy = 0.0, 0.0
    best_err = float("inf")
    step = 2
    max_shift = 14
    th, tw = tpl.shape
    for dy in range(-max_shift, max_shift + 1, step):
        ny0 = y0 + dy
        ny1 = ny0 + th
        if ny0 < 0 or ny1 > h:
            continue
        for dx in range(-max_shift, max_shift + 1, step):
            nx0 = x0 + dx
            nx1 = nx0 + tw
            if nx0 < 0 or nx1 > w:
                continue
            err = float(np.mean(np.abs(g1[ny0:ny1, nx0:nx1] - tpl)))
            if err < best_err:
                best_err = err
                best_dx, best_dy = float(dx), float(dy)
    conf = float(np.clip(1.0 - best_err / 128.0, 0.25, 0.95))
    return best_dx, best_dy, conf


def _shift_mask(mask: np.ndarray, dx: float, dy: float) -> np.ndarray:
    shifted = ndimage.shift(mask.astype(np.float32), shift=(dy, dx), order=1, mode="constant", cval=0.0)
    return np.clip(shifted, 0.0, 1.0).astype(np.float32)


def _shift_vertices(vertices: list[Any], dx: float, dy: float, w: int, h: int) -> list[Any]:
    out: list[Any] = []
    for v in vertices:
        if isinstance(v, dict):
            nv = dict(v)
            nv["x"] = float(np.clip(float(v.get("x", 0.0)) + dx, 0.0, float(w)))
            nv["y"] = float(np.clip(float(v.get("y", 0.0)) + dy, 0.0, float(h)))
            if "in_x" in v:
                nv["in_x"] = float(np.clip(float(v["in_x"]) + dx, 0.0, float(w)))
            if "in_y" in v:
                nv["in_y"] = float(np.clip(float(v["in_y"]) + dy, 0.0, float(h)))
            if "out_x" in v:
                nv["out_x"] = float(np.clip(float(v["out_x"]) + dx, 0.0, float(w)))
            if "out_y" in v:
                nv["out_y"] = float(np.clip(float(v["out_y"]) + dy, 0.0, float(h)))
            out.append(nv)
        else:
            vx, vy = _vertex_xy(v)
            out.append([float(np.clip(vx + dx, 0.0, float(w))), float(np.clip(vy + dy, 0.0, float(h)))])
    return out


def propagate_masks(
    frames_rgb: list[np.ndarray],
    shapes: list[MaskShape],
    *,
    start_frame: int = 0,
    direction: str = "both",
    temporal_ema: float = 0.2,
    occlusion_threshold: float = 0.35,
    cache: FrameMaskCache | None = None,
    alignment: ClipTimingAlignment | None = None,
) -> list[PropagationFrameResult]:
    """Propagate masks forward and/or backward with occlusion handling and manual overrides."""
    n = len(frames_rgb)
    if n == 0:
        return []

    anchor = int(np.clip(start_frame, 0, n - 1))
    h, w = frames_rgb[0].shape[:2]

    override_set: set[int] = set()
    for s in shapes:
        override_set.update(int(f) for f in s.manual_override_frames)
        override_set.update(int(f) for f in s.keyframed_paths)

    anchor_mask = combine_masks(
        shapes,
        w,
        h,
        frame_idx=anchor,
        frame_rgb=frames_rgb[anchor],
        cache=cache,
        alignment=alignment,
    )
    results: dict[int, PropagationFrameResult] = {
        anchor: PropagationFrameResult(
            frame_index=anchor,
            alpha_mask=anchor_mask,
            confidence=1.0,
            occluded=False,
            manual_override=anchor in override_set,
            tracked_vertices=list(shapes[0].vertices) if shapes else [],
        )
    }

    def _step_propagate(step: int) -> None:
        cur_mask = anchor_mask.copy()
        cur_verts = list(shapes[0].vertices) if shapes else []
        prev_idx = anchor
        idx = anchor + step
        while 0 <= idx < n:
            if idx in override_set:
                override_mask = combine_masks(
                    shapes,
                    w,
                    h,
                    frame_idx=idx,
                    frame_rgb=frames_rgb[idx],
                    cache=cache,
                    alignment=alignment,
                )
                cur_verts = interpolate_keyframed_vertices(shapes[0], idx) if shapes else []
                results[idx] = PropagationFrameResult(
                    frame_index=idx,
                    alpha_mask=override_mask,
                    confidence=1.0,
                    occluded=False,
                    manual_override=True,
                    tracked_vertices=cur_verts,
                )
                cur_mask = override_mask
                prev_idx = idx
                idx += step
                continue

            dx, dy, conf = _estimate_frame_flow(frames_rgb[prev_idx], frames_rgb[idx], cur_mask)

            # Occlusion check: compare appearance inside shifted ROI against previous frame ROI
            shifted = _shift_mask(cur_mask, dx, dy)
            occluded = False
            ys, xs = np.where(shifted > 0.5)
            prev_ys, prev_xs = np.where(cur_mask > 0.5)
            if xs.size >= 4 and prev_xs.size >= 4:
                prev_mean = np.mean(frames_rgb[prev_idx][prev_ys, prev_xs].astype(np.float32), axis=0)
                curr_mean = np.mean(frames_rgb[idx][ys, xs].astype(np.float32), axis=0)
                color_diff = float(np.linalg.norm(curr_mean - prev_mean)) / 441.67
                if color_diff > occlusion_threshold:
                    occluded = True
                    conf = min(conf, 0.4)
                    # Hold prior mask smoothly through brief occlusion instead of collapsing
                    shifted = cur_mask.copy()
                    dx, dy = 0.0, 0.0

            # Temporal consistency EMA smoothing (prevents frame-to-frame mask jitter)
            ema = float(np.clip(temporal_ema, 0.0, 0.8))
            blended = np.clip((1.0 - ema) * shifted + ema * cur_mask, 0.0, 1.0).astype(np.float32)
            cur_verts = _shift_vertices(cur_verts, dx, dy, w, h) if cur_verts else []

            results[idx] = PropagationFrameResult(
                frame_index=idx,
                alpha_mask=blended,
                confidence=round(conf, 4),
                occluded=occluded,
                manual_override=False,
                mean_flow_dx=round(dx, 3),
                mean_flow_dy=round(dy, 3),
                tracked_vertices=cur_verts,
            )
            cur_mask = blended
            prev_idx = idx
            idx += step

    if direction in ("forward", "both"):
        _step_propagate(+1)
    if direction in ("backward", "both"):
        _step_propagate(-1)

    # Fill any untouched frames (if unidirectional) with anchor mask
    for i in range(n):
        if i not in results:
            results[i] = PropagationFrameResult(
                frame_index=i,
                alpha_mask=anchor_mask.copy(),
                confidence=0.5,
                occluded=False,
            )

    return [results[i] for i in range(n)]


def render_preview(
    frame_rgb: np.ndarray,
    alpha_mask: np.ndarray,
    *,
    preview_mode: str = "overlay",
    background_mode: str = "transparent",
    background_color: tuple[int, int, int] = (18, 21, 27),
    replace_image_rgb: np.ndarray | None = None,
    blur_strength: float = 12.0,
    selective_effect: str | None = None,
    split_ratio: float = 0.5,
) -> np.ndarray:
    """Render Mask Overlay, Isolated Alpha, Before/After Split, or Final Composite."""
    src = np.asarray(frame_rgb, dtype=np.uint8)
    h, w = src.shape[:2]
    a = scale_mask_to_resolution(alpha_mask, w, h)
    a3 = a[..., None]

    if preview_mode == "alpha":
        gray = np.clip(a * 255.0, 0.0, 255.0).astype(np.uint8)
        return np.stack([gray, gray, gray], axis=2)

    if preview_mode == "overlay":
        # Rubylith tint over masked foreground + bright orange edge contour
        tint = np.array([255.0, 90.0, 31.0], dtype=np.float32)
        blended = src.astype(np.float32) * (1.0 - 0.42 * a3) + tint * (0.42 * a3)
        edge = (a > 0.45) ^ ndimage.binary_erosion(a > 0.45, iterations=2)
        blended[edge] = np.array([255.0, 180.0, 70.0], dtype=np.float32)
        return np.clip(blended, 0.0, 255.0).astype(np.uint8)

    # Build final composite frame
    if background_mode == "solid_color":
        bg = np.zeros_like(src, dtype=np.float32)
        bg[..., 0] = background_color[0]
        bg[..., 1] = background_color[1]
        bg[..., 2] = background_color[2]
        composite = src.astype(np.float32) * a3 + bg * (1.0 - a3)
    elif background_mode == "blur_bg":
        pil_bg = Image.fromarray(src, mode="RGB").filter(ImageFilter.GaussianBlur(radius=max(1.0, float(blur_strength))))
        bg = np.asarray(pil_bg, dtype=np.float32)
        composite = src.astype(np.float32) * a3 + bg * (1.0 - a3)
    elif background_mode == "replace_image" and replace_image_rgb is not None:
        resample = getattr(Image, "Resampling", Image).BILINEAR
        pil_rep = Image.fromarray(np.asarray(replace_image_rgb, dtype=np.uint8), mode="RGB").resize((w, h), resample)
        bg = np.asarray(pil_rep, dtype=np.float32)
        composite = src.astype(np.float32) * a3 + bg * (1.0 - a3)
    elif background_mode == "selective_effect":
        eff = (selective_effect or "warm_grade").lower()
        if "blur" in eff:
            fx = np.asarray(
                Image.fromarray(src, mode="RGB").filter(ImageFilter.GaussianBlur(radius=max(1.0, float(blur_strength)))),
                dtype=np.float32,
            )
        elif "desat" in eff or "mono" in eff or "bw" in eff or "gray" in eff:
            luma = (0.299 * src[..., 0] + 0.587 * src[..., 1] + 0.114 * src[..., 2]).astype(np.float32)
            fx = np.stack([luma, luma, luma], axis=2)
        else:
            fx = np.clip(
                src.astype(np.float32) * np.array([1.15, 1.04, 0.88], dtype=np.float32),
                0.0,
                255.0,
            )
        composite = fx * a3 + src.astype(np.float32) * (1.0 - a3)
    else:
        yy, xx = np.indices((h, w))
        checker = ((xx // 12) + (yy // 12)) % 2 == 0
        bg = np.where(checker[..., None], 44.0, 28.0).astype(np.float32)
        bg = np.broadcast_to(bg, (h, w, 3))
        composite = src.astype(np.float32) * a3 + bg * (1.0 - a3)

    composite_u8 = np.clip(composite, 0.0, 255.0).astype(np.uint8)

    if preview_mode == "before_after":
        split_x = int(np.clip(round(w * float(split_ratio)), 1, max(1, w - 1)))
        out = src.copy()
        out[:, split_x:] = composite_u8[:, split_x:]
        out[:, max(0, split_x - 1) : min(w, split_x + 1)] = np.array([255, 90, 31], dtype=np.uint8)
        return out

    return composite_u8


def build_mlt_rotoscoping_spline_json(
    shape: MaskShape,
    width: int,
    height: int,
    *,
    tracked_keyframes: dict[int, list[Any]] | None = None,
) -> str:
    """Serialize mask polygons/Béziers to Kdenlive/MLT ``rotoscoping`` spline JSON format."""
    w = float(max(1, width))
    h = float(max(1, height))
    frames_map: dict[int, list[Any]] = {}
    if tracked_keyframes:
        frames_map.update(tracked_keyframes)
    elif shape.keyframed_paths:
        frames_map.update(shape.keyframed_paths)
    else:
        frames_map[0] = list(shape.vertices)

    mlt_spline: dict[str, list[list[list[float]]]] = {}
    for frame_idx, verts in sorted(frames_map.items()):
        if len(verts) < 3:
            continue
        bpoints: list[list[list[float]]] = []
        for v in verts:
            px, py = _vertex_xy(v)
            if isinstance(v, dict):
                ix = float(v.get("in_x", px))
                iy = float(v.get("in_y", py))
                ox = float(v.get("out_x", px))
                oy = float(v.get("out_y", py))
            else:
                ix, iy, ox, oy = px, py, px, py
            bpoints.append([[ix / w, iy / h], [px / w, py / h], [ox / w, oy / h]])
        mlt_spline[str(int(frame_idx))] = bpoints
    return json.dumps(mlt_spline, separators=(",", ":"))


def export_mask_to_mlt(
    session: RotoSession,
    alpha_frames: list[np.ndarray],
    output_dir: str | Path,
    *,
    clip_id: str = "clip_v1",
    track_id: str = "V1",
    project_id: str = "proj_roto",
    sequence_id: str = "seq_main",
    expected_revision: int = 0,
    alignment: ClipTimingAlignment | None = None,
) -> dict[str, Any]:
    """Export mask matte PNG/MP4 sequence, MLT spline JSON, and Timeline Command API v1 batch."""
    out_root = Path(output_dir).resolve()
    frames_dir = out_root / "matte_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    frames_to_export = (
        align_mask_sequence_to_timeline(alpha_frames, alignment)
        if alignment is not None and alpha_frames
        else alpha_frames
    )

    matte_paths: list[str] = []
    for idx, alpha in enumerate(frames_to_export):
        u8 = np.clip(np.asarray(alpha, dtype=np.float32) * 255.0, 0.0, 255.0).astype(np.uint8)
        p = frames_dir / f"matte_{idx:05d}.png"
        Image.fromarray(u8, mode="L").save(p)
        matte_paths.append(str(p))

    primary_matte = matte_paths[0] if matte_paths else ""
    matte_video_path = ""
    export_fps = alignment.timeline_fps if alignment is not None else session.fps
    if len(matte_paths) >= 2 and shutil.which("ffmpeg"):
        mp4_out = out_root / f"{session.session_id}_matte.mp4"
        cmd = [
            "ffmpeg",
            "-y",
            "-framerate",
            f"{export_fps:.3f}",
            "-i",
            str(frames_dir / "matte_%05d.png"),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "16",
            str(mp4_out),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, shell=False)
        if res.returncode == 0 and mp4_out.is_file():
            matte_video_path = str(mp4_out)

    primary_shape = session.masks[0] if session.masks else MaskShape(id="mask_1", name="Mask 1")
    splines_json = build_mlt_rotoscoping_spline_json(primary_shape, session.width, session.height)

    tl_start = alignment.timeline_start_frame if alignment is not None else 0
    builder = TimelineCommandBuilder(
        project_id=project_id,
        sequence_id=sequence_id,
        expected_revision=expected_revision,
        agent_id="roto_studio",
        agent_role="rotoscoping_and_masking",
        transaction_label=f"Apply Rotoscope Mask ({primary_shape.name})",
        requires_approval=False,
        dry_run=False,
        fps_num=max(1, round(export_fps)),
        fps_den=1,
    )
    op_map = {
        "add": "add",
        "subtract": "subtract",
        "intersect": "intersect",
        "xor": "write_on_clear",
    }
    builder.add_operation(
        "apply_mask",
        clip_id=clip_id,
        track_id=track_id,
        in_frame=0,
        out_frame=max(1, len(frames_to_export)),
        timeline_start=tl_start,
        params={
            "mask_id": primary_shape.id,
            "mask_type": primary_shape.tool if primary_shape.tool in ("polygon", "bezier", "ai_sam") else "matte_file",
            "splines_json": splines_json,
            "matte_path": matte_video_path or primary_matte,
            "feather": float(primary_shape.feather_px),
            "expand": float(primary_shape.expand_px),
            "invert": bool(primary_shape.inverted),
            "operation": op_map.get(primary_shape.combine_mode, "write_on_clear"),
        },
        confidence=0.95,
        rationale=f"Apply native rotoscope mask '{primary_shape.name}' ({len(frames_to_export)} frames)",
    )
    batch = builder.build()

    batch_path = out_root / f"{session.session_id}_command_batch.json"
    batch_path.write_text(json.dumps(batch, indent=2), encoding="utf-8")

    return {
        "session_id": session.session_id,
        "frame_count": len(frames_to_export),
        "matte_frames": matte_paths,
        "matte_video_path": matte_video_path,
        "primary_matte_path": matte_video_path or primary_matte,
        "mlt_splines_json": splines_json,
        "command_batch": batch,
        "command_batch_path": str(batch_path),
    }
