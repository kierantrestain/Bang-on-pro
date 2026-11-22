"""Real executable FFmpeg + MLT (melt) media performance benchmark runner.

Measures four end-to-end editing pipeline stages on synthetic deterministic media:
1. Video decode throughput (FFmpeg -> null sink)
2. Intra-frame edit proxy generation (VideoToolbox on macOS arm64, libx264 ultrafast fallback on CPU)
3. MLT filter/effect rendering throughput (melt with lift_gamma_gain + boxblur + volume -> avformat)
4. Final delivery export throughput (h264_master profile via VideoToolbox or libx264 fallback)

Always tags every report with exact host platform/architecture metadata and an
explicit disclaimer when executed on non-Apple-Silicon CI hosts so Linux x86_64
numbers are never misrepresented as Apple Silicon M-series measurements.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import argparse
import json
import subprocess
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
)
from .proxy_cache_manager import (
    CacheBudgetManager,
    ProxyPolicy,
    StorageBudgetConfig,
    generate_proxy,
)


@dataclass
class StageBenchmark:
    """Measured telemetry for a single FFmpeg or MLT benchmark stage."""

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

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["elapsed_sec"] = round(self.elapsed_sec, 4)
        data["fps"] = round(self.fps, 2)
        data["realtime_factor"] = round(self.realtime_factor, 3)
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
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode != 0 or not output_path.is_file():
        raise RuntimeError(f"Failed to synthesize benchmark source media: {proc.stderr.strip()}")
    return output_path


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
        dec_proc = subprocess.run(decode_cmd, capture_output=True, text=True, timeout=180, check=False)
        dec_elapsed = max(1e-6, time.perf_counter() - t0)
        stages["decode"] = StageBenchmark(
            stage="decode",
            tool="ffmpeg",
            succeeded=(dec_proc.returncode == 0),
            elapsed_sec=dec_elapsed,
            total_frames=total_frames,
            fps=(total_frames / dec_elapsed) if dec_proc.returncode == 0 else 0.0,
            realtime_factor=(duration_sec / dec_elapsed) if dec_proc.returncode == 0 else 0.0,
            codec="h264_videotoolbox" if decode_args else "h264_cpu",
            hardware_accelerated=bool(decode_args),
            fallback_used=not bool(decode_args),
            output_bytes=0,
            command=decode_cmd,
            error=dec_proc.stderr.strip() if dec_proc.returncode != 0 else None,
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
            melt_proc = subprocess.run(melt_cmd, capture_output=True, text=True, timeout=240, check=False)
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
            )

        # Stage 4: Final Delivery Export Throughput (FFmpeg h264_master)
        export_out = base_dir / f"final_export.{export_profile.container}"
        export_cmd = [
            caps.ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel", "error",
            *export_profile.ffmpeg_input_args,
            "-i", str(src_clip),
            *export_profile.ffmpeg_output_args,
            str(export_out),
        ]
        t0 = time.perf_counter()
        exp_proc = subprocess.run(export_cmd, capture_output=True, text=True, timeout=240, check=False)
        exp_elapsed = max(1e-6, time.perf_counter() - t0)
        exp_ok = exp_proc.returncode == 0 and export_out.is_file() and export_out.stat().st_size > 0
        stages["final_export"] = StageBenchmark(
            stage="final_export",
            tool="ffmpeg",
            succeeded=exp_ok,
            elapsed_sec=exp_elapsed,
            total_frames=total_frames,
            fps=(total_frames / exp_elapsed) if exp_ok else 0.0,
            realtime_factor=(duration_sec / exp_elapsed) if exp_ok else 0.0,
            codec=export_profile.video_codec,
            hardware_accelerated=export_profile.hardware_accelerated,
            fallback_used=export_profile.fallback_used,
            output_bytes=export_out.stat().st_size if exp_ok else 0,
            command=export_cmd,
            error=None if exp_ok else exp_proc.stderr.strip(),
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Bang On Pro FFmpeg + MLT Performance Benchmark Runner")
    parser.add_argument("--width", type=int, default=1280, help="Test clip width (default: 1280)")
    parser.add_argument("--height", type=int, default=720, help="Test clip height (default: 720)")
    parser.add_argument("--fps", type=int, default=25, help="Test clip frame rate (default: 25)")
    parser.add_argument("--duration", type=float, default=2.0, help="Test clip duration in seconds (default: 2.0)")
    parser.add_argument("--output", type=str, default="", help="Optional path to write JSON benchmark report")
    args = parser.parse_args(argv)

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
