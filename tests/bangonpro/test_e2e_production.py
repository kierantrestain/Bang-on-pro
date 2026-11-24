"""Production End-to-End Acceptance Suite for Bang On Pro v1.0.0.

Tests complete real-world production scenarios beyond isolated unit tests:
1. Raw multi-camera mixed-FPS footage (25 fps + 30 fps) with inconsistent microphone
   levels, overlapping speech, dead-air interruption, and a missing-camera entry ->
   full AI production pipeline -> verifies ``.kdenlive``, ``.otio``, ``transcript.json``,
   real ``melt`` MP4 export + ``ffprobe`` stream checks, and byte-exact SHA-256 source
   immutability.
2. Editable transcript workflow: edit text inside ``transcript.json`` ->
   ``build_transcript_edit_command_batch`` -> schema-validated Timeline Command API v1 batch.
3. Custom production Show Profile creation, persistence, reload, and pipeline execution.
4. Local-first AI provider registry + strict ``ExternalConsentError`` enforcement when
   external providers are requested without explicit user consent.
5. Built-in 60+ legal native Premiere-equivalent starter preset library covering all 58+
   effect families + real visual before/after preview PNG generation + explicit missing-input
   audit report when proprietary ``effects pr.zip`` is absent.
6. Compiled native graphical binary launch check (``--version``) when built binary is present.

SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import wave
from pathlib import Path

import defusedxml.ElementTree as SafeET
import numpy as np
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bangonpro.ai_engine import (
    run_production_pipeline,
    validate_timeline_command_batch,
)
from bangonpro.ai_engine.agents.ingest_sync import compute_file_sha256
from bangonpro.ai_engine.providers import (
    AiProviderRegistry,
    ExternalConsentError,
    require_external_consent,
)
from bangonpro.ai_engine.transcript import (
    build_transcript_edit_command_batch,
    edit_segment_text,
    load_transcript,
)
from bangonpro.premiere_compat.builtin_library import build_builtin_starter_report
from bangonpro.premiere_compat.cli import main as premiere_cli_main
from bangonpro.premiere_compat.preview_generator import generate_preset_previews
from bangonpro.show_profiles import (
    create_custom_profile,
    load_show_profile,
    save_custom_profile,
)


def _write_wav(path: Path, samples: np.ndarray, sr: int = 48000) -> None:
    clipped = np.clip(samples, -0.98, 0.98)
    pcm16 = (clipped * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm16.tobytes())


def _generate_mixed_fps_unbalanced_studio(base_dir: Path) -> dict[str, Path]:
    """Generate 3 real MP4 cameras with mixed 25/30fps, loud vs quiet mics, overlap & gap."""
    media_dir = base_dir / "e2e_media"
    media_dir.mkdir(parents=True, exist_ok=True)
    sr = 48000
    dur = 9.0
    n = int(dur * sr)
    rng = np.random.default_rng(42)

    host = np.zeros(n, dtype=np.float64)
    guest = np.zeros(n, dtype=np.float64)

    def _voice(length_s: float, f0: float) -> np.ndarray:
        m = int(length_s * sr)
        tt = np.arange(m, dtype=np.float64) / float(sr)
        return 0.5 * np.sin(2.0 * np.pi * f0 * tt) + 0.25 * rng.standard_normal(m)

    def _place(buf: np.ndarray, start_s: float, sig: np.ndarray) -> None:
        i0 = int(round(start_s * sr))
        i1 = min(buf.size, i0 + sig.size)
        buf[i0:i1] = sig[: i1 - i0]

    # 0.5-3.0s Host speaks; 3.0-5.0s dead air; 5.0-6.8s Guest speaks; 6.8-8.2s overlap
    _place(host, 0.5, _voice(2.5, 145.0))
    _place(guest, 5.0, _voice(1.8, 220.0))
    _place(host, 6.8, 0.7 * _voice(1.4, 150.0))
    _place(guest, 6.8, 0.7 * _voice(1.4, 215.0))

    specs = {
        "cam_wide": (25, 0.35 * host + 0.35 * guest, "#1B3B6F"),
        # Deliberately inconsistent mic gain (cam_host hot, cam_guest quiet)
        "cam_host": (25, 0.90 * host + 0.08 * guest, "#9B2226"),
        "cam_guest": (30, 0.05 * host + 0.22 * guest, "#2D6A4F"),  # Mixed 30fps!
    }
    paths: dict[str, Path] = {}
    for cid, (fps, sig, color) in specs.items():
        wav_p = media_dir / f"{cid}.wav"
        mp4_p = media_dir / f"{cid}.mp4"
        _write_wav(wav_p, sig, sr)
        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=320x180:r={fps}:d={dur}",
                "-i",
                str(wav_p),
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-shortest",
                str(mp4_p),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr[-400:]
        paths[cid] = mp4_p
    return paths


def test_e2e_production_workflow_immutability_transcript_and_custom_profile(tmp_path: Path) -> None:
    footage = _generate_mixed_fps_unbalanced_studio(tmp_path)
    hashes_before = {k: compute_file_sha256(p) for k, p in footage.items()}

    # 1. Create & persist a custom production profile
    custom = create_custom_profile(
        profile_id="bangon_live_desk",
        display_name="Bang On Live Desk",
        base_profile="podcast_multicam",
        editing_rules={
            "remove_silence": True,
            "silence_threshold_db": -38.0,
            "min_silence_sec": 0.9,
            "min_shot_sec": 1.2,
            "max_shot_sec": 6.0,
        },
    )
    custom_path = save_custom_profile(custom, tmp_path / "custom_profiles" / "bangon_live_desk.json")
    reloaded = load_show_profile(custom_path)
    assert reloaded["profile_id"] == "bangon_live_desk"

    # 2. Enforce consent gate on external providers while allowing local default
    reg = AiProviderRegistry()
    providers = reg.list_providers()
    assert any(p.kind == "local" and not p.requires_network for p in providers)
    ext_provider = next(p for p in providers if p.requires_network)
    with pytest.raises(ExternalConsentError):
        require_external_consent(
            ext_provider.provider_id,
            consent_granted=False,
            payload_summary="audio transcript upload test",
            registry=reg,
        )

    # 3. Run full end-to-end production pipeline including missing-camera resilience + real melt export
    media_with_missing = dict(footage)
    media_with_missing["cam_reaction"] = tmp_path / "nonexistent_cam_reaction.mp4"

    out_dir = tmp_path / "e2e_output"
    state, _orch = run_production_pipeline(
        inputs=media_with_missing,
        profile_ref=custom_path,
        instruction="Assemble balanced multicam rough cut, trim dead air, captions, and master audio",
        output_dir=out_dir,
        dry_run=False,
        export_video=True,
    )

    # Verify core artifacts exist & project XML re-opens cleanly via hardened parser
    kdenlive_path = Path(state.artifacts["kdenlive_project"])
    otio_path = Path(state.artifacts["otio_timeline"])
    transcript_path = Path(state.artifacts["transcript_json"])
    rendered_mp4 = Path(state.artifacts["rendered_video"])

    assert kdenlive_path.is_file()
    assert otio_path.is_file()
    assert transcript_path.is_file()
    assert rendered_mp4.is_file() and rendered_mp4.stat().st_size > 2000

    # Parse saved .kdenlive XML to confirm reopen integrity (MLT 7 uses <producer> and/or <chain>)
    mlt_root = SafeET.parse(kdenlive_path).getroot()
    assert mlt_root.tag == "mlt"
    media_nodes = mlt_root.findall(".//producer") + mlt_root.findall(".//chain")
    assert len(media_nodes) >= 3

    # Verify ffprobe reports valid video + audio streams on exported MP4
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(rendered_mp4)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert probe.returncode == 0
    streams = json.loads(probe.stdout)["streams"]
    codec_types = {s["codec_type"] for s in streams}
    assert {"video", "audio"}.issubset(codec_types)

    # Verify source media byte-for-byte immutability
    hashes_after = {k: compute_file_sha256(p) for k, p in footage.items()}
    assert hashes_after == hashes_before

    # Verify editable transcript -> Timeline Command API v1 batch round-trip
    orig_transcript = load_transcript(transcript_path)
    assert len(orig_transcript["segments"]) >= 1
    first_seg_id = orig_transcript["segments"][0]["segment_id"]
    edited_transcript = edit_segment_text(orig_transcript, first_seg_id, "Edited Broadcast Caption Line")
    edit_batch = build_transcript_edit_command_batch(
        orig_transcript,
        edited_transcript,
        project_id=state.project_id,
        sequence_id=state.sequence_id,
        expected_revision=1,
    )
    validate_timeline_command_batch(edit_batch)
    assert any(op["type"] == "update_caption" for op in edit_batch["operations"])


def test_builtin_premiere_library_58_families_preview_and_missing_input_audit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = build_builtin_starter_report()
    summary = report.summary()
    assert summary["total_presets"] >= 60
    assert summary["distinct_effects"] >= 58

    # Generate real visual before/after split preview PNG for a native starter preset
    first_video = next(p for p in report.presets if p.mlt_filters and any(f.media_type == "video" for f in p.mlt_filters))
    previews = generate_preset_previews(first_video, tmp_path / "preview_cache", width=240, height=135)
    assert previews is not None
    assert Path(previews.split_path).is_file() and Path(previews.split_path).stat().st_size > 500

    # Audit CLI explicitly reports missing effects pr.zip / audit json without fabricating 578 entries
    audit_out = tmp_path / "audit_out" / "missing_audit.json"
    rc = premiere_cli_main(
        [
            "audit",
            "--input",
            str(tmp_path / "effects pr.zip"),
            "--audit-json",
            str(tmp_path / "Bang_on_pro_premiere_preset_audit.json"),
            "--output",
            str(audit_out),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "missing_source_inputs"
    assert audit_out.is_file()
    assert "effects pr.zip" in json.dumps(payload)


def test_compiled_binary_launch_smoke_if_present() -> None:
    candidates = [
        os.environ.get("BANGONPRO_BINARY", ""),
        str(_REPO_ROOT / "install-prefix" / "bin" / "kdenlive"),
        str(_REPO_ROOT / "build" / "bin" / "kdenlive"),
    ]
    binary = next((Path(c) for c in candidates if c and Path(c).is_file()), None)
    if binary is None:
        pytest.skip("Compiled Bang On Pro binary not present in this Python-only test job")

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    proc = subprocess.run([str(binary), "--version"], capture_output=True, text=True, env=env, timeout=20, check=False)
    assert proc.returncode == 0, proc.stderr
    assert "1.0.0" in proc.stdout or "kdenlive" in proc.stdout.lower()
