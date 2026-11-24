"""Command-Line Interface for Bang On Pro Rotoscoping, Masking & ArtCraft 2D Studio.

SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .artcraft_bridge import (
    ARTCRAFT_ATTRIBUTION_NOTICE,
    Canvas2DComposer,
    StoryboardEngine,
    get_artcraft_provenance,
    list_generative_models,
)
from .mask_engine import (
    combine_masks,
    evaluate_segmentation_backends,
    export_mask_to_mlt,
    propagate_masks,
    render_preview,
    segment_frame_detailed,
)
from .models import RotoSession, validate_roto_session


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bangonpro-roto",
        description="Bang On Pro Stage Two Rotoscoping, Masking & ArtCraft 2D Composition CLI",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_back = sub.add_parser("backends", help="Evaluate available local SAM/ONNX/OpenCV segmentation backends")
    p_back.add_argument("--model-dir", default=None, help="Optional override for BANGONPRO_MODEL_DIR")

    p_seg = sub.add_parser("segment", help="Segment an image frame from positive/negative points or session JSON")
    p_seg.add_argument("--image", required=True)
    p_seg.add_argument("--session", required=True)
    p_seg.add_argument("--output-dir", required=True)
    p_seg.add_argument("--model-dir", default=None)

    p_prop = sub.add_parser("propagate", help="Propagate masks across a directory of frames")
    p_prop.add_argument("--frames-dir", required=True)
    p_prop.add_argument("--session", required=True)
    p_prop.add_argument("--start-frame", type=int, default=0)
    p_prop.add_argument("--direction", choices=("forward", "backward", "both"), default="both")
    p_prop.add_argument("--output-dir", required=True)

    p_comp = sub.add_parser("compose-2d", help="Render an ArtCraft-adapted 2D layer composition JSON to RGBA PNG / MP4")
    p_comp.add_argument("--canvas-json", required=True)
    p_comp.add_argument("--output-png", required=True)
    p_comp.add_argument("--output-mp4", default=None)
    p_comp.add_argument("--duration-sec", type=float, default=3.0)
    p_comp.add_argument("--fps", type=float, default=25.0)

    p_sb = sub.add_parser("storyboard-export", help="Export a Storyboard JSON into plates + Timeline Command batch")
    p_sb.add_argument("--storyboard-json", required=True)
    p_sb.add_argument("--output-dir", required=True)
    p_sb.add_argument("--fps", type=float, default=25.0)

    args = parser.parse_args(argv)

    if args.cmd == "backends":
        payload = {
            "segmentation_backends": [asdict(b) for b in evaluate_segmentation_backends(model_dir=args.model_dir)],
            "generative_models": [asdict(m) for m in list_generative_models()],
            "artcraft_notice": ARTCRAFT_ATTRIBUTION_NOTICE,
            "artcraft_provenance": get_artcraft_provenance(),
        }
        print(json.dumps(payload, indent=2))
        return 0

    if args.cmd == "segment":
        raw = json.loads(Path(args.session).read_text(encoding="utf-8"))
        validate_roto_session(raw)
        session = RotoSession.from_dict(raw)
        frame_rgb = np.asarray(Image.open(args.image).convert("RGB"), dtype=np.uint8)
        segmentation_meta: list[dict[str, Any]] = []
        for shape in session.masks:
            if shape.tool == "ai_sam" and (shape.positive_points or shape.bbox is not None):
                seg_res = segment_frame_detailed(
                    frame_rgb,
                    positive_points=shape.positive_points,
                    negative_points=shape.negative_points,
                    bbox=shape.bbox,
                    model_dir=args.model_dir,
                )
                segmentation_meta.append({"mask_id": shape.id, **seg_res.to_metadata_dict()})
        alpha = combine_masks(
            session.masks,
            frame_rgb.shape[1],
            frame_rgb.shape[0],
            frame_idx=0,
            frame_rgb=frame_rgb,
            model_dir=args.model_dir,
        )
        out_dir = Path(args.output_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        preview = render_preview(
            frame_rgb,
            alpha,
            preview_mode=session.preview_mode,
            background_mode=session.background_mode,
            background_color=session.background_color,
            blur_strength=session.blur_strength,
            selective_effect=session.selective_effect_service,
        )
        preview_path = out_dir / "preview.png"
        Image.fromarray(preview, mode="RGB").save(preview_path)
        exported = export_mask_to_mlt(session, [alpha], out_dir)
        exported["preview_path"] = str(preview_path)
        exported["segmentation_reports"] = segmentation_meta
        print(json.dumps(exported, indent=2))
        return 0

    if args.cmd == "propagate":
        raw = json.loads(Path(args.session).read_text(encoding="utf-8"))
        validate_roto_session(raw)
        session = RotoSession.from_dict(raw)
        frame_files = sorted(Path(args.frames_dir).glob("*.png"))
        frames_rgb = [np.asarray(Image.open(p).convert("RGB"), dtype=np.uint8) for p in frame_files]
        tracked = propagate_masks(
            frames_rgb,
            session.masks,
            start_frame=args.start_frame,
            direction=args.direction,
        )
        exported = export_mask_to_mlt(session, [r.alpha_mask for r in tracked], args.output_dir)
        exported["occluded_frames"] = [r.frame_index for r in tracked if r.occluded]
        print(json.dumps(exported, indent=2))
        return 0

    if args.cmd == "compose-2d":
        data = json.loads(Path(args.canvas_json).read_text(encoding="utf-8"))
        comp = Canvas2DComposer.from_dict(data)
        out_png = comp.render_to_png(args.output_png)
        out_payload: dict[str, Any] = {"output_png": str(out_png), "layer_count": len(comp.layers)}
        if args.output_mp4:
            out_mp4 = comp.render_to_mp4(args.output_mp4, duration_sec=args.duration_sec, fps=args.fps)
            out_payload["output_mp4"] = str(out_mp4)
        print(json.dumps(out_payload, indent=2))
        return 0

    if args.cmd == "storyboard-export":
        data = json.loads(Path(args.storyboard_json).read_text(encoding="utf-8"))
        engine = StoryboardEngine.from_dict(data)
        result = engine.export_to_timeline(args.output_dir, fps=args.fps)
        print(json.dumps(result, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
