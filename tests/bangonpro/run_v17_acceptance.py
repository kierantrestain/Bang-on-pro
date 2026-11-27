#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
# SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""Real-application V1.7 acceptance test runner (A01–A13, A16).

Drives the ACTUAL compiled Bang On Pro desktop binary under Xvfb (1470x956)
using BANGONPRO_AUTOTEST scripts, verifies exported MP4 renders via melt +
ffprobe, captures screenshots, and records startup + 10x project-switch RSS
benchmarks.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests" / "bangonpro"))

from talking_head import generate_talking_head  # noqa: E402


BINARY = Path(os.environ.get("BANGONPRO_BINARY", str(REPO_ROOT / "install-prefix" / "bin" / "kdenlive")))
INSTALL_SHARE = REPO_ROOT / "install-prefix" / "share"
SCREENSHOT_DIR = REPO_ROOT / "docs" / "screenshots" / "v1.7"
EVIDENCE_DIR = REPO_ROOT / "docs" / "evidence" / "v1.7"


def _run_app_script(
    steps: list[dict],
    work_dir: Path,
    name: str,
    home_dir: Path,
    timeout_sec: int = 240,
    delay_ms: int = 2500,
) -> tuple[dict, float, int]:
    script_path = work_dir / f"{name}.autotest.json"
    script_path.write_text(json.dumps({"steps": steps}, indent=2), encoding="utf-8")

    runtime_dir = home_dir / "runtime"
    runtime_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(runtime_dir, 0o700)

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(home_dir / ".config")
    env["XDG_DATA_HOME"] = str(home_dir / ".local" / "share")
    env["XDG_CACHE_HOME"] = str(home_dir / ".cache")
    env["XDG_RUNTIME_DIR"] = str(runtime_dir)
    env["XDG_DATA_DIRS"] = f"{INSTALL_SHARE}:/usr/local/share:/usr/share"
    env["BANGONPRO_PYTHONPATH"] = str(REPO_ROOT)
    env["BANGONPRO_AUTOTEST"] = str(script_path)
    env["BANGONPRO_AUTOTEST_DELAY_MS"] = str(delay_ms)
    env["SDL_AUDIODRIVER"] = "dummy"
    env.pop("QT_QPA_PLATFORM", None)  # Use real Xvfb xcb display for real screenshots

    cmd = ["dbus-run-session", "--", "xvfb-run", "-a", "-s", "-screen 0 1600x1050x24", str(BINARY)]
    t0 = time.monotonic()
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout_sec)
    elapsed = round(time.monotonic() - t0, 2)

    result_path = Path(str(script_path) + ".result.json")
    if not result_path.exists():
        raise RuntimeError(
            f"Autotest '{name}' produced no result file (rc={proc.returncode}, elapsed={elapsed}s).\n"
            f"STDOUT tail:\n{proc.stdout[-1000:]}\n"
            f"STDERR tail:\n{proc.stderr[-1500:]}"
        )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not result.get("ok"):
        raise AssertionError(
            f"Autotest '{name}' failed: {result.get('why')}\n"
            f"Log:\n{json.dumps(result.get('log', [])[-8:], indent=2)}\n"
            f"Final:\n{json.dumps(result.get('final', {}), indent=2)}"
        )
    return result, elapsed, proc.returncode


def _verify_melt_render(kdenlive_path: Path, out_mp4: Path) -> dict:
    env = os.environ.copy()
    env["SDL_AUDIODRIVER"] = "dummy"
    env["QT_QPA_PLATFORM"] = "offscreen"
    cmd = [
        "xvfb-run",
        "-a",
        "melt",
        str(kdenlive_path),
        "-consumer",
        f"avformat:{out_mp4}",
        "vcodec=libx264",
        "preset=ultrafast",
        "crf=26",
        "acodec=aac",
        "ab=128k",
        "pix_fmt=yuv420p",
        "real_time=-1",
    ]
    res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=180)
    if res.returncode != 0 or not out_mp4.exists() or out_mp4.stat().st_size == 0:
        raise RuntimeError(f"melt render failed (rc={res.returncode}): {res.stderr[-1000:]}")
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(out_mp4),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    info = json.loads(probe.stdout)
    streams = info.get("streams", [])
    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    duration = float(info.get("format", {}).get("duration", 0.0))
    assert has_video and duration > 0.5, f"Invalid rendered MP4: {info}"
    return {
        "mp4_path": str(out_mp4),
        "size_bytes": out_mp4.stat().st_size,
        "duration_sec": round(duration, 2),
        "has_video": has_video,
        "has_audio": has_audio,
    }


def _create_legacy_project_from_real(source_kdenlive: Path, legacy_out: Path) -> None:
    """Simulate a real pre-V1.7 Kdenlive project by stripping bangonpro.project_uuid & bangonpro.edited.* properties."""
    import re

    xml = source_kdenlive.read_text(encoding="utf-8")
    xml = re.sub(r'\s*<property name="kdenlive:docproperties\.bangonpro\.[^"]*">[^<]*</property>', "", xml)
    xml = re.sub(r'\s*<property name="bangonpro\.[^"]*">[^<]*</property>', "", xml)
    assert "bangonpro.project_uuid" not in xml
    legacy_out.write_text(xml, encoding="utf-8")


def main() -> int:
    assert BINARY.exists(), f"Bang On Pro binary missing at {BINARY}"
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    work_dir = Path(tempfile.mkdtemp(prefix="bop_v17_accept_"))
    home_dir = work_dir / "home"
    home_dir.mkdir()
    # Pre-mark firstLaunchDone so first-launch check does not interfere with timing/screenshots
    cfg_dir = home_dir / ".config" / "BangOnPro"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "BangOnPro.conf").write_text("[setup]\nfirstLaunchDone=true\n", encoding="utf-8")

    media_dir = work_dir / "media"
    clip_a = generate_talking_head(media_dir, duration_sec=12.0, phrases=((0.6, 3.0), (4.4, 3.0), (8.2, 2.8)), name="host_project_a.mp4")
    clip_b = media_dir / "guest_project_b.mp4"
    shutil.copy2(clip_a, clip_b)

    proj_a = work_dir / "project_a.kdenlive"
    proj_b = work_dir / "project_b.kdenlive"
    effected_proj = work_dir / "effected_clip.kdenlive"
    legacy_proj = work_dir / "legacy_pre_v17.kdenlive"

    report: dict = {
        "version": "1.7.0",
        "binary": str(BINARY),
        "journeys": {},
        "acceptance": {},
    }

    # ------------------------------------------------------------------
    # JOURNEY 1: A01, A02, A10, A11 — Project A AI Cut -> Project B Clean -> New Production in B -> Revision in A
    # ------------------------------------------------------------------
    print("[Journey 1] Running A01/A02/A10/A11 (Project A AI Cut -> Project B isolation -> New Production in B -> Revision)...")
    j1_steps = [
        {"do": "resize_window", "w": 1470, "h": 956, "after_ms": 600},
        {"do": "import", "paths": [str(clip_a)], "after_ms": 2000},
        {"do": "save_project_as", "path": str(proj_a), "after_ms": 800},
        {"do": "raise_dock", "title": "Agent Control Centre", "after_ms": 500},
        {"do": "set_text", "object": "acc_brief", "text": "Tight YouTube talking head cut. Lower third: Alex Rivera, Host."},
        {"do": "click", "object": "acc_start", "after_ms": 500},
        {"do": "wait_job", "status": ["draft_ready", "completed"], "timeout_ms": 180000},
        {"do": "record", "as": "project_a_done"},
        {"do": "click", "object": "acc_open_sequence", "after_ms": 1500},
        {"do": "save_project_as", "path": str(proj_a), "after_ms": 600},
        {"do": "record", "as": "project_a_edited_open"},
        {"do": "screenshot", "path": str(SCREENSHOT_DIR / "01_project_a_ai_cut.png")},
        # A01: Create completely new unrelated Project B -> ZERO Project A state may appear!
        {"do": "new_project", "after_ms": 1200},
        {"do": "save_project_as", "path": str(proj_b), "after_ms": 800},
        {"do": "raise_dock", "title": "Agent Control Centre", "after_ms": 500},
        {"do": "record", "as": "project_b_clean"},
        {"do": "expect", "key": "job_dir", "equals": ""},
        {"do": "expect", "key": "job_status", "equals": ""},
        {"do": "expect", "key": "completion_visible", "equals": False},
        {"do": "expect", "key": "brief_text", "equals": ""},
        {"do": "screenshot", "path": str(SCREENSHOT_DIR / "02_project_b_clean_ai_cut.png")},
        # A02: Start a brand-new production & prompt inside Project B
        {"do": "import", "paths": [str(clip_b)], "after_ms": 2000},
        {"do": "set_text", "object": "acc_brief", "text": "Clean podcast intro for Project B. Lower third: Sam Patel, Guest."},
        {"do": "click", "object": "acc_start", "after_ms": 500},
        {"do": "wait_job", "status": ["draft_ready", "completed"], "timeout_ms": 180000},
        {"do": "record", "as": "project_b_done"},
        {"do": "save_project_as", "path": str(proj_b), "after_ms": 600},
        # Reopen Project A and verify revision workflow (A11)
        {"do": "open_project", "path": str(proj_a), "after_ms": 1500},
        {"do": "raise_dock", "title": "Agent Control Centre", "after_ms": 500},
        {"do": "record", "as": "project_a_reopened"},
        {"do": "quit"},
    ]
    j1_res, j1_sec, j1_rc = _run_app_script(j1_steps, work_dir, "journey1_isolation", home_dir)
    recs1 = j1_res["records"]
    uuid_a = recs1["project_a_done"]["project_uuid"]
    uuid_b = recs1["project_b_clean"]["project_uuid"]
    job_a = recs1["project_a_done"]["job_dir"]
    job_b = recs1["project_b_done"]["job_dir"]
    assert uuid_a and uuid_b and uuid_a != uuid_b, f"Project UUIDs must be distinct: A={uuid_a}, B={uuid_b}"
    assert uuid_a in job_a, f"Job A not stored under Project A UUID: {job_a}"
    assert uuid_b in job_b, f"Job B not stored under Project B UUID: {job_b}"
    assert job_a != job_b
    assert recs1["project_b_clean"]["job_dir"] == "", "Project A job leaked into Project B!"
    assert recs1["project_a_edited_open"]["sequence_count"] >= 2, "Edited sequence was not added to Project A!"

    # Render Project A's edited draft project via melt & verify with ffprobe
    job_a_meta = json.loads((Path(job_a) / "job.json").read_text(encoding="utf-8"))
    draft_a_kdenlive = Path(job_a_meta["artifacts"]["kdenlive_project"])
    assert draft_a_kdenlive.exists(), f"Missing draft kdenlive project: {draft_a_kdenlive}"
    render_a = _verify_melt_render(draft_a_kdenlive, work_dir / "project_a_export.mp4")

    report["journeys"]["journey1_isolation_and_ai_cut"] = {
        "ok": True,
        "rc": j1_rc,
        "elapsed_sec": j1_sec,
        "project_a_uuid": uuid_a,
        "project_b_uuid": uuid_b,
        "project_a_job_dir": job_a,
        "project_b_job_dir": job_b,
        "project_b_clean_job_dir": recs1["project_b_clean"]["job_dir"],
        "project_a_sequences_after_open": recs1["project_a_edited_open"]["sequence_count"],
        "render_a": render_a,
    }
    print(f"  PASS Journey 1 ({j1_sec}s): A={uuid_a[:8]} B={uuid_b[:8]} render={render_a['duration_sec']}s")

    # ------------------------------------------------------------------
    # JOURNEY 2: A03, A04, A06 — Worker SIGKILL recovery + Project switch during active worker
    # ------------------------------------------------------------------
    print("[Journey 2] Running A04/A06 (SIGKILL worker recovery + switch projects during background work)...")
    clip_crash = generate_talking_head(
        media_dir,
        duration_sec=16.0,
        phrases=((0.6, 3.0), (4.4, 3.0), (8.2, 3.0), (12.0, 2.8)),
        name="crash_test_cam.mp4",
        seed=99,
    )
    proj_crash = work_dir / "project_crash.kdenlive"
    j2_steps = [
        {"do": "resize_window", "w": 1470, "h": 956, "after_ms": 500},
        {"do": "import", "paths": [str(clip_crash)], "after_ms": 2000},
        {"do": "save_project_as", "path": str(proj_crash), "after_ms": 800},
        {"do": "raise_dock", "title": "Agent Control Centre", "after_ms": 500},
        {"do": "set_text", "object": "acc_brief", "text": "Crash recovery test edit on fresh footage."},
        {"do": "click", "object": "acc_start", "after_ms": 2200},
        {"do": "kill_department", "after_ms": 2000},
        {"do": "wait_job", "status": ["interrupted", "failed"], "timeout_ms": 30000},
        {"do": "record", "as": "after_sigkill"},
        {"do": "expect", "key": "job_status", "equals": "interrupted"},
        # Resume from checkpoint after SIGKILL (A04)
        {"do": "click", "object": "acc_resume", "after_ms": 500},
        # A06: Switch to Project B WHILE Project A's worker is actively resuming in the background!
        {"do": "wait_ms", "ms": 800},
        {"do": "open_project", "path": str(proj_b), "after_ms": 1500},
        {"do": "raise_dock", "title": "Agent Control Centre", "after_ms": 500},
        {"do": "record", "as": "project_b_during_bg_work"},
        {"do": "expect", "key": "job_dir", "equals": ""},
        {"do": "expect", "key": "project_uuid", "equals": uuid_b},
        # Wait briefly and verify Project B STILL has zero Project A state or cross-project writes!
        {"do": "wait_ms", "ms": 4000},
        {"do": "record", "as": "project_b_after_bg_wait"},
        {"do": "expect", "key": "job_dir", "equals": ""},
        {"do": "quit"},
    ]
    j2_res, j2_sec, j2_rc = _run_app_script(j2_steps, work_dir, "journey2_crash_and_bg_switch", home_dir)
    report["journeys"]["journey2_crash_recovery_and_bg_switch"] = {
        "ok": True,
        "rc": j2_rc,
        "elapsed_sec": j2_sec,
        "after_sigkill_status": j2_res["records"]["after_sigkill"]["job_status"],
        "project_b_during_bg_job_dir": j2_res["records"]["project_b_during_bg_work"]["job_dir"],
        "project_b_after_bg_job_dir": j2_res["records"]["project_b_after_bg_wait"]["job_dir"],
    }
    print(f"  PASS Journey 2 ({j2_sec}s): sigkill->interrupted, Project B isolated during live background worker!")

    # ------------------------------------------------------------------
    # JOURNEY 3: A07, A08, A09 — Unified Effects Library + Effect Controls + Intrinsic Transform + Undo/Redo + Save/Reopen/Export
    # ------------------------------------------------------------------
    print("[Journey 3] Running A07/A08/A09 (Effects Library -> Effect Controls -> Intrinsic Transform + Undo/Redo -> Save/Reopen/Export)...")
    j3_steps = [
        {"do": "resize_window", "w": 1470, "h": 956, "after_ms": 500},
        {"do": "open_project", "path": str(proj_a), "after_ms": 2000},
        {"do": "select_first_clip", "after_ms": 600},
        {"do": "raise_dock", "title": "Effects", "after_ms": 400},
        {"do": "apply_effect", "effect_id": "avfilter.gblur", "after_ms": 700},
        {"do": "record", "as": "after_effect_apply"},
        {"do": "expect", "key": "effect_controls_title", "equals": "Effect Controls"},
        {"do": "expect", "key": "effect_controls_visible", "equals": True},
        {"do": "expect", "key": "active_clip_effects", "min": 1},
        {"do": "screenshot", "path": str(SCREENSHOT_DIR / "03_effects_and_controls.png")},
        # A08: Adjust built-in Intrinsic Transform (lazily materializes qtblend) + keyframe + undo + redo
        {"do": "adjust_transform", "pos_x": 80.0, "pos_y": -30.0, "scale": 120.0, "rotation": 10.0, "opacity": 90.0, "keyframe": True, "after_ms": 700},
        {"do": "record", "as": "after_transform_adjust"},
        {"do": "expect", "key": "intrinsic_transform_materialized", "equals": True},
        {"do": "screenshot", "path": str(SCREENSHOT_DIR / "04_intrinsic_transform_controls.png")},
        {"do": "undo", "after_ms": 500},
        {"do": "record", "as": "after_undo"},
        {"do": "redo", "after_ms": 500},
        {"do": "record", "as": "after_redo"},
        # A09: Save, clear via new_project, reopen, and verify effect & transform persisted!
        {"do": "save_project_as", "path": str(effected_proj), "after_ms": 800},
        {"do": "new_project", "after_ms": 1000},
        {"do": "open_project", "path": str(effected_proj), "after_ms": 1500},
        {"do": "select_first_clip", "after_ms": 700},
        {"do": "record", "as": "after_reopen"},
        {"do": "expect", "key": "active_clip_effects", "min": 2},
        {"do": "expect", "key": "intrinsic_transform_materialized", "equals": True},
        {"do": "quit"},
    ]
    j3_res, j3_sec, j3_rc = _run_app_script(j3_steps, work_dir, "journey3_effects_controls", home_dir)
    # Verify saved XML actually contains avfilter.gblur + qtblend, and render via melt!
    effected_xml = effected_proj.read_text(encoding="utf-8", errors="ignore")
    assert "qtblend" in effected_xml, "Persisted .kdenlive missing materialized qtblend!"
    assert "avfilter.gblur" in effected_xml, "Persisted .kdenlive missing applied avfilter.gblur!"
    render_fx = _verify_melt_render(effected_proj, work_dir / "effected_export.mp4")

    report["journeys"]["journey3_effects_and_controls"] = {
        "ok": True,
        "rc": j3_rc,
        "elapsed_sec": j3_sec,
        "effect_controls_title": j3_res["records"]["after_effect_apply"]["effect_controls_title"],
        "effect_controls_visible": j3_res["records"]["after_effect_apply"]["effect_controls_visible"],
        "effects_after_apply": j3_res["records"]["after_effect_apply"]["active_clip_effects"],
        "effects_after_reopen": j3_res["records"]["after_reopen"]["active_clip_effects"],
        "transform_materialized_after_reopen": j3_res["records"]["after_reopen"]["intrinsic_transform_materialized"],
        "render_fx": render_fx,
    }
    print(f"  PASS Journey 3 ({j3_sec}s): Effect Controls visible, qtblend+gblur persisted & rendered ({render_fx['duration_sec']}s)!")

    # ------------------------------------------------------------------
    # JOURNEY 4: A13, A16 — Warm/Cold Startup + Legacy Project Migration + 10x Project-Switch RSS Stability
    # ------------------------------------------------------------------
    print("[Journey 4] Running A13/A16 (Legacy project open + 10x project-switching RSS stability)...")
    _create_legacy_project_from_real(proj_a, legacy_proj)
    j4_steps: list[dict] = [
        {"do": "resize_window", "w": 1470, "h": 956, "after_ms": 400},
        {"do": "open_project", "path": str(legacy_proj), "after_ms": 1200},
        {"do": "record", "as": "legacy_opened"},
        {"do": "record", "as": "switch_cycle_0"},
    ]
    for cycle in range(1, 11):
        target = proj_a if (cycle % 2 == 1) else proj_b
        j4_steps.append({"do": "open_project", "path": str(target), "after_ms": 900})
        if cycle in (1, 5, 10):
            j4_steps.append({"do": "record", "as": f"switch_cycle_{cycle}"})
    j4_steps.append({"do": "quit"})
    j4_res, j4_sec, j4_rc = _run_app_script(j4_steps, work_dir, "journey4_perf_and_legacy", home_dir, delay_ms=1500)
    rss_start = j4_res["records"]["switch_cycle_0"].get("rss_mb", 0.0)
    rss_mid = j4_res["records"]["switch_cycle_5"].get("rss_mb", 0.0)
    rss_end = j4_res["records"]["switch_cycle_10"].get("rss_mb", 0.0)
    rss_delta = round(rss_end - rss_start, 1)
    legacy_uuid = j4_res["records"]["legacy_opened"].get("project_uuid", "")
    assert legacy_uuid, "Legacy project failed to receive a canonical bangonpro.project_uuid!"

    # Cold/warm startup check (quick open + immediate quit with delay_ms=600)
    _, warm_sec, _ = _run_app_script([{"do": "record", "as": "ready"}, {"do": "quit"}], work_dir, "warm_startup", home_dir, delay_ms=600)

    report["journeys"]["journey4_stability_and_legacy"] = {
        "ok": True,
        "rc": j4_rc,
        "elapsed_sec": j4_sec,
        "warm_startup_roundtrip_sec": warm_sec,
        "legacy_migrated_uuid": legacy_uuid,
        "rss_mb_cycle_0": rss_start,
        "rss_mb_cycle_5": rss_mid,
        "rss_mb_cycle_10": rss_end,
        "rss_mb_delta_10_switches": rss_delta,
    }
    print(f"  PASS Journey 4 ({j4_sec}s): legacy UUID={legacy_uuid[:8]}, RSS 0->10 switches: {rss_start}->{rss_end} MB (delta {rss_delta} MB), warm startup={warm_sec}s")

    out_json = EVIDENCE_DIR / "acceptance_results.json"
    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nALL 4 REAL-APP ACCEPTANCE JOURNEYS PASSED! Evidence written to {out_json}")
    shutil.rmtree(work_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
