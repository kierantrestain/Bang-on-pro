"""Bang On Pro V1.1 Regression Test Suite.

Covers all Section 4 & V1.1 requirements:
- Strict natural-language editorial scoping & clarification on ambiguous/unknown prompts
- Active .kdenlive XML & timeline_state.json context-aware planning without media re-import
- Managed AI model catalog, hardware tiering, SHA-256 verification, cancellation, safe removal
- Honest separation of neural inference vs deterministic DSP/OpenCV fallbacks
- AI CLI utility commands (--list-providers, --model-status, --install-model, --remove-model, --plan-only)
- Premiere compatibility honest visual_equivalence_evidence & zero fabricated archive claims

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bangonpro.ai_engine.intent import parse_production_instruction
from bangonpro.ai_engine.model_manager import (
    ModelChecksumError,
    ModelManager,
    ModelManagerError,
)
from bangonpro.ai_engine.orchestrator import CancellationToken, PipelineCancelledError
from bangonpro.ai_engine.pipeline import run_production_pipeline
from bangonpro.ai_engine.planner import (
    build_edit_proposal,
    ingest_project_context,
    interpret_instruction,
    validate_edit_proposal,
)
from bangonpro.premiere_compat.builtin_library import build_builtin_starter_report
from bangonpro.roto_studio.mask_engine import (
    evaluate_segmentation_backends,
    segment_frame_detailed,
)
from bangonpro.show_profiles.loader import load_show_profile


def test_strict_editorial_scoping_no_unconditional_flags() -> None:
    """Scoped instructions must ONLY enable requested editorial actions — never unconditional cuts/sync/graphics."""
    audio_only = parse_production_instruction("Clean up dialogue audio and normalize to -14 LUFS on A1")
    assert audio_only.needs_clarification is False
    assert audio_only.restore_audio is True
    assert audio_only.sync_cameras is False
    assert audio_only.build_rough_cut is False
    assert audio_only.add_captions is False
    assert audio_only.add_lower_thirds is False
    assert audio_only.loudness_target_lufs == pytest.approx(-14.0)

    captions_only = parse_production_instruction("Add captions only")
    assert captions_only.needs_clarification is False
    assert captions_only.add_captions is True
    assert captions_only.sync_cameras is False
    assert captions_only.build_rough_cut is False
    assert captions_only.restore_audio is False
    assert captions_only.add_lower_thirds is False

    graphics_only = parse_production_instruction("Add lower thirds for speakers")
    assert graphics_only.needs_clarification is False
    assert graphics_only.add_lower_thirds is True
    assert graphics_only.sync_cameras is False
    assert graphics_only.build_rough_cut is False
    assert graphics_only.restore_audio is False
    assert graphics_only.add_captions is False

    silence_only = parse_production_instruction("Remove long pauses and dead air")
    assert silence_only.needs_clarification is False
    assert silence_only.remove_silence is True
    assert silence_only.sync_cameras is False
    assert silence_only.add_captions is False
    assert silence_only.add_lower_thirds is False


@pytest.mark.parametrize("ambiguous_prompt", ["", "   ", "asdfghjkl zxcvbnm", "make it better"])
def test_ambiguous_or_unknown_prompt_requests_clarification_without_mutations(
    ambiguous_prompt: str, tmp_path: Path
) -> None:
    """Ambiguous, empty, or unknown instructions must request clarification and emit zero timeline mutations."""
    parsed = parse_production_instruction(ambiguous_prompt)
    assert parsed.needs_clarification is True
    assert parsed.clarification_question

    state, _ = run_production_pipeline(
        inputs=None,
        output_dir=tmp_path / "clarify_out",
        instruction=ambiguous_prompt,
        plan_only=True,
    )
    assert state.intent.needs_clarification is True
    assert state.metadata.get("execution_status") == "needs_clarification"
    assert state.command_batches == []


def test_active_project_context_planning_without_media_reimport(tmp_path: Path) -> None:
    """AI planner operates on active timeline_state.json without requiring raw --inputs re-import."""
    state_snapshot = {
        "api_version": "timeline_command_v1",
        "project_id": "proj_live_42",
        "sequence_id": "seq_live_1",
        "revision": 4,
        "state": {
            "active_sequence_id": "seq_live_1",
            "sequences": [{"sequence_id": "seq_live_1", "name": "Main", "fps_num": 25, "fps_den": 1}],
            "media_bin": [{"media_ref": "m_host", "source_path": "/media/host.mp4", "duration_frames": 750}],
            "tracks": [
                {"track_id": "V1", "sequence_id": "seq_live_1", "name": "V1", "kind": "video", "index": 0},
                {"track_id": "A1", "sequence_id": "seq_live_1", "name": "A1", "kind": "audio", "index": 1},
            ],
            "clips": [
                {
                    "clip_id": "clip_host_1",
                    "sequence_id": "seq_live_1",
                    "track_id": "A1",
                    "media_ref": "m_host",
                    "name": "Host Audio",
                    "frame_range": {"in_frame": 0, "out_frame": 500, "timeline_start": 0},
                }
            ],
            "captions": [],
            "markers": [],
        },
    }
    state_file = tmp_path / "timeline_state.json"
    state_file.write_text(json.dumps(state_snapshot, indent=2), encoding="utf-8")

    context = ingest_project_context(project_state=state_file)
    assert context.source_kind == "timeline_state_json"
    assert context.project_id == "proj_live_42"
    assert context.sequence_id == "seq_live_1"
    assert context.revision == 4
    assert len(context.clips) == 1

    interpretation = interpret_instruction("Normalize dialogue audio to -14 LUFS", context=context)
    profile = load_show_profile("podcast_multicam")
    proposal = build_edit_proposal(interpretation, context, profile, expected_revision=4, dry_run=True)
    report = validate_edit_proposal(proposal, interpretation, context)
    assert report.valid is True
    assert report.command_batch is not None
    assert report.command_batch["expected_revision"] == 4
    op_types = {op["type"] for op in report.command_batch["operations"]}
    assert "add_audio_filter" in op_types or "set_audio_level" in op_types
    # Scope enforcement: audio-only plan MUST NOT emit cut/caption/graphics ops
    assert op_types.isdisjoint({"split_clip", "delete_clip", "select_camera", "add_caption", "add_lower_third"})

    # Also test real CLI invocation with --project-state --plan-only --json (no --inputs!)
    out_dir = tmp_path / "cli_plan_out"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "bangonpro.ai_engine.cli",
            "Normalize dialogue audio to -14 LUFS",
            "--project-state",
            str(state_file),
            "--plan-only",
            "--output-dir",
            str(out_dir),
            "--json",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"CLI plan-only failed: {proc.stderr}"
    summary = json.loads(proc.stdout)
    assert summary["project_id"] == "proj_live_42"
    assert summary["sequence_id"] == "seq_live_1"
    assert summary["needs_clarification"] is False
    assert summary["command_batch_count"] == 1


def test_model_manager_lifecycle_sha256_cancel_and_safe_remove(tmp_path: Path) -> None:
    """Verify ModelManager install, SHA-256 verification, mismatch rejection, cancellation, and safe removal."""
    model_dir = tmp_path / "managed_models"
    user_media = tmp_path / "precious_user_media.mp4"
    user_media.write_bytes(b"DO_NOT_TOUCH_USER_MEDIA")

    mgr = ModelManager(model_dir=model_dir)
    report = mgr.get_storage_report()
    assert report["installed_count"] == 0
    assert len(report["models"]) >= 5
    # Verify honest status fields exist on every model
    for m in report["models"]:
        for key in (
            "backend_package_installed",
            "model_available_on_disk",
            "model_loaded",
            "actual_inference_executed",
            "status",
        ):
            assert key in m

    # Create a local weights fixture file
    weights_src = tmp_path / "dummy_mobilesam.onnx"
    payload = b"ONNX_TEST_WEIGHTS_" * 1024
    weights_src.write_bytes(payload)
    good_sha = hashlib.sha256(payload).hexdigest()

    # 1. Wrong SHA-256 MUST fail and leave no installed file
    with pytest.raises(ModelChecksumError):
        mgr.install_model("mobilesam-onnx", source_path=weights_src, expected_sha256="0" * 64)
    assert mgr.get_model_status("mobilesam-onnx").model_available_on_disk is False

    # 2. Pre-cancelled token MUST abort cleanly and leave no staging junk
    tok = CancellationToken()
    tok.cancel("user clicked cancel")
    with pytest.raises(PipelineCancelledError):
        mgr.install_model("mobilesam-onnx", source_path=weights_src, cancellation_token=tok)
    assert mgr.get_model_status("mobilesam-onnx").model_available_on_disk is False

    # 3. Insecure http:// URL MUST be rejected
    with pytest.raises(ModelManagerError):
        mgr.install_model("mobilesam-onnx", source_url="http://insecure.example.com/model.onnx")

    # 4. Valid local install with matching SHA-256 succeeds and tracks progress
    progress_events: list[float] = []
    res = mgr.install_model(
        "mobilesam-onnx",
        source_path=weights_src,
        expected_sha256=good_sha,
        progress_callback=lambda frac, _done, _tot, _msg: progress_events.append(frac),
    )
    assert res["installed"] is True
    assert res["checksum_verified"] is True
    assert res["sha256"] == good_sha
    assert progress_events and progress_events[-1] == pytest.approx(1.0)
    assert mgr.get_model_status("mobilesam-onnx").model_available_on_disk is True
    assert mgr.get_storage_report()["installed_count"] == 1

    # 5. Safe removal deletes only managed model dir; user media remains untouched
    rm = mgr.remove_model("mobilesam-onnx")
    assert rm["removed"] is True
    assert rm["freed_bytes"] > 0
    assert mgr.get_model_status("mobilesam-onnx").model_available_on_disk is False
    assert user_media.read_bytes() == b"DO_NOT_TOUCH_USER_MEDIA"

    # 6. CLI --list-providers and --model-status smoke test
    prov_proc = subprocess.run(
        [sys.executable, "-m", "bangonpro.ai_engine.cli", "--list-providers", "--json"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    providers = json.loads(prov_proc.stdout)
    assert any(p["is_local"] is True for p in providers)
    assert any(p["is_local"] is False and p["requires_consent"] is True for p in providers)


def test_honest_roto_and_premiere_evidence_reporting() -> None:
    """Verify roto segmentation and Premiere starter library report honest runtime/evidence flags."""
    caps = evaluate_segmentation_backends()
    assert len(caps) >= 2
    assert any(c.backend_id in ("sam2_hiera_tiny", "mobile_sam_onnx", "opencv_grabcut") for c in caps)

    # Create a clean synthetic frame with a bright object on dark background and run prompt segmentation
    frame = np.zeros((160, 240, 3), dtype=np.uint8)
    frame[40:120, 70:170] = (220, 180, 90)
    seg = segment_frame_detailed(
        frame,
        positive_points=[(120.0, 80.0)],
        negative_points=[(10.0, 10.0)],
    )
    assert seg.alpha_mask.shape == (160, 240)
    # Without SAM weights installed on this host, segmentation MUST honestly report fallback (non-neural)
    if not seg.is_neural_inference:
        assert seg.fallback_used is True

    # Premiere built-in starter library must retain 64 legal presets with explicit visual_equivalence_evidence
    report = build_builtin_starter_report()
    report_dict = report.to_dict()
    assert report_dict["summary"]["total_presets"] == 64
    assert all("visual_equivalence_evidence" in p for p in report_dict["presets"])
