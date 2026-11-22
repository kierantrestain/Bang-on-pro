"""Runtime hardware probing and validated FFmpeg / MLT profile generation.

Targets Apple Silicon (macOS arm64 + VideoToolbox) with deterministic CPU
fallbacks on Linux/x86_64 or systems without hardware encoders, plus an honest
engineering assessment of Metal vs. OpenGL/Movit inside MLT 7.x.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_TRACKED_ENCODERS = (
    "h264_videotoolbox",
    "hevc_videotoolbox",
    "prores_videotoolbox",
    "prores_ks",
    "libx264",
    "libx265",
    "h264_vaapi",
    "hevc_vaapi",
    "aac",
    "aac_at",
    "pcm_s16le",
)


@dataclass(frozen=True)
class MetalMltAssessment:
    """Honest technical evaluation of GPU/Metal acceleration within MLT 7.x."""

    metal_mlt_native_supported: bool
    movit_opengl_detected: bool
    recommended_mlt_backend: str
    videotoolbox_io_supported: bool
    ai_worker_mps_candidate: bool
    stability_constraints: List[str]
    architecture_roadmap: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class HardwareCapabilities:
    """Runtime hardware, OS, and multimedia toolchain probe results."""

    platform_system: str
    machine: str
    is_apple_silicon: bool
    cpu_brand: str
    cpu_cores_logical: int
    total_ram_gb: float
    free_disk_gb: float
    ffmpeg_path: Optional[str] = None
    ffmpeg_version: str = ""
    ffprobe_path: Optional[str] = None
    melt_path: Optional[str] = None
    melt_version: str = ""
    ffmpeg_hwaccels: List[str] = field(default_factory=list)
    ffmpeg_encoders: List[str] = field(default_factory=list)
    mlt_filters: List[str] = field(default_factory=list)
    videotoolbox_available: bool = False
    metal_assessment: Optional[MetalMltAssessment] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        return data


@dataclass
class HardwareProfile:
    """Validated FFmpeg & MLT export/decode profile configuration."""

    profile_name: str
    description: str
    container: str
    video_codec: str
    audio_codec: str
    pixel_format: str
    hardware_accelerated: bool
    accelerator: str
    fallback_used: bool
    fallback_reason: Optional[str]
    ffmpeg_input_args: List[str]
    ffmpeg_output_args: List[str]
    mlt_consumer_properties: Dict[str, str]
    host_platform: str
    host_arch: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _run_probe_cmd(args: List[str], timeout_sec: float = 5.0) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.SubprocessError):
        return -1, "", ""


def _detect_cpu_brand(system_name: str) -> str:
    if system_name == "darwin":
        rc, out, _ = _run_probe_cmd(["sysctl", "-n", "machdep.cpu.brand_string"])
        if rc == 0 and out.strip():
            return out.strip()
    elif system_name == "linux":
        cpuinfo = Path("/proc/cpuinfo")
        if cpuinfo.is_file():
            try:
                for line in cpuinfo.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.lower().startswith("model name"):
                        _, _, value = line.partition(":")
                        if value.strip():
                            return value.strip()
            except OSError:
                pass
    proc_str = platform.processor().strip()
    return proc_str or platform.machine()


def _detect_total_ram_gb(system_name: str) -> float:
    if system_name == "darwin":
        rc, out, _ = _run_probe_cmd(["sysctl", "-n", "hw.memsize"])
        if rc == 0 and out.strip().isdigit():
            return round(int(out.strip()) / (1024**3), 2)
    elif system_name == "linux":
        meminfo = Path("/proc/meminfo")
        if meminfo.is_file():
            try:
                for line in meminfo.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.startswith("MemTotal:"):
                        parts = line.split()
                        if len(parts) >= 2 and parts[1].isdigit():
                            return round(int(parts[1]) / (1024**2), 2)
            except OSError:
                pass
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return round((pages * page_size) / (1024**3), 2)
    except (ValueError, OSError, AttributeError):
        pass
    return 0.0


def _parse_ffmpeg_hwaccels(ffmpeg_path: str) -> List[str]:
    rc, out, _ = _run_probe_cmd([ffmpeg_path, "-hide_banner", "-hwaccels"])
    if rc != 0:
        return []
    accels: List[str] = []
    seen_header = False
    for line in out.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("hardware acceleration methods"):
            seen_header = True
            continue
        if seen_header and re.match(r"^[a-zA-Z0-9_+-]+$", stripped):
            accels.append(stripped.lower())
    return accels


def _parse_ffmpeg_encoders(ffmpeg_path: str) -> List[str]:
    rc, out, _ = _run_probe_cmd([ffmpeg_path, "-hide_banner", "-encoders"])
    if rc != 0:
        return []
    available: set[str] = set()
    for line in out.splitlines():
        # Lines look like: " V..... libx264  libx264 H.264 / AVC ..."
        match = re.match(r"^\s*[VASF\.XBD]{6}\s+([a-zA-Z0-9_+-]+)\s+", line)
        if match:
            enc = match.group(1)
            if enc in _TRACKED_ENCODERS:
                available.add(enc)
    return sorted(available)


def _parse_mlt_filters(melt_path: str) -> List[str]:
    rc, out, _ = _run_probe_cmd([melt_path, "-query", "filters"])
    if rc != 0:
        return []
    filters: List[str] = []
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            filters.append(stripped[2:].strip())
    return filters


def assess_metal_mlt_support(
    is_apple_silicon: bool,
    videotoolbox_available: bool,
    mlt_filters: List[str],
) -> MetalMltAssessment:
    """Return an honest, verifiable assessment of Metal/OpenGL MLT processing."""
    movit_detected = any(f == "glsl.manager" or f.startswith("movit.") for f in mlt_filters)
    constraints = [
        "MLT 7.x does not ship a native Apple Metal render graph or Metal filter backend.",
        "MLT's GPU effect plugin (Movit / glsl.manager) relies on OpenGL 3.2+ core profile GLSL shaders, "
        "which Apple formally deprecated on macOS and which exhibit context-sharing instability with Qt Quick/RHI on Apple Silicon.",
        "Enabling experimental Movit OpenGL processing on macOS risks frame corruption, thread-affinity crashes, "
        "and stalls across multi-track timeline previews.",
    ]

    if is_apple_silicon and videotoolbox_available:
        recommended = "cpu_neon_sliced_with_videotoolbox_io"
        constraints.append(
            "Production-safe Apple Silicon pipeline: hardware VideoToolbox decode/encode through FFmpeg/MLT avformat "
            "combined with MLT multi-threaded CPU slice rendering (ARM64 NEON + unified memory) and automatic intra-frame proxies."
        )
    elif is_apple_silicon:
        recommended = "cpu_neon_sliced"
        constraints.append(
            "Apple Silicon host detected without VideoToolbox FFmpeg build; rebuild/bundle FFmpeg with --enable-videotoolbox."
        )
    else:
        recommended = "cpu_sliced_ci_fallback"
        constraints.append(
            "Non-Apple-Silicon host detected; Metal/VideoToolbox hardware paths are unavailable and deterministic CPU profiles are used."
        )

    roadmap = (
        "Future Metal MLT Bridge Architecture: introduce an IOSurface/CVPixelBuffer-backed mlt_image format "
        "paired with an out-of-tree Metal compute filter plugin (MTLCommandQueue + MPSImage filters) so MLT "
        "compositing and color transforms run directly on Apple Silicon GPUs without OpenGL/Movit. "
        "Meanwhile, Bang On Pro's out-of-process AI workers can safely use Metal/MPS/CoreML independently of MLT."
    )

    return MetalMltAssessment(
        metal_mlt_native_supported=False,
        movit_opengl_detected=movit_detected,
        recommended_mlt_backend=recommended,
        videotoolbox_io_supported=bool(is_apple_silicon and videotoolbox_available),
        ai_worker_mps_candidate=bool(is_apple_silicon),
        stability_constraints=constraints,
        architecture_roadmap=roadmap,
    )


def probe_hardware_capabilities(disk_path: str | Path = ".") -> HardwareCapabilities:
    """Probe the runtime platform, CPU/RAM/disk, FFmpeg accelerators, and MLT filters."""
    system_name = platform.system().lower()
    machine = platform.machine().lower()
    is_apple_silicon = system_name == "darwin" and machine in ("arm64", "aarch64")

    cpu_brand = _detect_cpu_brand(system_name)
    cpu_cores = os.cpu_count() or 1
    total_ram_gb = _detect_total_ram_gb(system_name)

    usage = shutil.disk_usage(Path(disk_path).resolve())
    free_disk_gb = round(usage.free / (1024**3), 2)

    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    melt_path = shutil.which("melt") or shutil.which("melt-7")

    ffmpeg_version = ""
    hwaccels: List[str] = []
    encoders: List[str] = []
    if ffmpeg_path:
        rc, out, _ = _run_probe_cmd([ffmpeg_path, "-version"])
        if rc == 0 and out:
            ffmpeg_version = out.splitlines()[0].strip()
        hwaccels = _parse_ffmpeg_hwaccels(ffmpeg_path)
        encoders = _parse_ffmpeg_encoders(ffmpeg_path)

    melt_version = ""
    mlt_filters: List[str] = []
    if melt_path:
        rc, out, _ = _run_probe_cmd([melt_path, "-version"])
        if rc == 0 and out:
            melt_version = out.splitlines()[0].strip()
        mlt_filters = _parse_mlt_filters(melt_path)

    videotoolbox_available = (
        system_name == "darwin"
        and "videotoolbox" in hwaccels
        and ("h264_videotoolbox" in encoders or "hevc_videotoolbox" in encoders)
    )

    metal_assessment = assess_metal_mlt_support(
        is_apple_silicon=is_apple_silicon,
        videotoolbox_available=videotoolbox_available,
        mlt_filters=mlt_filters,
    )

    return HardwareCapabilities(
        platform_system=system_name,
        machine=machine,
        is_apple_silicon=is_apple_silicon,
        cpu_brand=cpu_brand,
        cpu_cores_logical=cpu_cores,
        total_ram_gb=total_ram_gb,
        free_disk_gb=free_disk_gb,
        ffmpeg_path=ffmpeg_path,
        ffmpeg_version=ffmpeg_version,
        ffprobe_path=ffprobe_path,
        melt_path=melt_path,
        melt_version=melt_version,
        ffmpeg_hwaccels=hwaccels,
        ffmpeg_encoders=encoders,
        mlt_filters=mlt_filters,
        videotoolbox_available=videotoolbox_available,
        metal_assessment=metal_assessment,
    )


def list_profile_names() -> List[str]:
    """Return supported Bang On Pro hardware/export profile keys."""
    return [
        "h264_master",
        "hevc_master",
        "prores_422_proxy",
        "prores_422",
        "prores_422_hq",
        "edit_proxy_fast",
    ]


def generate_decode_args(capabilities: HardwareCapabilities) -> List[str]:
    """Generate validated FFmpeg hardware decode input arguments for the host."""
    if capabilities.videotoolbox_available and "videotoolbox" in capabilities.ffmpeg_hwaccels:
        return ["-hwaccel", "videotoolbox"]
    return []


def generate_export_profile(
    profile_name: str,
    capabilities: Optional[HardwareCapabilities] = None,
    *,
    video_bitrate: Optional[str] = None,
    audio_bitrate: str = "192k",
    threads: Optional[int] = None,
) -> HardwareProfile:
    """Generate a validated FFmpeg & MLT export/proxy profile with deterministic CPU fallback.

    On Apple Silicon with VideoToolbox encoders available, emits real
    ``h264_videotoolbox`` / ``hevc_videotoolbox`` arguments (and ``prores_ks`` for
    Apple ProRes 422 / 422 HQ / 422 Proxy mastering). When VideoToolbox is
    unavailable (e.g., Linux CI), automatically falls back to deterministic CPU
    encoders (``libx264``, ``libx265``, ``prores_ks``) with explicit metadata.
    """
    caps = capabilities if capabilities is not None else probe_hardware_capabilities()
    worker_threads = max(1, threads if threads is not None else min(caps.cpu_cores_logical, 8))
    decode_args = generate_decode_args(caps)
    aac_codec = "aac_at" if (caps.videotoolbox_available and "aac_at" in caps.ffmpeg_encoders) else "aac"

    vt_h264_ok = caps.videotoolbox_available and "h264_videotoolbox" in caps.ffmpeg_encoders
    vt_hevc_ok = caps.videotoolbox_available and "hevc_videotoolbox" in caps.ffmpeg_encoders

    if profile_name == "h264_master":
        bitrate = video_bitrate or "16M"
        if vt_h264_ok:
            return HardwareProfile(
                profile_name=profile_name,
                description="H.264 High Profile Delivery (Apple Silicon VideoToolbox)",
                container="mp4",
                video_codec="h264_videotoolbox",
                audio_codec=aac_codec,
                pixel_format="yuv420p",
                hardware_accelerated=True,
                accelerator="videotoolbox",
                fallback_used=False,
                fallback_reason=None,
                ffmpeg_input_args=decode_args,
                ffmpeg_output_args=[
                    "-c:v", "h264_videotoolbox",
                    "-profile:v", "high",
                    "-b:v", bitrate,
                    "-allow_sw", "1",
                    "-realtime", "0",
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    "-c:a", aac_codec,
                    "-b:a", audio_bitrate,
                ],
                mlt_consumer_properties={
                    "f": "mp4",
                    "vcodec": "h264_videotoolbox",
                    "vprofile": "high",
                    "vb": bitrate,
                    "pix_fmt": "yuv420p",
                    "movflags": "+faststart",
                    "acodec": aac_codec,
                    "ab": audio_bitrate,
                    "threads": str(worker_threads),
                    "real_time": f"-{min(worker_threads, 4)}",
                },
                host_platform=caps.platform_system,
                host_arch=caps.machine,
            )

        return HardwareProfile(
            profile_name=profile_name,
            description="H.264 High Profile Delivery (CPU libx264 Fallback)",
            container="mp4",
            video_codec="libx264",
            audio_codec="aac",
            pixel_format="yuv420p",
            hardware_accelerated=False,
            accelerator="cpu",
            fallback_used=True,
            fallback_reason="h264_videotoolbox unavailable on current host",
            ffmpeg_input_args=[],
            ffmpeg_output_args=[
                "-c:v", "libx264",
                "-preset", "medium",
                "-crf", "18",
                "-pix_fmt", "yuv420p",
                "-threads", str(worker_threads),
                "-movflags", "+faststart",
                "-c:a", "aac",
                "-b:a", audio_bitrate,
            ],
            mlt_consumer_properties={
                "f": "mp4",
                "vcodec": "libx264",
                "preset": "medium",
                "crf": "18",
                "pix_fmt": "yuv420p",
                "movflags": "+faststart",
                "acodec": "aac",
                "ab": audio_bitrate,
                "threads": str(worker_threads),
                "real_time": f"-{min(worker_threads, 4)}",
            },
            host_platform=caps.platform_system,
            host_arch=caps.machine,
        )

    if profile_name == "hevc_master":
        bitrate = video_bitrate or "12M"
        if vt_hevc_ok:
            return HardwareProfile(
                profile_name=profile_name,
                description="HEVC / H.265 Delivery (Apple Silicon VideoToolbox)",
                container="mp4",
                video_codec="hevc_videotoolbox",
                audio_codec=aac_codec,
                pixel_format="yuv420p",
                hardware_accelerated=True,
                accelerator="videotoolbox",
                fallback_used=False,
                fallback_reason=None,
                ffmpeg_input_args=decode_args,
                ffmpeg_output_args=[
                    "-c:v", "hevc_videotoolbox",
                    "-tag:v", "hvc1",
                    "-b:v", bitrate,
                    "-allow_sw", "1",
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    "-c:a", aac_codec,
                    "-b:a", audio_bitrate,
                ],
                mlt_consumer_properties={
                    "f": "mp4",
                    "vcodec": "hevc_videotoolbox",
                    "vtag": "hvc1",
                    "vb": bitrate,
                    "pix_fmt": "yuv420p",
                    "movflags": "+faststart",
                    "acodec": aac_codec,
                    "ab": audio_bitrate,
                    "threads": str(worker_threads),
                    "real_time": f"-{min(worker_threads, 4)}",
                },
                host_platform=caps.platform_system,
                host_arch=caps.machine,
            )

        return HardwareProfile(
            profile_name=profile_name,
            description="HEVC / H.265 Delivery (CPU libx265 Fallback)",
            container="mp4",
            video_codec="libx265",
            audio_codec="aac",
            pixel_format="yuv420p",
            hardware_accelerated=False,
            accelerator="cpu",
            fallback_used=True,
            fallback_reason="hevc_videotoolbox unavailable on current host",
            ffmpeg_input_args=[],
            ffmpeg_output_args=[
                "-c:v", "libx265",
                "-tag:v", "hvc1",
                "-preset", "medium",
                "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-threads", str(worker_threads),
                "-movflags", "+faststart",
                "-c:a", "aac",
                "-b:a", audio_bitrate,
            ],
            mlt_consumer_properties={
                "f": "mp4",
                "vcodec": "libx265",
                "vtag": "hvc1",
                "preset": "medium",
                "crf": "20",
                "pix_fmt": "yuv420p",
                "movflags": "+faststart",
                "acodec": "aac",
                "ab": audio_bitrate,
                "threads": str(worker_threads),
                "real_time": f"-{min(worker_threads, 4)}",
            },
            host_platform=caps.platform_system,
            host_arch=caps.machine,
        )

    prores_variants = {
        "prores_422_proxy": ("0", "Apple ProRes 422 Proxy (prores_ks)"),
        "prores_422": ("2", "Apple ProRes 422 Standard (prores_ks)"),
        "prores_422_hq": ("3", "Apple ProRes 422 HQ Mastering (prores_ks)"),
    }
    if profile_name in prores_variants:
        prores_level, desc = prores_variants[profile_name]
        return HardwareProfile(
            profile_name=profile_name,
            description=desc,
            container="mov",
            video_codec="prores_ks",
            audio_codec="pcm_s16le",
            pixel_format="yuv422p10le",
            hardware_accelerated=False,
            accelerator="cpu_prores_ks",
            fallback_used=False,
            fallback_reason=None,
            ffmpeg_input_args=decode_args,
            ffmpeg_output_args=[
                "-c:v", "prores_ks",
                "-profile:v", prores_level,
                "-vendor", "apl0",
                "-bits_per_mb", "8000",
                "-pix_fmt", "yuv422p10le",
                "-threads", str(worker_threads),
                "-c:a", "pcm_s16le",
            ],
            mlt_consumer_properties={
                "f": "mov",
                "vcodec": "prores_ks",
                "vprofile": prores_level,
                "vendor": "apl0",
                "pix_fmt": "yuv422p10le",
                "acodec": "pcm_s16le",
                "threads": str(worker_threads),
                "real_time": f"-{min(worker_threads, 4)}",
            },
            host_platform=caps.platform_system,
            host_arch=caps.machine,
        )

    if profile_name == "edit_proxy_fast":
        bitrate = video_bitrate or "6M"
        if vt_h264_ok:
            return HardwareProfile(
                profile_name=profile_name,
                description="Fast Intra-Frame Edit Proxy (Apple Silicon VideoToolbox)",
                container="mp4",
                video_codec="h264_videotoolbox",
                audio_codec=aac_codec,
                pixel_format="yuv420p",
                hardware_accelerated=True,
                accelerator="videotoolbox",
                fallback_used=False,
                fallback_reason=None,
                ffmpeg_input_args=decode_args,
                ffmpeg_output_args=[
                    "-c:v", "h264_videotoolbox",
                    "-b:v", bitrate,
                    "-g", "1",
                    "-bf", "0",
                    "-realtime", "1",
                    "-allow_sw", "1",
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    "-c:a", aac_codec,
                    "-b:a", "128k",
                ],
                mlt_consumer_properties={
                    "f": "mp4",
                    "vcodec": "h264_videotoolbox",
                    "vb": bitrate,
                    "g": "1",
                    "bf": "0",
                    "pix_fmt": "yuv420p",
                    "acodec": aac_codec,
                    "ab": "128k",
                    "threads": str(worker_threads),
                },
                host_platform=caps.platform_system,
                host_arch=caps.machine,
            )

        return HardwareProfile(
            profile_name=profile_name,
            description="Fast Intra-Frame Edit Proxy (CPU libx264 Ultrafast Fallback)",
            container="mp4",
            video_codec="libx264",
            audio_codec="aac",
            pixel_format="yuv420p",
            hardware_accelerated=False,
            accelerator="cpu",
            fallback_used=True,
            fallback_reason="h264_videotoolbox unavailable on current host",
            ffmpeg_input_args=[],
            ffmpeg_output_args=[
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "fastdecode",
                "-crf", "23",
                "-g", "1",
                "-bf", "0",
                "-pix_fmt", "yuv420p",
                "-threads", str(worker_threads),
                "-movflags", "+faststart",
                "-c:a", "aac",
                "-b:a", "128k",
            ],
            mlt_consumer_properties={
                "f": "mp4",
                "vcodec": "libx264",
                "preset": "ultrafast",
                "crf": "23",
                "g": "1",
                "bf": "0",
                "pix_fmt": "yuv420p",
                "acodec": "aac",
                "ab": "128k",
                "threads": str(worker_threads),
            },
            host_platform=caps.platform_system,
            host_arch=caps.machine,
        )

    raise ValueError(
        f"Unknown hardware profile '{profile_name}'. Supported profiles: {', '.join(list_profile_names())}"
    )


def validate_profile_against_capabilities(
    profile: HardwareProfile,
    capabilities: HardwareCapabilities,
) -> Tuple[bool, List[str]]:
    """Verify that a generated profile's video & audio codecs exist in detected FFmpeg."""
    issues: List[str] = []
    if not capabilities.ffmpeg_path:
        issues.append("ffmpeg binary not found in PATH")
        return False, issues

    if profile.video_codec not in capabilities.ffmpeg_encoders:
        issues.append(f"Video encoder '{profile.video_codec}' not reported by ffmpeg")
    if profile.audio_codec not in capabilities.ffmpeg_encoders:
        issues.append(f"Audio encoder '{profile.audio_codec}' not reported by ffmpeg")
    if profile.hardware_accelerated and profile.accelerator not in capabilities.ffmpeg_hwaccels:
        issues.append(f"Hardware accelerator '{profile.accelerator}' not available in ffmpeg")

    return len(issues) == 0, issues
