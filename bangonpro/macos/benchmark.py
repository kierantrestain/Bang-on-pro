"""Real executable FFmpeg + MLT (melt) + AI/Roto media performance benchmark runner.

Provides two benchmark entrypoints:
1. ``run_full_benchmark()`` — core 4-stage pipeline benchmark (decode, intra-frame proxy,
   MLT filter/effect render, final delivery export).
2. ``run_comprehensive_benchmarks(output_dir, include_4k=True)`` — full Apple Silicon /
   cross-platform workload benchmark measuring:
   - 1080p & 4K decode/transcode/export throughput (FPS & realtime factor)
   - 4K -> 720p intra-frame proxy generation speed
   - 4-camera multicam GCC-PHAT sync & MLT multi-track render latency
   - AI VAD / speech segmentation processing realtime factor
   - Rotoscoping / masking optical-flow propagation FPS (when roto_studio or OpenCV available)
   - Peak RSS memory (MB) via ``resource.getrusage``
   Emits both ``benchmark_results.json`` and ``benchmark_report.md``.

Always tags every report with exact host platform/architecture metadata and an
explicit disclaimer when executed on non-Apple-Silicon CI hosts so Linux x86_64
numbers are never misrepresented as Apple Silicon M-series measurements.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .hardware_profiles import (
    HardwareCapabilities,
    generate_decode_args,
    generate_export_profile,
    probe_hardware_capabilities,
    recommend_model_quantization,
)
from .proxy_cache_manager import (
    CacheBudgetManager,
    ProxyPolicy,
    StorageBudgetConfig,
    generate_proxy,
)


def _headless_melt_env() -> Dict[str, str]:
    env = os.environ.copy()
    env.setdefault("SDL_AUDIODRIVER", "dummy")
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    return env


def get_peak_rss_mb() -> float:
    """Return peak resident set size (MB) across current process and child subprocesses."""
    self_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    child_rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    raw_max = max(self_rss, child_rss)
    # macOS reports ru_maxrss in bytes; Linux reports KiB
    if sys.platform == "darwin":
        return round(raw_max / (1024.0 * 1024.0), 2)
    return round(raw_max / 1024.0, 2)


@dataclass
class StageBenchmark:
    """Measured telemetry for a single FFmpeg, MLT, AI, or Roto benchmark stage."""

    stage: str
    tool: str
    succeeded: bool
    elapsed_sec: float
    total_frames: int
    fps: float
    realtime_factor: float
    codec: str
    hardware_accelerated: bool
    fallback_used: bool
    output_bytes: int = 0
    command: List[str] = field(default_factory=list)
    error: Optional[str] = None
    peak_rss_mb: float = 0.0
    skipped: bool = False
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["elapsed_sec"] = round(self.elapsed_sec, 4)
        data["fps"] = round(self.fps, 2)
        data["realtime_factor"] = round(self.realtime_factor, 3)
        data["peak_rss_mb"] = round(self.peak_rss_mb, 2)
        return data


@dataclass
class BenchmarkReport:
    """Structured JSON benchmark report with explicit host architecture attribution."""

    schema_version: str
    timestamp_utc: str
    host_environment: Dict[str, Any]
    test_clip_spec: Dict[str, Any]
    capabilities: Dict[str, Any]
    stages: Dict[str, StageBenchmark]
    summary: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "timestamp_utc": self.timestamp_utc,
            "host_environment": self.host_environment,
            "test_clip_spec": self.test_clip_spec,
            "capabilities": self.capabilities,
            "stages": {k: v.to_dict() for k, v in self.stages.items()},
            "summary": self.summary,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


@dataclass
class ComprehensiveBenchmarkReport:
    """Full workload benchmark report with JSON & Markdown rendering."""

    schema_version: str
    timestamp_utc: str
    host_environment: Dict[str, Any]
    capabilities: Dict[str, Any]
    model_recommendation: Dict[str, Any]
    workloads: Dict[str, StageBenchmark]
    peak_rss_mb: float
    summary: Dict[str, Any]
    json_path: Optional[str] = None
    markdown_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "timestamp_utc": self.timestamp_utc,
            "host_environment": self.host_environment,
            "capabilities": self.capabilities,
            "model_recommendation": self.model_recommendation,
            "workloads": {k: v.to_dict() for k, v in self.workloads.items()},
            "peak_rss_mb": round(self.peak_rss_mb, 2),
            "summary": self.summary,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def to_markdown(self) -> str:
        env = self.host_environment
        rec = self.model_recommendation
        lines: List[str] = [
            "# Bang On Pro — Performance Benchmark Report",
            "",
            f"- **Timestamp (UTC):** `{self.timestamp_utc}`",
            f"- **Host Environment:** `{env['environment_label']}`",
            f"- **Platform / Arch:** `{env['platform_system']}/{env['machine']}`",
            f"- **CPU:** `{env['cpu_brand']}` ({env['cpu_cores_logical']} logical cores)",
            f"- **System RAM:** `{env['total_ram_gb']} GB`",
            f"- **Apple Silicon:** `{env['is_apple_silicon']}` | **VideoToolbox:** `{env['videotoolbox_available']}`",
            f"- **Measured Peak RSS:** `{self.peak_rss_mb:.2f} MB`",
            "",
            f"> **Hardware Attribution Note:** {env['measurement_disclaimer']}",
            "",
            "## Workload Measurements",
            "",
            "| Workload | Tool | Status | Elapsed (s) | Throughput (FPS) | Realtime Factor | Codec / Backend | HW Accel | Peak RSS (MB) |",
            "|---|---|---|---:|---:|---:|---|---|---:|",
        ]

        for name, st in self.workloads.items():
            if st.skipped:
                status = "SKIPPED"
            elif st.succeeded:
                status = "PASS"
            else:
                status = "FAIL"
            fps_str = f"{st.fps:.2f}" if st.fps > 0 else "—"
            rt_str = f"{st.realtime_factor:.2f}x" if st.realtime_factor > 0 else "—"
            hw_str = "Yes" if st.hardware_accelerated else "No (CPU)"
            lines.append(
                f"| `{name}` | `{st.tool}` | {status} | {st.elapsed_sec:.3f} | {fps_str} | {rt_str} | "
                f"`{st.codec}` | {hw_str} | {st.peak_rss_mb:.1f} |"
            )

        lines.extend(
            [
                "",
                "## Recommended Local AI Model Tier",
                "",
                f"- **Memory Tier:** `{rec['memory_tier']}` ({rec['ram_gb']} GB RAM, MPS=`{rec['has_mps']}`, Free Disk=`{rec['disk_free_gb']} GB`)",
                f"- **Whisper:** `{rec['whisper_model']}` (`{rec['whisper_quantization']}` on `{rec['whisper_device']}`)",
                f"- **SAM2 / Segmentation:** `{rec['sam2_model']}` (`{rec['sam2_precision']}` on `{rec['sam2_device']}`)",
                f"- **MPS Fallback Policy:** `{rec['mps_fallback_device']}` / `{rec['mps_fallback_precision']}` (active=`{rec['fallback_active']}`)",
                f"- **Max Concurrent AI Workers:** `{rec['max_concurrent_ai_workers']}`",
                "",
            ]
        )
        return "\n".join(lines)


def _select_mlt_profile(width: int, height: int, fps: int) -> str:
    fps_tag = "30" if fps >= 29 else "25"
    if width >= 3840 or height >= 2160:
        return f"uhd_2160p_{fps_tag}"
    if width >= 1920 or height >= 1080:
        return f"atsc_1080p_{fps_tag}"
    return f"atsc_720p_{fps_tag}"


def _build_host_environment(caps: HardwareCapabilities) -> Dict[str, Any]:
    if caps.is_apple_silicon:
        if caps.videotoolbox_available:
            label = f"macOS Apple Silicon ({caps.machine}) with VideoToolbox"
        else:
            label = f"macOS Apple Silicon ({caps.machine}) CPU-only FFmpeg"
        disclaimer = (
            "Measured natively on Apple Silicon macOS arm64. MLT filter stage executes on CPU NEON slices "
            "because MLT 7.x does not ship native Metal filters."
        )
    else:
        label = f"{caps.platform_system}/{caps.machine} (Non-Apple-Silicon CPU Fallback)"
        disclaimer = (
            f"IMPORTANT: Measured on {caps.platform_system}/{caps.machine} ({caps.cpu_brand}) using CPU fallback. "
            "These metrics are NOT Apple Silicon M-series or VideoToolbox measurements."
        )

    return {
        "platform_system": caps.platform_system,
        "machine": caps.machine,
        "is_apple_silicon": caps.is_apple_silicon,
        "videotoolbox_available": caps.videotoolbox_available,
        "cpu_brand": caps.cpu_brand,
        "cpu_cores_logical": caps.cpu_cores_logical,
        "total_ram_gb": caps.total_ram_gb,
        "environment_label": label,
        "measurement_disclaimer": disclaimer,
    }


def _generate_synthetic_clip(
    ffmpeg_path: str,
    output_path: Path,
    *,
    width: int,
    height: int,
    fps: int,
    duration_sec: float,
) -> Path:
    cmd = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"testsrc2=size={width}x{height}:rate={fps}:duration={duration_sec}",
        "-f", "lavfi",
        "-i", f"sine=frequency=440:sample_rate=48000:duration={duration_sec}",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        str(output_path),
    ]
    proc = subprocess.run(cmd, shell=False, capture_output=True, text=True, timeout=180, check=False)
    if proc.returncode != 0 or not output_path.is_file():
        raise RuntimeError(f"Failed to synthesize benchmark source media: {proc.stderr.strip()}")
    return output_path


def _benchmark_decode_stage(
    stage_name: str,
    src_clip: Path,
    caps: HardwareCapabilities,
    *,
    total_frames: int,
    duration_sec: float,
) -> StageBenchmark:
    assert caps.ffmpeg_path is not None
    decode_args = generate_decode_args(caps)
    decode_cmd = [
        caps.ffmpeg_path,
        "-hide_banner",
        "-loglevel", "error",
        *decode_args,
        "-i", str(src_clip),
        "-f", "null",
        "-",
    ]
    t0 = time.perf_counter()
    dec_proc = subprocess.run(decode_cmd, shell=False, capture_output=True, text=True, timeout=180, check=False)
    dec_elapsed = max(1e-6, time.perf_counter() - t0)
    ok = dec_proc.returncode == 0
    return StageBenchmark(
        stage=stage_name,
        tool="ffmpeg",
        succeeded=ok,
        elapsed_sec=dec_elapsed,
        total_frames=total_frames,
        fps=(total_frames / dec_elapsed) if ok else 0.0,
        realtime_factor=(duration_sec / dec_elapsed) if ok else 0.0,
        codec="h264_videotoolbox" if decode_args else "h264_cpu",
        hardware_accelerated=bool(decode_args),
        fallback_used=not bool(decode_args),
        output_bytes=0,
        command=decode_cmd,
        error=dec_proc.stderr.strip() if not ok else None,
        peak_rss_mb=get_peak_rss_mb(),
    )


def _benchmark_export_stage(
    stage_name: str,
    src_clip: Path,
    out_clip: Path,
    caps: HardwareCapabilities,
    *,
    total_frames: int,
    duration_sec: float,
) -> StageBenchmark:
    assert caps.ffmpeg_path is not None
    export_profile = generate_export_profile("h264_master", capabilities=caps)
    export_cmd = [
        caps.ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        *export_profile.ffmpeg_input_args,
        "-i", str(src_clip),
        *export_profile.ffmpeg_output_args,
        str(out_clip),
    ]
    t0 = time.perf_counter()
    exp_proc = subprocess.run(export_cmd, shell=False, capture_output=True, text=True, timeout=240, check=False)
    exp_elapsed = max(1e-6, time.perf_counter() - t0)
    exp_ok = exp_proc.returncode == 0 and out_clip.is_file() and out_clip.stat().st_size > 0
    return StageBenchmark(
        stage=stage_name,
        tool="ffmpeg",
        succeeded=exp_ok,
        elapsed_sec=exp_elapsed,
        total_frames=total_frames,
        fps=(total_frames / exp_elapsed) if exp_ok else 0.0,
        realtime_factor=(duration_sec / exp_elapsed) if exp_ok else 0.0,
        codec=export_profile.video_codec,
        hardware_accelerated=export_profile.hardware_accelerated,
        fallback_used=export_profile.fallback_used,
        output_bytes=out_clip.stat().st_size if exp_ok else 0,
        command=export_cmd,
        error=None if exp_ok else exp_proc.stderr.strip(),
        peak_rss_mb=get_peak_rss_mb(),
    )


def run_full_benchmark(
    *,
    width: int = 1280,
    height: int = 720,
    fps: int = 25,
    duration_sec: float = 2.0,
    work_dir: Optional[str | Path] = None,
    capabilities: Optional[HardwareCapabilities] = None,
) -> BenchmarkReport:
    """Execute real FFmpeg + MLT (melt) decode, proxy, filter, and export benchmarks."""
    caps = capabilities if capabilities is not None else probe_hardware_capabilities()
    if not caps.ffmpeg_path:
        raise RuntimeError("ffmpeg executable is required to run benchmarks")

    total_frames = max(1, int(round(duration_sec * fps)))
    stages: Dict[str, StageBenchmark] = {}

    tmp_ctx = None
    if work_dir is not None:
        base_dir = Path(work_dir).resolve()
        base_dir.mkdir(parents=True, exist_ok=True)
    else:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="bangonpro_bench_")
        base_dir = Path(tmp_ctx.name)

    try:
        src_clip = _generate_synthetic_clip(
            caps.ffmpeg_path,
            base_dir / "synthetic_source.mp4",
            width=width,
            height=height,
            fps=fps,
            duration_sec=duration_sec,
        )
        source_bytes = src_clip.stat().st_size

        # Stage 1: Video Decode Throughput
        stages["decode"] = _benchmark_decode_stage(
            "decode", src_clip, caps, total_frames=total_frames, duration_sec=duration_sec
        )

        # Stage 2: Proxy Generation Speed
        cache_mgr = CacheBudgetManager(
            base_dir / "proxy_cache",
            config=StorageBudgetConfig(max_cache_gb=2.0, max_models_gb=1.0),
        )
        proxy_policy = ProxyPolicy(
            proxy_target_width=min(960, width),
            proxy_target_height=min(540, height),
            force_proxy=True,
        )
        proxy_res = generate_proxy(
            src_clip,
            cache_mgr,
            timeline_fps=float(fps),
            policy=proxy_policy,
            capabilities=caps,
            force=True,
        )
        proxy_elapsed = max(1e-6, proxy_res.elapsed_sec)
        stages["proxy_generation"] = StageBenchmark(
            stage="proxy_generation",
            tool="ffmpeg",
            succeeded=proxy_res.created and Path(proxy_res.proxy_path).is_file(),
            elapsed_sec=proxy_elapsed,
            total_frames=total_frames,
            fps=total_frames / proxy_elapsed,
            realtime_factor=duration_sec / proxy_elapsed,
            codec=proxy_res.video_codec,
            hardware_accelerated=proxy_res.hardware_accelerated,
            fallback_used=not proxy_res.hardware_accelerated,
            output_bytes=proxy_res.proxy_bytes,
            command=[caps.ffmpeg_path, "<proxy_profile:edit_proxy_fast>"],
            error=None,
            peak_rss_mb=get_peak_rss_mb(),
        )

        # Stage 3: MLT Filter / Effect Rendering FPS
        export_profile = generate_export_profile("h264_master", capabilities=caps)
        if caps.melt_path:
            mlt_out = base_dir / "mlt_filtered.mp4"
            mlt_profile = _select_mlt_profile(width, height, fps)
            consumer_kv = [f"{k}={v}" for k, v in export_profile.mlt_consumer_properties.items()]
            melt_cmd = [
                caps.melt_path,
                "-profile", mlt_profile,
                str(src_clip),
                "-attach", "lift_gamma_gain", "lift_r=0.02", "gain_r=1.03",
                "-attach", "boxblur", "start=1", "end=1",
                "-attach", "volume", "level=-2",
                "-consumer", f"avformat:{mlt_out}",
                *consumer_kv,
            ]
            t0 = time.perf_counter()
            melt_proc = subprocess.run(
                melt_cmd,
                env=_headless_melt_env(),
                shell=False,
                capture_output=True,
                text=True,
                timeout=240,
                check=False,
            )
            melt_elapsed = max(1e-6, time.perf_counter() - t0)
            mlt_ok = melt_proc.returncode == 0 and mlt_out.is_file() and mlt_out.stat().st_size > 0
            stages["mlt_filter_render"] = StageBenchmark(
                stage="mlt_filter_render",
                tool="melt",
                succeeded=mlt_ok,
                elapsed_sec=melt_elapsed,
                total_frames=total_frames,
                fps=(total_frames / melt_elapsed) if mlt_ok else 0.0,
                realtime_factor=(duration_sec / melt_elapsed) if mlt_ok else 0.0,
                codec=export_profile.video_codec,
                hardware_accelerated=export_profile.hardware_accelerated,
                fallback_used=export_profile.fallback_used,
                output_bytes=mlt_out.stat().st_size if mlt_ok else 0,
                command=melt_cmd,
                error=None if mlt_ok else melt_proc.stderr.strip(),
                peak_rss_mb=get_peak_rss_mb(),
            )
        else:
            stages["mlt_filter_render"] = StageBenchmark(
                stage="mlt_filter_render",
                tool="melt",
                succeeded=False,
                elapsed_sec=0.0,
                total_frames=total_frames,
                fps=0.0,
                realtime_factor=0.0,
                codec="unavailable",
                hardware_accelerated=False,
                fallback_used=True,
                output_bytes=0,
                command=[],
                error="melt executable not found in PATH",
                peak_rss_mb=get_peak_rss_mb(),
            )

        # Stage 4: Final Delivery Export Throughput (FFmpeg h264_master)
        export_out = base_dir / f"final_export.{export_profile.container}"
        stages["final_export"] = _benchmark_export_stage(
            "final_export",
            src_clip,
            export_out,
            caps,
            total_frames=total_frames,
            duration_sec=duration_sec,
        )

        all_ok = all(s.succeeded for s in stages.values())
        summary = {
            "all_stages_succeeded": all_ok,
            "decode_fps": round(stages["decode"].fps, 2),
            "proxy_fps": round(stages["proxy_generation"].fps, 2),
            "mlt_filter_fps": round(stages["mlt_filter_render"].fps, 2),
            "export_fps": round(stages["final_export"].fps, 2),
            "is_apple_silicon": caps.is_apple_silicon,
            "videotoolbox_used": stages["final_export"].hardware_accelerated,
            "peak_rss_mb": get_peak_rss_mb(),
        }

        return BenchmarkReport(
            schema_version="1.0",
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            host_environment=_build_host_environment(caps),
            test_clip_spec={
                "width": width,
                "height": height,
                "fps": fps,
                "duration_sec": duration_sec,
                "total_frames": total_frames,
                "source_bytes": source_bytes,
            },
            capabilities=caps.to_dict(),
            stages=stages,
            summary=summary,
        )
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()


def _benchmark_multicam_sync_and_mlt(
    work_dir: Path,
    caps: HardwareCapabilities,
    *,
    duration_sec: float = 2.0,
) -> StageBenchmark:
    """Measure real 4-camera GCC-PHAT acoustic sync + MLT 4-cam render latency."""
    import numpy as np
    from bangonpro.ai_engine.agents.ingest_sync import estimate_offset_gcc_phat

    sr = 16000
    n_master = int(round((duration_sec + 1.5) * sr))
    rng = np.random.default_rng(42)
    t = np.arange(n_master, dtype=np.float64) / float(sr)
    # Broadband speech-like signal for crisp GCC-PHAT correlation peaks
    master_sig = 0.5 * np.sin(2.0 * np.pi * 220.0 * t) + 0.5 * rng.standard_normal(n_master)
    master_sig = master_sig.astype(np.float32)

    expected_offsets = {"cam_1": 0.0, "cam_2": 0.25, "cam_3": 0.50, "cam_4": 0.85}
    window_n = int(round(duration_sec * sr))
    ref_pcm = master_sig[:window_n]

    cam_pcms: Dict[str, Any] = {}
    for cid, off in expected_offsets.items():
        # Shift signal so acoustic event at t in ref occurs at t + off in target
        shift = int(round(off * sr))
        target = np.zeros(window_n + shift, dtype=np.float32)
        target[shift : shift + window_n] = ref_pcm + 0.05 * rng.standard_normal(window_n).astype(np.float32)
        cam_pcms[cid] = target

    t_sync_start = time.perf_counter()
    recovered: Dict[str, float] = {}
    max_err_ms = 0.0
    for cid, pcm in cam_pcms.items():
        if cid == "cam_1":
            recovered[cid] = 0.0
            continue
        est_off, _conf, _snr = estimate_offset_gcc_phat(ref_pcm, pcm, sample_rate=sr)
        recovered[cid] = round(est_off, 4)
        max_err_ms = max(max_err_ms, abs(est_off - expected_offsets[cid]) * 1000.0)
    sync_elapsed = max(1e-6, time.perf_counter() - t_sync_start)

    # Now measure real MLT 4-clip sequential multicam switch render via melt
    mlt_ok = False
    mlt_elapsed = 0.0
    mlt_bytes = 0
    mlt_err: Optional[str] = None
    fps = 25
    total_frames = max(1, int(round(duration_sec * fps)))

    if caps.ffmpeg_path and caps.melt_path:
        clip_path = _generate_synthetic_clip(
            caps.ffmpeg_path,
            work_dir / "multicam_src.mp4",
            width=640,
            height=360,
            fps=fps,
            duration_sec=duration_sec,
        )
        out_mp4 = work_dir / "multicam_mlt_out.mp4"
        export_profile = generate_export_profile("h264_master", capabilities=caps)
        consumer_kv = [f"{k}={v}" for k, v in export_profile.mlt_consumer_properties.items()]
        quarter = max(1, total_frames // 4)
        melt_cmd = [
            caps.melt_path,
            "-profile", "atsc_720p_25",
            str(clip_path), f"in=0", f"out={quarter - 1}",
            str(clip_path), f"in={quarter}", f"out={2 * quarter - 1}",
            str(clip_path), f"in={2 * quarter}", f"out={3 * quarter - 1}",
            str(clip_path), f"in={3 * quarter}", f"out={total_frames - 1}",
            "-consumer", f"avformat:{out_mp4}",
            *consumer_kv,
        ]
        t_mlt_start = time.perf_counter()
        proc = subprocess.run(
            melt_cmd,
            env=_headless_melt_env(),
            shell=False,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        mlt_elapsed = max(1e-6, time.perf_counter() - t_mlt_start)
        mlt_ok = proc.returncode == 0 and out_mp4.is_file() and out_mp4.stat().st_size > 0
        mlt_bytes = out_mp4.stat().st_size if mlt_ok else 0
        if not mlt_ok:
            mlt_err = proc.stderr.strip()
    else:
        mlt_err = "melt or ffmpeg unavailable"

    total_elapsed = sync_elapsed + mlt_elapsed
    return StageBenchmark(
        stage="multicam_4cam_sync_and_mlt_render",
        tool="gcc_phat+melt",
        succeeded=mlt_ok and max_err_ms < 50.0,
        elapsed_sec=total_elapsed,
        total_frames=total_frames,
        fps=(total_frames / mlt_elapsed) if (mlt_ok and mlt_elapsed > 0) else 0.0,
        realtime_factor=(duration_sec / total_elapsed) if (mlt_ok and total_elapsed > 0) else 0.0,
        codec="gcc_phat_fft+mlt_avformat",
        hardware_accelerated=caps.videotoolbox_available,
        fallback_used=not caps.videotoolbox_available,
        output_bytes=mlt_bytes,
        error=mlt_err,
        peak_rss_mb=get_peak_rss_mb(),
        details={
            "cameras": 4,
            "sync_elapsed_sec": round(sync_elapsed, 4),
            "mlt_render_elapsed_sec": round(mlt_elapsed, 4),
            "max_sync_error_ms": round(max_err_ms, 3),
            "recovered_offsets_sec": recovered,
        },
    )


def _benchmark_ai_vad_transcription(audio_duration_sec: float = 10.0) -> StageBenchmark:
    """Measure real DSP Voice Activity Detection & speech segmentation speed (realtime factor)."""
    import numpy as np
    from bangonpro.ai_engine.agents.transcribe_diarise import (
        _compute_frame_spectral_features,
        _extract_true_runs,
        _morphological_clean_mask,
        detect_available_asr_backends,
    )

    sr = 16000
    n = int(round(audio_duration_sec * sr))
    t = np.arange(n, dtype=np.float64) / float(sr)
    rng = np.random.default_rng(7)
    pcm = (0.005 * rng.standard_normal(n)).astype(np.float32)

    # Inject 3 synthetic speech bursts across the 10-second timeline
    for start_s, end_s in ((1.0, 3.5), (4.5, 7.0), (8.0, 9.5)):
        i0, i1 = int(start_s * sr), int(end_s * sr)
        burst = 0.45 * np.sin(2.0 * np.pi * 180.0 * t[i0:i1]) + 0.2 * rng.standard_normal(i1 - i0)
        pcm[i0:i1] += burst.astype(np.float32)

    asr_backends = detect_available_asr_backends()
    t0 = time.perf_counter()
    energy_db, _zcr, _centroid = _compute_frame_spectral_features(pcm, sample_rate=sr)
    raw_mask = energy_db > -35.0
    cleaned = _morphological_clean_mask(raw_mask, min_on=5, min_off=5)
    runs = _extract_true_runs(cleaned)
    elapsed = max(1e-6, time.perf_counter() - t0)

    realtime_factor = audio_duration_sec / elapsed
    return StageBenchmark(
        stage="ai_vad_transcription",
        tool="python_dsp_vad",
        succeeded=len(runs) >= 2,
        elapsed_sec=elapsed,
        total_frames=len(energy_db),
        fps=len(energy_db) / elapsed,
        realtime_factor=realtime_factor,
        codec="dsp_spectral_vad_cpu",
        hardware_accelerated=False,
        fallback_used=True,
        output_bytes=0,
        peak_rss_mb=get_peak_rss_mb(),
        details={
            "audio_duration_sec": audio_duration_sec,
            "detected_speech_segments": len(runs),
            "optional_asr_backends": asr_backends,
        },
    )


def _benchmark_roto_optical_flow(num_frames: int = 15, width: int = 640, height: int = 360) -> StageBenchmark:
    """Measure real optical-flow mask propagation FPS using roto_studio or OpenCV."""
    import numpy as np

    backend_used = ""
    cv2_mod = None
    try:
        cv2_mod = importlib.import_module("cv2")
        backend_used = f"opencv_{getattr(cv2_mod, '__version__', 'unknown')}"
    except ImportError:
        cv2_mod = None

    roto_available = False
    try:
        importlib.import_module("bangonpro.roto_studio")
        roto_available = True
    except ImportError:
        roto_available = False

    if cv2_mod is None:
        return StageBenchmark(
            stage="roto_optical_flow",
            tool="roto_studio/opencv",
            succeeded=True,
            elapsed_sec=0.0,
            total_frames=0,
            fps=0.0,
            realtime_factor=0.0,
            codec="unavailable",
            hardware_accelerated=False,
            fallback_used=True,
            skipped=True,
            peak_rss_mb=get_peak_rss_mb(),
            details={
                "skip_reason": "Neither OpenCV (cv2) nor bangonpro.roto_studio native optical-flow backend is installed",
                "roto_studio_importable": roto_available,
            },
        )

    # Synthesize moving bright rectangle on textured background and propagate binary mask
    rng = np.random.default_rng(123)
    bg = rng.integers(20, 45, size=(height, width), dtype=np.uint8)
    frames: List[Any] = []
    for idx in range(num_frames):
        frame = bg.copy()
        x0 = 80 + idx * 8
        y0 = 100 + idx * 2
        frame[y0 : y0 + 90, x0 : x0 + 110] = 220
        frames.append(frame)

    mask = np.zeros((height, width), dtype=np.float32)
    mask[100:190, 80:190] = 1.0
    grid_y, grid_x = np.mgrid[0:height, 0:width].astype(np.float32)

    t0 = time.perf_counter()
    cur_mask = mask
    propagated_frames = 0
    for idx in range(1, len(frames)):
        flow = cv2_mod.calcOpticalFlowFarneback(
            frames[idx - 1],
            frames[idx],
            None,
            0.5,
            3,
            15,
            3,
            5,
            1.2,
            0,
        )
        map_x = grid_x - flow[..., 0]
        map_y = grid_y - flow[..., 1]
        cur_mask = cv2_mod.remap(cur_mask, map_x, map_y, interpolation=cv2_mod.INTER_LINEAR)
        propagated_frames += 1
    elapsed = max(1e-6, time.perf_counter() - t0)
    fps = propagated_frames / elapsed
    duration_equiv = propagated_frames / 25.0

    return StageBenchmark(
        stage="roto_optical_flow",
        tool="opencv_farneback",
        succeeded=propagated_frames == num_frames - 1 and float(cur_mask.sum()) > 100.0,
        elapsed_sec=elapsed,
        total_frames=propagated_frames,
        fps=fps,
        realtime_factor=duration_equiv / elapsed,
        codec=backend_used,
        hardware_accelerated=False,
        fallback_used=True,
        peak_rss_mb=get_peak_rss_mb(),
        details={
            "resolution": f"{width}x{height}",
            "propagated_frames": propagated_frames,
            "roto_studio_importable": roto_available,
            "final_mask_area_px": round(float((cur_mask > 0.5).sum()), 1),
        },
    )


def run_comprehensive_benchmarks(
    output_dir: str | Path,
    *,
    include_4k: bool = True,
    duration_sec: float = 1.5,
    duration_4k_sec: float = 1.0,
    capabilities: Optional[HardwareCapabilities] = None,
) -> ComprehensiveBenchmarkReport:
    """Run full measurable Bang On Pro workloads and emit JSON + Markdown reports.

    Writes:
    - ``<output_dir>/benchmark_results.json``
    - ``<output_dir>/benchmark_report.md``
    """
    out_path = Path(output_dir).resolve()
    out_path.mkdir(parents=True, exist_ok=True)

    caps = capabilities if capabilities is not None else probe_hardware_capabilities(out_path)
    if not caps.ffmpeg_path:
        raise RuntimeError("ffmpeg executable is required to run comprehensive benchmarks")

    workloads: Dict[str, StageBenchmark] = {}
    fps = 25
    frames_1080 = max(1, int(round(duration_sec * fps)))
    frames_4k = max(1, int(round(duration_4k_sec * fps)))

    with tempfile.TemporaryDirectory(prefix="bangonpro_comp_bench_") as tmp_name:
        tmp_dir = Path(tmp_name)

        # 1. 1080p Decode & Export
        clip_1080 = _generate_synthetic_clip(
            caps.ffmpeg_path,
            tmp_dir / "source_1080p.mp4",
            width=1920,
            height=1080,
            fps=fps,
            duration_sec=duration_sec,
        )
        workloads["decode_1080p"] = _benchmark_decode_stage(
            "decode_1080p", clip_1080, caps, total_frames=frames_1080, duration_sec=duration_sec
        )
        workloads["export_1080p"] = _benchmark_export_stage(
            "export_1080p",
            clip_1080,
            tmp_dir / "export_1080p.mp4",
            caps,
            total_frames=frames_1080,
            duration_sec=duration_sec,
        )

        # 2. 4K Decode, 4K -> 720p Proxy Generation & 4K Export (optional)
        if include_4k:
            clip_4k = _generate_synthetic_clip(
                caps.ffmpeg_path,
                tmp_dir / "source_4k.mp4",
                width=3840,
                height=2160,
                fps=fps,
                duration_sec=duration_4k_sec,
            )
            workloads["decode_4k"] = _benchmark_decode_stage(
                "decode_4k", clip_4k, caps, total_frames=frames_4k, duration_sec=duration_4k_sec
            )
            cache_mgr = CacheBudgetManager(
                tmp_dir / "proxy_cache_4k",
                config=StorageBudgetConfig(max_cache_gb=2.0, max_models_gb=1.0),
            )
            proxy_res = generate_proxy(
                clip_4k,
                cache_mgr,
                timeline_fps=float(fps),
                policy=ProxyPolicy(proxy_target_width=1280, proxy_target_height=720, force_proxy=True),
                capabilities=caps,
                force=True,
            )
            p_elapsed = max(1e-6, proxy_res.elapsed_sec)
            workloads["proxy_4k_to_720p"] = StageBenchmark(
                stage="proxy_4k_to_720p",
                tool="ffmpeg",
                succeeded=proxy_res.created and Path(proxy_res.proxy_path).is_file(),
                elapsed_sec=p_elapsed,
                total_frames=frames_4k,
                fps=frames_4k / p_elapsed,
                realtime_factor=duration_4k_sec / p_elapsed,
                codec=proxy_res.video_codec,
                hardware_accelerated=proxy_res.hardware_accelerated,
                fallback_used=not proxy_res.hardware_accelerated,
                output_bytes=proxy_res.proxy_bytes,
                peak_rss_mb=get_peak_rss_mb(),
                details={"source_res": "3840x2160", "proxy_res": "1280x720"},
            )
            workloads["export_4k"] = _benchmark_export_stage(
                "export_4k",
                clip_4k,
                tmp_dir / "export_4k.mp4",
                caps,
                total_frames=frames_4k,
                duration_sec=duration_4k_sec,
            )

        # 3. 4-Camera Multicam GCC-PHAT Sync & MLT Render
        workloads["multicam_4cam_sync_and_mlt_render"] = _benchmark_multicam_sync_and_mlt(
            tmp_dir, caps, duration_sec=min(2.0, max(1.0, duration_sec))
        )

        # 4. AI VAD / Speech Segmentation Speed
        workloads["ai_vad_transcription"] = _benchmark_ai_vad_transcription(audio_duration_sec=10.0)

        # 5. Rotoscoping / Masking Optical-Flow Propagation
        workloads["roto_optical_flow"] = _benchmark_roto_optical_flow(num_frames=15)

    peak_rss = get_peak_rss_mb()
    has_mps = bool(caps.is_apple_silicon)
    model_rec = recommend_model_quantization(caps.total_ram_gb, has_mps, caps.free_disk_gb)

    all_ok = all(w.succeeded for w in workloads.values() if not w.skipped)
    summary = {
        "all_workloads_succeeded": all_ok,
        "include_4k": include_4k,
        "workloads_executed": sum(1 for w in workloads.values() if not w.skipped),
        "workloads_skipped": sum(1 for w in workloads.values() if w.skipped),
        "decode_1080p_fps": round(workloads["decode_1080p"].fps, 2),
        "export_1080p_fps": round(workloads["export_1080p"].fps, 2),
        "decode_4k_fps": round(workloads["decode_4k"].fps, 2) if "decode_4k" in workloads else None,
        "proxy_4k_to_720p_fps": round(workloads["proxy_4k_to_720p"].fps, 2) if "proxy_4k_to_720p" in workloads else None,
        "ai_vad_realtime_factor": round(workloads["ai_vad_transcription"].realtime_factor, 2),
        "roto_optical_flow_fps": round(workloads["roto_optical_flow"].fps, 2) if not workloads["roto_optical_flow"].skipped else None,
        "peak_rss_mb": peak_rss,
        "is_apple_silicon": caps.is_apple_silicon,
        "videotoolbox_available": caps.videotoolbox_available,
    }

    json_file = out_path / "benchmark_results.json"
    md_file = out_path / "benchmark_report.md"

    report = ComprehensiveBenchmarkReport(
        schema_version="2.0",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        host_environment=_build_host_environment(caps),
        capabilities=caps.to_dict(),
        model_recommendation=model_rec.to_dict(),
        workloads=workloads,
        peak_rss_mb=peak_rss,
        summary=summary,
        json_path=str(json_file),
        markdown_path=str(md_file),
    )

    json_file.write_text(report.to_json(indent=2) + "\n", encoding="utf-8")
    md_file.write_text(report.to_markdown(), encoding="utf-8")
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Bang On Pro FFmpeg + MLT + AI Performance Benchmark Runner")
    parser.add_argument("--comprehensive", action="store_true", help="Run comprehensive 1080p/4K/multicam/AI/roto suite")
    parser.add_argument("--output-dir", type=str, default="", help="Directory for benchmark_results.json and benchmark_report.md")
    parser.add_argument("--no-4k", action="store_true", help="Skip 4K workloads in comprehensive mode")
    parser.add_argument("--width", type=int, default=1280, help="Test clip width for quick mode (default: 1280)")
    parser.add_argument("--height", type=int, default=720, help="Test clip height for quick mode (default: 720)")
    parser.add_argument("--fps", type=int, default=25, help="Test clip frame rate (default: 25)")
    parser.add_argument("--duration", type=float, default=2.0, help="Test clip duration in seconds (default: 2.0)")
    parser.add_argument("--output", type=str, default="", help="Optional path to write JSON benchmark report (quick mode)")
    args = parser.parse_args(argv)

    if args.comprehensive:
        out_dir = Path(args.output_dir) if args.output_dir else Path("./benchmark_output")
        comp = run_comprehensive_benchmarks(
            out_dir,
            include_4k=not args.no_4k,
            duration_sec=args.duration,
        )
        print(comp.to_markdown())
        return 0 if comp.summary["all_workloads_succeeded"] else 1

    report = run_full_benchmark(
        width=args.width,
        height=args.height,
        fps=args.fps,
        duration_sec=args.duration,
    )
    json_text = report.to_json(indent=2)
    if args.output:
        Path(args.output).write_text(json_text, encoding="utf-8")
    else:
        print(json_text)
    return 0 if report.summary["all_stages_succeeded"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
