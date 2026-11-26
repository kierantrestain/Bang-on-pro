"""Comprehensive unit + real 4-camera end-to-end integration tests & benchmark
for Bang On Pro's AI Production & Autonomous Editing Engine.

Synthesizes real 4-camera video/audio test fixtures with ``ffmpeg`` (including
mixed 30fps vs 25fps frame rates, known non-zero recording offsets, a 2.0s
setup delay, a 2.0s interior silence gap, per-camera speaker mics + room bleed,
and crosstalk), runs the full multi-agent pipeline through validated Timeline
Command API v1 batches, emits ``.kdenlive`` + ``.otio``, renders a real MP4 via
``melt``, verifies streams via ``ffprobe``, and proves zero mutation of source
media SHA-256 hashes.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import subprocess
import sys
import wave
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import defusedxml.ElementTree as SafeET
import numpy as np
import opentimelineio as otio
import pytest
from scipy.signal import butter, sosfilt

from bangonpro.ai_engine import (
    ApprovalRejectedError,
    PipelineCancelledError,
    ProductionOrchestrator,
    ProjectAnalysisState,
    StageDependencyError,
    StageSpec,
    TimelineCommandBuilder,
    TimelineCommandValidationError,
    build_default_orchestrator,
    parse_production_instruction,
    run_production_pipeline,
    validate_timeline_command_batch,
)
from bangonpro.ai_engine.agents.audio_graphics_qc import build_audio_restoration_chain
from bangonpro.ai_engine.agents.experimental_vision import (
    EXPERIMENTAL_VISION_WARNING,
    ExperimentalVisionService,
)
from bangonpro.ai_engine.agents.ingest_sync import (
    compute_file_sha256,
    probe_media_file,
    verify_source_integrity,
)
from bangonpro.ai_engine.cli import main as cli_main
from bangonpro.show_profiles import (
    ShowProfileValidationError,
    list_show_profiles,
    load_show_profile,
    validate_show_profile,
)

# Ground-truth camera sync offsets (in seconds relative to cam_wide)
EXPECTED_CAMERA_OFFSETS_SEC: dict[str, float] = {
    "cam_wide": 0.00,
    "cam_host": 0.40,
    "cam_guest": 0.88,  # Mixed FPS camera (30 fps vs 25 fps project)
    "cam_reaction": 1.20,
}

CAMERA_FPS_SPEC: dict[str, int] = {
    "cam_wide": 25,
    "cam_host": 25,
    "cam_guest": 30,  # Mixed FPS!
    "cam_reaction": 25,
}

CAMERA_COLORS: dict[str, str] = {
    "cam_wide": "#1B3B6F",
    "cam_host": "#9B2226",
    "cam_guest": "#2D6A4F",
    "cam_reaction": "#CA6702",
}


def _synthesize_speech_like_signal(
    duration_sec: float,
    sample_rate: int,
    *,
    f0_hz: float,
    syllable_hz: float,
    seed: int,
) -> np.ndarray:
    """Create broadband speech-like voiced + consonant formant audio (deterministic)."""
    n = int(round(duration_sec * sample_rate))
    t = np.arange(n, dtype=np.float64) / float(sample_rate)
    rng = np.random.default_rng(seed)

    # Pitch vibrato + harmonic formant series
    f0_mod = f0_hz * (1.0 + 0.06 * np.sin(2.0 * np.pi * 2.3 * t))
    phase = 2.0 * np.pi * np.cumsum(f0_mod) / float(sample_rate)
    voiced = (
        0.45 * np.sin(phase)
        + 0.30 * np.sin(2.0 * phase)
        + 0.22 * np.sin(3.0 * phase)
        + 0.14 * np.sin(5.0 * phase)
        + 0.08 * np.sin(8.0 * phase)
    )

    # Bandpassed broadband fricative/consonant energy (400-3200 Hz) for razor-sharp GCC-PHAT
    raw_noise = rng.standard_normal(n)
    sos = butter(4, [400.0 / (0.5 * sample_rate), 3200.0 / (0.5 * sample_rate)], btype="bandpass", output="sos")
    speech_noise = sosfilt(sos, raw_noise)
    speech_noise /= max(1e-6, float(np.std(speech_noise)))

    # Syllable amplitude envelope (smooth bursts at syllable_hz)
    envelope = 0.55 + 0.45 * np.sin(2.0 * np.pi * syllable_hz * t)
    # Soft fade-in/fade-out at boundaries
    fade_len = min(n // 10, int(0.05 * sample_rate))
    if fade_len > 1:
        envelope[:fade_len] *= np.linspace(0.0, 1.0, fade_len)
        envelope[-fade_len:] *= np.linspace(1.0, 0.0, fade_len)

    combined = (0.62 * voiced + 0.38 * speech_noise) * envelope
    combined /= max(1e-6, float(np.max(np.abs(combined))))
    return combined


def _write_wav_16bit(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    clipped = np.clip(samples, -0.98, 0.98)
    pcm16 = (clipped * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())


def _generate_synthetic_4cam_studio(base_dir: Path) -> dict[str, Path]:
    """Synthesize 4 real camera MP4s with known offsets, mixed FPS, setup delay & silence gap.

    Master studio timeline (14.0 seconds):
      0.0 -  2.0s: 2.0s setup delay (quiet room floor + brief 20ms wideband sync slate at 0.50s)
      2.0 -  5.5s: Host speaks (close mic strong on cam_host, room bleed on all cameras)
      5.5 -  7.5s: 2.0s interior dead-air silence gap
      7.5 - 11.2s: Guest speaks (close mic strong on cam_guest, room bleed on all cameras)
     11.2 - 12.6s: Host + Guest crosstalk (equal energy -> triggers low-confidence review flag)
     12.6 - 14.0s: Host closing segment
    """
    media_dir = base_dir / "synthetic_4cam_media"
    media_dir.mkdir(parents=True, exist_ok=True)

    sr = 48000
    master_dur = 14.0
    n_master = int(round(master_dur * sr))
    rng = np.random.default_rng(2026)

    # Shared studio room baseline signals
    host_stem = np.zeros(n_master, dtype=np.float64)
    guest_stem = np.zeros(n_master, dtype=np.float64)
    slate_stem = np.zeros(n_master, dtype=np.float64)

    def _place(buf: np.ndarray, start_s: float, sig: np.ndarray, gain: float = 1.0) -> None:
        idx = int(round(start_s * sr))
        end = min(buf.size, idx + sig.size)
        if end > idx:
            buf[idx:end] += gain * sig[: end - idx]

    # Brief 20 ms wideband acoustic slate click at 0.50s inside setup delay
    slate_n = int(round(0.020 * sr))
    slate_burst = rng.standard_normal(slate_n) * np.hanning(slate_n)
    slate_burst /= max(1e-6, float(np.max(np.abs(slate_burst))))
    _place(slate_stem, 0.50, slate_burst, gain=0.45)

    # Host segment 1 (2.0s - 5.5s)
    _place(
        host_stem,
        2.0,
        _synthesize_speech_like_signal(3.5, sr, f0_hz=145.0, syllable_hz=3.8, seed=101),
        gain=0.55,
    )
    # 5.5s - 7.5s: 2.0s dead air silence gap

    # Guest segment (7.5s - 11.2s)
    _place(
        guest_stem,
        7.5,
        _synthesize_speech_like_signal(3.7, sr, f0_hz=215.0, syllable_hz=4.4, seed=202),
        gain=0.55,
    )

    # Crosstalk segment (11.2s - 12.6s): both speak equally -> ambiguous diarisation
    _place(
        host_stem,
        11.2,
        _synthesize_speech_like_signal(1.4, sr, f0_hz=150.0, syllable_hz=5.0, seed=303),
        gain=0.22,
    )
    _place(
        guest_stem,
        11.2,
        _synthesize_speech_like_signal(1.4, sr, f0_hz=210.0, syllable_hz=4.7, seed=404),
        gain=0.21,
    )

    # Host closing remarks (12.6s - 14.0s)
    _place(
        host_stem,
        12.6,
        _synthesize_speech_like_signal(1.4, sr, f0_hz=142.0, syllable_hz=3.5, seed=505),
        gain=0.52,
    )

    # Build camera-specific audio mixes (close mic + shared room bleed + subtle noise floor)
    mix_weights: dict[str, tuple[float, float, float]] = {
        # (host_gain, guest_gain, slate_gain)
        "cam_wide": (0.45, 0.45, 0.60),
        "cam_host": (1.00, 0.14, 0.55),
        "cam_guest": (0.14, 1.00, 0.55),
        "cam_reaction": (0.30, 0.35, 0.50),
    }

    camera_paths: dict[str, Path] = {}
    font_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    ]
    font_path = next((f for f in font_candidates if Path(f).is_file()), None)
    has_drawtext = font_path is not None and "drawtext" in subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, check=False
    ).stdout

    for idx, (cam_id, offset_sec) in enumerate(EXPECTED_CAMERA_OFFSETS_SEC.items()):
        h_g, g_g, s_g = mix_weights[cam_id]
        cam_master = h_g * host_stem + g_g * guest_stem + s_g * slate_stem

        # Prepend exact offset_sec leader so master event T appears at T + offset_sec
        leader_samples = int(round(offset_sec * sr))
        total_cam_samples = leader_samples + n_master
        cam_rng = np.random.default_rng(7000 + idx)
        cam_audio = 0.0004 * cam_rng.standard_normal(total_cam_samples)
        cam_audio[leader_samples:] += cam_master

        wav_path = media_dir / f"{cam_id}.wav"
        _write_wav_16bit(wav_path, cam_audio, sr)

        mp4_path = media_dir / f"{cam_id}.mp4"
        cam_fps = CAMERA_FPS_SPEC[cam_id]
        cam_dur = total_cam_samples / float(sr)
        color = CAMERA_COLORS[cam_id]

        if has_drawtext:
            vf = (
                f"drawbox=x=24:y=24:w=592:h=72:color=black@0.65:t=fill,"
                f"drawtext=fontfile={font_path}:text='{cam_id.upper()} ({cam_fps}fps off=+{offset_sec:.2f}s)':"
                f"fontcolor=white:fontsize=26:x=40:y=46,"
                f"drawtext=fontfile={font_path}:text='TC %{{pts\\:hms}}':"
                f"fontcolor=yellow:fontsize=24:x=40:y=300"
            )
        else:  # macOS bundles / FFmpeg builds without freetype: identical footage minus the burnt-in labels
            vf = "drawbox=x=24:y=24:w=592:h=72:color=black@0.65:t=fill"

        cmd = [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=640x360:r={cam_fps}:d={cam_dur:.4f}",
            "-i",
            str(wav_path),
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            str(sr),
            "-shortest",
            str(mp4_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        assert proc.returncode == 0, f"ffmpeg failed generating {cam_id}: {proc.stderr}"
        wav_path.unlink(missing_ok=True)
        camera_paths[cam_id] = mp4_path

    return camera_paths


@pytest.fixture(scope="module")
def synthetic_4cam_footage(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    base = tmp_path_factory.mktemp("bangonpro_4cam")
    return _generate_synthetic_4cam_studio(base)


# ---------------------------------------------------------------------------
# 1. Show Profiles & Schema Validation Tests
# ---------------------------------------------------------------------------


def test_all_four_show_profiles_conform_to_schema() -> None:
    expected = {"podcast_multicam", "gameshow_multicam", "sports_discussion", "daily_news"}
    available = set(list_show_profiles())
    assert expected.issubset(available)

    for profile_id in sorted(expected):
        prof = load_show_profile(profile_id)
        assert prof["schema_version"] == "1.0"
        assert prof["profile_id"] == profile_id
        assert prof["video_spec"]["width"] == 1920
        assert prof["video_spec"]["height"] == 1080
        # V1.2.2: broadcast genres follow EBU R128 (-23 LUFS); the podcast stays at -16 LUFS (online)
        broadcast = {"daily_news", "gameshow_multicam", "sports_discussion"}
        assert prof["audio_rules"]["target_lufs"] == (-23.0 if profile_id in broadcast else -16.0)
        assert len(prof["export_presets"]) >= 1

    assert len(load_show_profile("podcast_multicam")["cameras"]) == 4
    assert len(load_show_profile("gameshow_multicam")["cameras"]) == 4
    assert len(load_show_profile("sports_discussion")["cameras"]) == 4
    assert len(load_show_profile("daily_news")["cameras"]) == 2


def test_invalid_show_profile_rejected() -> None:
    bad = load_show_profile("podcast_multicam")
    bad["editing_rules"]["max_shot_sec"] = 1.0
    bad["editing_rules"]["min_shot_sec"] = 5.0
    with pytest.raises(ShowProfileValidationError):
        validate_show_profile(bad)

    bad2 = load_show_profile("daily_news")
    bad2["format_type"] = "unsupported_genre"
    with pytest.raises(ShowProfileValidationError):
        validate_show_profile(bad2)


# ---------------------------------------------------------------------------
# 2. Timeline Command API v1 Validation & Intent Parser Tests
# ---------------------------------------------------------------------------


def test_timeline_command_builder_and_schema_rejection() -> None:
    builder = TimelineCommandBuilder(
        project_id="proj_1",
        sequence_id="seq_1",
        expected_revision=0,
        agent_id="unit_test_agent",
        agent_role="tester",
    )
    builder.add_operation(
        "place_clip",
        media_ref="media_cam_wide",
        clip_id="clip_1",
        track_id="V1",
        in_frame=10,
        out_frame=50,
        timeline_start=0,
        confidence=0.95,
        rationale="Valid clip placement",
    )
    batch = builder.build()
    assert validate_timeline_command_batch(batch)["api_version"] == "1.0"

    # Reject unknown operation type
    invalid_op = json.loads(json.dumps(batch))
    invalid_op["operations"][0]["type"] = "mutate_raw_timeline_directly"
    with pytest.raises(TimelineCommandValidationError):
        validate_timeline_command_batch(invalid_op)

    # Reject inverted frame range
    invalid_fr = json.loads(json.dumps(batch))
    invalid_fr["operations"][0]["frame_range"]["in_frame"] = 100
    invalid_fr["operations"][0]["frame_range"]["out_frame"] = 20
    with pytest.raises(TimelineCommandValidationError):
        validate_timeline_command_batch(invalid_fr)


def test_natural_language_instruction_parser() -> None:
    intent = parse_production_instruction(
        "Sync all four cameras, remove setup delays, build a first cut and add our standard graphics"
    )
    assert intent.sync_cameras is True
    assert intent.remove_setup_delay is True
    assert intent.build_rough_cut is True
    assert intent.add_lower_thirds is True
    assert intent.add_captions is True
    assert "sync_cameras" in intent.parsed_actions
    assert "remove_setup_delay" in intent.parsed_actions

    news_intent = parse_production_instruction(
        "Assemble daily news bulletin, skip captions, and export mp4"
    )
    assert news_intent.profile_hint == "daily_news"
    assert news_intent.add_captions is False
    assert news_intent.export_video is True


# ---------------------------------------------------------------------------
# 3. Orchestrator DAG, Retries, Approval Checkpoint & Cancellation Tests
# ---------------------------------------------------------------------------


def test_orchestrator_dag_retries_approval_and_immutable_log(tmp_path: Path) -> None:
    # Cycle detection
    cyclic = ProductionOrchestrator(
        stages=[
            StageSpec(name="a", handler=lambda s, t, p: None, depends_on=("b",)),
            StageSpec(name="b", handler=lambda s, t, p: None, depends_on=("a",)),
        ]
    )
    with pytest.raises(StageDependencyError):
        cyclic.resolve_execution_order()

    # Bounded retry + approval checkpoint + immutable ActivityLogEntry
    attempts = {"count": 0}
    approved_calls: list[str] = []

    def flaky_stage(state: ProjectAnalysisState, token: Any, prog: Any) -> dict[str, Any]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("Transient disk hiccup")
        b = TimelineCommandBuilder(
            project_id=state.project_id,
            sequence_id=state.sequence_id,
            expected_revision=state.revision,
            agent_id="flaky_agent",
            agent_role="tester",
            requires_approval=True,
        )
        b.add_operation("add_marker", in_frame=0, out_frame=1, timeline_start=0, params={"label": "ok"})
        return b.build()

    profile = load_show_profile("podcast_multicam")
    state = ProjectAnalysisState(
        project_id="proj_retry",
        sequence_id="seq_retry",
        profile=profile,
        output_dir=str(tmp_path / "retry_ok"),
    )
    orch = ProductionOrchestrator(
        stages=[
            StageSpec(name="flaky", handler=flaky_stage, max_retries=2, retry_delay_sec=0.01, requires_approval=True),
        ]
    )
    orch.execute(
        state,
        approval_callback=lambda stage_name, st, batch: (approved_calls.append(stage_name) or True),
    )
    assert attempts["count"] == 2
    assert approved_calls == ["flaky"]
    assert state.revision == 1

    # Verify ActivityLogEntry objects are truly frozen/immutable
    entries = orch.activity_log.entries()
    assert len(entries) >= 5
    with pytest.raises(Exception):
        entries[0].event = "tampered"  # type: ignore[misc]

    # Approval rejection stops commit cleanly
    state_reject = ProjectAnalysisState(
        project_id="proj_rej",
        sequence_id="seq_rej",
        profile=profile,
        output_dir=str(tmp_path / "retry_reject"),
    )
    def gated_handler(s: ProjectAnalysisState, t: Any, p: Any) -> dict[str, Any]:
        b = TimelineCommandBuilder(
            project_id=s.project_id,
            sequence_id=s.sequence_id,
            expected_revision=s.revision,
            agent_id="gated_agent",
            agent_role="tester",
            requires_approval=True,
        )
        b.add_operation("add_marker", in_frame=0, out_frame=1, timeline_start=0, params={"label": "gated"})
        return b.build()

    orch_rej = ProductionOrchestrator(
        stages=[StageSpec(name="gated", handler=gated_handler, requires_approval=True)]
    )
    with pytest.raises(ApprovalRejectedError):
        orch_rej.execute(state_reject, approval_callback=lambda name, st, batch: False)
    assert state_reject.revision == 0


def test_orchestrator_cooperative_cancellation(
    synthetic_4cam_footage: dict[str, Path],
    tmp_path: Path,
) -> None:
    orch = build_default_orchestrator()
    stages_seen: list[str] = []

    def progress_and_cancel(stage_name: str, pct: float, msg: str) -> None:
        stages_seen.append(stage_name)
        if stage_name == "ingest_sync" and pct >= 0.15:
            orch.cancel("User pressed cancel during ingest")

    with pytest.raises(PipelineCancelledError):
        run_production_pipeline(
            inputs=synthetic_4cam_footage,
            output_dir=tmp_path / "cancelled_run",
            profile_ref="podcast_multicam",
            instruction="Sync all four cameras and build a first cut",
            export_video=False,
            progress_callback=progress_and_cancel,
            orchestrator=orch,
        )

    assert orch.is_cancelled()
    assert "rough_cut_multicam" not in stages_seen
    log_path = tmp_path / "cancelled_run" / "activity_log.json"
    assert log_path.is_file()
    events = [row["event"] for row in json.loads(log_path.read_text(encoding="utf-8"))]
    assert "pipeline_cancelled" in events


# ---------------------------------------------------------------------------
# 4. End-to-End 4-Camera Synthetic Media Pipeline + Melt Render + Benchmark
# ---------------------------------------------------------------------------


def test_end_to_end_4cam_pipeline_and_melt_render(
    synthetic_4cam_footage: dict[str, Path],
    tmp_path: Path,
) -> None:
    # Record original source media SHA-256 hashes BEFORE pipeline execution
    hashes_before = {
        cam_id: compute_file_sha256(path)
        for cam_id, path in synthetic_4cam_footage.items()
    }

    progress_events: list[tuple[str, float, str]] = []
    out_dir = tmp_path / "e2e_podcast_run"

    state, orch = run_production_pipeline(
        inputs=synthetic_4cam_footage,
        output_dir=out_dir,
        profile_ref="podcast_multicam",
        instruction="Sync all four cameras, remove setup delays, build a first cut and add our standard graphics",
        export_video=True,
        dry_run=False,
        sequence_id="ep01_podcast_cut",
        progress_callback=lambda s, p, m: progress_events.append((s, p, m)),
    )

    # A. Verify GCC-PHAT multicam sync recovered exact offsets within +/-1 frame (+/-0.04s @ 25fps)
    assert state.sync_result is not None
    assert state.sync_result.reference_camera_id == "cam_wide"
    frame_tolerance_sec = 1.0 / state.fps  # 0.04s at 25 fps

    for cam_id, expected_off in EXPECTED_CAMERA_OFFSETS_SEC.items():
        recovered = state.sync_result.offsets_sec[cam_id]
        err = abs(recovered - expected_off)
        assert err <= frame_tolerance_sec, (
            f"GCC-PHAT offset error for {cam_id} too high: "
            f"expected {expected_off:.4f}s, got {recovered:.4f}s (err={err:.4f}s > {frame_tolerance_sec:.4f}s)"
        )
        assert state.sync_result.confidences[cam_id] >= 0.75

    # Verify mixed-FPS camera (30 fps) was accurately probed and handled cleanly
    assert state.media_assets["cam_guest"].fps == pytest.approx(30.0, abs=0.1)
    assert state.media_assets["cam_wide"].fps == pytest.approx(25.0, abs=0.1)

    # B. Verify setup delay (~2.0s) and silence gap (~2.0s) were detected and trimmed
    assert 1.6 <= state.setup_delay_sec <= 2.3
    interior_silences = [iv for iv in state.silence_intervals if iv.label == "silence"]
    assert len(interior_silences) >= 1
    assert any(iv.duration_sec >= 1.4 for iv in interior_silences)

    edited_dur = state.rough_cut_shots[-1].timeline_out_sec
    assert 6.0 <= edited_dur <= 11.8, f"Expected trimmed rough cut duration, got {edited_dur:.2f}s"

    # C. Verify speaker-aware multicam angle switching & low-confidence crosstalk review flags
    switched_cams = {shot.camera_id for shot in state.rough_cut_shots}
    assert "cam_host" in switched_cams
    assert "cam_guest" in switched_cams
    assert len(state.rough_cut_shots) >= 2
    assert any(seg.is_low_confidence for seg in state.transcript_segments), (
        "Expected overlapping crosstalk segment to be flagged low-confidence for editorial review"
    )
    assert any(m.category == "review" for m in state.review_markers), (
        "Expected editorial review marker to be emitted for uncertain crosstalk cut"
    )

    # D. Verify audio restoration chain, captions (.srt/.ass), lower thirds & QC
    mlt_services = {f.mlt_service for f in state.audio_filters}
    # V1.2: Kdenlive-native, frame-accurate equivalents (afftdn/acompressor/loudnorm are in Kdenlive's
    # excluded_effects.txt and were stripped when the generated project was opened for editing).
    assert {"avfilter.highpass", "avfilter.compand", "dynamic_loudness"}.issubset(mlt_services)
    assert not mlt_services & {"avfilter.afftdn", "avfilter.acompressor", "avfilter.loudnorm"}
    assert "loudnorm=I=-16.0:TP=-1.5:LRA=7" in state.ffmpeg_audio_filtergraph

    srt_path = Path(state.artifacts["captions_srt"])
    ass_path = Path(state.artifacts["captions_ass"])
    assert srt_path.is_file() and "-->" in srt_path.read_text(encoding="utf-8")
    ass_content = ass_path.read_text(encoding="utf-8")
    assert ass_path.is_file() and "[Script Info]" in ass_content and "Dialogue:" in ass_content

    assert len(state.lower_thirds) >= 2
    assert state.qc_report.passed is True
    assert state.qc_report.metrics["timeline_gap_count"] == 0
    assert state.qc_report.metrics["source_out_of_bounds_count"] == 0

    # E. Verify EVERY emitted Timeline Command API v1 batch validates against JSON schema
    cmds_json = json.loads(Path(state.artifacts["timeline_commands_json"]).read_text(encoding="utf-8"))
    assert len(cmds_json["batches"]) == 5
    for idx, batch in enumerate(cmds_json["batches"]):
        validate_timeline_command_batch(batch)
        assert batch["expected_revision"] == idx

    # F. Verify .kdenlive XML parses cleanly and contains required Kdenlive/MLT structures
    kdenlive_path = Path(state.artifacts["kdenlive_project"])
    assert kdenlive_path.is_file()
    xml_root = SafeET.parse(str(kdenlive_path)).getroot()
    assert xml_root.tag == "mlt"
    assert xml_root.find("profile") is not None
    assert len(xml_root.findall("chain")) >= 4
    assert len(xml_root.findall("playlist")) >= 5
    assert len(xml_root.findall("tractor")) >= 6
    kdenlive_text = kdenlive_path.read_text(encoding="utf-8")
    assert "qtblend" in kdenlive_text
    assert "kdenlivetitle" in kdenlive_text
    assert "avfilter.subtitles" in kdenlive_text
    assert "kdenlive:sequenceproperties.guides" in kdenlive_text
    assert "kdenlive:projectTractor" in kdenlive_text

    # G. Verify OpenTimelineIO .otio parses cleanly
    otio_path = Path(state.artifacts["otio_timeline"])
    assert otio_path.is_file()
    loaded_otio = otio.adapters.read_from_file(str(otio_path))
    assert len(loaded_otio.tracks) == 3
    v1_otio = loaded_otio.tracks[0]
    a1_otio = loaded_otio.tracks[2]
    assert len(list(v1_otio)) >= 2
    assert len(a1_otio.effects) >= 4
    assert len(v1_otio.markers) >= 1

    # H. Verify REAL melt rendered MP4 exists with valid video + non-silent audio via ffprobe/ffmpeg
    rendered_mp4 = Path(state.artifacts["rendered_video"])
    assert rendered_mp4.is_file() and rendered_mp4.stat().st_size > 10_000
    render_probe = probe_media_file(rendered_mp4)
    assert render_probe["video_codec"] == "h264"
    assert render_probe["audio_codec"] == "aac"
    assert render_probe["width"] == 1920
    assert render_probe["height"] == 1080
    assert abs(render_probe["duration_sec"] - edited_dur) <= 0.6

    vol_proc = subprocess.run(
        ["ffmpeg", "-i", str(rendered_mp4), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    mean_vol_line = next(
        (ln for ln in vol_proc.stderr.splitlines() if "mean_volume:" in ln), ""
    )
    assert mean_vol_line, "volumedetect did not report rendered MP4 audio level"
    mean_db = float(mean_vol_line.split("mean_volume:")[1].split("dB")[0].strip())
    assert mean_db > -45.0, f"Rendered MP4 audio is unexpectedly silent: {mean_db} dB"
    assert isinstance(state.qc_report.metrics["pre_loudnorm_peak_dbfs"], float)

    # I. Verify source media files are 100% UNCHANGED (strict read-only immutability)
    integrity = verify_source_integrity(state)
    assert all(integrity.values())
    for cam_id, path in synthetic_4cam_footage.items():
        assert compute_file_sha256(path) == hashes_before[cam_id]

    # J. Verify wall-clock benchmark metrics & progress callbacks recorded
    assert len(progress_events) >= 10
    assert "total_pipeline" in state.stage_timings_sec
    assert state.stage_timings_sec["total_pipeline"] > 0.0
    print("\n[BENCHMARK] Bang On Pro 4-Cam E2E Timings (sec):", json.dumps(state.stage_timings_sec, indent=2))
    print("[BENCHMARK] Recovered GCC-PHAT Offsets (sec):", json.dumps(state.sync_result.offsets_sec, indent=2))


# ---------------------------------------------------------------------------
# 5. RNNoise Adapter, Alternate Show Profiles, CLI & Experimental Vision Tests
# ---------------------------------------------------------------------------


def test_rnnoise_adapter_switches_to_arnndn_when_model_exists(tmp_path: Path) -> None:
    fake_model = tmp_path / "sh.rnnn"
    fake_model.write_bytes(b"rnnoise-weights-placeholder")
    filters, ffmpeg_chain = build_audio_restoration_chain(
        {"target_lufs": -16.0, "true_peak_dbtp": -1.5, "enable_denoise": True, "enable_compressor": True},
        rnnoise_model_path=str(fake_model),
    )
    services = [f.mlt_service for f in filters]
    assert "avfilter.arnndn" in services
    assert "avfilter.afftdn" not in services
    assert "arnndn=m=" in ffmpeg_chain


def test_alternate_show_profiles_daily_news_and_gameshow(
    synthetic_4cam_footage: dict[str, Path],
    tmp_path: Path,
) -> None:
    # Daily News (2 cameras: cam_desk + cam_broll)
    news_state, _ = run_production_pipeline(
        inputs={
            "cam_desk": synthetic_4cam_footage["cam_host"],
            "cam_broll": synthetic_4cam_footage["cam_wide"],
        },
        output_dir=tmp_path / "daily_news_out",
        profile_ref="daily_news",
        instruction="Trim setup delays and assemble daily news broadcast cut with captions",
        export_video=False,
    )
    assert news_state.profile["profile_id"] == "daily_news"
    assert Path(news_state.artifacts["kdenlive_project"]).is_file()
    assert Path(news_state.artifacts["otio_timeline"]).is_file()
    assert news_state.qc_report.passed is True

    # Gameshow Multicam (4 cameras: wide, host, panel, reaction)
    game_state, _ = run_production_pipeline(
        inputs=[
            synthetic_4cam_footage["cam_wide"],
            synthetic_4cam_footage["cam_host"],
            synthetic_4cam_footage["cam_guest"],
            synthetic_4cam_footage["cam_reaction"],
        ],
        output_dir=tmp_path / "gameshow_out",
        profile_ref="gameshow_multicam",
        instruction="Sync gameshow cameras and cut fast reactions",
        export_video=False,
    )
    assert game_state.profile["profile_id"] == "gameshow_multicam"
    assert len(game_state.rough_cut_shots) >= 2
    assert game_state.qc_report.passed is True


def test_cli_list_profiles_and_dry_run(
    synthetic_4cam_footage: dict[str, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc_list = cli_main(["--list-profiles", "--json"])
    assert rc_list == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) >= 4

    out_dir = tmp_path / "cli_dry_run"
    rc_run = cli_main(
        [
            "Sync all four cameras, remove setup delays, build a first cut and add our standard graphics",
            "--profile",
            "sports_discussion",
            "--inputs",
            f"cam_desk={synthetic_4cam_footage['cam_wide']}",
            f"cam_host={synthetic_4cam_footage['cam_host']}",
            f"cam_guest={synthetic_4cam_footage['cam_guest']}",
            f"cam_panel={synthetic_4cam_footage['cam_reaction']}",
            "--output-dir",
            str(out_dir),
            "--dry-run",
            "--json",
        ]
    )
    assert rc_run == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["profile_id"] == "sports_discussion"
    assert summary["dry_run"] is True
    assert summary["shot_count"] >= 2
    assert "rendered_video" not in summary["artifacts"]


def test_experimental_vision_service_isolated_cpu_fallback(
    synthetic_4cam_footage: dict[str, Path],
    tmp_path: Path,
) -> None:
    svc = ExperimentalVisionService(prefer_sam2=True)
    assert svc.EXPERIMENTAL is True
    assert svc.STABILITY_TIER == "experimental"

    sample_video = synthetic_4cam_footage["cam_wide"]

    track_res = svc.track_object_bbox(sample_video, (40, 40, 120, 80), max_frames=8)
    assert track_res.experimental is True
    assert track_res.warning == EXPERIMENTAL_VISION_WARNING
    assert track_res.success is True
    assert len(track_res.tracks) == 8
    assert track_res.command_batch is not None
    assert track_res.command_batch["actor"]["requires_approval"] is True
    validate_timeline_command_batch(track_res.command_batch)

    mask_res = svc.generate_foreground_mask(sample_video, tmp_path / "exp_mask.png")
    assert mask_res.success is True
    assert Path(mask_res.output_artifacts["mask_png"]).is_file()

    inpaint_res = svc.inpaint_region(sample_video, (40, 40, 80, 40), tmp_path / "exp_inpaint.png")
    assert inpaint_res.success is True
    assert Path(inpaint_res.output_artifacts["inpainted_png"]).is_file()

    # Isolation guarantee: missing video returns error envelope rather than raising
    bad_res = svc.track_object_bbox(tmp_path / "does_not_exist.mp4", (0, 0, 10, 10))
    assert bad_res.success is False
    assert bad_res.error is not None


def test_max_shot_sec_and_periodic_reaction_cutaways(tmp_path: Path) -> None:
    """Verify Show Profile min_shot_sec / max_shot_sec inserts reaction/wide cutaways on long monologues."""
    from bangonpro.ai_engine.agents.rough_cut_multicam import _plan_shots_for_kept_interval
    from bangonpro.ai_engine.state import MediaAsset, TimeInterval, TranscriptSegment

    profile = load_show_profile("gameshow_multicam")  # min=1.2s, max=6.5s, reaction_interval=3.5s
    state = ProjectAnalysisState(
        project_id="proj_pace",
        sequence_id="seq_pace",
        profile=profile,
        output_dir=str(tmp_path / "pace"),
    )
    for cam in profile["cameras"]:
        cid = cam["camera_id"]
        state.media_assets[cid] = MediaAsset(
            media_ref=f"media_{cid}",
            camera_id=cid,
            camera_role=cam["role"],
            speaker_label=cam.get("speaker_label"),
            file_path=f"/fake/{cid}.mp4",
            sha256="0" * 64,
            file_size_bytes=1024,
            duration_sec=20.0,
            width=1920,
            height=1080,
            fps_num=25,
            fps_den=1,
            video_codec="h264",
            audio_codec="aac",
            audio_sample_rate=48000,
            audio_channels=2,
            priority=cam.get("priority", 1),
        )

    # Single 15-second uninterrupted Host monologue exceeding max_shot_sec (6.5s)
    state.transcript_segments = [
        TranscriptSegment(
            segment_id="seg_mono",
            start_sec=0.0,
            end_sec=15.0,
            speaker_id="Host",
            camera_id="cam_host",
            text="Host long studio introduction",
            confidence=0.95,
            is_low_confidence=False,
            energy_db=-18.0,
        )
    ]

    shots = _plan_shots_for_kept_interval(
        TimeInterval(start_sec=0.0, end_sec=15.0, label="kept_1"),
        state=state,
        wide_cam="cam_wide",
        reaction_cam="cam_reaction",
        speaker_to_cam={"Host": "cam_host"},
        applause_times=[],
        prev_end_camera=None,
    )

    assert len(shots) >= 4
    cameras_used = {s["camera_id"] for s in shots}
    assert "cam_host" in cameras_used
    assert ("cam_reaction" in cameras_used) or ("cam_wide" in cameras_used)
    for s in shots:
        dur = s["end"] - s["start"]
        assert dur >= profile["editing_rules"]["min_shot_sec"] - 0.05
        assert dur <= profile["editing_rules"]["max_shot_sec"] + 0.05

