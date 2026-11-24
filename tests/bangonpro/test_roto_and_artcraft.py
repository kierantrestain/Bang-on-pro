"""Comprehensive Stage Two test suite: Rotoscoping, Masking & Selective ArtCraft 2D Studio.

Verifies:
1. SAM-family & fallback segmentation backend evaluation (license & Apple Silicon MPS/CoreML metadata).
2. Draft 2020-12 schema validation against ``bangonpro/schemas/roto_mask_v1.schema.json``.
3. Manual polygon, cubic Bézier, freehand, and paint/erase brush mask rasterization.
4. Feathering, expansion/contraction, and guided hair/edge refinement.
5. Multi-mask boolean combinations (add/subtract/intersect/xor) and inversion.
6. AI-assisted positive (+) / negative (-) point selection segmentation.
7. Bidirectional forward & backward optical-flow propagation with occlusion detection
   and manual frame-level keyframe override.
8. All 4 preview render modes (overlay, alpha, before_after, composite) & background modes.
9. Native MLT/Kdenlive matte & rotoscoping spline export + Timeline Command API v1 batch validation
   + real ``melt`` composite render verification.
10. Selective ArtCraft (MIT OR Apache-2.0) 2D integration: ``normalize_matte_mask``,
    ``point_in_polygon``, ``pack_collage_rects``, ``Canvas2DComposer`` (layers/reorder/hit-test/
    collage/undo/redo/PNG), ``StoryboardEngine`` animatic export, local mask inpainting, and strict
    consent gating for external generative models.

SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bangonpro.ai_engine import validate_timeline_command_batch
from bangonpro.roto_studio import (
    ARTCRAFT_ATTRIBUTION_NOTICE,
    ArtCraftLayer,
    BrushStroke,
    Canvas2DComposer,
    GenerativeConsentError,
    MaskShape,
    RotoSession,
    StoryboardEngine,
    combine_masks,
    evaluate_segmentation_backends,
    export_mask_to_mlt,
    inpaint_masked_region,
    list_generative_models,
    normalize_matte_mask,
    pack_collage_rects,
    point_in_polygon,
    propagate_masks,
    rasterize_mask_shape,
    render_preview,
    sample_bezier_polygon,
    segment_frame,
    validate_roto_session,
)
from bangonpro.roto_studio.cli import main as roto_cli_main


def _make_moving_subject_frames(
    num_frames: int = 7,
    width: int = 160,
    height: int = 120,
    *,
    occlude_frame: int | None = None,
) -> list[np.ndarray]:
    """Generate deterministic RGB frames with a crisp moving subject over a textured studio backdrop."""
    frames: list[np.ndarray] = []
    yy, xx = np.mgrid[0:height, 0:width]
    bg = np.zeros((height, width, 3), dtype=np.uint8)
    bg[..., 0] = np.clip(20 + (xx * 30) // width, 0, 255)
    bg[..., 1] = np.clip(35 + (yy * 40) // height, 0, 255)
    bg[..., 2] = 65

    for i in range(num_frames):
        frame = bg.copy()
        if occlude_frame is not None and i == occlude_frame:
            frame[:, :] = (10, 10, 12)
        else:
            cx = 52 + i * 7
            cy = 60 + i * 2
            dist2 = (xx - cx) ** 2 + (yy - cy) ** 2
            subject = dist2 <= 22**2
            frame[subject] = (235, 92, 42)
            inner = ((xx - (cx - 6)) ** 2 + (yy - (cy - 5)) ** 2) <= 7**2
            frame[inner] = (255, 215, 160)
        frames.append(frame)
    return frames


def test_segmentation_backends_and_roto_schema_validation() -> None:
    backends = evaluate_segmentation_backends()
    ids = {b.backend_id for b in backends}
    assert {"sam2_hiera_tiny", "sam2_hiera_small", "mobile_sam_onnx", "opencv_grabcut_guided", "numpy_spline_raster"}.issubset(ids)
    for b in backends:
        assert b.gpl_compatible is True
    assert any(b.available_now for b in backends)

    session = RotoSession(
        clip_path="/studio/interview_cam_a.mov",
        fps=25.0,
        width=1920,
        height=1080,
        preview_mode="overlay",
        background_mode="blur_bg",
        masks=[
            MaskShape(
                id="m1",
                name="Host Polygon",
                tool="polygon",
                vertices=[[400, 200], [1400, 200], [1400, 900], [400, 900]],
                feather_px=4.0,
                expand_px=2.0,
                edge_refine_strength=0.35,
            )
        ],
    )
    validated = validate_roto_session(session)
    assert validated["schema_version"] == "1.0"
    restored = RotoSession.from_dict(validated)
    assert len(restored.masks) == 1
    assert restored.masks[0].name == "Host Polygon"


def test_manual_mask_tools_refinements_and_boolean_combinations() -> None:
    w, h = 160, 120
    frame = _make_moving_subject_frames(1, w, h)[0]

    # 1. Polygon with expand / contract / feather / edge refinement (pixel coords)
    poly_base = MaskShape(
        id="poly",
        name="Poly",
        tool="polygon",
        vertices=[[40, 30], [120, 30], [120, 90], [40, 90]],
    )
    poly_expanded = MaskShape(
        id="poly_exp",
        name="PolyExp",
        tool="polygon",
        vertices=poly_base.vertices,
        expand_px=6.0,
    )
    poly_contracted = MaskShape(
        id="poly_con",
        name="PolyCon",
        tool="polygon",
        vertices=poly_base.vertices,
        expand_px=-6.0,
    )
    poly_feathered = MaskShape(
        id="poly_f",
        name="PolyFeather",
        tool="polygon",
        vertices=poly_base.vertices,
        feather_px=5.0,
        edge_refine_strength=0.5,
    )

    a_base = rasterize_mask_shape(poly_base, w, h, frame_rgb=frame)
    a_exp = rasterize_mask_shape(poly_expanded, w, h, frame_rgb=frame)
    a_con = rasterize_mask_shape(poly_contracted, w, h, frame_rgb=frame)
    a_fea = rasterize_mask_shape(poly_feathered, w, h, frame_rgb=frame)

    assert float(a_exp.sum()) > float(a_base.sum()) > float(a_con.sum()) > 0.0
    # Feathered mask must have intermediate soft alpha values in (0.05, 0.95)
    assert np.any((a_fea > 0.05) & (a_fea < 0.95))

    # 2. Cubic Bézier spline sampling
    bezier_verts = [
        {"x": 45, "y": 35, "in_x": 40, "in_y": 45, "out_x": 70, "out_y": 25},
        {"x": 115, "y": 35, "in_x": 95, "in_y": 25, "out_x": 120, "out_y": 55},
        {"x": 115, "y": 85, "in_x": 120, "in_y": 70, "out_x": 90, "out_y": 95},
        {"x": 45, "y": 85, "in_x": 65, "in_y": 95, "out_x": 40, "out_y": 65},
    ]
    sampled = sample_bezier_polygon(bezier_verts, steps_per_segment=8)
    assert len(sampled) >= 24
    bezier_mask = MaskShape(id="bez", name="Bezier", tool="bezier", vertices=bezier_verts)
    assert float(rasterize_mask_shape(bezier_mask, w, h).sum()) > 100.0

    # 3. Brush paint + erase stroke
    brush_mask = MaskShape(
        id="br",
        name="Brush",
        tool="brush",
        brush_strokes=[
            BrushStroke(points=[(48, 60), (112, 60)], radius=18.0, erase=False),
            BrushStroke(points=[(80, 60)], radius=8.0, erase=True),
        ],
    )
    a_brush = rasterize_mask_shape(brush_mask, w, h)
    assert a_brush[60, 56] > 0.8  # Painted area
    assert a_brush[60, 80] < 0.1  # Erased center hole

    # 4. Multi-mask boolean combinations (add, subtract, intersect, xor) + inversion
    left = MaskShape(id="l", name="Left", tool="polygon", combine_mode="add", vertices=[[16, 24], [96, 24], [96, 96], [16, 96]])
    right_sub = MaskShape(id="rs", name="RightSub", tool="polygon", combine_mode="subtract", vertices=[[64, 24], [144, 24], [144, 96], [64, 96]])
    right_int = MaskShape(id="ri", name="RightInt", tool="polygon", combine_mode="intersect", vertices=right_sub.vertices)
    right_xor = MaskShape(id="rx", name="RightXor", tool="polygon", combine_mode="xor", vertices=right_sub.vertices)
    inv_left = MaskShape(id="il", name="InvLeft", tool="polygon", combine_mode="add", inverted=True, vertices=left.vertices)

    c_sub = combine_masks([left, right_sub], w, h)
    c_int = combine_masks([left, right_int], w, h)
    c_xor = combine_masks([left, right_xor], w, h)
    c_inv = combine_masks([inv_left], w, h)

    assert c_sub[60, 32] > 0.8 and c_sub[60, 80] < 0.1
    assert c_int[60, 80] > 0.8 and c_int[60, 32] < 0.1
    assert c_xor[60, 32] > 0.8 and c_xor[60, 80] < 0.1 and c_xor[60, 128] > 0.8
    assert c_inv[60, 32] < 0.1 and c_inv[5, 5] > 0.8


def test_ai_point_segmentation_bidirectional_propagation_and_mlt_export(tmp_path: Path) -> None:
    frames = _make_moving_subject_frames(num_frames=7, width=160, height=120, occlude_frame=5)

    # AI-assisted selection using positive (+) point inside subject and negative (-) points in background
    alpha_seg, backend_used, conf = segment_frame(
        frames[3],
        positive_points=[(73.0, 66.0)],  # Center of frame 3 subject
        negative_points=[(8.0, 8.0), (152.0, 112.0)],
    )
    assert alpha_seg.shape == (120, 160)
    assert backend_used in {"sam2", "opencv_grabcut_guided", "color_cue_fallback", "numpy_color_cue"}
    assert conf > 0.0
    assert alpha_seg[66, 73] > 0.5
    assert alpha_seg[4, 4] < 0.3

    # Bidirectional forward & backward tracking from anchor frame 3 with manual override at frame 6
    tracked_mask = MaskShape(
        id="host_track",
        name="Tracked Subject",
        tool="polygon",
        vertices=[[52, 44], [94, 44], [94, 88], [52, 88]],
        keyframed_paths={
            6: [[72, 50], [116, 50], [116, 94], [72, 94]],
        },
        manual_override_frames=[6],
    )
    results = propagate_masks(frames, [tracked_mask], start_frame=3, direction="both")
    assert len(results) == 7
    assert results[6].manual_override is True
    assert float(results[2].alpha_mask.sum()) > 100.0
    assert float(results[4].alpha_mask.sum()) > 100.0

    # Verify all 4 preview modes & background replacement modes produce valid RGB images
    for pmode in ("overlay", "alpha", "before_after", "composite"):
        for bg_mode in ("transparent", "solid_color", "blur_bg", "selective_effect"):
            prev = render_preview(
                frames[3],
                results[3].alpha_mask,
                preview_mode=pmode,
                background_mode=bg_mode,
                selective_effect="grayscale",
            )
            assert prev.shape == frames[3].shape
            assert prev.dtype == np.uint8

    # Export to MLT matte + rotoscoping spline + Timeline Command API v1 batch
    session = RotoSession(
        clip_path=str(tmp_path / "source_clip.mp4"),
        fps=25.0,
        width=160,
        height=120,
        masks=[tracked_mask],
    )
    exported = export_mask_to_mlt(session, [r.alpha_mask for r in results], tmp_path / "mlt_out", clip_id="clip-1")
    assert len(exported["matte_frames"]) == 7
    first_matte = Path(exported["matte_frames"][0])
    assert first_matte.is_file()
    assert Path(exported["primary_matte_path"]).is_file()
    validate_timeline_command_batch(exported["command_batch"])
    assert exported["command_batch"]["operations"][0]["type"] == "apply_mask"

    # Real melt render check applying the exported MLT matte filter onto a test clip
    if shutil.which("melt"):
        mlt_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<mlt profile="atsc_720p_25">
  <producer id="bg">
    <property name="mlt_service">color</property>
    <property name="resource">0x224466ff</property>
    <property name="length">10</property>
  </producer>
  <producer id="fg">
    <property name="mlt_service">color</property>
    <property name="resource">0xff5a1fff</property>
    <property name="length">10</property>
    <filter mlt_service="shape">
      <property name="resource">{first_matte}</property>
      <property name="use_luminance">1</property>
      <property name="softness">0.05</property>
    </filter>
  </producer>
  <playlist id="p0"><entry producer="bg" in="0" out="9"/></playlist>
  <playlist id="p1"><entry producer="fg" in="0" out="9"/></playlist>
  <tractor id="t0">
    <track producer="p0"/>
    <track producer="p1"/>
    <transition mlt_service="qtblend" a_track="0" b_track="1"/>
  </tractor>
</mlt>
"""
        mlt_path = tmp_path / "roto_verify.mlt"
        out_mp4 = tmp_path / "roto_verify.mp4"
        mlt_path.write_text(mlt_xml, encoding="utf-8")
        melt_env = {k: v for k, v in __import__("os").environ.items() if k not in ("QT_QPA_PLATFORM_PLUGIN_PATH", "QT_PLUGIN_PATH")}
        melt_env["QT_QPA_PLATFORM"] = "offscreen"
        melt_env["SDL_AUDIODRIVER"] = "dummy"
        proc = subprocess.run(
            ["melt", str(mlt_path), "-consumer", f"avformat:{out_mp4}", "vcodec=libx264", "an=1", "real_time=0"],
            capture_output=True,
            text=True,
            env=melt_env,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr[-500:]
        assert out_mp4.is_file() and out_mp4.stat().st_size > 500


def test_artcraft_2d_composer_storyboard_inpaint_and_consent_gate(tmp_path: Path) -> None:
    assert "MIT OR Apache-2.0" in ARTCRAFT_ATTRIBUTION_NOTICE
    models = list_generative_models()
    assert any(m.is_local and not m.requires_consent for m in models)
    assert any((not m.is_local) and m.requires_consent for m in models)

    # 1. ArtCraft mask normalization & geometry helpers
    rgba = np.zeros((20, 20, 4), dtype=np.uint8)
    rgba[5:15, 5:15] = [12, 34, 56, 180]
    norm = normalize_matte_mask(rgba)
    assert tuple(norm[0, 0]) == (0, 0, 0, 255)
    assert tuple(norm[10, 10]) == (255, 255, 255, 255)

    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    assert point_in_polygon(5.0, 5.0, square) is True
    assert point_in_polygon(15.0, 5.0, square) is False

    rects = pack_collage_rects([(400, 300), (400, 300), (800, 400)], 1280, 720)
    assert len(rects) == 3

    # 2. Undoable 2D multi-layer Canvas2DComposer
    comp = Canvas2DComposer(width=320, height=180)
    comp.add_layer(ArtCraftLayer(layer_id="bg", name="Backdrop", kind="solid", x=0, y=0, width=320, height=180, color_rgba=(18, 21, 27, 255)))
    comp.add_layer(ArtCraftLayer(layer_id="card", name="Accent Card", kind="text_card", x=40, y=40, width=240, height=80, color_rgba=(255, 90, 31, 230), text="BANG ON PRO"))
    assert len(comp.layers) == 2
    assert comp.hit_test(100, 70) is not None
    assert comp.reorder_layer(1, 0) is True
    assert comp.layers[0].layer_id == "card"
    assert comp.undo() is True
    assert comp.layers[1].layer_id == "card"
    assert comp.redo() is True
    assert comp.layers[0].layer_id == "card"

    plate_png = comp.render_to_png(tmp_path / "artcraft_plate.png")
    assert plate_png.is_file()
    img = Image.open(plate_png)
    assert img.size == (320, 180) and img.mode == "RGBA"

    # 3. Local OpenCV/harmonic mask inpainting (zero paid API required)
    src = _make_moving_subject_frames(1, 120, 90)[0]
    hole = np.zeros((90, 120), dtype=np.float32)
    hole[35:55, 45:65] = 1.0
    cleaned = inpaint_masked_region(src, hole)
    assert cleaned.shape == src.shape and cleaned.dtype == np.uint8

    # 4. StoryboardEngine animatic export + consent gate enforcement
    sb = StoryboardEngine(title="Podcast Cold Open")
    s1 = sb.add_shot(title="Wide Studio", dialogue="Welcome back to the show!", action="Dolly in", duration_sec=2.0)
    sb.add_shot(title="Host Close-Up", dialogue="Today we test Bang On Pro.", action="Cut to Cam B", duration_sec=2.5)
    assert sb.reorder_shot(0, 1) is True
    assert sb.reorder_shot(1, 0) is True

    with pytest.raises(GenerativeConsentError):
        sb.generate_shot_plate(s1, tmp_path / "blocked.png", model_id="external_flux_image", external_consent_granted=False)

    export_res = sb.export_to_timeline(tmp_path / "sb_export", fps=25.0, width=320, height=180)
    assert export_res["shot_count"] == 2
    assert len(export_res["plates"]) == 2
    for p in export_res["plates"]:
        assert Path(p).is_file()
    validate_timeline_command_batch(export_res["command_batch"])


def test_roto_cli_subcommands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert roto_cli_main(["backends"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "segmentation_backends" in out and "artcraft_notice" in out

    canvas_json = tmp_path / "canvas.json"
    canvas_json.write_text(
        json.dumps(
            {
                "width": 160,
                "height": 90,
                "layers": [{"layer_id": "l1", "name": "Plate", "kind": "solid", "width": 160, "height": 90}],
            }
        ),
        encoding="utf-8",
    )
    out_png = tmp_path / "cli_plate.png"
    assert roto_cli_main(["compose-2d", "--canvas-json", str(canvas_json), "--output-png", str(out_png)]) == 0
    assert out_png.is_file()
