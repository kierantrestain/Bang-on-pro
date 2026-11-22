"""Automatic proxy policy engine and low-storage macOS cache/model budget manager.

Provides real FFmpeg intra-frame proxy generation (VideoToolbox on Apple Silicon,
fast libx264 ultrafast fallback on CPU) and strict LRU cache eviction that NEVER
modifies or deletes user source media or .kdenlive project files.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .hardware_profiles import (
    HardwareCapabilities,
    generate_export_profile,
    probe_hardware_capabilities,
)


_PROTECTED_PROJECT_SUFFIXES = (
    ".kdenlive",
    ".kdenlive.bak",
    ".otio",
    ".prproj",
    ".fcpxml",
    ".drp",
    ".mlt",
)


@dataclass
class MediaProbeInfo:
    """Technical stream metadata extracted from ffprobe."""

    path: str
    width: int
    height: int
    codec_name: str
    pixel_format: str
    fps: float
    avg_fps: float
    bit_rate_bps: int
    duration_sec: float
    has_audio: bool
    file_size_bytes: int

    @property
    def bit_rate_mbps(self) -> float:
        return self.bit_rate_bps / 1_000_000.0 if self.bit_rate_bps > 0 else 0.0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["bit_rate_mbps"] = round(self.bit_rate_mbps, 3)
        return data


@dataclass(frozen=True)
class ProxyPolicy:
    """Thresholds controlling automatic proxy generation."""

    max_direct_width: int = 2560
    max_direct_height: int = 1440
    max_direct_bitrate_mbps: float = 40.0
    heavy_codecs: Tuple[str, ...] = ("hevc", "h265", "av1", "vp9")
    heavy_pixel_formats: Tuple[str, ...] = ("yuv420p10le", "yuv422p10le", "yuv444p10le", "p010le")
    fps_tolerance: float = 0.05
    vfr_tolerance: float = 0.25
    proxy_target_width: int = 1280
    proxy_target_height: int = 720
    force_proxy: bool = False


@dataclass
class ProxyDecision:
    """Result of evaluating a source clip against the automatic proxy policy."""

    needs_proxy: bool
    reasons: List[str]
    target_width: int
    target_height: int
    target_fps: Optional[float]
    profile_name: str = "edit_proxy_fast"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProxyResult:
    """Outcome of generating or reusing an edit proxy clip."""

    source_path: str
    proxy_path: str
    created: bool
    reused_cached: bool
    decision: ProxyDecision
    video_codec: str
    hardware_accelerated: bool
    elapsed_sec: float
    source_bytes: int
    proxy_bytes: int
    proxy_probe: Optional[MediaProbeInfo] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_path": self.source_path,
            "proxy_path": self.proxy_path,
            "created": self.created,
            "reused_cached": self.reused_cached,
            "decision": self.decision.to_dict(),
            "video_codec": self.video_codec,
            "hardware_accelerated": self.hardware_accelerated,
            "elapsed_sec": round(self.elapsed_sec, 4),
            "source_bytes": self.source_bytes,
            "proxy_bytes": self.proxy_bytes,
            "proxy_probe": self.proxy_probe.to_dict() if self.proxy_probe else None,
        }


@dataclass(frozen=True)
class StorageBudgetConfig:
    """Disk storage quotas for low-storage Apple Silicon Macs (e.g., 256 GB / 512 GB SSDs)."""

    max_cache_gb: float = 20.0
    max_models_gb: float = 8.0
    protected_dirs: Tuple[Path, ...] = ()

    @property
    def max_cache_bytes(self) -> int:
        return max(0, int(self.max_cache_gb * (1024**3)))

    @property
    def max_models_bytes(self) -> int:
        return max(0, int(self.max_models_gb * (1024**3)))


@dataclass
class EvictionReport:
    """Audit record returned by LRU cache budget enforcement."""

    evicted_files: List[str] = field(default_factory=list)
    freed_bytes: int = 0
    remaining_cache_bytes: int = 0
    remaining_models_bytes: int = 0
    max_cache_bytes: int = 0
    max_models_bytes: int = 0
    protected_skipped: List[str] = field(default_factory=list)
    models_over_budget_bytes: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _parse_rational(rate_str: str) -> float:
    if not rate_str or rate_str == "0/0":
        return 0.0
    if "/" in rate_str:
        num_s, _, den_s = rate_str.partition("/")
        try:
            num = float(num_s)
            den = float(den_s)
            return num / den if den != 0.0 else 0.0
        except ValueError:
            return 0.0
    try:
        return float(rate_str)
    except ValueError:
        return 0.0


def probe_media(media_path: str | Path, ffprobe_path: Optional[str] = None) -> MediaProbeInfo:
    """Inspect a media file using real ffprobe and return structured MediaProbeInfo."""
    path = Path(media_path)
    if not path.is_file():
        raise FileNotFoundError(f"Media file not found: {path}")

    ffprobe_bin = ffprobe_path or shutil.which("ffprobe")
    if not ffprobe_bin:
        raise RuntimeError("ffprobe executable not found in PATH")

    cmd = [
        ffprobe_bin,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {proc.stderr.strip()}")

    payload = json.loads(proc.stdout)
    streams = payload.get("streams", [])
    fmt = payload.get("format", {})

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video_stream is None:
        raise ValueError(f"No video stream found in {path}")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    codec_name = str(video_stream.get("codec_name") or "unknown").lower()
    pixel_format = str(video_stream.get("pix_fmt") or "unknown").lower()

    r_fps = _parse_rational(str(video_stream.get("r_frame_rate") or "0/1"))
    avg_fps = _parse_rational(str(video_stream.get("avg_frame_rate") or "0/1"))
    fps = r_fps if r_fps > 0 else avg_fps

    raw_bitrate = video_stream.get("bit_rate") or fmt.get("bit_rate") or 0
    try:
        bit_rate_bps = int(float(raw_bitrate))
    except (TypeError, ValueError):
        bit_rate_bps = 0

    raw_dur = fmt.get("duration") or video_stream.get("duration") or 0.0
    try:
        duration_sec = float(raw_dur)
    except (TypeError, ValueError):
        duration_sec = 0.0

    file_size = path.stat().st_size
    if bit_rate_bps <= 0 and duration_sec > 0.0:
        bit_rate_bps = int((file_size * 8) / duration_sec)

    return MediaProbeInfo(
        path=str(path),
        width=width,
        height=height,
        codec_name=codec_name,
        pixel_format=pixel_format,
        fps=round(fps, 4),
        avg_fps=round(avg_fps, 4),
        bit_rate_bps=bit_rate_bps,
        duration_sec=round(duration_sec, 4),
        has_audio=has_audio,
        file_size_bytes=file_size,
    )


def _compute_proxy_dimensions(src_w: int, src_h: int, max_w: int, max_h: int) -> Tuple[int, int]:
    if src_w <= 0 or src_h <= 0:
        return max_w, max_h
    scale = min(1.0, max_w / float(src_w), max_h / float(src_h))
    out_w = max(2, int(round(src_w * scale)) & ~1)
    out_h = max(2, int(round(src_h * scale)) & ~1)
    return out_w, out_h


def evaluate_proxy_policy(
    media_info: MediaProbeInfo,
    *,
    timeline_fps: float = 25.0,
    policy: Optional[ProxyPolicy] = None,
) -> ProxyDecision:
    """Decide whether a clip requires an edit proxy based on resolution, bitrate, codec, and FPS."""
    pol = policy if policy is not None else ProxyPolicy()
    reasons: List[str] = []

    if pol.force_proxy:
        reasons.append("forced_by_policy")
    if media_info.width > pol.max_direct_width or media_info.height > pol.max_direct_height:
        reasons.append(f"resolution_exceeds_threshold:{media_info.width}x{media_info.height}")
    if media_info.bit_rate_mbps > pol.max_direct_bitrate_mbps:
        reasons.append(f"high_bitrate:{media_info.bit_rate_mbps:.1f}Mbps")
    if media_info.codec_name in pol.heavy_codecs:
        reasons.append(f"heavy_decode_codec:{media_info.codec_name}")
    if media_info.pixel_format in pol.heavy_pixel_formats:
        reasons.append(f"high_bit_depth_pix_fmt:{media_info.pixel_format}")
    if media_info.fps > 0 and timeline_fps > 0 and abs(media_info.fps - timeline_fps) > pol.fps_tolerance:
        reasons.append(f"mixed_fps_mismatch:{media_info.fps:.3f}_vs_{timeline_fps:.3f}")
    if (
        media_info.fps > 0
        and media_info.avg_fps > 0
        and abs(media_info.fps - media_info.avg_fps) > pol.vfr_tolerance
    ):
        reasons.append(f"variable_frame_rate:{media_info.avg_fps:.3f}_vs_{media_info.fps:.3f}")

    target_w, target_h = _compute_proxy_dimensions(
        media_info.width,
        media_info.height,
        pol.proxy_target_width,
        pol.proxy_target_height,
    )

    needs_fps_conform = any(r.startswith(("mixed_fps_mismatch", "variable_frame_rate")) for r in reasons)
    target_fps = timeline_fps if needs_fps_conform else None

    return ProxyDecision(
        needs_proxy=len(reasons) > 0,
        reasons=reasons,
        target_width=target_w,
        target_height=target_h,
        target_fps=target_fps,
        profile_name="edit_proxy_fast",
    )


class CacheBudgetManager:
    """Manages temporary proxy/render caches and AI model disk quotas with LRU eviction.

    Safety invariants:
    - Only files physically located inside ``root_dir/proxies`` or ``root_dir/render_cache``
      are eligible for automatic cache eviction.
    - User source media directories, ``.kdenlive`` / project files, symlinks escaping the
      cache root, and AI model weights (during cache eviction) are NEVER deleted.
    """

    def __init__(self, root_dir: str | Path, config: Optional[StorageBudgetConfig] = None) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.config = config if config is not None else StorageBudgetConfig()
        self.proxies_dir = self.root_dir / "proxies"
        self.render_cache_dir = self.root_dir / "render_cache"
        self.models_dir = self.root_dir / "models"
        self.manifest_path = self.root_dir / "cache_manifest.json"

        self.proxies_dir.mkdir(parents=True, exist_ok=True)
        self.render_cache_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)

        self._protected_dirs: Tuple[Path, ...] = tuple(Path(p).resolve() for p in self.config.protected_dirs)
        self._manifest: Dict[str, Any] = self._load_manifest()

    def _load_manifest(self) -> Dict[str, Any]:
        if self.manifest_path.is_file():
            try:
                data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "entries" in data and "models" in data:
                    return data
            except (OSError, json.JSONDecodeError):
                pass
        return {"schema_version": "1.0", "entries": {}, "models": {}}

    def _save_manifest(self) -> None:
        tmp_path = self.manifest_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(self._manifest, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self.manifest_path)

    @staticmethod
    def _is_within_dir(target: Path, parent: Path) -> bool:
        try:
            target.relative_to(parent)
            return True
        except ValueError:
            return False

    def is_protected_path(self, candidate: Path) -> bool:
        """Return True if candidate is a project file, outside managed cache dirs, or inside protected dirs."""
        name_lower = candidate.name.lower()
        if any(name_lower.endswith(ext) for ext in _PROTECTED_PROJECT_SUFFIXES):
            return True

        # Reject symlinks to prevent escaping managed cache boundaries
        if candidate.is_symlink():
            return True

        resolved = candidate.resolve()
        for protected_dir in self._protected_dirs:
            if self._is_within_dir(resolved, protected_dir):
                return True

        in_proxies = self._is_within_dir(resolved, self.proxies_dir)
        in_render = self._is_within_dir(resolved, self.render_cache_dir)
        if not (in_proxies or in_render):
            return True

        return False

    def register_cache_entry(
        self,
        file_path: str | Path,
        *,
        category: str = "proxies",
        pinned: bool = False,
        last_access_ts: Optional[float] = None,
    ) -> None:
        """Record a managed temporary proxy or render cache file in the LRU manifest."""
        if category not in ("proxies", "render_cache"):
            raise ValueError(f"Unsupported cache category '{category}'; expected 'proxies' or 'render_cache'")

        path = Path(file_path)
        if self.is_protected_path(path):
            raise ValueError(f"Refusing to register protected or out-of-bounds path as evictable cache: {path}")
        if not path.is_file():
            raise FileNotFoundError(f"Cache file does not exist: {path}")

        resolved = path.resolve()
        ts = float(last_access_ts) if last_access_ts is not None else time.time()
        self._manifest["entries"][str(resolved)] = {
            "category": category,
            "pinned": bool(pinned),
            "last_access_ts": ts,
            "size_bytes": resolved.stat().st_size,
        }
        self._save_manifest()

    def touch_entry(self, file_path: str | Path, access_ts: Optional[float] = None) -> None:
        """Update LRU access timestamp for an existing managed cache entry."""
        resolved = str(Path(file_path).resolve())
        if resolved in self._manifest["entries"]:
            self._manifest["entries"][resolved]["last_access_ts"] = (
                float(access_ts) if access_ts is not None else time.time()
            )
            self._save_manifest()

    def pin_entry(self, file_path: str | Path, pinned: bool = True) -> None:
        """Pin or unpin a cache entry to protect active project proxies from eviction."""
        resolved = str(Path(file_path).resolve())
        if resolved in self._manifest["entries"]:
            self._manifest["entries"][resolved]["pinned"] = bool(pinned)
            self._save_manifest()

    def register_model_artifact(
        self,
        model_path: str | Path,
        model_id: str,
        *,
        pinned: bool = False,
        last_access_ts: Optional[float] = None,
    ) -> None:
        """Track a local AI model artifact under ``models/`` against max_models_gb."""
        path = Path(model_path)
        if path.is_symlink():
            raise ValueError(f"Refusing symlink model artifact: {path}")
        resolved = path.resolve()
        if not self._is_within_dir(resolved, self.models_dir):
            raise ValueError(f"Model artifact must live under {self.models_dir}: {resolved}")
        if not resolved.exists():
            raise FileNotFoundError(f"Model artifact does not exist: {resolved}")

        size_bytes = self._path_size_bytes(resolved)
        self._manifest["models"][model_id] = {
            "path": str(resolved),
            "size_bytes": size_bytes,
            "pinned": bool(pinned),
            "last_access_ts": float(last_access_ts) if last_access_ts is not None else time.time(),
        }
        self._save_manifest()

    @staticmethod
    def _path_size_bytes(path: Path) -> int:
        if path.is_file():
            return path.stat().st_size
        total = 0
        if path.is_dir():
            for child in path.rglob("*"):
                if child.is_file() and not child.is_symlink():
                    total += child.stat().st_size
        return total

    def _scan_evictable_cache_files(self) -> Tuple[List[Path], List[str]]:
        evictable: List[Path] = []
        skipped: List[str] = []
        for managed_dir in (self.proxies_dir, self.render_cache_dir):
            if not managed_dir.exists():
                continue
            for item in managed_dir.rglob("*"):
                if item.is_dir():
                    continue
                if self.is_protected_path(item):
                    skipped.append(str(item))
                    continue
                evictable.append(item)
        return evictable, skipped

    def cache_usage_bytes(self) -> int:
        files, _ = self._scan_evictable_cache_files()
        return sum(f.stat().st_size for f in files if f.is_file())

    def models_usage_bytes(self) -> int:
        return self._path_size_bytes(self.models_dir)

    def enforce_cache_budget(self) -> EvictionReport:
        """Evict least-recently-used unpinned temporary proxy/render files to fit max_cache_gb.

        Never touches source media, project files, symlinks, or AI models.
        """
        max_cache = self.config.max_cache_bytes
        max_models = self.config.max_models_bytes

        files, protected_skipped = self._scan_evictable_cache_files()
        entries = self._manifest.get("entries", {})

        records: List[Tuple[float, str, Path, int, bool]] = []
        total_bytes = 0
        for fpath in files:
            resolved_str = str(fpath.resolve())
            stat = fpath.stat()
            size = stat.st_size
            total_bytes += size
            meta = entries.get(resolved_str, {})
            pinned = bool(meta.get("pinned", False))
            last_access = float(meta.get("last_access_ts", stat.st_mtime))
            records.append((last_access, resolved_str, fpath, size, pinned))

        # True LRU order: oldest access timestamp first
        records.sort(key=lambda item: (item[0], item[1]))

        evicted_files: List[str] = []
        freed_bytes = 0

        for _, resolved_str, fpath, size, pinned in records:
            if total_bytes <= max_cache:
                break
            if pinned:
                protected_skipped.append(resolved_str)
                continue
            if self.is_protected_path(fpath):
                protected_skipped.append(resolved_str)
                continue
            try:
                fpath.unlink()
                evicted_files.append(resolved_str)
                freed_bytes += size
                total_bytes -= size
                entries.pop(resolved_str, None)
            except OSError:
                protected_skipped.append(resolved_str)

        # Prune stale manifest entries
        for key in list(entries.keys()):
            if not Path(key).exists():
                entries.pop(key, None)

        self._save_manifest()

        models_bytes = self.models_usage_bytes()
        models_over = max(0, models_bytes - max_models)

        return EvictionReport(
            evicted_files=evicted_files,
            freed_bytes=freed_bytes,
            remaining_cache_bytes=total_bytes,
            remaining_models_bytes=models_bytes,
            max_cache_bytes=max_cache,
            max_models_bytes=max_models,
            protected_skipped=protected_skipped,
            models_over_budget_bytes=models_over,
        )

    def get_model_eviction_candidates(self) -> List[Dict[str, Any]]:
        """Return unpinned AI model artifacts ordered LRU if model storage exceeds max_models_gb."""
        models_bytes = self.models_usage_bytes()
        if models_bytes <= self.config.max_models_bytes:
            return []
        candidates: List[Dict[str, Any]] = []
        for model_id, info in self._manifest.get("models", {}).items():
            if info.get("pinned", False):
                continue
            candidates.append({"model_id": model_id, **info})
        candidates.sort(key=lambda item: float(item.get("last_access_ts", 0.0)))
        return candidates


def _proxy_cache_key(source_path: Path, decision: ProxyDecision) -> str:
    stat = source_path.stat()
    fingerprint = (
        f"{source_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|"
        f"{decision.target_width}x{decision.target_height}|{decision.target_fps}|{decision.profile_name}"
    )
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]


def generate_proxy(
    source_path: str | Path,
    cache_manager: CacheBudgetManager,
    *,
    timeline_fps: float = 25.0,
    policy: Optional[ProxyPolicy] = None,
    capabilities: Optional[HardwareCapabilities] = None,
    force: bool = False,
) -> ProxyResult:
    """Evaluate proxy policy and execute real FFmpeg intra-frame proxy transcoding."""
    src = Path(source_path).resolve()
    caps = capabilities if capabilities is not None else probe_hardware_capabilities(cache_manager.root_dir)
    if not caps.ffmpeg_path:
        raise RuntimeError("ffmpeg executable not found in PATH")

    src_probe = probe_media(src, ffprobe_path=caps.ffprobe_path)
    pol = policy if policy is not None else ProxyPolicy()
    if force and not pol.force_proxy:
        pol = ProxyPolicy(
            max_direct_width=pol.max_direct_width,
            max_direct_height=pol.max_direct_height,
            max_direct_bitrate_mbps=pol.max_direct_bitrate_mbps,
            heavy_codecs=pol.heavy_codecs,
            heavy_pixel_formats=pol.heavy_pixel_formats,
            fps_tolerance=pol.fps_tolerance,
            vfr_tolerance=pol.vfr_tolerance,
            proxy_target_width=pol.proxy_target_width,
            proxy_target_height=pol.proxy_target_height,
            force_proxy=True,
        )

    decision = evaluate_proxy_policy(src_probe, timeline_fps=timeline_fps, policy=pol)
    hw_profile = generate_export_profile(decision.profile_name, capabilities=caps)

    if not decision.needs_proxy:
        return ProxyResult(
            source_path=str(src),
            proxy_path=str(src),
            created=False,
            reused_cached=False,
            decision=decision,
            video_codec=src_probe.codec_name,
            hardware_accelerated=False,
            elapsed_sec=0.0,
            source_bytes=src_probe.file_size_bytes,
            proxy_bytes=src_probe.file_size_bytes,
            proxy_probe=src_probe,
        )

    key = _proxy_cache_key(src, decision)
    proxy_filename = f"{src.stem}_{key}_proxy.{hw_profile.container}"
    proxy_path = cache_manager.proxies_dir / proxy_filename

    if proxy_path.is_file() and proxy_path.stat().st_size > 0:
        cache_manager.touch_entry(proxy_path)
        existing_probe = probe_media(proxy_path, ffprobe_path=caps.ffprobe_path)
        return ProxyResult(
            source_path=str(src),
            proxy_path=str(proxy_path),
            created=False,
            reused_cached=True,
            decision=decision,
            video_codec=existing_probe.codec_name,
            hardware_accelerated=hw_profile.hardware_accelerated,
            elapsed_sec=0.0,
            source_bytes=src_probe.file_size_bytes,
            proxy_bytes=proxy_path.stat().st_size,
            proxy_probe=existing_probe,
        )

    tmp_proxy = proxy_path.with_name(f".{proxy_path.stem}.tmp.{hw_profile.container}")
    vf_chain = [
        f"scale={decision.target_width}:{decision.target_height}:force_original_aspect_ratio=decrease",
        "pad=ceil(iw/2)*2:ceil(ih/2)*2",
    ]

    cmd: List[str] = [caps.ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error"]
    cmd.extend(hw_profile.ffmpeg_input_args)
    cmd.extend(["-i", str(src)])
    cmd.extend(["-vf", ",".join(vf_chain)])
    if decision.target_fps is not None:
        cmd.extend(["-r", f"{decision.target_fps:.3f}"])
    cmd.extend(hw_profile.ffmpeg_output_args)
    if not src_probe.has_audio:
        cmd.append("-an")
    cmd.append(str(tmp_proxy))

    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
    elapsed = time.perf_counter() - t0

    if proc.returncode != 0 or not tmp_proxy.is_file():
        if tmp_proxy.exists():
            tmp_proxy.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg proxy generation failed (rc={proc.returncode}): {proc.stderr.strip()}")

    tmp_proxy.replace(proxy_path)
    proxy_probe = probe_media(proxy_path, ffprobe_path=caps.ffprobe_path)

    cache_manager.register_cache_entry(proxy_path, category="proxies")
    cache_manager.enforce_cache_budget()

    return ProxyResult(
        source_path=str(src),
        proxy_path=str(proxy_path),
        created=True,
        reused_cached=False,
        decision=decision,
        video_codec=hw_profile.video_codec,
        hardware_accelerated=hw_profile.hardware_accelerated,
        elapsed_sec=elapsed,
        source_bytes=src_probe.file_size_bytes,
        proxy_bytes=proxy_path.stat().st_size,
        proxy_probe=proxy_probe,
    )
