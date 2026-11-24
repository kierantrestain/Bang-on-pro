"""Selective, License-Clean ArtCraft 2D Composition, Storyboard & GenAI Bridge.

Selectively adapts clean architectural concepts from ArtCraft (storytold/artcraft),
which is dual-licensed under MIT OR Apache-2.0 (both GPL-3.0-compatible):
  1. Mask normalization (`normalize_matte_mask` <- `normalize_image_bytes_to_flux_mask.rs`)
  2. Polygon hit-testing & collage auto-packing (`point_in_polygon`, `pack_collage_rects`
     <- `pointInPolygon.ts`, `packCollage.ts`)
  3. Undoable 2D multi-layer composition canvas (`Canvas2DComposer` <- `MoodboardStore.ts`,
     `MoodboardHistoryStore.ts`) outputting RGBA plates straight into Kdenlive's Media Bin
  4. Storyboard shot-card workflow (`StoryboardEngine` <- `StoryboardStore.ts`) exporting
     animatic sequences straight into Kdenlive's Media Bin and native Timeline
  5. Consent-gated generative 2D asset & inpainting catalog (`list_generative_models`
     <- `artcraft-native/src/models.rs`), defaulting to 100% local deterministic/OpenCV
     plate rendering and inpainting with ZERO paid API required.

COMPLIANCE: Zero third-party excluded assets from ArtCraft's `NOTICE` (sound/, memes/,
skybox/, pose/, characters_gif/) are included or referenced. 3D scene/mesh/splat workflows
are intentionally deferred so the native NLE remains fast and rock-solid.

SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
SPDX-FileCopyrightText: 2026 ArtCraft Team and ArtCraft Contributors (MIT OR Apache-2.0 adapted concepts)
SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import copy
import importlib.util
import json
import math
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from bangonpro.ai_engine.commands import TimelineCommandBuilder

ARTCRAFT_ATTRIBUTION_NOTICE = (
    "Selectively incorporates 2D canvas, mask normalization, storyboard, and model-catalog "
    "patterns adapted from ArtCraft (https://github.com/storytold/artcraft), Copyright (c) 2026 "
    "ArtCraft Team and Contributors, licensed under MIT OR Apache-2.0. Excluded third-party "
    "media assets listed in ArtCraft's NOTICE are NOT included."
)


class GenerativeConsentError(PermissionError):
    """Raised when an external generative AI model is invoked without explicit user consent."""


@dataclass(frozen=True)
class GenerativeModelSpec:
    """Declarative generative asset model descriptor (adapted from ArtCraft models.rs)."""

    model_id: str
    display_name: str
    family: str
    media_kind: str  # "image" | "inpaint" | "bg_remove" | "storyboard_plate"
    is_local: bool
    requires_consent: bool
    license: str
    description: str


def list_generative_models() -> list[GenerativeModelSpec]:
    """Return local-first (zero cost) and optional consent-gated external generative models."""
    return [
        GenerativeModelSpec(
            model_id="local_plate_renderer",
            display_name="Bang On Pro Local 2D Plate & Storyboard Renderer",
            family="Local Native",
            media_kind="storyboard_plate",
            is_local=True,
            requires_consent=False,
            license="GPL-3.0-only",
            description="Instant local RGBA studio plates, gradients, lower-third cards, and annotated storyboard frames.",
        ),
        GenerativeModelSpec(
            model_id="local_opencv_inpaint",
            display_name="Local Mask Inpainting (OpenCV Telea / Harmonic Diffusion)",
            family="Local Native",
            media_kind="inpaint",
            is_local=True,
            requires_consent=False,
            license="Apache-2.0 / GPL-3.0-only",
            description="Offline local object removal & clean-plate background fill inside a mask region.",
        ),
        GenerativeModelSpec(
            model_id="local_bg_cutout",
            display_name="Local Subject Cutout & Background Removal",
            family="Local Native",
            media_kind="bg_remove",
            is_local=True,
            requires_consent=False,
            license="Apache-2.0 / GPL-3.0-only",
            description="Isolate foreground cutouts into transparent RGBA 2D layers using Bang On Pro Roto Engine.",
        ),
        GenerativeModelSpec(
            model_id="external_flux_image",
            display_name="External FLUX / OpenAI-Compatible Image Generator (Opt-In)",
            family="External Provider",
            media_kind="image",
            is_local=False,
            requires_consent=True,
            license="External API Terms",
            description="Optional external text-to-image plate generation; requires user API endpoint + explicit consent.",
        ),
    ]


def normalize_matte_mask(rgba_array: np.ndarray) -> np.ndarray:
    """Normalize arbitrary RGBA image to crisp binary mask RGBA (ArtCraft mask normalizer).

    Transparent pixels (alpha == 0) become opaque black ``[0, 0, 0, 255]`` (ignore region);
    non-transparent pixels (alpha > 0) become opaque white ``[255, 255, 255, 255]`` (interest region).
    """
    arr = np.asarray(rgba_array, dtype=np.uint8)
    if arr.ndim == 2:
        alpha = arr
    elif arr.ndim == 3 and arr.shape[2] == 4:
        alpha = arr[..., 3]
    elif arr.ndim == 3 and arr.shape[2] == 3:
        alpha = np.max(arr, axis=2)
    else:
        raise ValueError(f"Expected HxW, HxWx3, or HxWx4 image array; got shape {arr.shape}")

    h, w = alpha.shape
    out = np.zeros((h, w, 4), dtype=np.uint8)
    fg = alpha > 0
    out[fg, 0:3] = 255
    out[..., 3] = 255
    return out


def point_in_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
    """Ray-casting 2D point-in-polygon hit test (adapted from ArtCraft pointInPolygon.ts)."""
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = float(polygon[i][0]), float(polygon[i][1])
        xj, yj = float(polygon[j][0]), float(polygon[j][1])
        intersects = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) if abs(yj - yi) > 1e-12 else 1e-12) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def pack_collage_rects(
    item_sizes: list[tuple[int, int]],
    canvas_width: int,
    canvas_height: int,
    *,
    padding: int = 16,
) -> list[tuple[int, int, int, int]]:
    """Arrange 2D layers cleanly into a balanced collage grid (adapted from ArtCraft packCollage.ts)."""
    count = len(item_sizes)
    if count == 0:
        return []
    cols = max(1, int(math.ceil(math.sqrt(count))))
    rows = max(1, int(math.ceil(count / float(cols))))
    cell_w = max(16, (canvas_width - padding * (cols + 1)) // cols)
    cell_h = max(16, (canvas_height - padding * (rows + 1)) // rows)

    placements: list[tuple[int, int, int, int]] = []
    for idx, (iw, ih) in enumerate(item_sizes):
        r = idx // cols
        c = idx % cols
        scale = min(cell_w / float(max(1, iw)), cell_h / float(max(1, ih)))
        dw = max(8, int(round(iw * scale)))
        dh = max(8, int(round(ih * scale)))
        cx = padding + c * (cell_w + padding) + (cell_w - dw) // 2
        cy = padding + r * (cell_h + padding) + (cell_h - dh) // 2
        placements.append((cx, cy, dw, dh))
    return placements


def inpaint_masked_region(frame_rgb: np.ndarray, alpha_mask: np.ndarray, *, radius: int = 5) -> np.ndarray:
    """Cleanly inpaint/fill a masked region locally (OpenCV Telea with NumPy fallback)."""
    src = np.asarray(frame_rgb, dtype=np.uint8)
    mask_u8 = (np.asarray(alpha_mask, dtype=np.float32) > 0.5).astype(np.uint8) * 255
    if not np.any(mask_u8):
        return src.copy()

    if importlib.util.find_spec("cv2") is not None:
        try:
            import cv2

            bgr = cv2.cvtColor(src, cv2.COLOR_RGB2BGR)
            inpainted = cv2.inpaint(bgr, mask_u8, max(1, int(radius)), cv2.INPAINT_TELEA)
            return cv2.cvtColor(inpainted, cv2.COLOR_BGR2RGB)
        except Exception:  # noqa: BLE001
            pass

    # Pure-NumPy iterative harmonic boundary diffusion fallback
    out = src.astype(np.float32).copy()
    mask_bool = mask_u8 > 0
    bg_vals = out[~mask_bool]
    if bg_vals.size > 0:
        out[mask_bool] = np.mean(bg_vals, axis=0)
    for _ in range(18):
        avg = (
            np.roll(out, 1, axis=0)
            + np.roll(out, -1, axis=0)
            + np.roll(out, 1, axis=1)
            + np.roll(out, -1, axis=1)
        ) * 0.25
        out[mask_bool] = avg[mask_bool]
    return np.clip(out, 0.0, 255.0).astype(np.uint8)


@dataclass
class ArtCraftLayer:
    """Single 2D compositing layer inside Bang On Pro's ArtCraft-adapted Canvas2DComposer."""

    layer_id: str
    name: str
    kind: str = "solid"  # "image" | "cutout" | "solid" | "gradient" | "text"
    x: float = 0.0
    y: float = 0.0
    width: int = 320
    height: int = 180
    scale: float = 1.0
    rotation_deg: float = 0.0
    opacity: float = 1.0
    blend_mode: str = "normal"  # "normal" | "multiply" | "screen" | "overlay"
    visible: bool = True
    locked: bool = False
    z_index: int = 0
    image_path: str | None = None
    color_rgba: tuple[int, int, int, int] = (255, 90, 31, 255)
    secondary_color_rgba: tuple[int, int, int, int] = (18, 21, 27, 255)
    text: str = ""

    def bounding_polygon(self) -> list[tuple[float, float]]:
        w = float(self.width) * float(self.scale)
        h = float(self.height) * float(self.scale)
        return [
            (self.x, self.y),
            (self.x + w, self.y),
            (self.x + w, self.y + h),
            (self.x, self.y + h),
        ]


class Canvas2DComposer:
    """Undoable 2D multi-layer compositor (adapted from ArtCraft MoodboardStore/HistoryStore)."""

    def __init__(self, width: int = 1920, height: int = 1080) -> None:
        self.width = max(16, int(width))
        self.height = max(16, int(height))
        self.layers: list[ArtCraftLayer] = []
        self._undo_stack: list[list[ArtCraftLayer]] = []
        self._redo_stack: list[list[ArtCraftLayer]] = []

    def _snapshot(self) -> None:
        self._undo_stack.append(copy.deepcopy(self.layers))
        if len(self._undo_stack) > 50:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def _sync_z(self) -> None:
        for idx, layer in enumerate(self.layers):
            layer.z_index = idx

    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    def undo(self) -> bool:
        if not self._undo_stack:
            return False
        self._redo_stack.append(copy.deepcopy(self.layers))
        self.layers = self._undo_stack.pop()
        return True

    def redo(self) -> bool:
        if not self._redo_stack:
            return False
        self._undo_stack.append(copy.deepcopy(self.layers))
        self.layers = self._redo_stack.pop()
        return True

    def add_layer(self, layer: ArtCraftLayer) -> ArtCraftLayer:
        self._snapshot()
        self.layers.append(layer)
        self._sync_z()
        return layer

    def remove_layer(self, layer_id: str) -> bool:
        before = len(self.layers)
        if not any(l.layer_id == layer_id for l in self.layers):
            return False
        self._snapshot()
        self.layers = [l for l in self.layers if l.layer_id != layer_id]
        self._sync_z()
        return len(self.layers) < before

    def reorder_layer(self, from_index: int, to_index: int) -> bool:
        if not (0 <= from_index < len(self.layers) and 0 <= to_index < len(self.layers)):
            return False
        self._snapshot()
        item = self.layers.pop(from_index)
        self.layers.insert(to_index, item)
        self._sync_z()
        return True

    def hit_test(self, x: float, y: float) -> ArtCraftLayer | None:
        """Return topmost visible layer containing ``(x, y)`` using polygon hit testing."""
        for layer in reversed(self.layers):
            if layer.visible and point_in_polygon(x, y, layer.bounding_polygon()):
                return layer
        return None

    def auto_pack_collage(self, *, padding: int = 24) -> None:
        """Auto-arrange all layers into a clean grid collage (undoable)."""
        if not self.layers:
            return
        self._snapshot()
        rects = pack_collage_rects(
            [(l.width, l.height) for l in self.layers],
            self.width,
            self.height,
            padding=padding,
        )
        for layer, (rx, ry, rw, rh) in zip(self.layers, rects):
            layer.x = float(rx)
            layer.y = float(ry)
            layer.width = int(rw)
            layer.height = int(rh)
            layer.scale = 1.0

    def _render_layer_rgba(self, layer: ArtCraftLayer) -> np.ndarray:
        dw = max(1, int(round(layer.width * layer.scale)))
        dh = max(1, int(round(layer.height * layer.scale)))

        if layer.kind in ("image", "cutout") and layer.image_path and Path(layer.image_path).is_file():
            img = Image.open(layer.image_path).convert("RGBA").resize((dw, dh), Image.BILINEAR)
            return np.asarray(img, dtype=np.float32)

        if layer.kind == "gradient":
            c0 = np.array(layer.color_rgba, dtype=np.float32)
            c1 = np.array(layer.secondary_color_rgba, dtype=np.float32)
            t = np.linspace(0.0, 1.0, dh, dtype=np.float32)[:, None, None]
            grad = c0 * (1.0 - t) + c1 * t
            return np.broadcast_to(grad, (dh, dw, 4)).copy()

        img = Image.new("RGBA", (dw, dh), tuple(int(c) for c in layer.color_rgba))
        if layer.kind == "text" and layer.text:
            draw = ImageDraw.Draw(img)
            draw.rectangle([0, 0, dw - 1, dh - 1], outline=(255, 90, 31, 255), width=2)
            draw.text((14, max(8, dh // 3)), layer.text, fill=(242, 241, 238, 255))
        return np.asarray(img, dtype=np.float32)

    def render_rgba(self) -> np.ndarray:
        """Flatten all visible 2D layers with blend modes and opacity to an HxWx4 uint8 RGBA plate."""
        canvas = np.zeros((self.height, self.width, 4), dtype=np.float32)
        for layer in sorted(self.layers, key=lambda l: l.z_index):
            if not layer.visible or layer.opacity <= 0.0:
                continue
            src = self._render_layer_rgba(layer)
            sh, sw = src.shape[:2]
            x0 = int(round(layer.x))
            y0 = int(round(layer.y))
            x1 = min(self.width, x0 + sw)
            y1 = min(self.height, y0 + sh)
            sx0 = max(0, -x0)
            sy0 = max(0, -y0)
            dx0 = max(0, x0)
            dy0 = max(0, y0)
            if x1 <= dx0 or y1 <= dy0:
                continue

            patch = src[sy0 : sy0 + (y1 - dy0), sx0 : sx0 + (x1 - dx0)]
            dst = canvas[dy0:y1, dx0:x1]

            sa = np.clip((patch[..., 3:4] / 255.0) * float(np.clip(layer.opacity, 0.0, 1.0)), 0.0, 1.0)
            da = np.clip(dst[..., 3:4] / 255.0, 0.0, 1.0)
            s_rgb = patch[..., :3] / 255.0
            d_rgb = dst[..., :3] / 255.0

            mode = layer.blend_mode.lower()
            if mode == "multiply":
                blended = s_rgb * d_rgb
            elif mode == "screen":
                blended = 1.0 - (1.0 - s_rgb) * (1.0 - d_rgb)
            elif mode == "overlay":
                blended = np.where(d_rgb < 0.5, 2.0 * s_rgb * d_rgb, 1.0 - 2.0 * (1.0 - s_rgb) * (1.0 - d_rgb))
            else:
                blended = s_rgb

            out_a = sa + da * (1.0 - sa)
            safe_a = np.where(out_a > 1e-6, out_a, 1.0)
            out_rgb = (blended * sa + d_rgb * da * (1.0 - sa)) / safe_a
            canvas[dy0:y1, dx0:x1, :3] = np.clip(out_rgb * 255.0, 0.0, 255.0)
            canvas[dy0:y1, dx0:x1, 3:4] = np.clip(out_a * 255.0, 0.0, 255.0)

        return np.clip(canvas, 0.0, 255.0).astype(np.uint8)

    def render_to_png(self, output_path: str | Path) -> Path:
        """Render the flattened RGBA composition directly to PNG ready for Media Bin import."""
        out = Path(output_path).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        rgba = self.render_rgba()
        Image.fromarray(rgba, mode="RGBA").save(out)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "layers": [asdict(l) for l in self.layers],
        }


@dataclass
class StoryboardShot:
    """Single storyboard shot card (adapted from ArtCraft StoryboardStore.ts)."""

    id: str = field(default_factory=lambda: f"shot_{uuid.uuid4().hex[:8]}")
    shot_number: int = 1
    title: str = ""
    dialogue: str = ""
    action: str = ""
    notes: str = ""
    duration_sec: float = 3.0
    image_path: str | None = None
    prompt: str = ""


class StoryboardEngine:
    """Storyboard shot-card planner exporting animatics directly to Bang On Pro's timeline."""

    def __init__(self, title: str = "Bang On Pro Storyboard") -> None:
        self.title = title
        self.shots: list[StoryboardShot] = []

    def _sync_shot_numbers(self) -> None:
        for idx, shot in enumerate(self.shots, start=1):
            shot.shot_number = idx

    def add_shot(
        self,
        *,
        title: str = "",
        dialogue: str = "",
        action: str = "",
        notes: str = "",
        duration_sec: float = 3.0,
        image_path: str | None = None,
        prompt: str = "",
    ) -> StoryboardShot:
        shot = StoryboardShot(
            shot_number=len(self.shots) + 1,
            title=title or f"Shot {len(self.shots) + 1}",
            dialogue=dialogue,
            action=action,
            notes=notes,
            duration_sec=max(0.25, float(duration_sec)),
            image_path=image_path,
            prompt=prompt,
        )
        self.shots.append(shot)
        self._sync_shot_numbers()
        return shot

    def delete_shot(self, shot_id: str) -> bool:
        before = len(self.shots)
        self.shots = [s for s in self.shots if s.id != shot_id]
        self._sync_shot_numbers()
        return len(self.shots) < before

    def reorder_shot(self, from_index: int, to_index: int) -> bool:
        if not (0 <= from_index < len(self.shots) and 0 <= to_index < len(self.shots)):
            return False
        moved = self.shots.pop(from_index)
        self.shots.insert(to_index, moved)
        self._sync_shot_numbers()
        return True

    def generate_shot_plate(
        self,
        shot: StoryboardShot,
        output_path: str | Path,
        *,
        width: int = 1280,
        height: int = 720,
        model_id: str = "local_plate_renderer",
        external_consent_granted: bool = False,
    ) -> Path:
        """Render a shot visual plate locally (or enforce explicit consent for external models)."""
        spec = next((m for m in list_generative_models() if m.model_id == model_id), None)
        if spec is not None and not spec.is_local and not external_consent_granted:
            raise GenerativeConsentError(
                f"Model '{model_id}' requires external network transmission and explicit user consent."
            )

        out = Path(output_path).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGBA", (width, height), (18, 21, 27, 255))
        draw = ImageDraw.Draw(img)
        # Studio framing guides & accent header bar
        draw.rectangle([0, 0, width, 54], fill=(24, 28, 36, 255))
        draw.rectangle([0, 52, width, 56], fill=(255, 90, 31, 255))
        draw.rectangle([48, 84, width - 48, height - 110], outline=(58, 67, 86, 255), width=2)
        draw.text((24, 18), f"SHOT {shot.shot_number:02d}  |  {shot.title}  ({shot.duration_sec:.1f}s)", fill=(232, 236, 242, 255))
        if shot.action:
            draw.text((68, 120), f"ACTION: {shot.action}", fill=(200, 208, 220, 255))
        if shot.prompt:
            draw.text((68, 160), f"VISUAL: {shot.prompt}", fill=(150, 160, 178, 255))
        if shot.dialogue:
            draw.rectangle([48, height - 92, width - 48, height - 24], fill=(11, 13, 17, 230), outline=(42, 48, 60, 255))
            draw.text((64, height - 70), f'"{shot.dialogue}"', fill=(255, 198, 132, 255))
        img.save(out)
        shot.image_path = str(out)
        return out

    def export_to_timeline(
        self,
        output_dir: str | Path,
        *,
        fps: float = 25.0,
        width: int = 1280,
        height: int = 720,
        project_id: str = "proj_storyboard",
        sequence_id: str = "seq_storyboard",
        expected_revision: int = 0,
    ) -> dict[str, Any]:
        """Generate missing plates & emit Timeline Command API v1 animatic batch for Kdenlive."""
        out_root = Path(output_dir).resolve()
        plates_dir = out_root / "storyboard_plates"
        plates_dir.mkdir(parents=True, exist_ok=True)

        builder = TimelineCommandBuilder(
            project_id=project_id,
            sequence_id=sequence_id,
            expected_revision=expected_revision,
            agent_id="artcraft_storyboard",
            agent_role="storyboard_animatic",
            transaction_label=f"Import Storyboard Animatic ({len(self.shots)} shots)",
            requires_approval=False,
            dry_run=False,
            fps_num=max(1, int(round(fps))),
            fps_den=1,
        )

        cursor_frame = 0
        generated_plates: list[str] = []
        for shot in self.shots:
            if not shot.image_path or not Path(shot.image_path).is_file():
                plate_path = self.generate_shot_plate(
                    shot,
                    plates_dir / f"shot_{shot.shot_number:03d}.png",
                    width=width,
                    height=height,
                )
            else:
                plate_path = Path(shot.image_path).resolve()
            generated_plates.append(str(plate_path))

            dur_frames = max(1, int(round(shot.duration_sec * fps)))
            media_ref = f"sb_media_{shot.shot_number:03d}"
            clip_id = f"sb_clip_{shot.shot_number:03d}"

            builder.add_operation(
                "import_media",
                media_ref=media_ref,
                params={"path": str(plate_path), "name": shot.title, "media_type": "image"},
                confidence=1.0,
                rationale=f"Import storyboard plate for Shot {shot.shot_number:02d}",
            )
            builder.add_operation(
                "place_clip",
                media_ref=media_ref,
                clip_id=clip_id,
                track_id="V1",
                in_frame=0,
                out_frame=dur_frames,
                timeline_start=cursor_frame,
                params={"shot_number": shot.shot_number, "title": shot.title},
                confidence=1.0,
                rationale=f"Place storyboard Shot {shot.shot_number:02d} ({shot.duration_sec:.1f}s)",
            )
            if shot.dialogue.strip():
                builder.add_operation(
                    "add_caption",
                    track_id="V2",
                    in_frame=cursor_frame,
                    out_frame=cursor_frame + dur_frames,
                    timeline_start=cursor_frame,
                    params={"text": shot.dialogue.strip(), "speaker_id": f"Shot {shot.shot_number:02d}"},
                    confidence=0.98,
                    rationale=f"Add dialogue caption for storyboard Shot {shot.shot_number:02d}",
                )
            builder.add_operation(
                "add_marker",
                track_id="V1",
                in_frame=cursor_frame,
                out_frame=cursor_frame,
                timeline_start=cursor_frame,
                params={"label": f"Shot {shot.shot_number:02d}: {shot.title}", "category": "storyboard"},
                confidence=1.0,
                rationale="Mark storyboard shot start on timeline",
            )
            cursor_frame += dur_frames

        batch = builder.build()
        batch_path = out_root / "storyboard_timeline_batch.json"
        batch_path.write_text(json.dumps(batch, indent=2), encoding="utf-8")

        return {
            "title": self.title,
            "shot_count": len(self.shots),
            "total_frames": cursor_frame,
            "plates": generated_plates,
            "command_batch": batch,
            "command_batch_path": str(batch_path),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "shots": [asdict(s) for s in self.shots]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StoryboardEngine":
        eng = cls(title=str(data.get("title", "Bang On Pro Storyboard")))
        for raw in data.get("shots", []):
            eng.add_shot(
                title=str(raw.get("title", "")),
                dialogue=str(raw.get("dialogue", "")),
                action=str(raw.get("action", "")),
                notes=str(raw.get("notes", "")),
                duration_sec=float(raw.get("duration_sec", 3.0)),
                image_path=raw.get("image_path"),
                prompt=str(raw.get("prompt", "")),
            )
        return eng
