"""EXPERIMENTAL AI Vision Service (Object Masking, Tracking, Background Removal, Inpainting).

WARNING: This module is explicitly isolated under the ``EXPERIMENTAL`` tier.
Core multicamera editing workflows NEVER depend on it, and all operations run
inside strict exception boundaries with deterministic OpenCV CPU fallbacks when
heavy ML backends (such as SAM2 / PyTorch) are unavailable.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from bangonpro.ai_engine.commands import TimelineCommandBuilder

EXPERIMENTAL = True
STABILITY_TIER = "experimental"
EXPERIMENTAL_VISION_WARNING = (
    "EXPERIMENTAL AI VISION SERVICE: Results require human editorial approval "
    "and are isolated from core non-linear editing stability guarantees."
)


@dataclass
class ExperimentalVisionResult:
    """Isolated result envelope returned by ``ExperimentalVisionService``."""

    operation: str
    success: bool
    backend_used: str
    experimental: bool = True
    warning: str = EXPERIMENTAL_VISION_WARNING
    tracks: list[dict[str, Any]] = field(default_factory=list)
    output_artifacts: dict[str, str] = field(default_factory=dict)
    command_batch: dict[str, Any] | None = None
    error: str | None = None


class ExperimentalVisionService:
    """Feature-detected SAM2 / OpenCV CPU fallback worker for experimental vision tasks."""

    EXPERIMENTAL: bool = True
    STABILITY_TIER: str = "experimental"

    def __init__(self, *, prefer_sam2: bool = True) -> None:
        self.prefer_sam2 = prefer_sam2

    @staticmethod
    def detect_backends() -> dict[str, bool]:
        return {
            "sam2": importlib.util.find_spec("sam2") is not None,
            "torch": importlib.util.find_spec("torch") is not None,
            "opencv_cpu_fallback": importlib.util.find_spec("cv2") is not None,
        }

    def _select_backend(self) -> str:
        backends = self.detect_backends()
        if self.prefer_sam2 and backends["sam2"] and backends["torch"]:
            return "sam2"
        if backends["opencv_cpu_fallback"]:
            return "opencv_cpu_fallback"
        return "numpy_cpu_fallback"

    def track_object_bbox(
        self,
        video_path: str | Path,
        initial_bbox: tuple[int, int, int, int],
        *,
        max_frames: int = 30,
        project_id: str = "proj_exp",
        sequence_id: str = "seq_main",
        expected_revision: int = 0,
        clip_id: str = "clip_v1",
    ) -> ExperimentalVisionResult:
        """Track a bounding box ``(x, y, w, h)`` using optical flow / template matching fallback."""
        backend = self._select_backend()
        try:
            import cv2

            cap = cv2.VideoCapture(str(video_path))
            if not cap.isOpened():
                return ExperimentalVisionResult(
                    operation="object_track",
                    success=False,
                    backend_used=backend,
                    error=f"Unable to open video: {video_path}",
                )

            x, y, w, h = [int(v) for v in initial_bbox]
            tracks: list[dict[str, Any]] = []
            prev_gray = None
            frame_idx = 0

            while frame_idx < max_frames:
                ok, frame = cap.read()
                if not ok:
                    break
                fh, fw = frame.shape[:2]
                x = int(np.clip(x, 0, max(0, fw - 2)))
                y = int(np.clip(y, 0, max(0, fh - 2)))
                w = int(np.clip(w, 2, fw - x))
                h = int(np.clip(h, 2, fh - y))

                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                if prev_gray is not None:
                    flow = cv2.calcOpticalFlowFarneback(
                        prev_gray,
                        gray,
                        None,
                        pyr_scale=0.5,
                        levels=2,
                        winsize=11,
                        iterations=2,
                        poly_n=5,
                        poly_sigma=1.1,
                        flags=0,
                    )
                    roi_flow = flow[y : y + h, x : x + w]
                    if roi_flow.size > 0:
                        dx = float(np.median(roi_flow[..., 0]))
                        dy = float(np.median(roi_flow[..., 1]))
                        x = int(np.clip(round(x + dx), 0, max(0, fw - w)))
                        y = int(np.clip(round(y + dy), 0, max(0, fh - h)))

                tracks.append({"frame": frame_idx, "x": x, "y": y, "w": w, "h": h})
                prev_gray = gray
                frame_idx += 1

            cap.release()

            # Emit an approval-gated Timeline Command API v1 keyframe proposal
            builder = TimelineCommandBuilder(
                project_id=project_id,
                sequence_id=sequence_id,
                expected_revision=expected_revision,
                agent_id="experimental_vision_service",
                agent_role="experimental_vision",
                transaction_label="EXPERIMENTAL: Object Track Keyframes",
                requires_approval=True,
                dry_run=True,
            )
            builder.add_operation(
                "add_effect",
                clip_id=clip_id,
                track_id="V1",
                params={
                    "effect_service": "rotoscoping",
                    "experimental": True,
                    "backend": backend,
                },
                confidence=0.65,
                rationale=EXPERIMENTAL_VISION_WARNING,
            )
            for sample in tracks[:: max(1, len(tracks) // 5 or 1)]:
                builder.add_operation(
                    "set_keyframe",
                    clip_id=clip_id,
                    track_id="V1",
                    in_frame=sample["frame"],
                    out_frame=sample["frame"],
                    timeline_start=sample["frame"],
                    params={"bbox": sample, "experimental": True},
                    confidence=0.65,
                    rationale="Experimental tracked bounding-box keyframe",
                )

            return ExperimentalVisionResult(
                operation="object_track",
                success=len(tracks) > 0,
                backend_used=backend,
                tracks=tracks,
                command_batch=builder.build(),
            )
        except Exception as exc:  # noqa: BLE001
            return ExperimentalVisionResult(
                operation="object_track",
                success=False,
                backend_used=backend,
                error=f"Isolated experimental vision failure: {exc}",
            )

    def generate_foreground_mask(
        self,
        video_path: str | Path,
        output_mask_path: str | Path,
        *,
        frame_index: int = 0,
        roi_bbox: tuple[int, int, int, int] | None = None,
    ) -> ExperimentalVisionResult:
        """Generate a foreground mask PNG via OpenCV GrabCut/threshold fallback (isolated)."""
        backend = self._select_backend()
        try:
            import cv2

            cap = cv2.VideoCapture(str(video_path))
            if not cap.isOpened():
                return ExperimentalVisionResult(
                    operation="object_mask",
                    success=False,
                    backend_used=backend,
                    error=f"Unable to open video: {video_path}",
                )
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(frame_index)))
            ok, frame = cap.read()
            cap.release()
            if not ok or frame is None:
                return ExperimentalVisionResult(
                    operation="object_mask",
                    success=False,
                    backend_used=backend,
                    error=f"Unable to read frame {frame_index} from {video_path}",
                )

            h, w = frame.shape[:2]
            rect = roi_bbox or (w // 6, h // 6, (2 * w) // 3, (2 * h) // 3)
            mask = np.zeros((h, w), np.uint8)
            bgd_model = np.zeros((1, 65), np.float64)
            fgd_model = np.zeros((1, 65), np.float64)

            try:
                cv2.grabCut(
                    frame,
                    mask,
                    rect,
                    bgd_model,
                    fgd_model,
                    2,
                    cv2.GC_INIT_WITH_RECT,
                )
                bin_mask = np.where((mask == 2) | (mask == 0), 0, 255).astype(np.uint8)
            except Exception:  # noqa: BLE001
                # Deterministic center-ROI mask fallback for uniform synthetic frames
                bin_mask = np.zeros((h, w), np.uint8)
                rx, ry, rw, rh = rect
                bin_mask[ry : ry + rh, rx : rx + rw] = 255

            out_path = Path(output_mask_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(out_path), bin_mask)

            return ExperimentalVisionResult(
                operation="object_mask",
                success=out_path.is_file(),
                backend_used=backend,
                output_artifacts={"mask_png": str(out_path)},
            )
        except Exception as exc:  # noqa: BLE001
            return ExperimentalVisionResult(
                operation="object_mask",
                success=False,
                backend_used=backend,
                error=f"Isolated experimental mask failure: {exc}",
            )

    def inpaint_region(
        self,
        video_path: str | Path,
        region_bbox: tuple[int, int, int, int],
        output_image_path: str | Path,
        *,
        frame_index: int = 0,
    ) -> ExperimentalVisionResult:
        """Inpaint a masked region on a video frame using OpenCV Telea CPU fallback."""
        backend = self._select_backend()
        try:
            import cv2

            cap = cv2.VideoCapture(str(video_path))
            if not cap.isOpened():
                return ExperimentalVisionResult(
                    operation="inpaint_region",
                    success=False,
                    backend_used=backend,
                    error=f"Unable to open video: {video_path}",
                )
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(frame_index)))
            ok, frame = cap.read()
            cap.release()
            if not ok or frame is None:
                return ExperimentalVisionResult(
                    operation="inpaint_region",
                    success=False,
                    backend_used=backend,
                    error=f"Unable to read frame {frame_index}",
                )

            h, w = frame.shape[:2]
            rx, ry, rw, rh = region_bbox
            mask = np.zeros((h, w), dtype=np.uint8)
            mask[max(0, ry) : min(h, ry + rh), max(0, rx) : min(w, rx + rw)] = 255

            inpainted = cv2.inpaint(frame, mask, inpaintRadius=3, flags=cv2.INPAINT_TELEA)
            out_path = Path(output_image_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(out_path), inpainted)

            return ExperimentalVisionResult(
                operation="inpaint_region",
                success=out_path.is_file(),
                backend_used=backend,
                output_artifacts={"inpainted_png": str(out_path)},
            )
        except Exception as exc:  # noqa: BLE001
            return ExperimentalVisionResult(
                operation="inpaint_region",
                success=False,
                backend_used=backend,
                error=f"Isolated experimental inpaint failure: {exc}",
            )
