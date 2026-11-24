"""Media Ingest, SHA-256 Verification, and GCC-PHAT Multicam Sync Agent.

Guarantees strictly read-only access to original camera files, verifies all
streams via ``ffprobe``, computes SHA-256 integrity hashes, extracts mono PCM
via ``ffmpeg``, and recovers exact sub-frame/frame multicam offsets across
mixed frame rates using Generalized Cross-Correlation with Phase Transform
(GCC-PHAT).

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import subprocess
from collections.abc import Callable, Iterator
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
from scipy.fft import next_fast_len
from scipy.signal import butter, sosfiltfilt

from bangonpro.ai_engine.commands import TimelineCommandBuilder
from bangonpro.ai_engine.orchestrator import CancellationToken
from bangonpro.ai_engine.state import (
    MediaAsset,
    ProjectAnalysisState,
    SyncResult,
    TimeInterval,
)
from bangonpro.show_profiles.loader import get_effective_profile_settings

_STANDARD_BROADCAST_FPS: tuple[tuple[int, int], ...] = (
    (24000, 1001),  # 23.976
    (24, 1),
    (25, 1),
    (30000, 1001),  # 29.97
    (30, 1),
    (50, 1),
    (60000, 1001),  # 59.94
    (60, 1),
)


# Shared in-process analysis memo (V1.2 production department): keyed by
# (resolved path, size, mtime_ns) so specialist agents never re-hash or re-probe
# unchanged media. Any modification to a file changes its key and forces a fresh read.
_SHA256_MEMO: dict[tuple[str, int, int], str] = {}
_PROBE_MEMO: dict[tuple[str, int, int], dict[str, Any]] = {}


def media_identity_key(path: str | Path) -> tuple[str, int, int]:
    """Return the (resolved path, size, mtime_ns) identity used by shared analysis caches."""
    file_path = Path(path).resolve()
    st = file_path.stat()
    return (str(file_path), int(st.st_size), int(st.st_mtime_ns))


def compute_file_sha256(
    path: str | Path, chunk_size: int = 1024 * 1024, *, use_cache: bool = True
) -> str:
    """Compute SHA-256 digest of a source media file in strictly read-only mode.

    Results are memoised per media identity; pass ``use_cache=False`` to force a
    full re-read (used by source-immutability verification).
    """
    key: tuple[str, int, int] | None = None
    if use_cache:
        try:
            key = media_identity_key(path)
        except OSError:
            key = None
        if key is not None and key in _SHA256_MEMO:
            return _SHA256_MEMO[key]
    file_path = Path(path)
    hasher = hashlib.sha256()
    with file_path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if key is not None:
        _SHA256_MEMO[key] = digest
    return digest


def verify_source_integrity(state: ProjectAnalysisState) -> dict[str, bool]:
    """Re-hash every registered source media asset and confirm zero mutation."""
    results: dict[str, bool] = {}
    for cam_id, asset in state.media_assets.items():
        current = compute_file_sha256(asset.file_path, use_cache=False)
        results[cam_id] = current == asset.sha256
    return results


def normalize_fps_rational(fps_num: int, fps_den: int) -> tuple[int, int]:
    """Snap near-standard broadcast frame rates (23.976, 24, 25, 29.97, 30, 50, 59.94, 60) to exact rationals."""
    if fps_num <= 0 or fps_den <= 0:
        return (25, 1)
    fps_val = float(fps_num) / float(fps_den)
    for std_num, std_den in _STANDARD_BROADCAST_FPS:
        std_val = float(std_num) / float(std_den)
        if abs(fps_val - std_val) <= 0.005:
            return (std_num, std_den)
    frac = Fraction(fps_num, fps_den).limit_denominator(10000)
    return (int(frac.numerator), int(frac.denominator))


def _parse_rational_fps(rate_str: str | None) -> tuple[int, int]:
    if not rate_str or rate_str in ("0/0", "N/A"):
        return (25, 1)
    try:
        frac = Fraction(rate_str).limit_denominator(10000)
        if frac <= 0:
            return (25, 1)
        return normalize_fps_rational(int(frac.numerator), int(frac.denominator))
    except (ValueError, ZeroDivisionError):
        return (25, 1)


def probe_media_file(path: str | Path) -> dict[str, Any]:
    """Inspect a video or independent audio-recorder file via ``ffprobe``."""
    file_path = Path(path).resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"Media file not found: {file_path}")
    probe_key = media_identity_key(file_path)
    cached_probe = _PROBE_MEMO.get(probe_key)
    if cached_probe is not None:
        return copy.deepcopy(cached_probe)
    result = _probe_media_file_uncached(file_path)
    _PROBE_MEMO[probe_key] = copy.deepcopy(result)
    return result


def _probe_media_file_uncached(file_path: Path) -> dict[str, Any]:

    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(file_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, shell=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for '{file_path}' (rc={proc.returncode}): {proc.stderr.strip()}"
        )

    raw = json.loads(proc.stdout)
    streams = raw.get("streams", [])
    fmt = raw.get("format", {})

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video_stream is None and audio_stream is None:
        raise ValueError(f"No video or audio stream detected in '{file_path}'")

    if video_stream is not None:
        fps_num, fps_den = _parse_rational_fps(
            video_stream.get("avg_frame_rate")
            if video_stream.get("avg_frame_rate") not in (None, "0/0")
            else video_stream.get("r_frame_rate")
        )
        width = int(video_stream.get("width", 0))
        height = int(video_stream.get("height", 0))
        video_codec: str | None = str(video_stream.get("codec_name", "unknown"))
    else:
        fps_num, fps_den = (0, 1)
        width = 0
        height = 0
        video_codec = None

    duration_candidates: list[float] = []
    for src in (
        fmt.get("duration"),
        video_stream.get("duration") if video_stream else None,
        audio_stream.get("duration") if audio_stream else None,
    ):
        if src is not None:
            try:
                val = float(src)
                if val > 0:
                    duration_candidates.append(val)
            except (TypeError, ValueError):
                pass

    if not duration_candidates:
        raise ValueError(f"Unable to determine positive media duration for '{file_path}'")

    duration_sec = duration_candidates[0]

    return {
        "file_path": str(file_path),
        "file_size_bytes": file_path.stat().st_size,
        "duration_sec": duration_sec,
        "width": width,
        "height": height,
        "fps_num": fps_num,
        "fps_den": fps_den,
        "video_codec": video_codec,
        "audio_codec": str(audio_stream.get("codec_name")) if audio_stream else None,
        "audio_sample_rate": int(audio_stream.get("sample_rate", 48000)) if audio_stream else 0,
        "audio_channels": int(audio_stream.get("channels", 0)) if audio_stream else 0,
        "has_video": video_stream is not None,
        "has_audio": audio_stream is not None,
        "raw_format_name": str(fmt.get("format_name", "")),
    }


def extract_mono_pcm_window(
    path: str | Path,
    *,
    sample_rate: int = 16000,
    start_sec: float = 0.0,
    duration_sec: float | None = None,
) -> np.ndarray:
    """Extract a bounded window of mono float32 PCM samples from a media file via ``ffmpeg``."""
    file_path = Path(path).resolve()
    cmd = ["ffmpeg", "-v", "error"]
    if start_sec > 0.0:
        cmd.extend(["-ss", f"{float(start_sec):.6f}"])
    cmd.extend(["-i", str(file_path)])
    if duration_sec is not None and duration_sec > 0.0:
        cmd.extend(["-t", f"{float(duration_sec):.6f}"])
    cmd.extend(
        [
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(int(sample_rate)),
            "-f",
            "f32le",
            "pipe:1",
        ]
    )
    proc = subprocess.run(cmd, capture_output=True, check=False, shell=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg audio extraction failed for '{file_path}': "
            f"{proc.stderr.decode('utf-8', errors='replace').strip()}"
        )
    if not proc.stdout:
        return np.zeros(0, dtype=np.float32)

    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def extract_mono_pcm(path: str | Path, sample_rate: int = 16000) -> np.ndarray:
    """Extract mono float32 PCM samples from a media file via ``ffmpeg``."""
    return extract_mono_pcm_window(path, sample_rate=sample_rate)


def iter_mono_pcm_chunks(
    path: str | Path,
    *,
    sample_rate: int = 16000,
    chunk_duration_sec: float = 30.0,
    start_sec: float = 0.0,
    max_duration_sec: float | None = None,
) -> Iterator[tuple[float, np.ndarray]]:
    """Stream mono float32 PCM in O(1) bounded-RAM chunks of ``chunk_duration_sec`` via ``ffmpeg``."""
    file_path = Path(path).resolve()
    if chunk_duration_sec <= 0.0:
        raise ValueError("chunk_duration_sec must be > 0")

    chunk_samples = max(256, round(chunk_duration_sec * sample_rate))
    chunk_bytes = chunk_samples * 4  # float32 = 4 bytes/sample

    cmd = ["ffmpeg", "-v", "error"]
    if start_sec > 0.0:
        cmd.extend(["-ss", f"{float(start_sec):.6f}"])
    cmd.extend(["-i", str(file_path)])
    if max_duration_sec is not None and max_duration_sec > 0.0:
        cmd.extend(["-t", f"{float(max_duration_sec):.6f}"])
    cmd.extend(
        [
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(int(sample_rate)),
            "-f",
            "f32le",
            "pipe:1",
        ]
    )

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None

    emitted_samples = 0
    leftover = b""
    try:
        while True:
            raw = proc.stdout.read(chunk_bytes)
            if not raw:
                break
            buf = leftover + raw
            usable_bytes = (len(buf) // 4) * 4
            leftover = buf[usable_bytes:]
            if usable_bytes == 0:
                continue
            chunk = np.frombuffer(buf[:usable_bytes], dtype=np.float32).copy()
            chunk_t0 = float(start_sec) + (emitted_samples / float(sample_rate))
            emitted_samples += int(chunk.size)
            yield (round(chunk_t0, 6), chunk)
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
        try:
            _, err_bytes = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err_bytes = proc.communicate()
        if proc.returncode not in (0, None, -15, 255) and emitted_samples == 0:
            raise RuntimeError(
                f"ffmpeg streaming audio extraction failed for '{file_path}': "
                f"{err_bytes.decode('utf-8', errors='replace').strip()}"
            )


def _bandpass_speech(pcm: np.ndarray, sample_rate: int) -> np.ndarray:
    """Apply zero-phase Butterworth bandpass (150 Hz - 6000 Hz) for robust acoustic sync."""
    if pcm.size < 64:
        return pcm.astype(np.float64, copy=True)

    x = pcm.astype(np.float64) - float(np.mean(pcm))
    nyq = 0.5 * sample_rate
    low = min(0.95, max(0.005, 150.0 / nyq))
    high = min(0.98, max(low + 0.05, 6000.0 / nyq))
    sos = butter(4, [low, high], btype="bandpass", output="sos")
    try:
        return sosfiltfilt(sos, x)
    except ValueError:
        return x


def estimate_offset_gcc_phat(
    ref_pcm: np.ndarray,
    target_pcm: np.ndarray,
    sample_rate: int = 16000,
    max_offset_sec: float | None = 30.0,
    sync_window_sec: float | None = 120.0,
) -> tuple[float, float, float]:
    """Estimate time offset of ``target_pcm`` relative to ``ref_pcm`` using GCC-PHAT.

    Bounds correlation to ``sync_window_sec`` so multi-hour recordings run in O(1) RAM.

    Sign convention:
        Returns ``offset_sec`` such that an acoustic event occurring at ``t`` in
        ``ref_pcm`` occurs at ``t + offset_sec`` in ``target_pcm``.
        Equivalently, ``target_pcm(t + offset_sec) ~= ref_pcm(t)``.

    Returns:
        ``(offset_sec, confidence_0_to_1, peak_snr)``
    """
    if ref_pcm.size < 64 or target_pcm.size < 64:
        return (0.0, 0.05, 0.0)

    if sync_window_sec is not None and sync_window_sec > 0.0:
        max_samples = max(1024, round(sync_window_sec * sample_rate))
        if ref_pcm.size > max_samples:
            ref_pcm = ref_pcm[:max_samples]
        if target_pcm.size > max_samples:
            target_pcm = target_pcm[:max_samples]

    x = _bandpass_speech(ref_pcm, sample_rate)
    y = _bandpass_speech(target_pcm, sample_rate)

    if float(np.std(x)) < 1e-7 or float(np.std(y)) < 1e-7:
        return (0.0, 0.05, 0.0)

    n_total = x.size + y.size - 1
    n_fft = int(next_fast_len(n_total))

    X = np.fft.rfft(x, n=n_fft)
    Y = np.fft.rfft(y, n=n_fft)

    # Cross-spectrum: for y[n] = x[n - d], Y * conj(X) has phase -2*pi*m*d/N,
    # whose irfft peaks sharply at lag +d.
    cross = Y * np.conj(X)
    mag = np.abs(cross)
    peak_mag = float(np.max(mag))
    if peak_mag <= 1e-12:
        return (0.0, 0.05, 0.0)

    # Regularized PHAT weighting prevents zero-energy bins from blowing up noise
    eps = max(1e-10, 1e-4 * peak_mag)
    phat = cross / (mag + eps)
    cc = np.fft.irfft(phat, n=n_fft)

    max_possible_lag = min(x.size, y.size) - 1
    if max_offset_sec is not None:
        max_lag = min(max_possible_lag, round(max_offset_sec * sample_rate))
    else:
        max_lag = max_possible_lag

    max_lag = max(1, max_lag)

    # Assemble contiguous correlation slice for lags [-max_lag, ..., +max_lag]
    neg_part = cc[n_fft - max_lag :]
    pos_part = cc[: max_lag + 1]
    cc_window = np.concatenate((neg_part, pos_part))

    peak_idx = int(np.argmax(cc_window))
    peak_val = float(cc_window[peak_idx])

    # Parabolic sub-sample refinement around peak
    delta = 0.0
    if 0 < peak_idx < cc_window.size - 1:
        y0 = float(cc_window[peak_idx - 1])
        y1 = peak_val
        y2 = float(cc_window[peak_idx + 1])
        denom = y0 - 2.0 * y1 + y2
        if abs(denom) > 1e-12:
            delta = float(np.clip(0.5 * (y0 - y2) / denom, -0.5, 0.5))

    lag_samples = (peak_idx - max_lag) + delta
    offset_sec = float(lag_samples) / float(sample_rate)

    # Compute Peak-to-Sidelobe SNR excluding a +/-20 ms guard band around peak
    guard = max(4, round(0.02 * sample_rate))
    mask = np.ones(cc_window.size, dtype=bool)
    lo = max(0, peak_idx - guard)
    hi = min(cc_window.size, peak_idx + guard + 1)
    mask[lo:hi] = False

    sidelobes = cc_window[mask]
    if sidelobes.size > 4:
        bg_mean = float(np.mean(sidelobes))
        bg_std = float(np.std(sidelobes))
        snr = (peak_val - bg_mean) / (bg_std + 1e-12)
    else:
        snr = 0.0

    # Smooth monotonic mapping from SNR -> confidence in [0.05, 0.999]
    confidence = float(np.clip(1.0 - np.exp(-max(0.0, snr - 3.0) / 10.0), 0.05, 0.999))
    return (round(offset_sec, 6), round(confidence, 4), round(float(snr), 3))


def detect_recording_interruptions(
    pcm_or_path: np.ndarray | str | Path,
    sample_rate: int = 16000,
    *,
    silence_threshold_db: float = -38.0,
    interruption_gap_sec: float = 10.0,
    window_sec: float = 0.1,
    chunk_duration_sec: float = 30.0,
) -> list[dict[str, Any]]:
    """Detect mid-recording dead-air interruptions & restart slates exceeding ``interruption_gap_sec``.

    Works in O(1) RAM when passed a filesystem path (streaming via ``iter_mono_pcm_chunks``)
    or directly on an in-memory PCM array.
    """
    win_samples = max(64, round(window_sec * sample_rate))
    rms_db_blocks: list[np.ndarray] = []
    peak_db_blocks: list[np.ndarray] = []

    def _accumulate_chunk(arr: np.ndarray) -> None:
        n_wins = arr.size // win_samples
        if n_wins <= 0:
            return
        shaped = arr[: n_wins * win_samples].reshape(n_wins, win_samples).astype(np.float64)
        rms = np.sqrt(np.mean(shaped * shaped, axis=1) + 1e-18)
        pk = np.max(np.abs(shaped), axis=1)
        rms_db_blocks.append((20.0 * np.log10(rms + 1e-12)).astype(np.float32))
        peak_db_blocks.append((20.0 * np.log10(pk + 1e-12)).astype(np.float32))

    if isinstance(pcm_or_path, (str, Path)):
        for _, chunk in iter_mono_pcm_chunks(
            pcm_or_path, sample_rate=sample_rate, chunk_duration_sec=chunk_duration_sec
        ):
            _accumulate_chunk(chunk)
    else:
        _accumulate_chunk(np.asarray(pcm_or_path, dtype=np.float32))

    if not rms_db_blocks:
        return []

    win_rms_db = np.concatenate(rms_db_blocks)
    win_peak_db = np.concatenate(peak_db_blocks)
    n_wins = win_rms_db.size

    # Active dialogue requires sustained energy (>= 3 windows = 0.3s) so brief restart claps
    # inside dead air are recognized as slates rather than breaking the interruption span!
    raw_active = win_rms_db >= float(silence_threshold_db)
    sustained_active = raw_active.copy()
    idx = 0
    while idx < n_wins:
        if sustained_active[idx]:
            s = idx
            while idx < n_wins and sustained_active[idx]:
                idx += 1
            if (idx - s) < 3:
                sustained_active[s:idx] = False
        else:
            idx += 1

    # Extract sustained active runs
    active_runs: list[tuple[int, int]] = []
    idx = 0
    while idx < n_wins:
        if sustained_active[idx]:
            s = idx
            while idx < n_wins and sustained_active[idx]:
                idx += 1
            active_runs.append((s, idx))
        else:
            idx += 1

    if len(active_runs) < 2:
        return []

    interruptions: list[dict[str, Any]] = []
    for take_idx, (prev_run, next_run) in enumerate(itertools.pairwise(active_runs), start=2):
        gap_s = prev_run[1] * window_sec
        gap_e = next_run[0] * window_sec
        gap_dur = gap_e - gap_s
        if gap_dur >= float(interruption_gap_sec):
            # Check for brief high-crest restart slate/clap inside the dead-air gap
            gap_rms = win_rms_db[prev_run[1] : next_run[0]]
            gap_peak = win_peak_db[prev_run[1] : next_run[0]]
            crest = gap_peak - gap_rms
            slate_indices = np.where((gap_peak >= float(silence_threshold_db) + 8.0) & (crest >= 8.0))[0]
            has_slate = bool(slate_indices.size > 0)
            slate_sec = round(gap_s + float(slate_indices[0]) * window_sec, 4) if has_slate else None
            interruptions.append(
                {
                    "take_number": take_idx,
                    "start_sec": round(gap_s, 4),
                    "end_sec": round(gap_e, 4),
                    "duration_sec": round(gap_dur, 4),
                    "has_restart_slate": has_slate,
                    "restart_slate_sec": slate_sec,
                }
            )
    return interruptions


def run_ingest_sync_agent(
    state: ProjectAnalysisState,
    token: CancellationToken,
    report_progress: Callable[[float, str], None],
) -> dict[str, Any]:
    """Ingest camera media, verify SHA-256 + ffprobe streams, and run GCC-PHAT sync."""
    token.check("ingest_sync start")
    if not state.input_camera_files:
        raise ValueError("No input camera files registered in ProjectAnalysisState")

    profile_cams = {cam["camera_id"]: cam for cam in state.profile.get("cameras", [])}
    effective = get_effective_profile_settings(state.profile)
    profile_roles: dict[str, str] = effective["camera_roles"]
    speaker_cam_map: dict[str, str] = effective["speaker_camera_map"]
    cam_to_speaker = {cid: spk for spk, cid in speaker_cam_map.items()}
    cam_ids = list(state.input_camera_files.keys())

    builder = TimelineCommandBuilder(
        project_id=state.project_id,
        sequence_id=state.sequence_id,
        expected_revision=state.revision,
        agent_id="ingest_sync_agent",
        agent_role="media_ingest_and_sync",
        transaction_label=(
            "Ingest Media & GCC-PHAT Multicam Sync"
            if state.intent.sync_cameras
            else "Ingest & Verify Source Media"
        ),
        requires_approval=False,
        dry_run=state.dry_run,
        fps_num=state.fps_num,
        fps_den=state.fps_den,
    )

    sample_rate = state.audio_sample_rate
    project_fps = state.fps
    sync_window_sec = float(state.metadata.get("sync_window_sec", 120.0))
    long_threshold_sec = float(state.metadata.get("long_recording_threshold_sec", 300.0))
    chunk_duration_sec = float(state.metadata.get("chunk_duration_sec", 30.0))
    force_streaming = bool(state.metadata.get("force_streaming_audio", False))

    warnings: list[str] = []
    missing_media: list[dict[str, Any]] = []
    sync_pcm_by_cam: dict[str, np.ndarray] = {}
    streaming_cams: list[str] = []
    fps_report_cams: dict[str, dict[str, Any]] = {}
    any_fps_conversion = False

    # 1. Probe, SHA-256 hash, and extract bounded mono PCM for every input camera/recorder
    for idx, cam_id in enumerate(cam_ids):
        token.check(f"ingest media {cam_id}")
        report_progress(0.05 + 0.45 * (idx / max(1, len(cam_ids))), f"Verifying {cam_id}")

        raw_path = state.input_camera_files[cam_id]
        try:
            probe = probe_media_file(raw_path)
            sha256 = compute_file_sha256(probe["file_path"])
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            miss_record = {
                "camera_id": cam_id,
                "file_path": str(raw_path),
                "reason": str(exc),
                "error_type": type(exc).__name__,
            }
            missing_media.append(miss_record)
            warnings.append(
                f"missing_media: Camera '{cam_id}' is offline or unreadable ({raw_path}): {exc}"
            )
            marker = state.add_review_marker(
                timeline_sec=0.0,
                label=f"Offline Media: {cam_id} ({Path(str(raw_path)).name})",
                category="missing_media",
                confidence=1.0,
                rationale=f"Camera '{cam_id}' file missing/unreadable; continuing assembly with available cameras",
            )
            builder.add_operation(
                "add_marker",
                track_id="V1",
                in_frame=0,
                out_frame=0,
                timeline_start=0,
                params={
                    "marker_id": marker.marker_id,
                    "category": "missing_media",
                    "category_index": 6,
                    "code": "MISSING_MEDIA",
                    "camera_id": cam_id,
                    "file_path": str(raw_path),
                    "frame": 0,
                    "label": marker.label,
                },
                confidence=1.0,
                rationale=marker.rationale,
            )
            continue

        cam_meta = profile_cams.get(cam_id, {})
        default_role = "audio_recorder" if probe["video_codec"] is None else "wide"
        resolved_role = str(profile_roles.get(cam_id, cam_meta.get("role", default_role)))
        resolved_speaker = cam_to_speaker.get(cam_id, cam_meta.get("speaker_label"))
        media_ref = f"media_{cam_id}"
        asset = MediaAsset(
            media_ref=media_ref,
            camera_id=cam_id,
            camera_role=resolved_role,
            speaker_label=resolved_speaker,
            file_path=probe["file_path"],
            sha256=sha256,
            file_size_bytes=probe["file_size_bytes"],
            duration_sec=probe["duration_sec"],
            width=probe["width"],
            height=probe["height"],
            fps_num=probe["fps_num"],
            fps_den=probe["fps_den"],
            video_codec=probe["video_codec"],
            audio_codec=probe["audio_codec"],
            audio_sample_rate=probe["audio_sample_rate"],
            audio_channels=probe["audio_channels"],
            priority=int(cam_meta.get("priority", 1)),
            verified=True,
        )
        state.media_assets[cam_id] = asset

        fps_converted = bool(asset.has_video and abs(asset.fps - project_fps) > 1e-3)
        if fps_converted:
            any_fps_conversion = True
        fps_report_cams[cam_id] = {
            "source_fps": round(asset.fps, 6),
            "source_fps_rational": f"{asset.fps_num}/{asset.fps_den}",
            "sequence_fps": round(project_fps, 6),
            "sequence_fps_rational": f"{state.fps_num}/{state.fps_den}",
            "fps_conversion_applied": fps_converted,
            "frame_rate_ratio": round(asset.fps / project_fps, 6) if asset.has_video else 0.0,
            "is_audio_only": asset.is_audio_only,
        }

        use_streaming = force_streaming or (asset.duration_sec > long_threshold_sec)
        if asset.audio_codec is not None:
            if use_streaming:
                streaming_cams.append(cam_id)
                sync_pcm = extract_mono_pcm_window(
                    asset.file_path,
                    sample_rate=sample_rate,
                    start_sec=0.0,
                    duration_sec=sync_window_sec,
                )
                sync_pcm_by_cam[cam_id] = sync_pcm
                # Intentionally omit full multi-hour PCM from state.audio_pcm_cache to maintain O(1) RAM
            else:
                pcm = extract_mono_pcm(asset.file_path, sample_rate=sample_rate)
                state.audio_pcm_cache[cam_id] = pcm
                sync_pcm_by_cam[cam_id] = pcm
        else:
            if not use_streaming:
                pcm = np.zeros(round(asset.duration_sec * sample_rate), dtype=np.float32)
                state.audio_pcm_cache[cam_id] = pcm
                sync_pcm_by_cam[cam_id] = pcm
            else:
                streaming_cams.append(cam_id)
                sync_pcm_by_cam[cam_id] = np.zeros(
                    round(min(asset.duration_sec, sync_window_sec) * sample_rate),
                    dtype=np.float32,
                )

        builder.add_operation(
            "import_media",
            media_ref=media_ref,
            params={
                "camera_id": cam_id,
                "name": cam_id,
                "camera_role": asset.camera_role,
                "speaker_label": asset.speaker_label,
                "path": asset.file_path,
                "source_path": asset.file_path,
                "file_path": asset.file_path,
                "sha256": asset.sha256,
                "duration_sec": round(asset.duration_sec, 4),
                "duration_frames": state.sec_to_frame(asset.duration_sec),
                "width": asset.width,
                "height": asset.height,
                "fps_num": asset.fps_num,
                "fps_den": asset.fps_den,
                "source_fps": round(asset.fps, 6),
                "sequence_fps": round(project_fps, 6),
                "fps_conversion_applied": fps_converted,
                "video_codec": asset.video_codec,
                "audio_codec": asset.audio_codec,
                "is_audio_only": asset.is_audio_only,
                "read_only_verified": True,
            },
            confidence=1.0,
            rationale=(
                f"Verified source media '{cam_id}' "
                + (
                    "(independent audio recorder)"
                    if asset.is_audio_only
                    else f"({asset.fps:.3f} fps -> sequence {project_fps:.3f} fps)"
                )
                + " via ffprobe and SHA-256 checksum"
            ),
        )

    state.missing_media = missing_media
    available_cam_ids = [cid for cid in cam_ids if cid in state.media_assets]
    if not available_cam_ids:
        failed_desc = "; ".join(
            f"{m['camera_id']}={m['file_path']} ({m['reason']})" for m in missing_media
        )
        raise FileNotFoundError(
            f"All {len(cam_ids)} input camera media files are missing or unreadable: {failed_desc}"
        )

    state.metadata["frame_rate_report"] = {
        "sequence_fps": round(project_fps, 6),
        "sequence_fps_rational": f"{state.fps_num}/{state.fps_den}",
        "fps_conversion_applied": any_fps_conversion,
        "cameras": fps_report_cams,
    }
    state.metadata["independent_audio_recorders"] = [
        cid
        for cid in available_cam_ids
        if state.media_assets[cid].is_audio_only
        or state.media_assets[cid].camera_role == "audio_recorder"
    ]
    state.metadata["streaming_audio_mode"] = bool(streaming_cams)
    state.metadata["streaming_audio_cameras"] = streaming_cams
    state.metadata["sync_window_sec"] = sync_window_sec
    state.metadata["chunk_duration_sec"] = chunk_duration_sec

    # 2. Choose reference asset (prefer 'wide' or 'desk' video with audio, then audio_recorder, then any with audio)
    ref_cam_id = next(
        (
            cid
            for cid in available_cam_ids
            if state.media_assets[cid].camera_role in ("wide", "desk")
            and state.media_assets[cid].audio_codec
        ),
        next(
            (
                cid
                for cid in available_cam_ids
                if state.media_assets[cid].camera_role == "audio_recorder"
                and state.media_assets[cid].audio_codec
            ),
            next(
                (cid for cid in available_cam_ids if state.media_assets[cid].audio_codec),
                available_cam_ids[0],
            ),
        ),
    )
    ref_pcm = sync_pcm_by_cam[ref_cam_id]

    offsets_sec: dict[str, float] = {ref_cam_id: 0.0}
    offsets_project_frames: dict[str, int] = {ref_cam_id: 0}
    offsets_native_frames: dict[str, int] = {ref_cam_id: 0}
    confidences: dict[str, float] = {ref_cam_id: 1.0}
    peak_snr: dict[str, float] = {ref_cam_id: 100.0}

    other_cams = [cid for cid in available_cam_ids if cid != ref_cam_id]

    for idx, cam_id in enumerate(other_cams):
        token.check(f"gcc-phat sync {cam_id}")
        report_progress(
            0.55 + 0.35 * (idx / max(1, len(other_cams))),
            f"Syncing {cam_id} against {ref_cam_id} (GCC-PHAT)"
            if state.intent.sync_cameras
            else f"Skipping sync for {cam_id} (not requested)",
        )
        asset = state.media_assets[cam_id]
        target_pcm = sync_pcm_by_cam[cam_id]

        if state.intent.sync_cameras and asset.audio_codec is not None and ref_pcm.size > 64:
            offset_sec, conf, snr = estimate_offset_gcc_phat(
                ref_pcm,
                target_pcm,
                sample_rate=sample_rate,
                sync_window_sec=sync_window_sec,
            )
            if conf < 0.5:
                warnings.append(
                    f"Low GCC-PHAT sync confidence ({conf:.2f}, SNR={snr:.1f}) for '{cam_id}'"
                )
        else:
            offset_sec, conf, snr = (0.0, 1.0 if not state.intent.sync_cameras else 0.5, 0.0)
            if state.intent.sync_cameras:
                warnings.append(f"Skipped audio sync for '{cam_id}' (no audio stream)")

        offsets_sec[cam_id] = offset_sec
        offsets_project_frames[cam_id] = round(offset_sec * project_fps)
        offsets_native_frames[cam_id] = (
            round(offset_sec * asset.fps) if asset.has_video else offsets_project_frames[cam_id]
        )
        confidences[cam_id] = conf
        peak_snr[cam_id] = snr

    # 3. Scan reference asset for long mid-recording interruptions / restart slates
    editing_rules = state.profile.get("editing_rules", {})
    interruption_gap_sec = float(editing_rules.get("interruption_gap_sec", 10.0))
    sil_thresh_db = float(
        state.intent.silence_threshold_db
        if state.intent.silence_threshold_db is not None
        else effective["silence_threshold_db"]
    )
    ref_asset = state.media_assets[ref_cam_id]
    if ref_asset.audio_codec is not None and (
        state.intent.remove_silence
        or state.intent.build_rough_cut
        or state.intent.transcribe
        or state.intent.sync_cameras
    ):
        interrupt_src: np.ndarray | str = (
            ref_asset.file_path
            if ref_cam_id in streaming_cams
            else state.audio_pcm_cache.get(ref_cam_id, ref_pcm)
        )
        interruptions = detect_recording_interruptions(
            interrupt_src,
            sample_rate=sample_rate,
            silence_threshold_db=sil_thresh_db,
            interruption_gap_sec=interruption_gap_sec,
            chunk_duration_sec=chunk_duration_sec,
        )
    else:
        interruptions = []

    state.metadata["recording_interruptions"] = interruptions
    state.interruption_intervals = [
        TimeInterval(
            start_sec=float(it["start_sec"]),
            end_sec=float(it["end_sec"]),
            label="interruption_restart_slate" if it["has_restart_slate"] else "interruption_gap",
            confidence=0.94,
        )
        for it in interruptions
    ]

    # Compute common overlapping duration on master sync timeline starting at T=0
    common_end_candidates = [
        state.media_assets[cid].duration_sec - offsets_sec[cid] for cid in available_cam_ids
    ]
    common_duration_sec = max(0.0, min(common_end_candidates))

    sync_method = (
        "skipped_not_requested"
        if not state.intent.sync_cameras
        else ("gcc_phat_16khz_windowed" if streaming_cams else "gcc_phat_16khz")
    )
    state.sync_result = SyncResult(
        reference_camera_id=ref_cam_id,
        method=sync_method,
        sample_rate=sample_rate,
        offsets_sec=offsets_sec,
        offsets_project_frames=offsets_project_frames,
        offsets_native_frames=offsets_native_frames,
        confidences=confidences,
        peak_snr=peak_snr,
        common_duration_sec=round(common_duration_sec, 4),
        warnings=warnings,
    )

    if state.intent.sync_cameras:
        mean_conf = float(np.mean(list(confidences.values()))) if confidences else 1.0
        builder.add_operation(
            "sync_clips",
            media_ref=state.media_assets[ref_cam_id].media_ref,
            params={
                "reference_camera_id": ref_cam_id,
                "method": state.sync_result.method,
                "sample_rate": sample_rate,
                "sync_window_sec": sync_window_sec,
                "offsets_sec": offsets_sec,
                "offsets_project_frames": offsets_project_frames,
                "offsets_native_frames": offsets_native_frames,
                "confidences": confidences,
                "peak_snr": peak_snr,
                "common_duration_sec": round(common_duration_sec, 4),
                "missing_media": missing_media,
                "fps_conversion_applied": any_fps_conversion,
            },
            confidence=mean_conf,
            rationale=(
                f"Synchronised {len(available_cam_ids)} available asset(s) to '{ref_cam_id}' via GCC-PHAT "
                f"(common overlap {common_duration_sec:.2f}s"
                + (f", {len(missing_media)} missing camera(s) skipped" if missing_media else "")
                + ")"
            ),
        )

    report_progress(0.98, "Validated ingest & sync command batch")
    return builder.build()
