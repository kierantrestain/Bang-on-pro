"""Verification tests for Bang On Pro Apple Silicon macOS performance toolkit.

Covers:
1. Runtime hardware probing & honest Metal/MLT assessment (no fake Metal claims).
2. FFmpeg/MLT VideoToolbox profile generation + deterministic CPU fallback.
3. Automatic proxy policy engine & real FFmpeg proxy transcoding + ffprobe verification.
4. Low-storage LRU cache & AI model budget manager (verifying source media and
   .kdenlive project files are NEVER touched or deleted).
5. Consent-gated, HTTPS/TLS-enforced remote GPU offload protocol & metadata scrubbing.
6. Real executable FFmpeg + MLT (melt) benchmark runner with host-architecture tagging.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import inspect
import json
import os
import ssl
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from bangonpro.macos.benchmark import (
    _build_host_environment,
    get_peak_rss_mb,
    run_comprehensive_benchmarks,
    run_full_benchmark,
    run_sustained_benchmarks,
)
from bangonpro.macos.hardware_profiles import (
    HardwareCapabilities,
    assess_metal_mlt_support,
    classify_host_tier,
    generate_decode_args,
    generate_export_profile,
    list_profile_names,
    probe_hardware_capabilities,
    recommend_model_quantization,
    validate_profile_against_capabilities,
)
from bangonpro.macos.proxy_cache_manager import (
    CacheBudgetManager,
    MediaProbeInfo,
    ProxyCancelledError,
    ProxyPolicy,
    StorageAndModelManager,
    StorageBudgetConfig,
    evaluate_proxy_policy,
    generate_proxy,
    probe_media,
)
from bangonpro.macos.remote_gpu_offload import (
    ConsentExpiredError,
    ConsentRequiredError,
    InsecureTransportError,
    OffloadJobRequest,
    RawMediaUploadDeniedError,
    RemoteGpuEndpointConfig,
    RemoteGpuOffloadClient,
    RemoteOffloadConsent,
    build_scrubbed_manifest,
    scrub_metadata_dict,
)


def _simulated_apple_silicon_caps() -> HardwareCapabilities:
    return HardwareCapabilities(
        platform_system="darwin",
        machine="arm64",
        is_apple_silicon=True,
        cpu_brand="Apple M3 Max",
        cpu_cores_logical=16,
        total_ram_gb=64.0,
        free_disk_gb=250.0,
        ffmpeg_path="/opt/homebrew/bin/ffmpeg",
        ffmpeg_version="ffmpeg version 6.1",
        ffprobe_path="/opt/homebrew/bin/ffprobe",
        melt_path="/opt/homebrew/bin/melt",
        melt_version="melt 7.22.0",
        ffmpeg_hwaccels=["videotoolbox"],
        ffmpeg_encoders=[
            "aac",
            "aac_at",
            "h264_videotoolbox",
            "hevc_videotoolbox",
            "libx264",
            "libx265",
            "pcm_s16le",
            "prores_ks",
            "prores_videotoolbox",
        ],
        mlt_filters=["lift_gamma_gain", "boxblur", "volume"],
        videotoolbox_available=True,
        metal_assessment=assess_metal_mlt_support(
            is_apple_silicon=True,
            videotoolbox_available=True,
            mlt_filters=["lift_gamma_gain", "boxblur", "volume"],
        ),
    )


def _create_synthetic_video(
    dest: Path,
    *,
    width: int = 1280,
    height: int = 720,
    fps: int = 30,
    duration_sec: float = 1.0,
) -> Path:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"testsrc2=size={width}x{height}:rate={fps}:duration={duration_sec}",
        "-f", "lavfi",
        "-i", f"sine=frequency=440:sample_rate=48000:duration={duration_sec}",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, f"ffmpeg failed: {proc.stderr}"
    assert dest.is_file()
    return dest


# ---------------------------------------------------------------------------
# 1. Hardware Probing & VideoToolbox / CPU Fallback Profile Generation
# ---------------------------------------------------------------------------


def test_runtime_hardware_probe_and_honest_metal_assessment() -> None:
    caps = probe_hardware_capabilities()
    assert caps.platform_system in ("linux", "darwin", "windows")
    assert caps.cpu_cores_logical >= 1
    assert caps.total_ram_gb > 0.0
    assert caps.free_disk_gb > 0.0
    assert caps.ffmpeg_path is not None
    assert caps.ffprobe_path is not None
    assert caps.melt_path is not None
    assert "libx264" in caps.ffmpeg_encoders
    assert "prores_ks" in caps.ffmpeg_encoders

    assert caps.metal_assessment is not None
    # Honest engineering check: MLT 7.x does NOT have native Metal filters
    assert caps.metal_assessment.metal_mlt_native_supported is False
    assert len(caps.metal_assessment.stability_constraints) >= 3
    assert "OpenGL" in " ".join(caps.metal_assessment.stability_constraints)

    if caps.platform_system == "linux":
        assert caps.is_apple_silicon is False
        assert caps.videotoolbox_available is False
        assert caps.metal_assessment.recommended_mlt_backend == "cpu_sliced_ci_fallback"


def test_apple_silicon_videotoolbox_profiles_and_prores() -> None:
    mac_caps = _simulated_apple_silicon_caps()
    assert generate_decode_args(mac_caps) == ["-hwaccel", "videotoolbox"]
    assert mac_caps.metal_assessment is not None
    assert mac_caps.metal_assessment.metal_mlt_native_supported is False
    assert mac_caps.metal_assessment.recommended_mlt_backend == "cpu_neon_sliced_with_videotoolbox_io"

    h264 = generate_export_profile("h264_master", capabilities=mac_caps)
    assert h264.video_codec == "h264_videotoolbox"
    assert h264.audio_codec == "aac_at"
    assert h264.hardware_accelerated is True
    assert h264.fallback_used is False
    assert "-hwaccel" in h264.ffmpeg_input_args
    assert "h264_videotoolbox" in h264.ffmpeg_output_args
    assert h264.mlt_consumer_properties["vcodec"] == "h264_videotoolbox"
    ok, issues = validate_profile_against_capabilities(h264, mac_caps)
    assert ok, issues

    hevc = generate_export_profile("hevc_master", capabilities=mac_caps)
    assert hevc.video_codec == "hevc_videotoolbox"
    assert hevc.hardware_accelerated is True
    assert "hvc1" in hevc.ffmpeg_output_args
    ok, issues = validate_profile_against_capabilities(hevc, mac_caps)
    assert ok, issues

    proxy = generate_export_profile("edit_proxy_fast", capabilities=mac_caps)
    assert proxy.video_codec == "h264_videotoolbox"
    assert proxy.hardware_accelerated is True
    assert "-g" in proxy.ffmpeg_output_args

    for name, expected_level in (
        ("prores_422_proxy", "0"),
        ("prores_422", "2"),
        ("prores_422_hq", "3"),
    ):
        pr = generate_export_profile(name, capabilities=mac_caps)
        assert pr.video_codec == "prores_videotoolbox"
        assert pr.hardware_accelerated is True
        assert pr.accelerator == "videotoolbox"
        assert pr.fallback_used is False
        assert pr.container == "mov"
        assert pr.audio_codec == "pcm_s16le"
        assert pr.pixel_format == "yuv422p10le"
        assert expected_level in pr.ffmpeg_output_args
        assert pr.mlt_consumer_properties["vcodec"] == "prores_videotoolbox"
        assert pr.mlt_consumer_properties["vprofile"] == expected_level
        ok, issues = validate_profile_against_capabilities(pr, mac_caps)
        assert ok, issues


def test_deterministic_cpu_fallback_on_non_videotoolbox_host() -> None:
    caps = probe_hardware_capabilities()
    if caps.videotoolbox_available:
        pytest.skip("Host has VideoToolbox; testing non-VideoToolbox fallback via simulated Linux caps")

    for profile_name in list_profile_names():
        prof = generate_export_profile(profile_name, capabilities=caps)
        ok, issues = validate_profile_against_capabilities(prof, caps)
        assert ok, f"Profile {profile_name} failed validation on host: {issues}"

    h264 = generate_export_profile("h264_master", capabilities=caps)
    assert h264.video_codec == "libx264"
    assert h264.hardware_accelerated is False
    assert h264.fallback_used is True
    assert h264.fallback_reason is not None

    hevc = generate_export_profile("hevc_master", capabilities=caps)
    assert hevc.video_codec == "libx265"
    assert hevc.fallback_used is True

    proxy = generate_export_profile("edit_proxy_fast", capabilities=caps)
    assert proxy.video_codec == "libx264"
    assert "ultrafast" in proxy.ffmpeg_output_args

    with pytest.raises(ValueError, match="Unknown hardware profile"):
        generate_export_profile("nonexistent_profile", capabilities=caps)


# ---------------------------------------------------------------------------
# 2. Automatic Proxy Policy & Real FFmpeg Proxy Creation
# ---------------------------------------------------------------------------


def test_proxy_policy_decisions() -> None:
    # Clean 1080p 25fps H.264 low bitrate -> no proxy needed
    clean_1080 = MediaProbeInfo(
        path="/media/clean.mp4",
        width=1920,
        height=1080,
        codec_name="h264",
        pixel_format="yuv420p",
        fps=25.0,
        avg_fps=25.0,
        bit_rate_bps=12_000_000,
        duration_sec=120.0,
        has_audio=True,
        file_size_bytes=180_000_000,
    )
    dec_clean = evaluate_proxy_policy(clean_1080, timeline_fps=25.0)
    assert dec_clean.needs_proxy is False
    assert dec_clean.reasons == []

    # 4K HEVC 10-bit 59.94fps on 25fps timeline -> triggers multiple policy rules
    uhd_hevc = MediaProbeInfo(
        path="/media/cam_a_4k.mov",
        width=3840,
        height=2160,
        codec_name="hevc",
        pixel_format="yuv420p10le",
        fps=59.94,
        avg_fps=59.94,
        bit_rate_bps=95_000_000,
        duration_sec=600.0,
        has_audio=True,
        file_size_bytes=7_000_000_000,
    )
    dec_uhd = evaluate_proxy_policy(uhd_hevc, timeline_fps=25.0)
    assert dec_uhd.needs_proxy is True
    assert any(r.startswith("resolution_exceeds_threshold") for r in dec_uhd.reasons)
    assert any(r.startswith("high_bitrate") for r in dec_uhd.reasons)
    assert any(r.startswith("heavy_decode_codec") for r in dec_uhd.reasons)
    assert any(r.startswith("high_bit_depth_pix_fmt") for r in dec_uhd.reasons)
    assert any(r.startswith("mixed_fps_mismatch") for r in dec_uhd.reasons)
    assert dec_uhd.target_width == 1280
    assert dec_uhd.target_height == 720
    assert dec_uhd.target_fps == 25.0


def test_real_ffmpeg_proxy_creation_and_reuse(tmp_path: Path) -> None:
    source_dir = tmp_path / "source_footage"
    source_dir.mkdir()
    src_clip = _create_synthetic_video(source_dir / "cam_mixed_30fps.mp4", width=1920, height=1080, fps=30, duration_sec=1.0)

    cache_mgr = CacheBudgetManager(
        tmp_path / "bangonpro_cache",
        config=StorageBudgetConfig(max_cache_gb=1.0, max_models_gb=1.0, protected_dirs=(source_dir,)),
    )

    # 30fps source on 25fps timeline triggers mixed-FPS automatic proxy generation
    result = generate_proxy(
        src_clip,
        cache_mgr,
        timeline_fps=25.0,
        policy=ProxyPolicy(proxy_target_width=960, proxy_target_height=540),
    )
    assert result.created is True
    assert result.reused_cached is False
    assert result.decision.needs_proxy is True
    assert any("mixed_fps_mismatch" in r for r in result.decision.reasons)

    proxy_file = Path(result.proxy_path)
    assert proxy_file.is_file()
    assert proxy_file.parent == cache_mgr.proxies_dir
    assert result.proxy_probe is not None
    assert result.proxy_probe.width == 960
    assert result.proxy_probe.height == 540
    assert abs(result.proxy_probe.fps - 25.0) < 0.2

    # Second call must deterministically reuse the cached proxy
    reused = generate_proxy(
        src_clip,
        cache_mgr,
        timeline_fps=25.0,
        policy=ProxyPolicy(proxy_target_width=960, proxy_target_height=540),
    )
    assert reused.created is False
    assert reused.reused_cached is True
    assert reused.proxy_path == result.proxy_path

    # Source clip untouched
    assert src_clip.is_file()
    assert probe_media(src_clip).width == 1920


# ---------------------------------------------------------------------------
# 3. Low-Storage LRU Cache & Model Budget Manager (Safety Invariants)
# ---------------------------------------------------------------------------


def test_lru_cache_eviction_and_source_project_protection(tmp_path: Path) -> None:
    source_dir = tmp_path / "user_source_media"
    source_dir.mkdir()
    source_clip = source_dir / "interview_cam1.mov"
    source_clip.write_bytes(b"RAW_SOURCE_FOOTAGE" * 5000)

    project_file = tmp_path / "show_episode_01.kdenlive"
    project_file.write_text("<mlt><playlist/></mlt>", encoding="utf-8")

    # Configure tight cache quota: ~250 KB (in GB) and model quota: ~100 KB
    max_cache_bytes = 250 * 1024
    max_models_bytes = 100 * 1024
    cfg = StorageBudgetConfig(
        max_cache_gb=max_cache_bytes / (1024**3),
        max_models_gb=max_models_bytes / (1024**3),
        protected_dirs=(source_dir,),
    )
    mgr = CacheBudgetManager(tmp_path / "cache_root", config=cfg)

    # Create 4 temporary cache files of 100 KB each = 400 KB total (> 250 KB budget)
    p1 = mgr.proxies_dir / "proxy_oldest.mp4"
    p2 = mgr.proxies_dir / "proxy_pinned.mp4"
    p3 = mgr.render_cache_dir / "render_mid.bin"
    p4 = mgr.proxies_dir / "proxy_newest.mp4"
    for f in (p1, p2, p3, p4):
        f.write_bytes(b"X" * (100 * 1024))

    mgr.register_cache_entry(p1, category="proxies", pinned=False, last_access_ts=100.0)
    mgr.register_cache_entry(p2, category="proxies", pinned=True, last_access_ts=110.0)
    mgr.register_cache_entry(p3, category="render_cache", pinned=False, last_access_ts=200.0)
    mgr.register_cache_entry(p4, category="proxies", pinned=False, last_access_ts=300.0)

    # Place a .kdenlive project file AND a symlink to source media inside cache dirs to prove safety guard!
    stray_project = mgr.proxies_dir / "accidental_backup.kdenlive"
    stray_project.write_text("<mlt/>", encoding="utf-8")
    escape_symlink = mgr.proxies_dir / "evil_link.mov"
    os.symlink(source_clip, escape_symlink)

    # Registering protected/out-of-bounds paths must fail loudly
    with pytest.raises(ValueError):
        mgr.register_cache_entry(source_clip)
    with pytest.raises(ValueError):
        mgr.register_cache_entry(project_file)
    with pytest.raises(ValueError):
        mgr.register_cache_entry(stray_project)
    with pytest.raises(ValueError):
        mgr.register_cache_entry(escape_symlink)

    # Place an AI model exceeding max_models_bytes -> must be tracked, NEVER deleted by cache eviction
    model_bin = mgr.models_dir / "whisper_large_v3.bin"
    model_bin.write_bytes(b"M" * (150 * 1024))
    mgr.register_model_artifact(model_bin, "whisper-large-v3", pinned=False, last_access_ts=50.0)

    report = mgr.enforce_cache_budget()

    # Oldest unpinned p1 (100 KB) and next-oldest unpinned p3 (100 KB) should be evicted to get <= 250 KB
    assert str(p1.resolve()) in report.evicted_files
    assert str(p3.resolve()) in report.evicted_files
    assert not p1.exists()
    assert not p3.exists()

    # Pinned p2 and newest p4 MUST remain
    assert p2.exists()
    assert p4.exists()
    assert report.remaining_cache_bytes <= max_cache_bytes

    # Safety invariants: source footage, project files, symlink target, and AI model MUST be 100% untouched!
    assert source_clip.exists()
    assert source_clip.read_bytes() == b"RAW_SOURCE_FOOTAGE" * 5000
    assert project_file.exists()
    assert stray_project.exists()
    assert escape_symlink.is_symlink()
    assert model_bin.exists()
    assert report.models_over_budget_bytes > 0

    candidates = mgr.get_model_eviction_candidates()
    assert len(candidates) == 1
    assert candidates[0]["model_id"] == "whisper-large-v3"


# ---------------------------------------------------------------------------
# 4. Consent-Gated Remote GPU Offload Protocol
# ---------------------------------------------------------------------------


def test_remote_gpu_offload_consent_tls_and_scrubbing(tmp_path: Path) -> None:
    clip = tmp_path / "secret_client_interview.mp4"
    clip.write_bytes(b"confidential-media-bytes")

    # Non-TLS remote URLs MUST be rejected immediately
    with pytest.raises(InsecureTransportError):
        RemoteGpuOffloadClient(RemoteGpuEndpointConfig(endpoint_url="http://gpu.example.com/jobs"))

    with pytest.raises(InsecureTransportError):
        RemoteGpuOffloadClient(RemoteGpuEndpointConfig(endpoint_url="https://gpu.example.com/jobs", verify_tls=False))

    transport_calls: List[Dict[str, object]] = []

    def fake_transport(
        url: str,
        body: bytes,
        headers: Dict[str, str],
        timeout_sec: float,
        ssl_ctx: ssl.SSLContext,
    ) -> tuple[int, bytes]:
        assert ssl_ctx.verify_mode == ssl.CERT_REQUIRED
        assert ssl_ctx.check_hostname is True
        transport_calls.append({"url": url, "body": json.loads(body.decode("utf-8")), "headers": headers})
        return 202, json.dumps({"status": "queued", "worker": "gpu-node-01"}).encode("utf-8")

    client = RemoteGpuOffloadClient(
        RemoteGpuEndpointConfig(endpoint_url="https://gpu.bangonpro.dev/v1/offload"),
        transport=fake_transport,
    )

    request = OffloadJobRequest(
        job_id="job-001",
        task_type="diarization",
        media_paths=[clip],
        technical_metadata={
            str(clip): {
                "width": 1920,
                "height": 1080,
                "fps": 25.0,
                "codec_name": "h264",
                "filename": "/Users/kieran/Private/secret_client_interview.mp4",
                "com.apple.quicktime.location.ISO6709": "+51.5074-0.1278/",
                "artist": "Private Client",
            }
        },
        include_raw_media=False,
    )

    # Blocked when consent is False or None
    with pytest.raises(ConsentRequiredError):
        client.submit_job(request, consent=None)
    with pytest.raises(ConsentRequiredError):
        client.submit_job(request, consent=RemoteOffloadConsent(user_consented=False, consent_token="tok-12345678"))
    with pytest.raises(ConsentRequiredError):
        client.submit_job(request, consent=RemoteOffloadConsent(user_consented=True, consent_token="short"))
    assert len(transport_calls) == 0

    # Blocked when consent expired
    expired_consent = RemoteOffloadConsent(
        user_consented=True,
        consent_token="valid-user-consent-token-001",
        expires_at_utc=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    )
    with pytest.raises(ConsentExpiredError):
        client.submit_job(request, consent=expired_consent)
    assert len(transport_calls) == 0

    # Blocked when host-pinned consent targets another host
    wrong_host_consent = RemoteOffloadConsent(
        user_consented=True,
        consent_token="valid-user-consent-token-001",
        approved_endpoint_host="other.example.com",
    )
    with pytest.raises(ConsentRequiredError):
        client.submit_job(request, consent=wrong_host_consent)

    # Raw footage upload blocked unless explicitly allowed
    raw_req = OffloadJobRequest(
        job_id="job-raw",
        task_type="upscale",
        media_paths=[clip],
        include_raw_media=True,
    )
    feature_only_consent = RemoteOffloadConsent(
        user_consented=True,
        consent_token="valid-user-consent-token-001",
        allow_raw_media_upload=False,
        approved_endpoint_host="gpu.bangonpro.dev",
        expires_at_utc=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    with pytest.raises(RawMediaUploadDeniedError):
        client.submit_job(raw_req, consent=feature_only_consent)
    assert len(transport_calls) == 0

    # Metadata scrubbing check
    scrubbed = scrub_metadata_dict(request.technical_metadata[str(clip)])
    assert scrubbed == {"width": 1920, "height": 1080, "fps": 25.0, "codec_name": "h264"}
    manifest = build_scrubbed_manifest([clip], request.technical_metadata)
    assert len(manifest) == 1
    assert "secret_client_interview" not in json.dumps(manifest)
    assert "51.5074" not in json.dumps(manifest)

    # Valid consented features-only submission succeeds over TLS
    resp = client.submit_job(request, consent=feature_only_consent)
    assert resp.accepted is True
    assert resp.status_code == 202
    assert resp.payload_mode == "features_only"
    assert len(transport_calls) == 1
    sent_payload = transport_calls[0]["body"]
    assert isinstance(sent_payload, dict)
    assert sent_payload["raw_media_included"] is False
    # Raw consent token is never transmitted in plaintext in JSON body (SHA-256 digest only)
    assert "valid-user-consent-token-001" not in json.dumps(sent_payload)


# ---------------------------------------------------------------------------
# 5. Real Executable FFmpeg + MLT (melt) Benchmark Runner
# ---------------------------------------------------------------------------


def test_real_ffmpeg_and_melt_benchmark_execution(tmp_path: Path) -> None:
    report = run_full_benchmark(
        width=640,
        height=360,
        fps=25,
        duration_sec=1.0,
        work_dir=tmp_path / "bench_run",
    )

    assert report.summary["all_stages_succeeded"] is True
    for stage_name in ("decode", "proxy_generation", "mlt_filter_render", "final_export"):
        st = report.stages[stage_name]
        assert st.succeeded is True, f"Benchmark stage {stage_name} failed: {st.error}"
        assert st.fps > 0.0
        assert st.elapsed_sec > 0.0
        assert st.realtime_factor > 0.0

    assert report.stages["mlt_filter_render"].tool == "melt"
    assert report.stages["mlt_filter_render"].output_bytes > 0
    assert report.stages["final_export"].output_bytes > 0
    assert report.stages["proxy_generation"].output_bytes > 0

    # Verify JSON serialization and explicit host architecture attribution
    parsed = json.loads(report.to_json())
    env = parsed["host_environment"]
    assert env["platform_system"] in ("linux", "darwin", "windows")
    assert env["machine"] != ""
    assert "measurement_disclaimer" in env
    if not env["is_apple_silicon"]:
        assert "NOT Apple Silicon" in env["measurement_disclaimer"]


# ---------------------------------------------------------------------------
# 6. Apple Silicon RAM-Tier Model Quantization & MPS->CPU Fallback
# ---------------------------------------------------------------------------


def test_recommend_model_quantization_across_apple_silicon_tiers() -> None:
    # 8 GB base Apple Silicon Mac -> strict INT8 CPU quantization to prevent swap pressure
    rec_8 = recommend_model_quantization(ram_gb=8.0, has_mps=True, disk_free_gb=60.0)
    assert rec_8.memory_tier == "8gb"
    assert rec_8.whisper_quantization == "int8"
    assert rec_8.whisper_model in ("tiny", "small")
    assert rec_8.sam2_precision == "onnx_int8"
    assert rec_8.sam2_device == "cpu"
    assert rec_8.max_concurrent_ai_workers == 1
    assert rec_8.recommend_proxies is True

    # 16 GB Apple Silicon Mac -> Whisper INT8 + SAM2 small FP16 on MPS
    rec_16 = recommend_model_quantization(ram_gb=16.0, has_mps=True, disk_free_gb=120.0)
    assert rec_16.memory_tier == "16gb"
    assert rec_16.whisper_quantization == "int8"
    assert rec_16.whisper_model == "medium"
    assert rec_16.sam2_model == "sam2.1_hiera_small"
    assert rec_16.sam2_device == "mps"
    assert rec_16.sam2_precision == "fp16"
    assert rec_16.mps_fallback_device == "cpu"
    assert rec_16.mps_fallback_precision == "onnx_int8"

    # 24 GB Apple Silicon Mac
    rec_24 = recommend_model_quantization(ram_gb=24.0, has_mps=True, disk_free_gb=200.0)
    assert rec_24.memory_tier == "24gb"
    assert rec_24.sam2_device == "mps"
    assert rec_24.recommend_proxies is False

    # 32 GB+ Apple Silicon Pro/Max -> Whisper large-v3 FP16 + SAM2 base+ MPS
    rec_32 = recommend_model_quantization(ram_gb=36.0, has_mps=True, disk_free_gb=250.0)
    assert rec_32.memory_tier == "32gb_plus"
    assert rec_32.whisper_model == "large-v3"
    assert rec_32.whisper_quantization == "fp16"
    assert rec_32.whisper_device == "mps"
    assert rec_32.sam2_model == "sam2.1_hiera_base_plus"
    assert rec_32.sam2_device == "mps"
    assert rec_32.max_concurrent_ai_workers >= 3

    # Non-MPS fallback on 32 GB Linux/CPU host -> deterministic INT8/ONNX CPU fallback
    rec_cpu = recommend_model_quantization(ram_gb=32.0, has_mps=False, disk_free_gb=250.0)
    assert rec_cpu.whisper_device == "cpu"
    assert rec_cpu.whisper_quantization == "int8"
    assert rec_cpu.sam2_device == "cpu"
    assert rec_cpu.sam2_precision == "onnx_int8"
    assert rec_cpu.fallback_active is True
    assert any("MPS" in w for w in rec_cpu.warnings)

    # Low free disk space (< 10 GB) -> forces tiny/MobileSAM & recommends external SSD
    rec_low_disk = recommend_model_quantization(ram_gb=32.0, has_mps=True, disk_free_gb=4.5)
    assert rec_low_disk.whisper_model == "tiny"
    assert rec_low_disk.sam2_model == "mobilesam"
    assert rec_low_disk.recommend_external_ssd is True
    assert any("external SSD" in w for w in rec_low_disk.warnings)


# ---------------------------------------------------------------------------
# 7. StorageAndModelManager (Summary, Clear Caches, Models, No-Duplicate Media)
# ---------------------------------------------------------------------------


def test_storage_and_model_manager_lifecycle_and_safety(tmp_path: Path) -> None:
    source_dir = tmp_path / "camera_cards"
    source_dir.mkdir()
    raw_clip = source_dir / "A001_C001.mp4"
    raw_clip.write_bytes(b"ORIGINAL_CAMERA_MEDIA_PAYLOAD" * 2048)

    mgr = StorageAndModelManager(
        tmp_path / "bop_storage",
        config=StorageBudgetConfig(max_cache_gb=1.0, max_models_gb=1.0, protected_dirs=(source_dir,)),
    )

    # Populate managed cache categories
    proxy_f = mgr.proxies_dir / "clip_proxy.mp4"
    pinned_proxy = mgr.proxies_dir / "active_pinned_proxy.mp4"
    thumb_f = mgr.thumbnails_dir / "frame_001.jpg"
    temp_f = mgr.temp_dir / "render_scratch.tmp"
    project_inside_temp = mgr.temp_dir / "never_delete_me.kdenlive"

    proxy_f.write_bytes(b"P" * 4096)
    pinned_proxy.write_bytes(b"A" * 2048)
    thumb_f.write_bytes(b"T" * 1024)
    temp_f.write_bytes(b"S" * 3072)
    project_inside_temp.write_text("<mlt/>", encoding="utf-8")

    mgr.register_cache_entry(proxy_f, category="proxies", pinned=False)
    mgr.register_cache_entry(pinned_proxy, category="proxies", pinned=True)
    mgr.register_cache_entry(thumb_f, category="thumbnails", pinned=False)
    mgr.register_cache_entry(temp_f, category="temp", pinned=False)

    # Register & list AI models
    model_file = mgr.models_dir / "whisper_small_int8.bin"
    model_file.write_bytes(b"WEIGHTS" * 1500)
    rec = mgr.register_model("whisper-small-int8", model_file, quantization="int8", backend="cpu")
    assert rec["sha256"] is not None
    assert len(rec["sha256"]) == 64

    # SHA-256 mismatch must raise ValueError
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        mgr.register_model("bad-hash", model_file, verify_sha256="0" * 64)

    models = mgr.list_installed_models()
    assert len(models) == 1
    assert models[0]["model_id"] == "whisper-small-int8"
    assert models[0]["exists"] is True

    # Storage summary accuracy
    summary = mgr.get_storage_summary()
    assert summary.proxies_bytes == 4096 + 2048
    assert summary.thumbnails_bytes == 1024
    assert summary.temp_bytes >= 3072
    assert summary.models_bytes == model_file.stat().st_size
    assert summary.installed_model_count == 1
    assert summary.disk_free_bytes > 0

    # Verify zero duplicate source media inside cache root
    clean_audit = mgr.verify_no_duplicate_media([raw_clip])
    assert clean_audit.verified_clean is True
    assert clean_audit.checked_source_count == 1
    assert clean_audit.duplicates_found == []

    # Plant an accidental duplicate of raw_clip in temp/ and verify detector catches it
    dup_copy = mgr.temp_dir / "accidental_copy.mp4"
    dup_copy.write_bytes(raw_clip.read_bytes())
    dirty_audit = mgr.verify_no_duplicate_media([raw_clip])
    assert dirty_audit.verified_clean is False
    assert len(dirty_audit.duplicates_found) == 1

    # Refuses invalid category (such as 'models') in clear_caches
    with pytest.raises(ValueError, match="Unsupported cache category"):
        mgr.clear_caches(categories=("models",))

    # Clear caches: deletes unpinned proxies/temp/thumbnails, keeps pinned proxy, .kdenlive, models, source media
    clear_rep = mgr.clear_caches(categories=("proxies", "temp", "thumbnails"))
    assert not proxy_f.exists()
    assert not thumb_f.exists()
    assert not temp_f.exists()
    assert not dup_copy.exists()
    assert pinned_proxy.exists()
    assert project_inside_temp.exists()
    assert model_file.exists()
    assert raw_clip.exists()
    assert clear_rep.freed_bytes > 0

    # Clean audit restored after clearing temp duplicate
    assert mgr.verify_no_duplicate_media([raw_clip]).verified_clean is True

    # Remove model safely
    rm_res = mgr.remove_model("whisper-small-int8", delete_files=True)
    assert rm_res["removed"] is True
    assert rm_res["deleted_from_disk"] is True
    assert not model_file.exists()
    assert mgr.list_installed_models() == []


# ---------------------------------------------------------------------------
# 8. Comprehensive Workload Benchmarks (1080p, 4K, Multicam, AI VAD, Roto, RSS)
# ---------------------------------------------------------------------------


def test_comprehensive_benchmarks_with_4k_and_report_artifacts(tmp_path: Path) -> None:
    out_dir = tmp_path / "comprehensive_bench"
    report = run_comprehensive_benchmarks(
        out_dir,
        include_4k=True,
        duration_sec=1.0,
        duration_4k_sec=0.5,
    )

    assert report.summary["all_workloads_succeeded"] is True
    assert report.peak_rss_mb > 0.0
    assert get_peak_rss_mb() > 0.0

    expected_workloads = [
        "decode_1080p",
        "export_1080p",
        "decode_4k",
        "proxy_4k_to_720p",
        "export_4k",
        "multicam_4cam_sync_and_mlt_render",
        "ai_vad_transcription",
        "roto_optical_flow",
    ]
    for name in expected_workloads:
        assert name in report.workloads, f"Missing workload '{name}'"
        wl = report.workloads[name]
        if not wl.skipped:
            assert wl.succeeded is True, f"Workload '{name}' failed: {wl.error}"
            assert wl.elapsed_sec > 0.0
            assert wl.realtime_factor > 0.0

    # Verify JSON and Markdown artifacts written on disk
    json_path = out_dir / "benchmark_results.json"
    md_path = out_dir / "benchmark_report.md"
    assert json_path.is_file()
    assert md_path.is_file()

    parsed = json.loads(json_path.read_text(encoding="utf-8"))
    assert parsed["schema_version"] == "2.0"
    assert "host_environment" in parsed
    assert "model_recommendation" in parsed
    assert parsed["peak_rss_mb"] > 0.0
    if not parsed["host_environment"]["is_apple_silicon"]:
        assert "NOT Apple Silicon" in parsed["host_environment"]["measurement_disclaimer"]

    md_text = md_path.read_text(encoding="utf-8")
    assert "# Bang On Pro — Performance Benchmark Report" in md_text
    assert "decode_1080p" in md_text
    assert "proxy_4k_to_720p" in md_text
    assert "multicam_4cam_sync_and_mlt_render" in md_text
    assert "ai_vad_transcription" in md_text


# ---------------------------------------------------------------------------
# 9. V1.1.0 Virtualized vs Bare-Metal Apple Silicon Attribution & Sustained Mode
# ---------------------------------------------------------------------------


def test_virtualized_vs_bare_metal_apple_silicon_and_linux_host_tier() -> None:
    """Verify bare-metal Apple Silicon, virtualized CI Apple Silicon, and Linux CPU tiers are distinct."""
    assert classify_host_tier("darwin", "arm64", "Apple M1 (Virtual)") == (
        "macos_arm64_virtualized_ci",
        True,
    )
    assert classify_host_tier("darwin", "arm64", "Apple M3 Max") == (
        "macos_arm64_bare_metal",
        False,
    )
    assert classify_host_tier("darwin", "arm64", "Apple M2", hypervisor_present=True) == (
        "macos_arm64_virtualized_ci",
        True,
    )
    assert classify_host_tier("linux", "x86_64", "AMD EPYC 7763 64-Core Processor") == (
        "linux_cpu_fallback",
        False,
    )

    bare_caps = _simulated_apple_silicon_caps()
    assert bare_caps.host_tier == "macos_arm64_bare_metal"
    assert bare_caps.is_virtualized is False
    bare_env = _build_host_environment(bare_caps)
    assert bare_env["host_tier"] == "macos_arm64_bare_metal"
    assert bare_env["is_virtualized"] is False
    assert "bare-metal" in bare_env["measurement_disclaimer"].lower()

    virt_caps = HardwareCapabilities(
        platform_system="darwin",
        machine="arm64",
        is_apple_silicon=True,
        cpu_brand="Apple M1 (Virtual)",
        cpu_cores_logical=3,
        total_ram_gb=7.0,
        free_disk_gb=34.0,
        videotoolbox_available=True,
    )
    assert virt_caps.host_tier == "macos_arm64_virtualized_ci"
    assert virt_caps.is_virtualized is True
    virt_env = _build_host_environment(virt_caps)
    assert virt_env["host_tier"] == "macos_arm64_virtualized_ci"
    assert virt_env["is_virtualized"] is True
    assert "NOT bare-metal" in virt_env["measurement_disclaimer"]
    assert "Virtualized CI" in virt_env["environment_label"]

    linux_caps = HardwareCapabilities(
        platform_system="linux",
        machine="x86_64",
        is_apple_silicon=False,
        cpu_brand="Intel Xeon",
        cpu_cores_logical=4,
        total_ram_gb=16.0,
        free_disk_gb=80.0,
    )
    assert linux_caps.host_tier == "linux_cpu_fallback"
    linux_env = _build_host_environment(linux_caps)
    assert linux_env["host_tier"] == "linux_cpu_fallback"
    assert "NOT Apple Silicon" in linux_env["measurement_disclaimer"]


def test_sustained_benchmarks_metrics_cache_growth_and_attribution(tmp_path: Path) -> None:
    """Verify --sustained default duration contract and full sustained telemetry metrics."""
    sig = inspect.signature(run_sustained_benchmarks)
    assert sig.parameters["duration_sec"].default >= 10.0
    assert sig.parameters["duration_4k_sec"].default >= 10.0

    out_dir = tmp_path / "sustained_bench"
    report = run_sustained_benchmarks(
        out_dir,
        include_4k=True,
        duration_sec=1.5,
        duration_4k_sec=1.0,
        multicam_duration_sec=1.5,
        audio_duration_sec=10.0,
        roto_frames=12,
    )

    assert report.summary["benchmark_mode"] == "sustained"
    assert report.summary["all_workloads_succeeded"] is True
    assert report.summary["host_tier"] in (
        "macos_arm64_bare_metal",
        "macos_arm64_virtualized_ci",
        "linux_cpu_fallback",
    )
    assert isinstance(report.summary["is_virtualized"], bool)
    assert report.summary["total_cache_growth_mb"] > 0.0
    assert report.summary["total_dropped_frames_estimate"] >= 0

    for name, wl in report.workloads.items():
        if wl.skipped:
            continue
        assert wl.succeeded is True, f"Sustained workload '{name}' failed: {wl.error}"
        assert wl.elapsed_sec > 0.0
        assert wl.fps > 0.0
        assert wl.realtime_factor > 0.0
        assert wl.peak_rss_mb > 0.0
        assert wl.cpu_percent_estimate >= 0.0
        assert wl.dropped_frames_estimate >= 0

    assert report.workloads["proxy_4k_to_720p"].cache_growth_mb > 0.0
    assert report.workloads["export_1080p"].cache_growth_mb > 0.0
    assert report.workloads["export_4k"].cache_growth_mb > 0.0

    parsed = json.loads((out_dir / "benchmark_results.json").read_text(encoding="utf-8"))
    assert parsed["summary"]["benchmark_mode"] == "sustained"
    assert parsed["host_environment"]["host_tier"] == report.summary["host_tier"]
    assert "cache_growth_mb" in parsed["workloads"]["proxy_4k_to_720p"]
    assert "cpu_percent_estimate" in parsed["workloads"]["export_1080p"]
    assert "dropped_frames_estimate" in parsed["workloads"]["export_1080p"]

    md_text = (out_dir / "benchmark_report.md").read_text(encoding="utf-8")
    assert "**Benchmark Mode:** `sustained`" in md_text
    assert "**Host Tier:**" in md_text
    assert "Cache Growth (MB)" in md_text
    assert "CPU (%)" in md_text
    assert "Dropped Frames" in md_text


def test_proxy_cancellation_cleanup_corrupt_cache_recovery_and_url_creds(tmp_path: Path) -> None:
    """Verify proxy task cancellation removes partial temps, corrupt caches recover, and URL creds fail."""
    # Remote GPU endpoint rejects embedded credentials
    with pytest.raises(InsecureTransportError, match="Embedded credentials"):
        RemoteGpuOffloadClient(
            RemoteGpuEndpointConfig(endpoint_url="https://user:secret@gpu.example.com/v1/offload")
        )

    source_dir = tmp_path / "src_media"
    source_dir.mkdir()
    src_clip = _create_synthetic_video(
        source_dir / "long_clip.mp4", width=1280, height=720, fps=30, duration_sec=3.0
    )
    baseline_bytes = src_clip.read_bytes()

    mgr = StorageAndModelManager(
        tmp_path / "cache_store",
        config=StorageBudgetConfig(max_cache_gb=1.0, max_models_gb=1.0, protected_dirs=(source_dir,)),
    )

    # 1. Pre-cancelled event aborts immediately without writing anything
    pre_cancel = threading.Event()
    pre_cancel.set()
    with pytest.raises(ProxyCancelledError):
        generate_proxy(
            src_clip,
            mgr,
            timeline_fps=25.0,
            policy=ProxyPolicy(proxy_target_width=640, proxy_target_height=360, force_proxy=True),
            cancel_event=pre_cancel,
        )
    assert list(mgr.proxies_dir.glob("*")) == []

    # 2. Mid-flight cancellation cleans up temporary files and preserves source media
    mid_cancel = threading.Event()
    timer = threading.Timer(0.03, mid_cancel.set)
    timer.start()
    try:
        try:
            generate_proxy(
                src_clip,
                mgr,
                timeline_fps=25.0,
                policy=ProxyPolicy(proxy_target_width=640, proxy_target_height=360, force_proxy=True),
                cancel_event=mid_cancel,
            )
        except ProxyCancelledError:
            # Verify ZERO orphaned .tmp. files remain after cancellation
            leftover_tmps = [p for p in mgr.proxies_dir.glob("*.tmp.*")]
            assert leftover_tmps == []
    finally:
        timer.cancel()

    assert src_clip.read_bytes() == baseline_bytes

    # 3. Valid proxy creation followed by corrupt cache auto-recovery
    created = generate_proxy(
        src_clip,
        mgr,
        timeline_fps=25.0,
        policy=ProxyPolicy(proxy_target_width=640, proxy_target_height=360, force_proxy=True),
    )
    assert created.created is True or created.reused_cached is True
    proxy_file = Path(created.proxy_path)
    assert proxy_file.is_file()

    # Corrupt cached proxy file on disk and verify generate_proxy regenerates cleanly
    proxy_file.write_bytes(b"CORRUPT_TRUNCATED_MP4_HEADER")
    recovered = generate_proxy(
        src_clip,
        mgr,
        timeline_fps=25.0,
        policy=ProxyPolicy(proxy_target_width=640, proxy_target_height=360, force_proxy=True),
    )
    assert recovered.created is True
    assert recovered.reused_cached is False
    assert recovered.proxy_probe is not None
    assert recovered.proxy_probe.width == 640

    # 4. Orphaned temp file cleanup removes stale .*.tmp.* while preserving valid proxies & .kdenlive
    orphan = mgr.proxies_dir / ".stale_job.99999.tmp.mp4"
    orphan.write_bytes(b"partial-temp-data")
    stray_proj = mgr.proxies_dir / "keep_me.kdenlive"
    stray_proj.write_text("<mlt/>", encoding="utf-8")

    removed = mgr.cleanup_orphaned_temp_files()
    assert str(orphan.resolve()) in removed
    assert not orphan.exists()
    assert proxy_file.exists()
    assert stray_proj.exists()
    assert mgr.verify_no_duplicate_media([src_clip]).verified_clean is True


def test_prores_videotoolbox_selection_and_prores_ks_fallback() -> None:
    """Verify hardware prores_videotoolbox is selected when available and falls back cleanly to prores_ks."""
    mac_with_prores_vt = _simulated_apple_silicon_caps()
    for name, level in (("prores_422_proxy", "0"), ("prores_422", "2"), ("prores_422_hq", "3")):
        hw_prof = generate_export_profile(name, capabilities=mac_with_prores_vt)
        assert hw_prof.video_codec == "prores_videotoolbox"
        assert hw_prof.hardware_accelerated is True
        assert hw_prof.accelerator == "videotoolbox"
        assert hw_prof.fallback_used is False
        assert "-allow_sw" in hw_prof.ffmpeg_output_args
        assert hw_prof.mlt_consumer_properties["vcodec"] == "prores_videotoolbox"
        assert hw_prof.mlt_consumer_properties["vprofile"] == level
        ok, issues = validate_profile_against_capabilities(hw_prof, mac_with_prores_vt)
        assert ok, issues

    mac_without_prores_vt = _simulated_apple_silicon_caps()
    mac_without_prores_vt.ffmpeg_encoders = [
        enc for enc in mac_without_prores_vt.ffmpeg_encoders if enc != "prores_videotoolbox"
    ]
    for name, level in (("prores_422_proxy", "0"), ("prores_422", "2"), ("prores_422_hq", "3")):
        sw_prof = generate_export_profile(name, capabilities=mac_without_prores_vt)
        assert sw_prof.video_codec == "prores_ks"
        assert sw_prof.hardware_accelerated is False
        assert sw_prof.accelerator == "cpu_prores_ks"
        assert sw_prof.fallback_used is True
        assert sw_prof.fallback_reason is not None
        assert sw_prof.mlt_consumer_properties["vcodec"] == "prores_ks"
        assert sw_prof.mlt_consumer_properties["vprofile"] == level
        ok, issues = validate_profile_against_capabilities(sw_prof, mac_without_prores_vt)
        assert ok, issues


def test_cache_budget_manager_locking_corrupt_backup_and_throttling(tmp_path: Path) -> None:
    """Verify flock manifest locking (0o600), corrupt manifest backup, and throttled enforcement."""
    cache_root = tmp_path / "locked_cache"
    cache_root.mkdir(parents=True, exist_ok=True)

    # 1. Corrupt cache_manifest.json must be backed up to cache_manifest.json.corrupt.<ts> before reset
    corrupt_payload = "{corrupt-json-truncated"
    manifest_path = cache_root / "cache_manifest.json"
    manifest_path.write_text(corrupt_payload, encoding="utf-8")

    mgr = CacheBudgetManager(
        cache_root,
        config=StorageBudgetConfig(max_cache_gb=1.0, max_models_gb=1.0),
    )
    corrupt_backups = sorted(cache_root.glob("cache_manifest.json.corrupt.*"))
    assert len(corrupt_backups) == 1
    assert corrupt_backups[0].read_text(encoding="utf-8") == corrupt_payload
    assert mgr.manifest_lock_path.is_file()
    if os.name == "posix":
        assert (mgr.manifest_lock_path.stat().st_mode & 0o777) == 0o600

    # 2. Concurrent registrations serialize cleanly via lock without corrupting manifest JSON
    created_files: list[Path] = []
    for idx in range(12):
        fpath = mgr.proxies_dir / f"proxy_{idx:02d}.mp4"
        fpath.write_bytes(b"P" * 1024)
        created_files.append(fpath)

    errors: list[Exception] = []

    def worker(files_slice: list[Path]) -> None:
        try:
            for item in files_slice:
                mgr.register_cache_entry(item, category="proxies")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(created_files[i::3],))
        for i in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    assert errors == []

    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(persisted["entries"]) == len(created_files)

    # 3. maybe_enforce_cache_budget runs first time, then throttles while under high-water mark
    first_report = mgr.maybe_enforce_cache_budget(min_interval_sec=60.0, high_water_ratio=0.85)
    assert first_report is not None
    throttled_report = mgr.maybe_enforce_cache_budget(min_interval_sec=60.0, high_water_ratio=0.85)
    assert throttled_report is None
    # Direct enforce_cache_budget always executes unconditionally
    forced_report = mgr.enforce_cache_budget()
    assert forced_report is not None



