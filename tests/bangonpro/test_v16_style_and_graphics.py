"""V1.6 tests: highpass native param, reference-video style learning, intelligent motion graphics.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
import pytest

from bangonpro.ai_engine.agents.audio_graphics_qc import build_audio_restoration_chain
from bangonpro.ai_engine.department.agents import JobOptions
from bangonpro.ai_engine.department.cache import AnalysisCache
from bangonpro.ai_engine.department.cli import main as dept_cli
from bangonpro.ai_engine.department.jobs import ProductionJob
from bangonpro.ai_engine.department.motion_graphics import (
    MOTION_TEMPLATES,
    build_qtblend_animation_rect,
    choose_non_colliding_position,
    parse_motion_graphics_from_brief,
    render_motion_graphic_xml,
)
from bangonpro.ai_engine.department.reference_style import (
    analyse_reference_video,
    delete_reference_style,
    learn_style_from_references,
    list_reference_styles,
)
from bangonpro.ai_engine.department.registry import AGENT_ORDER
from bangonpro.show_profiles.loader import validate_show_profile

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from talking_head import generate_talking_head


def _make_reference_video(path: Path, *, style: str) -> Path:
    """Create a short synthetic reference video with distinct cuts, colour, and loudness."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if style == "fast_bright_warm":
        # 5 distinct bright warm-tinted colour scenes cut every 1.2s + louder speech-like tone + lower-left accent bar
        colors = ["#e08030", "#d8a040", "#cc6028", "#e8b050", "#c85020"]
        seg_dur = 1.2
        tone_amp = 3.5
        filters = []
        for i, col in enumerate(colors):
            filters.append(
                f"color=c={col}:s=640x360:d={seg_dur}:r=25,"
                f"drawbox=x=24:y=270:w=220:h=50:color=#00C8FF@0.9:t=fill,"
                f"drawbox=x=120:y=280:w=400:h=40:color=white@0.85:t=fill[v{i}]"
            )
        concat_in = "".join(f"[v{i}]" for i in range(len(colors)))
        vfilter = ";".join(filters) + f";{concat_in}concat=n={len(colors)}:v=1:a=0[vout]"
        dur = seg_dur * len(colors)
        af = f"sine=f=320:d={dur}:r=48000,volume={tone_amp}"
    else:
        # 2 slower darker cool-tinted scenes + quieter audio with longer pauses
        colors = ["#223850", "#1e2e44"]
        seg_dur = 3.5
        tone_amp = 0.85
        filters = [f"color=c={col}:s=640x360:d={seg_dur}:r=25[v{i}]" for i, col in enumerate(colors)]
        concat_in = "".join(f"[v{i}]" for i in range(len(colors)))
        vfilter = ";".join(filters) + f";{concat_in}concat=n={len(colors)}:v=1:a=0[vout]"
        dur = seg_dur * len(colors)
        af = f"sine=f=220:d={dur}:r=48000,volume='if(lt(mod(t,2.5),1.2),{tone_amp},0.0001)'"

    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        af,
        "-filter_complex",
        vfilter,
        "-map",
        "[vout]",
        "-map",
        "0:a:0",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    assert res.returncode == 0, f"ffmpeg ref generation failed: {res.stderr}"
    return path


def _render_frame_rgb(kdenlive_path: Path, frame: int, out_png: Path) -> np.ndarray:
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "SDL_AUDIODRIVER": "dummy"}
    cmd = [
        "melt",
        str(kdenlive_path),
        f"in={frame}",
        f"out={frame}",
        "-consumer",
        f"avformat:{out_png}",
        "vcodec=png",
        "an=1",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=env, check=False)
    assert res.returncode == 0 and out_png.is_file(), f"melt frame render failed: {res.stderr}"
    from PIL import Image

    return np.asarray(Image.open(out_png).convert("RGB"), dtype=np.float64)


def test_highpass_uses_native_kdenlive_av_f_parameter() -> None:
    filters, _chain = build_audio_restoration_chain({"highpass_hz": 90.0})
    hp = next(f for f in filters if f.mlt_service == "avfilter.highpass")
    assert "av.f" in hp.mlt_params
    assert "av.frequency" not in hp.mlt_params
    assert hp.mlt_params["av.f"] == 90.0


def test_reference_style_analysis_cache_cli_and_measurable_edit_impact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profiles_dir = tmp_path / "profiles"
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("BANGONPRO_PROFILES_DIR", str(profiles_dir))

    ref_fast = _make_reference_video(tmp_path / "ref_fast.mp4", style="fast_bright_warm")
    ref_slow = _make_reference_video(tmp_path / "ref_slow.mp4", style="slow_dark_cool")

    cache = AnalysisCache(cache_dir)
    obs1 = analyse_reference_video(ref_fast, cache=cache)
    assert obs1["cache_hit"] is False
    for key in ("pacing", "transitions", "opening", "camera_framing", "captions", "graphics", "colour", "audio", "broll"):
        assert key in obs1
    # Content-hash cache hit on second analysis
    obs2 = analyse_reference_video(ref_fast, cache=cache)
    assert obs2["cache_hit"] is True
    assert obs2["content_hash"] == obs1["content_hash"]

    # Learn & save two distinct named styles (no fake reasoning)
    fast_res = learn_style_from_references(
        [ref_fast],
        name="Fast Bright Style",
        save=True,
        output_dir=profiles_dir,
        cache=cache,
        allow_reasoning=False,
    )
    slow_res = learn_style_from_references(
        [ref_slow],
        name="Slow Moody Style",
        save=True,
        output_dir=profiles_dir,
        cache=cache,
        allow_reasoning=False,
    )
    validate_show_profile(fast_res["profile"])
    validate_show_profile(slow_res["profile"])
    assert fast_res["reasoning"]["used"] is False
    assert fast_res["profile"]["colour_rules"]["target_luma"] > slow_res["profile"]["colour_rules"]["target_luma"]
    assert fast_res["profile"]["audio_rules"]["target_lufs"] > slow_res["profile"]["audio_rules"]["target_lufs"]
    assert fast_res["profile"]["editing_rules"]["min_silence_sec"] < slow_res["profile"]["editing_rules"]["min_silence_sec"]

    # Verify list & CLI round-trip
    listed = list_reference_styles(profiles_dir)
    assert {s["profile_id"] for s in listed} >= {fast_res["profile_id"], slow_res["profile_id"]}
    assert dept_cli(["style", "list", "--out", str(profiles_dir)]) == 0
    assert dept_cli(["style", "show", fast_res["profile_id"], "--out", str(profiles_dir)]) == 0

    # Validate against DIFFERENT source footage: same talking-head clip produces measurably different edits & renders!
    footage = generate_talking_head(tmp_path, name="source_talk.mp4")
    job_fast = ProductionJob.create(
        brief="Tighten the pauses, match colour and clean up audio, lower third: Jane Doe, Host",
        inputs=[f"cam_main={footage}"],
        profile_ref=fast_res["profile_id"],
        mode="full_production_draft",
        job_root=tmp_path / "jobs",
        options=JobOptions(mode="full_production_draft", episode_title="Fast Cut"),
    )
    job_fast.run()
    job_slow = ProductionJob.create(
        brief="Tighten the pauses, match colour and clean up audio, lower third: Jane Doe, Host",
        inputs=[f"cam_main={footage}"],
        profile_ref=slow_res["profile_id"],
        mode="full_production_draft",
        job_root=tmp_path / "jobs",
        options=JobOptions(mode="full_production_draft", episode_title="Slow Cut"),
    )
    job_slow.run()

    assert job_fast.doc["status"] == "draft_ready"
    assert job_slow.doc["status"] == "draft_ready"

    k_fast = Path(job_fast.doc["artifacts"]["kdenlive_project"])
    k_slow = Path(job_slow.doc["artifacts"]["kdenlive_project"])
    assert k_fast.is_file() and k_slow.is_file()

    # Verify audio target loudness & colour grade differ measurably between styles
    cmds_fast = json.loads(Path(job_fast.doc["artifacts"]["timeline_commands_json"]).read_text(encoding="utf-8"))
    cmds_slow = json.loads(Path(job_slow.doc["artifacts"]["timeline_commands_json"]).read_text(encoding="utf-8"))
    ops_fast = [op for b in cmds_fast["batches"] for op in b["operations"]]
    ops_slow = [op for b in cmds_slow["batches"] for op in b["operations"]]

    lufs_fast = [op["params"]["mlt_params"]["target_loudness"] for op in ops_fast if op.get("params", {}).get("mlt_service") == "dynamic_loudness"]
    lufs_slow = [op["params"]["mlt_params"]["target_loudness"] for op in ops_slow if op.get("params", {}).get("mlt_service") == "dynamic_loudness"]
    assert lufs_fast and lufs_slow and lufs_fast[0] != lufs_slow[0]

    # Render frame 100 from each .kdenlive and prove visible pixel difference from the learned style
    rgb_fast = _render_frame_rgb(k_fast, 100, tmp_path / "fast_f100.png")
    rgb_slow = _render_frame_rgb(k_slow, 100, tmp_path / "slow_f100.png")
    mean_abs_diff = float(np.mean(np.abs(rgb_fast - rgb_slow)))
    assert mean_abs_diff > 5.0, f"Expected measurable rendered difference between styles, got {mean_abs_diff:.2f}"

    # Verify delete removes saved style cleanly
    assert delete_reference_style(slow_res["profile_id"], custom_dir=profiles_dir) is True
    assert slow_res["profile_id"] not in {s["profile_id"] for s in list_reference_styles(profiles_dir)}


def test_intelligent_motion_graphics_all_types_animated_and_rendered(tmp_path: Path) -> None:
    # Permanent agent count remains strictly 14
    assert len(AGENT_ORDER) == 14

    # Unit-check all supported motion graphic families: XML, safe areas, WCAG contrast, keyframes
    sample_payloads = {
        "scoreboard": {"title": "MATCH SCORE", "entries": [("RED", "3"), ("BLUE", "2")]},
        "stat_card": {"metric": "85%", "label": "FASTER EDITING"},
        "comparison": {"title": "WORKFLOW", "left_label": "MANUAL", "left_value": "4h", "right_label": "BANG ON PRO", "right_value": "15m"},
        "chart": {"title": "GROWTH", "bars": [("Q1", 40), ("Q2", 65), ("Q3", 95)]},
        "animated_caption": {"title": "WATCH THIS MOMENT"},
        "callout": {"badge": "TIP", "title": "Native editable keyframes"},
        "branded_transition": {"title": "CHAPTER TWO", "subtitle": "Deep Dive"},
        "social_graphic": {"title": "Subscribe", "subtitle": "@bangonpro"},
    }
    for kind, data in sample_payloads.items():
        assert kind in MOTION_TEMPLATES
        xml, bbox, val = render_motion_graphic_xml(kind=kind, data=data, width=1920, height=1080, duration_frames=75)
        root = ET.fromstring(xml)
        assert root.tag == "kdenlivetitle"
        assert val["inside_title_safe"] is True
        assert val["inside_action_safe"] is True
        assert val["contrast_ok"] is True and val["contrast_ratio"] >= 4.5
        assert bbox[2] > 0 and bbox[3] > 0
        kf = build_qtblend_animation_rect(animation=MOTION_TEMPLATES[kind]["animation"], width=1920, height=1080, duration_frames=75)
        assert kf.startswith("0=") and ";" in kf

    # Collision avoidance moves bottom-center graphics away from active bottom-center captions
    pos = choose_non_colliding_position("stat_card", "bottom_center", 1920, 1080, captions_active=True, caption_alignment="bottom_center")
    assert pos != "bottom_center"

    # Full production run with brief requesting lower third + scoreboard + stat + chart + callout + social graphic
    brief = (
        "Tighten the pauses, add animated captions, lower third: Jane Doe, Host, "
        "scoreboard: Red 3 - Blue 2, stat: 85% faster editing, "
        "chart: Q1 40, Q2 65, Q3 95, callout: Real native keyframes, social CTA: @bangonpro"
    )
    parsed = parse_motion_graphics_from_brief(brief, {"display_name": "Demo"}, total_sec=20.0)
    assert {"scoreboard", "stat_card", "chart", "callout", "social_graphic"}.issubset({p["kind"] for p in parsed})

    footage = generate_talking_head(tmp_path, name="mg_source.mp4")
    job = ProductionJob.create(
        brief=brief,
        inputs=[f"cam_main={footage}"],
        profile_ref="youtube_talking_head",
        mode="full_production_draft",
        job_root=tmp_path / "jobs",
        options=JobOptions(mode="full_production_draft", episode_title="Motion Graphics Demo"),
    )
    job.run()
    assert job.doc["status"] == "draft_ready"

    gfx_report = json.loads((Path(job.dir) / "draft" / "graphics_report.json").read_text(encoding="utf-8"))
    kinds = {g["kind"] for g in gfx_report["graphics"]}
    assert "title_card" in kinds
    assert len(kinds & {"scoreboard", "stat_card", "chart", "callout", "social_graphic", "animated_caption"}) >= 2
    assert all(g.get("inside_title_safe") for g in gfx_report["graphics"])
    assert all(g.get("keyframes") for g in gfx_report["graphics"])

    # Verify non-overlapping V2 intervals
    intervals = sorted((float(g["start_sec"]), float(g["start_sec"]) + float(g["duration_sec"])) for g in gfx_report["graphics"])
    for (_, e1), (s2, _) in zip(intervals[:-1], intervals[1:]):
        assert s2 >= e1 - 1e-3, f"Overlapping V2 graphics: {intervals}"

    # Verify real MLT render shows animation delta on the title card entrance (frame 0 vs frame 18)
    kproj = Path(job.doc["artifacts"]["kdenlive_project"])
    f0 = _render_frame_rgb(kproj, 0, tmp_path / "mg_f0.png")
    f18 = _render_frame_rgb(kproj, 18, tmp_path / "mg_f18.png")
    anim_diff = float(np.mean(np.abs(f18 - f0)))
    assert anim_diff > 1.5, f"Expected visible qtblend entrance animation delta, got {anim_diff:.3f}"
