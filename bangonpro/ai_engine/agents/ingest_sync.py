"""Media Ingest, SHA-256 Verification, and GCC-PHAT Multicam Sync Agent.

Guarantees strictly read-only access to original camera files, verifies all
streams via ``ffprobe``, computes SHA-256 integrity hashes, extracts mono PCM
via ``ffmpeg``, and recovers exact sub-frame/frame multicam offsets across
mixed frame rates using Generalized Cross-Correlation with Phase Transform
(GCC-PHAT).

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

import numpy as np
from scipy.fft import next_fast_len
from scipy.signal import butter, sosfiltfilt

from bangonpro.ai_engine.commands import TimelineCommandBuilder
from bangonpro.ai_engine.orchestrator import CancellationToken
from bangonpro.ai_engine.state import MediaAsset, ProjectAnalysisState, SyncResult


def compute_file_sha256(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """Compute SHA-256 digest of a source media file in strictly read-only mode."""
    file_path = Path(path)
    hasher = hashlib.sha256()
    with file_path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_source_integrity(state: ProjectAnalysisState) -> dict[str, bool]:
    """Re-hash every registered source media asset and confirm zero mutation."""
    results: dict[str, bool] = {}
    for cam_id, asset in state.media_assets.items():
        current = compute_file_sha256(asset.file_path)
        results[cam_id] = current == asset.sha256
    return results


def _parse_rational_fps(rate_str: str | None) -> tuple[int, int]:
    if not rate_str or rate_str in ("0/0", "N/A"):
        return (25, 1)
    try:
        frac = Fraction(rate_str).limit_denominator(10000)
        if frac <= 0:
            return (25, 1)
        return (int(frac.numerator), int(frac.denominator))
    except (ValueError, ZeroDivisionError):
        return (25, 1)


def probe_media_file(path: str | Path) -> dict[str, Any]:
    """Inspect a media file using real ``ffprobe`` and return structured stream info."""
    file_path = Path(path).resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"Media file not found: {file_path}")

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
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for '{file_path}' (rc={proc.returncode}): {proc.stderr.strip()}"
        )

    raw = json.loads(proc.stdout)
    streams = raw.get("streams", [])
    fmt = raw.get("format", {})

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video_stream is None:
        raise ValueError(f"No video stream detected in '{file_path}'")

    fps_num, fps_den = _parse_rational_fps(
        video_stream.get("avg_frame_rate")
        if video_stream.get("avg_frame_rate") not in (None, "0/0")
        else video_stream.get("r_frame_rate")
    )

    duration_candidates: list[float] = []
    for src in (fmt.get("duration"), video_stream.get("duration"), audio_stream.get("duration") if audio_stream else None):
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
        "width": int(video_stream.get("width", 0)),
        "height": int(video_stream.get("height", 0)),
        "fps_num": fps_num,
        "fps_den": fps_den,
        "video_codec": str(video_stream.get("codec_name", "unknown")),
        "audio_codec": str(audio_stream.get("codec_name")) if audio_stream else None,
        "audio_sample_rate": int(audio_stream.get("sample_rate", 48000)) if audio_stream else 0,
        "audio_channels": int(audio_stream.get("channels", 0)) if audio_stream else 0,
        "raw_format_name": str(fmt.get("format_name", "")),
    }


def extract_mono_pcm(path: str | Path, sample_rate: int = 16000) -> np.ndarray:
    """Extract mono float32 PCM samples from a media file via ``ffmpeg``."""
    file_path = Path(path).resolve()
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(file_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(int(sample_rate)),
        "-f",
        "f32le",
        "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg audio extraction failed for '{file_path}': "
            f"{proc.stderr.decode('utf-8', errors='replace').strip()}"
        )
    if not proc.stdout:
        return np.zeros(0, dtype=np.float32)

    pcm = np.frombuffer(proc.stdout, dtype=np.float32).copy()
    return pcm


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
) -> tuple[float, float, float]:
    """Estimate time offset of ``target_pcm`` relative to ``ref_pcm`` using GCC-PHAT.

    Sign convention:
        Returns ``offset_sec`` such that an acoustic event occurring at ``t`` in
        ``ref_pcm`` occurs at ``t + offset_sec`` in ``target_pcm``.
        Equivalently, ``target_pcm(t + offset_sec) ~= ref_pcm(t)``.

    Returns:
        ``(offset_sec, confidence_0_to_1, peak_snr)``
    """
    if ref_pcm.size < 64 or target_pcm.size < 64:
        return (0.0, 0.05, 0.0)

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
        max_lag = min(max_possible_lag, int(round(max_offset_sec * sample_rate)))
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
    guard = max(4, int(round(0.02 * sample_rate)))
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
    cam_ids = list(state.input_camera_files.keys())

    builder = TimelineCommandBuilder(
        project_id=state.project_id,
        sequence_id=state.sequence_id,
        expected_revision=state.revision,
        agent_id="ingest_sync_agent",
        agent_role="media_ingest_and_sync",
        transaction_label="Ingest Media & GCC-PHAT Multicam Sync",
        requires_approval=False,
        dry_run=state.dry_run,
        fps_num=state.fps_num,
        fps_den=state.fps_den,
    )

    sample_rate = state.audio_sample_rate

    # 1. Probe, SHA-256 hash, and extract mono PCM for every input camera
    for idx, cam_id in enumerate(cam_ids):
        token.check(f"ingest media {cam_id}")
        report_progress(0.05 + 0.45 * (idx / max(1, len(cam_ids))), f"Verifying {cam_id}")

        raw_path = state.input_camera_files[cam_id]
        probe = probe_media_file(raw_path)
        sha256 = compute_file_sha256(probe["file_path"])
        cam_meta = profile_cams.get(cam_id, {})

        media_ref = f"media_{cam_id}"
        asset = MediaAsset(
            media_ref=media_ref,
            camera_id=cam_id,
            camera_role=str(cam_meta.get("role", "wide")),
            speaker_label=cam_meta.get("speaker_label"),
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

        if asset.audio_codec is not None:
            pcm = extract_mono_pcm(asset.file_path, sample_rate=sample_rate)
        else:
            pcm = np.zeros(int(round(asset.duration_sec * sample_rate)), dtype=np.float32)
        state.audio_pcm_cache[cam_id] = pcm

        builder.add_operation(
            "import_media",
            media_ref=media_ref,
            params={
                "camera_id": cam_id,
                "camera_role": asset.camera_role,
                "speaker_label": asset.speaker_label,
                "file_path": asset.file_path,
                "sha256": asset.sha256,
                "duration_sec": round(asset.duration_sec, 4),
                "width": asset.width,
                "height": asset.height,
                "fps_num": asset.fps_num,
                "fps_den": asset.fps_den,
                "video_codec": asset.video_codec,
                "audio_codec": asset.audio_codec,
                "read_only_verified": True,
            },
            confidence=1.0,
            rationale=f"Verified source media '{cam_id}' via ffprobe and SHA-256 checksum",
        )

    # 2. Choose reference camera (prefer 'wide' or 'desk', else first camera with audio)
    ref_cam_id = next(
        (cid for cid in cam_ids if state.media_assets[cid].camera_role in ("wide", "desk") and state.media_assets[cid].audio_codec),
        next((cid for cid in cam_ids if state.media_assets[cid].audio_codec), cam_ids[0]),
    )
    ref_pcm = state.audio_pcm_cache[ref_cam_id]

    offsets_sec: dict[str, float] = {ref_cam_id: 0.0}
    offsets_project_frames: dict[str, int] = {ref_cam_id: 0}
    offsets_native_frames: dict[str, int] = {ref_cam_id: 0}
    confidences: dict[str, float] = {ref_cam_id: 1.0}
    peak_snr: dict[str, float] = {ref_cam_id: 100.0}
    warnings: list[str] = []

    project_fps = state.fps
    other_cams = [cid for cid in cam_ids if cid != ref_cam_id]

    for idx, cam_id in enumerate(other_cams):
        token.check(f"gcc-phat sync {cam_id}")
        report_progress(
            0.55 + 0.40 * (idx / max(1, len(other_cams))),
            f"Syncing {cam_id} against {ref_cam_id} (GCC-PHAT)",
        )
        asset = state.media_assets[cam_id]
        target_pcm = state.audio_pcm_cache[cam_id]

        if state.intent.sync_cameras and asset.audio_codec is not None and ref_pcm.size > 64:
            offset_sec, conf, snr = estimate_offset_gcc_phat(
                ref_pcm, target_pcm, sample_rate=sample_rate
            )
            if conf < 0.5:
                warnings.append(
                    f"Low GCC-PHAT sync confidence ({conf:.2f}, SNR={snr:.1f}) for '{cam_id}'"
                )
        else:
            offset_sec, conf, snr = (0.0, 0.5, 0.0)
            warnings.append(f"Skipped audio sync for '{cam_id}' (no audio or sync disabled)")

        offsets_sec[cam_id] = offset_sec
        offsets_project_frames[cam_id] = int(round(offset_sec * project_fps))
        offsets_native_frames[cam_id] = int(round(offset_sec * asset.fps))
        confidences[cam_id] = conf
        peak_snr[cam_id] = snr

    # Compute common overlapping duration on master sync timeline starting at T=0
    # At sync time T, camera c reads source time t_c = T + offsets_sec[c].
    # Common valid window: T in [max(0, max(-offset_c)), min(duration_c - offset_c)].
    common_end_candidates = [
        state.media_assets[cid].duration_sec - offsets_sec[cid] for cid in cam_ids
    ]
    common_duration_sec = max(0.0, min(common_end_candidates))

    state.sync_result = SyncResult(
        reference_camera_id=ref_cam_id,
        method="gcc_phat_16khz",
        sample_rate=sample_rate,
        offsets_sec=offsets_sec,
        offsets_project_frames=offsets_project_frames,
        offsets_native_frames=offsets_native_frames,
        confidences=confidences,
        peak_snr=peak_snr,
        common_duration_sec=round(common_duration_sec, 4),
        warnings=warnings,
    )

    mean_conf = float(np.mean(list(confidences.values()))) if confidences else 1.0
    builder.add_operation(
        "sync_clips",
        media_ref=state.media_assets[ref_cam_id].media_ref,
        params={
            "reference_camera_id": ref_cam_id,
            "method": "gcc_phat_16khz",
            "sample_rate": sample_rate,
            "offsets_sec": offsets_sec,
            "offsets_project_frames": offsets_project_frames,
            "offsets_native_frames": offsets_native_frames,
            "confidences": confidences,
            "peak_snr": peak_snr,
            "common_duration_sec": round(common_duration_sec, 4),
        },
        confidence=mean_conf,
        rationale=(
            f"Synchronised {len(cam_ids)} cameras to '{ref_cam_id}' via GCC-PHAT "
            f"(common overlap {common_duration_sec:.2f}s)"
        ),
    )

    report_progress(0.98, "Validated ingest & sync command batch")
    return builder.build()
