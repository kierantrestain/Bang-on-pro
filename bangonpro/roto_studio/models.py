"""Data models and Draft 2020-12 JSON Schema validation for Bang On Pro Rotoscoping & Masking.

SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "roto_mask_v1.schema.json"

VALID_MASK_TOOLS = ("ai_sam", "polygon", "bezier", "freehand", "brush")
VALID_COMBINE_MODES = ("add", "subtract", "intersect", "xor")
VALID_PREVIEW_MODES = ("overlay", "alpha", "before_after", "composite")
VALID_BACKGROUND_MODES = (
    "transparent",
    "solid_color",
    "blur_bg",
    "replace_image",
    "selective_effect",
)


@dataclass
class BrushStroke:
    """Single freehand paint/erase brush stroke over normalized or pixel coordinates."""

    points: list[tuple[float, float]] = field(default_factory=list)
    radius: float = 16.0
    erase: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "points": [[float(x), float(y)] for x, y in self.points],
            "radius": float(max(0.5, self.radius)),
            "erase": bool(self.erase),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BrushStroke:
        pts = [(float(p[0]), float(p[1])) for p in data.get("points", []) if len(p) >= 2]
        return cls(
            points=pts,
            radius=float(data.get("radius", 16.0)),
            erase=bool(data.get("erase", False)),
        )


@dataclass
class MaskShape:
    """Editable rotoscope or AI-segmented mask layer with keyframe & edge controls."""

    id: str
    name: str
    tool: str = "polygon"
    combine_mode: str = "add"
    inverted: bool = False
    feather_px: float = 0.0
    expand_px: float = 0.0
    edge_refine_strength: float = 0.0
    opacity: float = 1.0
    positive_points: list[tuple[float, float]] = field(default_factory=list)
    negative_points: list[tuple[float, float]] = field(default_factory=list)
    bbox: tuple[float, float, float, float] | None = None
    vertices: list[Any] = field(default_factory=list)
    brush_strokes: list[BrushStroke] = field(default_factory=list)
    keyframed_paths: dict[int, list[Any]] = field(default_factory=dict)
    manual_override_frames: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "name": str(self.name),
            "tool": self.tool if self.tool in VALID_MASK_TOOLS else "polygon",
            "combine_mode": self.combine_mode if self.combine_mode in VALID_COMBINE_MODES else "add",
            "inverted": bool(self.inverted),
            "feather_px": float(max(0.0, self.feather_px)),
            "expand_px": float(self.expand_px),
            "edge_refine_strength": float(min(1.0, max(0.0, self.edge_refine_strength))),
            "opacity": float(min(1.0, max(0.0, self.opacity))),
            "positive_points": [[float(x), float(y)] for x, y in self.positive_points],
            "negative_points": [[float(x), float(y)] for x, y in self.negative_points],
            "bbox": [float(v) for v in self.bbox] if self.bbox is not None else None,
            "vertices": list(self.vertices),
            "brush_strokes": [s.to_dict() for s in self.brush_strokes],
            "keyframed_paths": {str(k): list(v) for k, v in sorted(self.keyframed_paths.items())},
            "manual_override_frames": sorted({int(f) for f in self.manual_override_frames if int(f) >= 0}),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MaskShape:
        raw_bbox = data.get("bbox")
        bbox = (
            (float(raw_bbox[0]), float(raw_bbox[1]), float(raw_bbox[2]), float(raw_bbox[3]))
            if isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) == 4
            else None
        )
        kf_paths: dict[int, list[Any]] = {}
        for k, v in (data.get("keyframed_paths") or {}).items():
            kf_paths[int(k)] = list(v)
        return cls(
            id=str(data.get("id", "mask_1")),
            name=str(data.get("name", "Mask 1")),
            tool=str(data.get("tool", "polygon")),
            combine_mode=str(data.get("combine_mode", "add")),
            inverted=bool(data.get("inverted", False)),
            feather_px=float(data.get("feather_px", 0.0)),
            expand_px=float(data.get("expand_px", 0.0)),
            edge_refine_strength=float(data.get("edge_refine_strength", 0.0)),
            opacity=float(data.get("opacity", 1.0)),
            positive_points=[(float(p[0]), float(p[1])) for p in data.get("positive_points", []) if len(p) >= 2],
            negative_points=[(float(p[0]), float(p[1])) for p in data.get("negative_points", []) if len(p) >= 2],
            bbox=bbox,
            vertices=list(data.get("vertices", [])),
            brush_strokes=[BrushStroke.from_dict(s) for s in data.get("brush_strokes", [])],
            keyframed_paths=kf_paths,
            manual_override_frames=[int(f) for f in data.get("manual_override_frames", [])],
        )


@dataclass
class RotoSession:
    """Complete rotoscoping & masking session attached to a video clip."""

    clip_path: str
    fps: float = 25.0
    width: int = 1920
    height: int = 1080
    masks: list[MaskShape] = field(default_factory=list)
    preview_mode: str = "overlay"
    background_mode: str = "transparent"
    selective_effect_service: str | None = None
    selective_effect_params: dict[str, Any] = field(default_factory=dict)
    background_color: tuple[int, int, int] = (18, 21, 27)
    replace_image_path: str | None = None
    blur_strength: float = 12.0
    session_id: str = "roto_session_1"
    schema_version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "clip_path": self.clip_path,
            "fps": float(max(1.0, self.fps)),
            "width": int(max(1, self.width)),
            "height": int(max(1, self.height)),
            "preview_mode": self.preview_mode if self.preview_mode in VALID_PREVIEW_MODES else "overlay",
            "background_mode": self.background_mode if self.background_mode in VALID_BACKGROUND_MODES else "transparent",
            "selective_effect_service": self.selective_effect_service,
            "selective_effect_params": dict(self.selective_effect_params),
            "background_color": [int(c) for c in self.background_color[:3]],
            "replace_image_path": self.replace_image_path,
            "blur_strength": float(max(0.0, self.blur_strength)),
            "masks": [m.to_dict() for m in self.masks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RotoSession:
        bg = data.get("background_color") or [18, 21, 27]
        return cls(
            clip_path=str(data.get("clip_path", "")),
            fps=float(data.get("fps", 25.0)),
            width=int(data.get("width", 1920)),
            height=int(data.get("height", 1080)),
            masks=[MaskShape.from_dict(m) for m in data.get("masks", [])],
            preview_mode=str(data.get("preview_mode", "overlay")),
            background_mode=str(data.get("background_mode", "transparent")),
            selective_effect_service=data.get("selective_effect_service"),
            selective_effect_params=dict(data.get("selective_effect_params") or {}),
            background_color=(int(bg[0]), int(bg[1]), int(bg[2])),
            replace_image_path=data.get("replace_image_path"),
            blur_strength=float(data.get("blur_strength", 12.0)),
            session_id=str(data.get("session_id", "roto_session_1")),
            schema_version=str(data.get("schema_version", "1.0")),
        )


def validate_roto_session(payload: dict[str, Any] | RotoSession) -> dict[str, Any]:
    """Validate a RotoSession dict against ``roto_mask_v1.schema.json``."""
    data = payload.to_dict() if isinstance(payload, RotoSession) else dict(payload)
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(instance=data, schema=schema)
    return data
