"""Transcription, Energy/Spectral-Centroid VAD & Multi-Mic Speaker Diarisation Agent.

Executes genuine local Whisper-family neural ASR (``faster-whisper`` or
``openai-whisper``) when the backend package and local model weights are
installed and selected. When neural ASR is unavailable or not enabled, falls
back honestly to deterministic CPU DSP spectral-centroid VAD and multi-mic
loudness diarisation — explicitly reporting ``is_neural_inference=False``,
``fallback_used=True``, ``fallback_reason``, and
``transcript_quality="vad_speech_segments_only"`` without fabricating spoken words.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import bisect
import importlib.util
import itertools
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
from scipy.ndimage import uniform_filter1d

from bangonpro.ai_engine.agents.ingest_sync import (
    extract_mono_pcm,
    iter_mono_pcm_chunks,
)
from bangonpro.ai_engine.commands import TimelineCommandBuilder
from bangonpro.ai_engine.model_manager import ModelManager
from bangonpro.ai_engine.orchestrator import CancellationToken
from bangonpro.ai_engine.state import (
    AsrBackendReport,
    ProjectAnalysisState,
    StorySection,
    TimeInterval,
    TranscriptSegment,
)
from bangonpro.ai_engine.transcript import (
    build_transcript_document,
    build_words_for_segment,
    is_filler_or_silence_text,
    save_transcript,
)
from bangonpro.show_profiles.loader import get_effective_profile_settings


def detect_available_asr_backends() -> dict[str, bool]:
    """Feature-detect optional ML ASR packages without importing them eagerly."""
    return {
        "whisperx": importlib.util.find_spec("whisperx") is not None,
        "faster_whisper": importlib.util.find_spec("faster_whisper") is not None,
        "openai_whisper": importlib.util.find_spec("whisper") is not None,
        "deterministic_multimic_vad": True,
    }


def inspect_asr_backend_status(model_dir: str | Path | None = None) -> dict[str, Any]:
    """Return honest ASR package vs disk-weights status via ``ModelManager``."""
    mgr = ModelManager(model_dir=model_dir)
    asr_models = mgr.list_models(family="asr_whisper")
    return {
        "packages_installed": detect_available_asr_backends(),
        "any_neural_package_installed": any(m.backend_package_installed for m in asr_models),
        "any_model_available_on_disk": any(m.model_available_on_disk for m in asr_models),
        "ready_models": [m.model_id for m in asr_models if m.status in ("ready", "loaded")],
        "models": [m.to_dict() for m in asr_models],
    }


def _align_pcm_to_master(
    pcm: np.ndarray,
    offset_sec: float,
    total_samples: int,
    sample_rate: int,
) -> np.ndarray:
    """Slice/pad a camera PCM array so index 0 aligns to master sync time T=0."""
    out = np.zeros(total_samples, dtype=np.float32)
    if pcm.size == 0 or total_samples <= 0:
        return out

    src_start = round(offset_sec * sample_rate)
    if src_start >= 0:
        if src_start < pcm.size:
            copy_len = min(total_samples, pcm.size - src_start)
            out[:copy_len] = pcm[src_start : src_start + copy_len]
    else:
        dst_start = -src_start
        if dst_start < total_samples:
            copy_len = min(total_samples - dst_start, pcm.size)
            out[dst_start : dst_start + copy_len] = pcm[:copy_len]
    return out


def _compute_frame_spectral_features(
    pcm: np.ndarray,
    sample_rate: int,
    frame_sec: float = 0.02,
    hop_sec: float = 0.01,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Compute per-frame full RMS dB, speech-band (250-3500 Hz) RMS dB, speech ratio, and spectral centroid."""
    frame_len = max(32, round(frame_sec * sample_rate))
    hop_len = max(16, round(hop_sec * sample_rate))

    if pcm.size < frame_len:
        padded = np.pad(pcm, (0, frame_len - pcm.size))
    else:
        padded = pcm

    n_frames = 1 + (padded.size - frame_len) // hop_len
    indices = np.arange(frame_len)[None, :] + (np.arange(n_frames) * hop_len)[:, None]
    frames = padded[indices].astype(np.float64)

    # Full-band RMS in dBFS
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-18)
    full_db = 20.0 * np.log10(rms + 1e-12)

    # Windowed FFT for speech-band energy & spectral centroid
    window = np.hanning(frame_len)[None, :]
    spec = np.fft.rfft(frames * window, axis=1)
    mag = np.abs(spec)
    power = mag * mag
    freqs = np.fft.rfftfreq(frame_len, d=1.0 / sample_rate)

    speech_mask = (freqs >= 250.0) & (freqs <= 3500.0)
    total_power = np.sum(power, axis=1) + 1e-18
    speech_power = np.sum(power[:, speech_mask], axis=1)
    speech_ratio = speech_power / total_power

    # Estimate speech-band RMS dB scaled cleanly from full-band RMS
    speech_rms = rms * np.sqrt(np.clip(speech_ratio, 1e-9, 1.0))
    speech_db = 20.0 * np.log10(speech_rms + 1e-12)

    mag_sum = np.sum(mag, axis=1) + 1e-12
    centroid_hz = np.sum(mag * freqs[None, :], axis=1) / mag_sum

    return (
        full_db.astype(np.float32),
        speech_db.astype(np.float32),
        speech_ratio.astype(np.float32),
        centroid_hz.astype(np.float32),
    )


def compute_frame_spectral_features_chunked(
    pcm: np.ndarray,
    sample_rate: int,
    *,
    frame_sec: float = 0.02,
    hop_sec: float = 0.01,
    chunk_duration_sec: float = 30.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Compute per-frame spectral features in O(1)-RAM bounded windows of ``chunk_duration_sec``."""
    frame_len = max(32, round(frame_sec * sample_rate))
    hop_len = max(16, round(hop_sec * sample_rate))
    chunk_samples = max(frame_len * 2, round(max(0.5, chunk_duration_sec) * sample_rate))

    if pcm.size <= chunk_samples:
        return _compute_frame_spectral_features(
            pcm, sample_rate, frame_sec=frame_sec, hop_sec=hop_sec
        )

    total_frames = 1 + (max(pcm.size, frame_len) - frame_len) // hop_len
    full_out = np.empty(total_frames, dtype=np.float32)
    speech_out = np.empty(total_frames, dtype=np.float32)
    ratio_out = np.empty(total_frames, dtype=np.float32)
    cent_out = np.empty(total_frames, dtype=np.float32)

    frames_per_chunk = max(1, (chunk_samples - frame_len) // hop_len + 1)
    frame_cursor = 0
    while frame_cursor < total_frames:
        batch_frames = min(frames_per_chunk, total_frames - frame_cursor)
        s_start = frame_cursor * hop_len
        s_end = s_start + (batch_frames - 1) * hop_len + frame_len
        sub_pcm = pcm[s_start : min(pcm.size, s_end)]
        if sub_pcm.size < (batch_frames - 1) * hop_len + frame_len:
            sub_pcm = np.pad(
                sub_pcm, (0, (batch_frames - 1) * hop_len + frame_len - sub_pcm.size)
            )
        f_db, s_db, s_rat, c_hz = _compute_frame_spectral_features(
            sub_pcm, sample_rate, frame_sec=frame_sec, hop_sec=hop_sec
        )
        take = min(batch_frames, f_db.size)
        full_out[frame_cursor : frame_cursor + take] = f_db[:take]
        speech_out[frame_cursor : frame_cursor + take] = s_db[:take]
        ratio_out[frame_cursor : frame_cursor + take] = s_rat[:take]
        cent_out[frame_cursor : frame_cursor + take] = c_hz[:take]
        frame_cursor += take

    return full_out, speech_out, ratio_out, cent_out


def compute_frame_spectral_features_streaming(
    media_path: str | Path,
    *,
    offset_sec: float,
    total_samples: int,
    sample_rate: int = 16000,
    frame_sec: float = 0.02,
    hop_sec: float = 0.01,
    chunk_duration_sec: float = 30.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Stream audio from ``media_path`` in bounded chunks to compute aligned spectral features in O(1) RAM."""
    frame_len = max(32, round(frame_sec * sample_rate))
    hop_len = max(16, round(hop_sec * sample_rate))
    eff_samples = max(total_samples, frame_len)
    n_frames_expected = 1 + (eff_samples - frame_len) // hop_len

    full_out = np.full(n_frames_expected, -240.0, dtype=np.float32)
    speech_out = np.full(n_frames_expected, -240.0, dtype=np.float32)
    ratio_out = np.zeros(n_frames_expected, dtype=np.float32)
    cent_out = np.zeros(n_frames_expected, dtype=np.float32)

    src_start = round(offset_sec * sample_rate)
    if src_start >= 0:
        stream_start_sec = float(src_start) / float(sample_rate)
        carry = np.zeros(0, dtype=np.float32)
        needed_stream_sec = (float(eff_samples) / float(sample_rate)) + 0.5
    else:
        stream_start_sec = 0.0
        lead_zeros = min(eff_samples, -src_start)
        carry = np.zeros(lead_zeros, dtype=np.float32)
        needed_stream_sec = max(
            0.5, float(eff_samples - lead_zeros) / float(sample_rate) + 0.5
        )

    frame_cursor = 0

    def _drain_carry(*, final: bool = False) -> None:
        nonlocal carry, frame_cursor
        if frame_cursor >= n_frames_expected:
            carry = np.zeros(0, dtype=np.float32)
            return
        if carry.size < frame_len:
            if not final or carry.size == 0:
                return
            carry = np.pad(carry, (0, frame_len - carry.size))

        avail_frames = 1 + (carry.size - frame_len) // hop_len
        n_emit = min(avail_frames, n_frames_expected - frame_cursor)
        if n_emit <= 0:
            return
        slice_end = (n_emit - 1) * hop_len + frame_len
        f_db, s_db, s_rat, c_hz = _compute_frame_spectral_features(
            carry[:slice_end], sample_rate, frame_sec=frame_sec, hop_sec=hop_sec
        )
        take = min(n_emit, f_db.size)
        full_out[frame_cursor : frame_cursor + take] = f_db[:take]
        speech_out[frame_cursor : frame_cursor + take] = s_db[:take]
        ratio_out[frame_cursor : frame_cursor + take] = s_rat[:take]
        cent_out[frame_cursor : frame_cursor + take] = c_hz[:take]
        frame_cursor += take
        carry = carry[take * hop_len :]

    _drain_carry(final=False)
    for _, chunk in iter_mono_pcm_chunks(
        media_path,
        sample_rate=sample_rate,
        chunk_duration_sec=chunk_duration_sec,
        start_sec=stream_start_sec,
        max_duration_sec=needed_stream_sec,
    ):
        if frame_cursor >= n_frames_expected:
            break
        carry = chunk if carry.size == 0 else np.concatenate((carry, chunk))
        _drain_carry(final=False)

    _drain_carry(final=True)
    return full_out, speech_out, ratio_out, cent_out


def _morphological_clean_mask(
    mask: np.ndarray,
    *,
    min_gap_frames: int,
    min_run_frames: int,
) -> np.ndarray:
    """Bridge short gaps inside speech and remove ultra-short transient blips."""
    cleaned = mask.astype(bool).copy()
    n = cleaned.size
    if n == 0:
        return cleaned

    # 1. Fill short False gaps flanked by True
    idx = 0
    while idx < n:
        if not cleaned[idx]:
            start = idx
            while idx < n and not cleaned[idx]:
                idx += 1
            end = idx
            if start > 0 and end < n and (end - start) < min_gap_frames:
                cleaned[start:end] = True
        else:
            idx += 1

    # 2. Remove short True blips (e.g. sync claps, desk knocks)
    idx = 0
    while idx < n:
        if cleaned[idx]:
            start = idx
            while idx < n and cleaned[idx]:
                idx += 1
            end = idx
            if (end - start) < min_run_frames:
                cleaned[start:end] = False
        else:
            idx += 1

    return cleaned


def _extract_true_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    n = mask.size
    idx = 0
    while idx < n:
        if mask[idx]:
            start = idx
            while idx < n and mask[idx]:
                idx += 1
            runs.append((start, idx))
        else:
            idx += 1
    return runs


def _majority_smooth_labels(labels: list[int], half_window: int = 15) -> list[int]:
    n = len(labels)
    if n == 0 or half_window <= 0:
        return list(labels)
    arr = np.asarray(labels, dtype=np.int32)
    smoothed = np.empty_like(arr)
    for i in range(n):
        lo = max(0, i - half_window)
        hi = min(n, i + half_window + 1)
        vals, counts = np.unique(arr[lo:hi], return_counts=True)
        smoothed[i] = vals[int(np.argmax(counts))]
    return smoothed.tolist()


def measure_nonspeech_activity(
    features_by_cam: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]],
    cam_ids: list[str],
    vad_mask: np.ndarray,
    *,
    hop_sec: float,
    speech_thresh_db: float,
    speaker_labels: dict[str, str | None] | None = None,
) -> tuple[list[dict[str, Any]], float]:
    """Find reaction audio the VAD does not treat as dialogue (V1.2.2).

    * **In pauses:** sustained (>= 0.5 s) sound on any microphone at least 18 dB above the room
      floor and within 14 dB of the speech threshold (laughter or applause from the room, a
      listener reacting, a quiet answer). These are protected by the Rough Cut.
    * **Inside speech runs:** diffuse broadband sound (high spectral centroid, low
      speech-band ratio, similar level on every microphone) is labelled applause.

    Returns ``(events, noise_floor_db)``; events are master-clock dicts. Deterministic DSP
    only; it never claims words.
    """
    if not cam_ids:
        return [], -120.0
    n = min(features_by_cam[c][0].size for c in cam_ids)
    n = min(n, vad_mask.size)
    if n <= 0:
        return [], -120.0
    full_all = np.vstack([features_by_cam[c][0][:n] for c in cam_ids]).astype(np.float64)
    audible = full_all.max(axis=1) > -150.0
    if not bool(audible.any()):
        return [], -120.0
    cams = [c for c, a in zip(cam_ids, audible) if a]
    full = full_all[audible]
    ratio = np.vstack([features_by_cam[c][2][:n] for c in cams]).astype(np.float64)
    cent = np.vstack([features_by_cam[c][3][:n] for c in cams]).astype(np.float64)
    max_full = full.max(axis=0)
    floor = float(np.percentile(max_full, 10))
    best = full.argmax(axis=0)
    idx = np.arange(n)
    best_cent = cent[best, idx]
    best_ratio = ratio[best, idx]
    multi = len(cams) > 1
    spread = (full.max(axis=0) - full.min(axis=0)) if multi else np.zeros(n)
    vm = vad_mask[:n].astype(bool)
    loud = (max_full >= floor + 18.0) & (max_full >= speech_thresh_db - 14.0)
    in_gaps = _morphological_clean_mask(
        (~vm) & loud, min_gap_frames=max(2, round(0.15 / hop_sec)), min_run_frames=max(5, round(0.5 / hop_sec))
    )
    diffuse = (best_cent >= 2500.0) & (best_ratio <= 0.6) & ((spread <= 8.0) if multi else (best_cent >= 3000.0))
    in_speech = _morphological_clean_mask(
        vm & diffuse & (max_full >= floor + 15.0), min_gap_frames=max(2, round(0.2 / hop_sec)), min_run_frames=max(8, round(0.8 / hop_sec))
    )
    labels = speaker_labels or {}
    events: list[dict[str, Any]] = []
    for mask, where in ((in_gaps, "pause"), (in_speech, "speech_run")):
        for fs, fe in _extract_true_runs(mask):
            c_mean = float(np.mean(best_cent[fs:fe]))
            r_mean = float(np.mean(best_ratio[fs:fe]))
            sp = float(np.mean(spread[fs:fe])) if multi else None
            dom = cams[int(np.argmax(full[:, fs:fe].mean(axis=1)))]
            dur = (fe - fs) * hop_sec
            if where == "speech_run" or (c_mean >= 2200.0 and r_mean <= 0.65 and (sp is None or sp <= 9.0) and dur >= 0.6):
                kind = "applause"
            elif r_mean >= 0.45 and c_mean < 2200.0:
                kind = "voice_activity"
            else:
                kind = "activity"
            events.append(
                {
                    "start_sec": round(fs * hop_sec, 4),
                    "end_sec": round(fe * hop_sec, 4),
                    "where": where,
                    "kind_hint": kind,
                    "peak_db_above_floor": round(float(np.max(max_full[fs:fe]) - floor), 2),
                    "mean_db_above_floor": round(float(np.mean(max_full[fs:fe]) - floor), 2),
                    "centroid_hz": round(c_mean, 1),
                    "speech_band_ratio": round(r_mean, 3),
                    "mic_spread_db": round(sp, 2) if sp is not None else None,
                    "dominant_camera": dom,
                    "dominant_speaker": labels.get(dom),
                    "evidence_class": "dsp_features",
                }
            )
    events.sort(key=lambda e: e["start_sec"])
    return events, round(floor, 2)


def _carve_intervals(intervals: list[TimeInterval], regions: list[tuple[float, float]], min_len: dict[str, float]) -> list[TimeInterval]:
    """Remove ``regions`` (speech) from silence intervals, keeping labels; drop short leftovers."""
    out: list[TimeInterval] = []
    for iv in intervals:
        pieces = [(iv.start_sec, iv.end_sec)]
        for rs, re_ in regions:
            nxt: list[tuple[float, float]] = []
            for a, b in pieces:
                if re_ <= a or rs >= b:
                    nxt.append((a, b))
                    continue
                if rs > a:
                    nxt.append((a, rs))
                if re_ < b:
                    nxt.append((re_, b))
            pieces = nxt
        for a, b in pieces:
            if b - a >= min_len.get(iv.label, 0.0) and b - a > 1e-3:
                out.append(TimeInterval(round(a, 4), round(b, 4), iv.label, iv.confidence))
    return out


def _try_neural_whisper_asr(
    audio_path: str,
    state: ProjectAnalysisState,
) -> tuple[AsrBackendReport, list[dict[str, Any]] | None]:
    """Attempt genuine local ``faster-whisper`` / ``openai-whisper`` inference with honest fallback reporting.

    NEVER labels DSP VAD or synthetic text as neural Whisper inference.
    """
    provider_cfg = state.metadata.get("provider_config") or {}
    provider_id = str(provider_cfg.get("provider_id", "local_deterministic"))
    requested_model_id = str(state.metadata.get("asr_model_id") or "").strip() or None
    model_dir = state.metadata.get("model_dir")
    mgr = ModelManager(model_dir=model_dir)
    backends = detect_available_asr_backends()

    whisper_requested = bool(
        state.metadata.get("enable_whisper", False)
        or requested_model_id is not None
        or provider_id in ("local_faster_whisper", "local_openai_whisper")
    )

    if not whisper_requested:
        any_pkg = bool(backends["faster_whisper"] or backends["openai_whisper"])
        any_disk = any(m.model_available_on_disk for m in mgr.list_models(family="asr_whisper"))
        return (
            AsrBackendReport(
                backend_id="deterministic_multimic_vad",
                model_id=None,
                is_neural_inference=False,
                fallback_used=True,
                fallback_reason=(
                    "Neural Whisper ASR was not enabled for this run; "
                    "using deterministic DSP spectral VAD speech segmentation only."
                ),
                transcript_quality="vad_speech_segments_only",
                backend_package_installed=any_pkg,
                model_available_on_disk=any_disk,
                actual_inference_executed=False,
            ),
            None,
        )

    device = str(state.metadata.get("whisper_device", "cpu"))
    preferred_backend = (
        "openai_whisper"
        if (
            provider_id == "local_openai_whisper"
            or (requested_model_id and requested_model_id.startswith("openai-whisper"))
        )
        else "faster_whisper"
    )
    fallback_reasons: list[str] = []

    # 1. Genuine faster-whisper path
    if preferred_backend == "faster_whisper" or backends["faster_whisper"]:
        fw_model_id = (
            requested_model_id
            if (requested_model_id and requested_model_id.startswith("faster-whisper"))
            else f"faster-whisper-{state.metadata.get('whisper_model', 'tiny')}"
        )
        local_model_path: Path | None = None
        if fw_model_id in {m.model_id for m in mgr.list_catalog(family="asr_whisper")}:
            local_model_path = mgr.resolve_installed_model_path(fw_model_id)

        if not backends["faster_whisper"]:
            fallback_reasons.append("faster_whisper Python package is not installed")
        else:
            try:
                from faster_whisper import WhisperModel  # type: ignore[import-not-found]

                fw_download_root = str(mgr.model_dir / fw_model_id)
                if local_model_path is not None and local_model_path.is_file():
                    model = WhisperModel(str(local_model_path.parent), device=device, local_files_only=True)
                else:
                    model = WhisperModel(
                        str(state.metadata.get("whisper_model", "tiny")),
                        device=device,
                        download_root=fw_download_root,
                        local_files_only=True,
                    )
                # Decode 16kHz mono float32 PCM via FFmpeg so faster-whisper never crashes on
                # mismatched host PyAV versions (e.g. av.open metadata_errors TypeError).
                audio_pcm_16k = np.asarray(extract_mono_pcm(audio_path, sample_rate=16000), dtype=np.float32)
                segments_iter, _ = model.transcribe(
                    audio_pcm_16k,
                    vad_filter=True,
                    word_timestamps=True,
                )
                rows: list[dict[str, Any]] = []
                for seg in segments_iter:
                    text = str(getattr(seg, "text", "")).strip()
                    if not text:
                        continue
                    seg_words: list[dict[str, Any]] = []
                    for w in getattr(seg, "words", None) or []:
                        w_txt = str(getattr(w, "word", "")).strip()
                        if w_txt:
                            seg_words.append(
                                {
                                    "word": w_txt,
                                    "start_sec": float(getattr(w, "start", seg.start)),
                                    "end_sec": float(getattr(w, "end", seg.end)),
                                    "confidence": float(getattr(w, "probability", 0.9)),
                                }
                            )
                    rows.append(
                        {
                            "start": float(seg.start),
                            "end": float(seg.end),
                            "text": text,
                            "confidence": float(
                                np.clip(
                                    np.exp(getattr(seg, "avg_logprob", -0.3)), 0.1, 0.99
                                )
                            ),
                            "words": seg_words,
                        }
                    )
                if rows:
                    if fw_model_id in {m.model_id for m in mgr.list_catalog(family="asr_whisper")}:
                        mgr.mark_loaded(fw_model_id)
                        mgr.mark_inference_executed(fw_model_id)
                    return (
                        AsrBackendReport(
                            backend_id="faster_whisper",
                            model_id=fw_model_id,
                            is_neural_inference=True,
                            fallback_used=False,
                            fallback_reason=None,
                            transcript_quality="neural_asr",
                            backend_package_installed=True,
                            model_available_on_disk=True,
                            actual_inference_executed=True,
                            segments_transcribed=len(rows),
                        ),
                        rows,
                    )
                fallback_reasons.append(
                    f"faster_whisper ({fw_model_id}) executed but returned zero spoken segments"
                )
            except Exception as exc:  # noqa: BLE001
                fallback_reasons.append(f"faster_whisper unavailable/failed: {exc}")

    # 2. Genuine openai-whisper (PyTorch) path
    if preferred_backend == "openai_whisper" or backends["openai_whisper"]:
        ow_model_id = (
            requested_model_id
            if (requested_model_id and requested_model_id.startswith("openai-whisper"))
            else f"openai-whisper-{state.metadata.get('whisper_model', 'tiny')}"
        )
        local_pt: Path | None = None
        if ow_model_id in {m.model_id for m in mgr.list_catalog(family="asr_whisper")}:
            local_pt = mgr.resolve_installed_model_path(ow_model_id)

        if not backends["openai_whisper"]:
            fallback_reasons.append("openai-whisper ('whisper') Python package is not installed")
        elif local_pt is None or not local_pt.is_file():
            fallback_reasons.append(
                f"openai-whisper weights for '{ow_model_id}' are not installed on disk in BANGONPRO_MODEL_DIR"
            )
        else:
            try:
                import whisper  # type: ignore[import-not-found]

                model_ow = whisper.load_model(str(local_pt), device=device)
                result = model_ow.transcribe(audio_path, word_timestamps=True)
                ow_rows: list[dict[str, Any]] = []
                for seg in result.get("segments", []):
                    text = str(seg.get("text", "")).strip()
                    if not text:
                        continue
                    seg_words_ow: list[dict[str, Any]] = []
                    for w in seg.get("words") or []:
                        w_txt = str(w.get("word", "")).strip()
                        if w_txt:
                            seg_words_ow.append(
                                {
                                    "word": w_txt,
                                    "start_sec": float(w.get("start", seg.get("start", 0.0))),
                                    "end_sec": float(w.get("end", seg.get("end", 0.0))),
                                    "confidence": float(w.get("probability", 0.9)),
                                }
                            )
                    ow_rows.append(
                        {
                            "start": float(seg.get("start", 0.0)),
                            "end": float(seg.get("end", 0.0)),
                            "text": text,
                            "confidence": float(
                                np.clip(
                                    np.exp(float(seg.get("avg_logprob", -0.3))), 0.1, 0.99
                                )
                            ),
                            "words": seg_words_ow,
                        }
                    )
                if ow_rows:
                    mgr.mark_loaded(ow_model_id)
                    mgr.mark_inference_executed(ow_model_id)
                    return (
                        AsrBackendReport(
                            backend_id="openai_whisper",
                            model_id=ow_model_id,
                            is_neural_inference=True,
                            fallback_used=False,
                            fallback_reason=None,
                            transcript_quality="neural_asr",
                            backend_package_installed=True,
                            model_available_on_disk=True,
                            actual_inference_executed=True,
                            segments_transcribed=len(ow_rows),
                        ),
                        ow_rows,
                    )
                fallback_reasons.append(
                    f"openai_whisper ({ow_model_id}) executed but returned zero spoken segments"
                )
            except Exception as exc:  # noqa: BLE001
                fallback_reasons.append(f"openai_whisper unavailable/failed: {exc}")

    pkg_installed = bool(backends["faster_whisper"] or backends["openai_whisper"])
    disk_installed = any(
        m.model_available_on_disk for m in mgr.list_models(family="asr_whisper")
    )
    reason_str = (
        "; ".join(fallback_reasons)
        if fallback_reasons
        else "No neural Whisper ASR backend executed; falling back to DSP VAD speech segmentation."
    )
    return (
        AsrBackendReport(
            backend_id="deterministic_multimic_vad",
            model_id=requested_model_id,
            is_neural_inference=False,
            fallback_used=True,
            fallback_reason=reason_str,
            transcript_quality="vad_speech_segments_only",
            backend_package_installed=pkg_installed,
            model_available_on_disk=disk_installed,
            actual_inference_executed=False,
        ),
        None,
    )


def run_transcribe_diarise_agent(
    state: ProjectAnalysisState,
    token: CancellationToken,
    report_progress: Callable[[float, str], None],
) -> dict[str, Any] | None:
    """Run chunked/streaming VAD, optional genuine Whisper ASR, and multi-mic diarisation."""
    token.check("transcribe_diarise start")

    # Respect strict editorial scoping: skip entirely if no stage needs VAD/transcription
    needs_vad_or_asr = any(
        (
            state.intent.transcribe,
            state.intent.add_captions,
            state.intent.remove_silence,
            state.intent.remove_setup_delay,
            state.intent.build_rough_cut,
            state.intent.multicam_switch,
        )
    )
    if not needs_vad_or_asr:
        report_progress(1.0, "Skipping transcription/VAD (not requested by instruction scope)")
        return None

    if state.sync_result is None or not state.media_assets:
        raise RuntimeError("ingest_sync must complete before transcribe_diarise")

    sample_rate = state.audio_sample_rate
    hop_sec = 0.01
    frame_sec = 0.02
    chunk_duration_sec = float(state.metadata.get("chunk_duration_sec", 30.0))
    force_streaming = bool(state.metadata.get("force_streaming_audio", False))
    streaming_cams = set(state.metadata.get("streaming_audio_cameras", []))

    common_duration = state.sync_result.common_duration_sec
    if common_duration <= 0.0:
        common_duration = min(a.duration_sec for a in state.media_assets.values())
    total_samples = max(round(common_duration * sample_rate), sample_rate // 2)

    report_progress(0.1, "Extracting aligned per-camera spectral features (chunked/streaming)")

    # 1. Compute aligned spectral features per asset in O(1) bounded chunk memory
    features_by_cam: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
    used_streaming_vad = False

    for cam_id, asset in state.media_assets.items():
        token.check(f"align audio {cam_id}")
        offset = state.sync_result.offsets_sec.get(cam_id, 0.0)
        raw_pcm = state.audio_pcm_cache.get(cam_id)
        should_stream = (
            force_streaming
            or (cam_id in streaming_cams)
            or (raw_pcm is None and asset.audio_codec is not None)
        )

        if should_stream and asset.audio_codec is not None:
            used_streaming_vad = True
            features_by_cam[cam_id] = compute_frame_spectral_features_streaming(
                asset.file_path,
                offset_sec=offset,
                total_samples=total_samples,
                sample_rate=sample_rate,
                frame_sec=frame_sec,
                hop_sec=hop_sec,
                chunk_duration_sec=chunk_duration_sec,
            )
        else:
            if raw_pcm is None or len(raw_pcm) == 0:
                if asset.audio_codec is not None:
                    raw_pcm = extract_mono_pcm(asset.file_path, sample_rate=sample_rate)
                    state.audio_pcm_cache[cam_id] = raw_pcm
                else:
                    raw_pcm = np.zeros(total_samples, dtype=np.float32)
            aligned = _align_pcm_to_master(raw_pcm, offset, total_samples, sample_rate)
            features_by_cam[cam_id] = compute_frame_spectral_features_chunked(
                aligned,
                sample_rate,
                frame_sec=frame_sec,
                hop_sec=hop_sec,
                chunk_duration_sec=chunk_duration_sec,
            )

    state.metadata["vad_chunk_duration_sec"] = chunk_duration_sec
    state.metadata["streaming_vad_used"] = used_streaming_vad

    # Identify candidate speaker cameras (prefer speaker-labeled non-wide/non-recorder cams)
    speaker_cams = [
        cid
        for cid, asset in state.media_assets.items()
        if asset.speaker_label
        and asset.camera_role not in ("wide", "broll", "reaction", "audio_recorder")
    ]
    if not speaker_cams:
        speaker_cams = [
            cid
            for cid, asset in state.media_assets.items()
            if asset.speaker_label and asset.camera_role != "audio_recorder"
        ]
    if not speaker_cams:
        speaker_cams = [
            cid for cid, asset in state.media_assets.items() if asset.has_video
        ] or list(state.media_assets.keys())

    # Include reaction camera ONLY if it has a distinct speaker_label not already covered
    covered_speakers = {state.media_assets[cid].speaker_label for cid in speaker_cams}
    for cid, asset in state.media_assets.items():
        if (
            cid not in speaker_cams
            and asset.speaker_label
            and asset.camera_role != "audio_recorder"
            and asset.speaker_label not in covered_speakers
        ):
            speaker_cams.append(cid)
            covered_speakers.add(asset.speaker_label)

    # V1.3: a close-up camera the profile does not name (e.g. a news guest) is still a speaker's
    # microphone. Without this its speech collapsed into the named presenter and stayed on their
    # camera. The person role comes from Media Ingest (metadata/filename); wide/reaction/broll
    # microphones hear everyone and are never used as a speaker's mic.
    inv_roles = {i.get("camera_id"): i.get("role_label") for i in (state.metadata.get("media_inventory") or {}).get("inputs", [])}
    person_roles = {"host", "guest", "panel", "desk", "presenter", "anchor", "contestant", "subject", "interviewer"}
    for cid, asset in state.media_assets.items():
        role = str(inv_roles.get(cid) or asset.camera_role or "")
        if cid in speaker_cams or asset.speaker_label or not asset.has_audio or role not in person_roles:
            continue
        label = role.title() if role.title() not in covered_speakers else cid
        asset.speaker_label = label
        speaker_cams.append(cid)
        covered_speakers.add(label)

    # 2. Compute frame-wise VAD across all audio-bearing assets
    report_progress(0.35, "Running energy & spectral-centroid VAD")
    min_len = min(features_by_cam[cid][1].size for cid in state.media_assets)
    all_speech_db = np.vstack([features_by_cam[cid][1][:min_len] for cid in state.media_assets])
    all_speech_ratio = np.vstack(
        [features_by_cam[cid][2][:min_len] for cid in state.media_assets]
    )
    all_centroid = np.vstack([features_by_cam[cid][3][:min_len] for cid in state.media_assets])

    max_speech_db = np.max(all_speech_db, axis=0)
    best_overall_idx = np.argmax(all_speech_db, axis=0)
    n_frames = max_speech_db.size
    frame_idx_arr = np.arange(n_frames)
    best_speech_ratio = all_speech_ratio[best_overall_idx, frame_idx_arr]
    best_centroid = all_centroid[best_overall_idx, frame_idx_arr]

    editing_rules = state.profile["editing_rules"]
    effective = get_effective_profile_settings(state.profile)
    profile_thresh_db = float(
        state.intent.silence_threshold_db
        if state.intent.silence_threshold_db is not None
        else effective["silence_threshold_db"]
    )
    min_silence_sec = float(
        state.intent.min_pause_sec
        if state.intent.min_pause_sec is not None
        else editing_rules["min_silence_sec"]
    )
    interruption_gap_sec = float(editing_rules.get("interruption_gap_sec", 10.0))
    review_thresh = float(editing_rules.get("review_confidence_threshold", 0.72))

    peak_db = float(np.percentile(max_speech_db, 95)) if n_frames else -20.0
    floor_db = float(np.percentile(max_speech_db, 15)) if n_frames else -80.0
    if state.intent.silence_threshold_db is not None:
        speech_thresh_db = profile_thresh_db
    elif peak_db - floor_db >= 14.0:
        adaptive_thresh = floor_db + 0.30 * (peak_db - floor_db)
        speech_thresh_db = max(profile_thresh_db, min(adaptive_thresh, peak_db - 10.0))
    else:
        speech_thresh_db = profile_thresh_db

    raw_vad = (max_speech_db >= speech_thresh_db) & (best_speech_ratio >= 0.22)

    min_gap_frames = max(3, round(min(0.28, min_silence_sec * 0.45) / hop_sec))
    min_speech_frames = max(10, round(0.28 / hop_sec))
    vad_mask = _morphological_clean_mask(
        raw_vad,
        min_gap_frames=min_gap_frames,
        min_run_frames=min_speech_frames,
    )

    speech_runs = _extract_true_runs(vad_mask)

    # Fallback if ultra-quiet audio yielded no runs: take highest-energy region
    if not speech_runs and n_frames > 0:
        speech_runs = [(0, n_frames)]

    # 3. Detect setup delay, interior silences & long mid-recording interruption gaps
    first_speech_sec = speech_runs[0][0] * hop_sec if speech_runs else 0.0
    setup_delay_sec = round(first_speech_sec, 4) if first_speech_sec >= 0.40 else 0.0
    state.setup_delay_sec = setup_delay_sec

    silence_intervals: list[TimeInterval] = []
    detected_interruptions: list[TimeInterval] = []
    if setup_delay_sec >= 0.40:
        silence_intervals.append(
            TimeInterval(
                start_sec=0.0,
                end_sec=setup_delay_sec,
                label="setup_delay",
                confidence=0.95,
            )
        )

    for prev_run, next_run in itertools.pairwise(speech_runs):
        gap_start = prev_run[1] * hop_sec
        gap_end = next_run[0] * hop_sec
        gap_dur = gap_end - gap_start
        if gap_dur >= interruption_gap_sec:
            iv = TimeInterval(
                start_sec=round(gap_start, 4),
                end_sec=round(gap_end, 4),
                label="interruption_gap",
                confidence=0.95,
            )
            silence_intervals.append(iv)
            detected_interruptions.append(iv)
        elif gap_dur >= min_silence_sec:
            silence_intervals.append(
                TimeInterval(
                    start_sec=round(gap_start, 4),
                    end_sec=round(gap_end, 4),
                    label="silence",
                    confidence=0.92,
                )
            )

    if speech_runs:
        trailing_start = speech_runs[-1][1] * hop_sec
        if (common_duration - trailing_start) >= min_silence_sec:
            silence_intervals.append(
                TimeInterval(
                    start_sec=round(trailing_start, 4),
                    end_sec=round(common_duration, 4),
                    label="trailing_silence",
                    confidence=0.90,
                )
            )

    state.silence_intervals = silence_intervals
    if detected_interruptions:
        state.interruption_intervals = detected_interruptions

    # V1.2.2: reaction audio (laughter/applause/listener) the VAD does not count as dialogue
    audio_cams = [cid for cid, a in state.media_assets.items() if a.audio_codec is not None] or list(state.media_assets)
    nonspeech_events, noise_floor_db = measure_nonspeech_activity(
        features_by_cam,
        audio_cams,
        vad_mask,
        hop_sec=hop_sec,
        speech_thresh_db=speech_thresh_db,
        speaker_labels={cid: a.speaker_label for cid, a in state.media_assets.items()},
    )
    state.metadata["nonspeech_activity"] = nonspeech_events
    state.metadata["noise_floor_db"] = noise_floor_db

    # 4. Multi-mic speaker diarisation & genuine neural Whisper ASR (when enabled & available)
    report_progress(0.60, "Performing multi-camera speaker diarisation & ASR check")
    spk_db_matrix = np.vstack([features_by_cam[cid][1][:min_len] for cid in speaker_cams])
    num_spk_cams = len(speaker_cams)

    # Prefer independent audio recorder as ASR audio source if available, else sync reference asset
    recorder_ids = state.metadata.get("independent_audio_recorders") or []
    asr_source_cam_id = (
        recorder_ids[0]
        if recorder_ids and recorder_ids[0] in state.media_assets
        else state.sync_result.reference_camera_id
    )
    asr_source_asset = state.media_assets[asr_source_cam_id]
    asr_report, whisper_rows = _try_neural_whisper_asr(asr_source_asset.file_path, state)
    active_backend = asr_report.backend_id
    state.transcript_backend = active_backend
    state.asr_backend_report = asr_report

    segments: list[TranscriptSegment] = []
    seg_specs: list[dict[str, Any]] = []
    min_turn_frames = max(12, round(0.40 / hop_sec))
    overlap_margin_db = 4.0

    for run_start, run_end in speech_runs:
        token.check("diarise speech run")
        crosstalk_run_mask = np.zeros(max(1, run_end - run_start), dtype=bool)
        if num_spk_cams == 1:
            sub_turns = [(run_start, run_end, 0, False)]
        else:
            win_scores = spk_db_matrix[:, run_start:run_end]
            smooth_win = uniform_filter1d(
                win_scores, size=min(40, max(1, win_scores.shape[1])), axis=1
            )
            raw_winners = np.argmax(smooth_win, axis=0).tolist()
            smoothed_winners = _majority_smooth_labels(raw_winners, half_window=18)

            # Simultaneous active-speaker detection across 2+ mics
            sorted_smooth = np.sort(smooth_win, axis=0)
            smooth_margins = sorted_smooth[-1] - sorted_smooth[-2]
            active_cam_count = np.sum(smooth_win >= (speech_thresh_db - 2.5), axis=0)
            overlap_raw = (smooth_margins < overlap_margin_db) & (active_cam_count >= 2)
            crosstalk_run_mask = _morphological_clean_mask(
                overlap_raw,
                min_gap_frames=max(5, round(0.20 / hop_sec)),
                min_run_frames=max(20, round(0.45 / hop_sec)),
            )

            raw_groups: list[list[int]] = []
            for local_i, spk_idx in enumerate(smoothed_winners):
                is_xtalk = 1 if crosstalk_run_mask[local_i] else 0
                if not raw_groups:
                    raw_groups.append(
                        [run_start + local_i, run_start + local_i + 1, spk_idx, is_xtalk]
                    )
                elif (is_xtalk == 1 and raw_groups[-1][3] == 1) or (
                    raw_groups[-1][2] == spk_idx and raw_groups[-1][3] == is_xtalk
                ):
                    raw_groups[-1][1] = run_start + local_i + 1
                else:
                    raw_groups.append(
                        [run_start + local_i, run_start + local_i + 1, spk_idx, is_xtalk]
                    )

            merged_groups: list[list[int]] = []
            for raw_grp in raw_groups:
                if merged_groups and (raw_grp[1] - raw_grp[0]) < min_turn_frames:
                    merged_groups[-1][1] = raw_grp[1]
                elif merged_groups and (
                    (merged_groups[-1][3] == 1 and raw_grp[3] == 1)
                    or (merged_groups[-1][2] == raw_grp[2] and merged_groups[-1][3] == raw_grp[3])
                ):
                    merged_groups[-1][1] = raw_grp[1]
                else:
                    merged_groups.append(raw_grp)
            if (
                len(merged_groups) >= 2
                and (merged_groups[0][1] - merged_groups[0][0]) < min_turn_frames
            ):
                merged_groups[1][0] = merged_groups[0][0]
                merged_groups.pop(0)

            sub_turns = [(g[0], g[1], g[2], bool(g[3])) for g in merged_groups]

        for t_start_f, t_end_f, spk_idx, flagged_overlap in sub_turns:
            start_sec = round(t_start_f * hop_sec, 4)
            end_sec = round(min(common_duration, t_end_f * hop_sec), 4)
            if end_sec - start_sec < 0.15:
                continue

            seg_db_slice = spk_db_matrix[:, t_start_f:t_end_f]
            per_cam_means = np.mean(seg_db_slice, axis=1)
            dominant_idx = int(np.argmax(per_cam_means)) if flagged_overlap else spk_idx

            cam_id = speaker_cams[dominant_idx]
            asset = state.media_assets[cam_id]
            speaker_id = asset.speaker_label or cam_id

            winner_db = float(per_cam_means[dominant_idx])
            centroid_hz = float(np.mean(best_centroid[t_start_f:t_end_f]))

            if num_spk_cams > 1:
                sorted_idx = np.argsort(per_cam_means)
                second_idx = int(sorted_idx[-2])
                second_db = float(per_cam_means[second_idx])
                margin_db = float(per_cam_means[sorted_idx[-1]] - second_db)
            else:
                second_db = -120.0
                margin_db = 12.0

            loc_s = max(0, t_start_f - run_start)
            loc_e = max(loc_s + 1, min(crosstalk_run_mask.size, t_end_f - run_start))
            overlap_frac = float(np.mean(crosstalk_run_mask[loc_s:loc_e]))

            is_overlap = bool(
                num_spk_cams > 1
                and (
                    flagged_overlap
                    or overlap_frac >= 0.45
                    or (margin_db < overlap_margin_db and second_db >= speech_thresh_db - 3.0)
                )
            )

            overlap_speakers: list[str] = []
            if is_overlap:
                active_names: list[str] = []
                for c_idx in np.argsort(per_cam_means)[::-1]:
                    c_mean = float(per_cam_means[int(c_idx)])
                    if (winner_db - c_mean) <= (overlap_margin_db + 1.0) and c_mean >= (
                        speech_thresh_db - 4.0
                    ):
                        cid_i = speaker_cams[int(c_idx)]
                        lbl = state.media_assets[cid_i].speaker_label or cid_i
                        if lbl not in active_names:
                            active_names.append(lbl)
                if len(active_names) < 2:
                    for c_idx in np.argsort(per_cam_means)[::-1][:2]:
                        cid_i = speaker_cams[int(c_idx)]
                        lbl = state.media_assets[cid_i].speaker_label or cid_i
                        if lbl not in active_names:
                            active_names.append(lbl)
                overlap_speakers = active_names

            snr_above_thresh = winner_db - speech_thresh_db
            energy_conf = float(np.clip((snr_above_thresh + 2.0) / 14.0, 0.25, 1.0))
            if is_overlap or (num_spk_cams > 1 and margin_db < 3.5):
                diar_conf = float(np.clip(0.30 + 0.08 * margin_db, 0.28, 0.58))
                confidence = round(
                    float(np.clip(0.35 * energy_conf + 0.65 * diar_conf, 0.25, 0.68)), 4
                )
            else:
                diar_conf = float(np.clip(0.55 + 0.45 * (margin_db / 6.0), 0.45, 0.99))
                confidence = round(
                    float(np.clip(0.5 * energy_conf + 0.5 * diar_conf, 0.20, 0.98)), 4
                )

            seg_specs.append(
                {
                    "start_sec": start_sec,
                    "end_sec": end_sec,
                    "speaker_id": speaker_id,
                    "camera_id": cam_id,
                    "confidence": confidence,
                    "energy_db": round(winner_db, 2),
                    "centroid_hz": round(centroid_hz, 1),
                    "is_overlap": is_overlap,
                    "overlap_speakers": overlap_speakers,
                }
            )

    # V1.2.2: assign every recognised word to exactly one segment (by its midpoint). V1.2.1 gave a
    # segment the full text of every Whisper row it overlapped, so a sentence spanning two speech
    # runs was transcribed twice. Words the VAD morphology dropped (a short "Yes." inside a pause)
    # become their own segments instead of being lost to a pause cut.
    spec_words: list[list[dict[str, Any]]] = [[] for _ in seg_specs]
    spec_rows: list[list[str]] = [[] for _ in seg_specs]
    orphan_words: list[dict[str, Any]] = []
    if whisper_rows and asr_report.is_neural_inference:
        spec_starts = [sp["start_sec"] for sp in seg_specs]

        def _spec_for(t: float, reach: float) -> int | None:
            k = bisect.bisect_right(spec_starts, t) - 1
            best_k, best_d = None, reach
            for cand in (k, k + 1):
                if 0 <= cand < len(seg_specs):
                    sp = seg_specs[cand]
                    d = 0.0 if sp["start_sec"] <= t <= sp["end_sec"] else min(abs(t - sp["start_sec"]), abs(t - sp["end_sec"]))
                    if d <= best_d:
                        best_k, best_d = cand, d
            return best_k

        for row in whisper_rows:
            row_words = [w for w in (row.get("words") or []) if str(w.get("word", "")).strip()]
            if row_words:
                for w in row_words:
                    ws, we = float(w["start_sec"]), float(w["end_sec"])
                    k = _spec_for((ws + we) / 2.0, 0.35)
                    if k is None:
                        orphan_words.append(dict(w))
                        continue
                    spec_words[k].append(dict(w))
                    seg_specs[k]["start_sec"] = round(min(seg_specs[k]["start_sec"], ws), 4)
                    seg_specs[k]["end_sec"] = round(min(common_duration, max(seg_specs[k]["end_sec"], we)), 4)
            elif str(row.get("text", "")).strip():
                ov = [max(0.0, min(row["end"], sp["end_sec"]) - max(row["start"], sp["start_sec"])) for sp in seg_specs]
                if ov and max(ov) > 0.0:
                    spec_rows[int(np.argmax(ov))].append(str(row["text"]).strip())
        # Group dropped words into short segments attributed by microphone dominance
        orphan_words.sort(key=lambda w: float(w["start_sec"]))
        groups: list[list[dict[str, Any]]] = []
        for w in orphan_words:
            if groups and float(w["start_sec"]) - float(groups[-1][-1]["end_sec"]) < 0.6:
                groups[-1].append(w)
            else:
                groups.append([w])
        for grp in groups:
            g_s, g_e = float(grp[0]["start_sec"]), float(grp[-1]["end_sec"])
            f0 = max(0, min(min_len - 1, round(g_s / hop_sec)))
            f1 = max(f0 + 1, min(min_len, round(g_e / hop_sec)))
            means = np.mean(spk_db_matrix[:, f0:f1], axis=1)
            dom = int(np.argmax(means))
            cid = speaker_cams[dom]
            seg_specs.append(
                {
                    "start_sec": round(g_s, 4),
                    "end_sec": round(min(common_duration, max(g_e, g_s + 0.05)), 4),
                    "speaker_id": state.media_assets[cid].speaker_label or cid,
                    "camera_id": cid,
                    "confidence": round(float(np.clip(np.mean([float(w.get("confidence", 0.8)) for w in grp]) * 0.85, 0.2, 0.95)), 4),
                    "energy_db": round(float(means[dom]), 2),
                    "centroid_hz": round(float(np.mean(best_centroid[f0:f1])), 1),
                    "is_overlap": False,
                    "overlap_speakers": [],
                    "recovered_from_pause": True,
                }
            )
            spec_words.append([dict(w) for w in grp])
            spec_rows.append([])
    order = sorted(range(len(seg_specs)), key=lambda k: (seg_specs[k]["start_sec"], seg_specs[k]["end_sec"]))

    for k in order:
        sp = seg_specs[k]
        start_sec, end_sec = sp["start_sec"], sp["end_sec"]
        assigned = sorted(spec_words[k], key=lambda w: float(w["start_sec"]))
        text = " ".join(str(w["word"]).strip() for w in assigned) if assigned else " ".join(spec_rows[k])
        seg_is_neural = bool(text.strip()) and asr_report.is_neural_inference
        seg_quality = "neural_asr" if seg_is_neural else "vad_speech_segments_only"
        confidence = sp["confidence"]
        if not seg_is_neural:
            # Honest VAD-only speech segment marker — NEVER fabricated spoken words!
            dur = end_sec - start_sec
            if sp["is_overlap"] and len(sp["overlap_speakers"]) >= 2:
                joined = " & ".join(sp["overlap_speakers"])
                text = f"[VAD speech segment: {joined} overlap, {dur:.1f}s — install Whisper ASR for spoken transcript]"
            else:
                text = f"[VAD speech segment: {sp['speaker_id']}, {dur:.1f}s — install Whisper ASR for spoken transcript]"
            words: list[dict[str, Any]] = []
        else:
            words = build_words_for_segment(text, start_sec, end_sec, state.fps, confidence=confidence, explicit_words=assigned or None)
            for w in words:
                w["timing"] = "asr" if assigned else "interpolated"

        seg_id = f"seg_{len(segments) + 1:04d}"
        segments.append(
            TranscriptSegment(
                segment_id=seg_id,
                start_sec=start_sec,
                end_sec=end_sec,
                speaker_id=sp["speaker_id"],
                speaker_name=sp["speaker_id"],
                camera_id=sp["camera_id"],
                text=text,
                confidence=confidence,
                is_low_confidence=confidence < review_thresh,
                energy_db=sp["energy_db"],
                spectral_centroid_hz=sp["centroid_hz"],
                backend=active_backend if seg_is_neural else "deterministic_multimic_vad",
                is_neural_inference=seg_is_neural,
                transcript_quality=seg_quality,
                is_overlap=sp["is_overlap"],
                overlap_speakers=sp["overlap_speakers"],
                is_filler_or_silence=is_filler_or_silence_text(text) if seg_is_neural else False,
                deleted=False,
                words=words,
            )
        )

    # Silences never overlap recognised speech (extended or recovered segments carve them)
    if segments:
        regions = [(s.start_sec, s.end_sec) for s in segments]
        state.silence_intervals = _carve_intervals(
            state.silence_intervals,
            regions,
            {"silence": min_silence_sec, "trailing_silence": min_silence_sec, "setup_delay": 0.40, "interruption_gap": interruption_gap_sec},
        )
        if detected_interruptions:
            detected_interruptions = [iv for iv in state.silence_intervals if iv.label == "interruption_gap"]
            state.interruption_intervals = detected_interruptions
        first_start = min(s.start_sec for s in segments)
        if setup_delay_sec >= 0.40 and first_start < setup_delay_sec:
            setup_delay_sec = round(first_start, 4) if first_start >= 0.40 else 0.0
            state.setup_delay_sec = setup_delay_sec

    # 5. Group transcript segments into editorial Story Sections (V1.2.2: topic segmentation from
    # lexical cohesion when words exist, turn structure/pauses/energy when they do not)
    report_progress(0.82, "Identifying topics, overlaps & editorial review flags")
    story_sections: list[StorySection] = []
    topic_rows: list[dict[str, Any]] | None = None
    if segments:
        try:
            from bangonpro.ai_engine.department.editorial_analysis import segment_topics

            topic_rows = segment_topics(segments, state.profile, min_silence_sec=min_silence_sec, interruptions=detected_interruptions)
        except Exception as exc:  # noqa: BLE001 - topic analysis must never fail transcription
            state.metadata["topic_segmentation_error"] = f"{type(exc).__name__}: {exc}"
            topic_rows = None
    if topic_rows:
        for row in topic_rows:
            story_sections.append(
                StorySection(
                    section_id=row["section_id"],
                    label=row["label"],
                    start_sec=row["start_sec"],
                    end_sec=row["end_sec"],
                    dominant_speaker=row["dominant_speaker"],
                    confidence=row["confidence"],
                )
            )
            for seg in segments:
                if row["start_sec"] - 1e-6 <= seg.start_sec <= row["end_sec"] + 1e-6:
                    seg.section_label = row["label"]
        state.metadata["topic_segmentation"] = {
            "method": topic_rows[0]["method"],
            "topics": [{k: r[k] for k in ("section_id", "label", "start_sec", "end_sec", "keywords", "boundary_evidence", "boundary_score")} for r in topic_rows],
        }
    elif segments:
        section_gap_sec = max(1.2, min_silence_sec * 1.4)
        cur_group: list[TranscriptSegment] = [segments[0]]
        section_groups: list[list[TranscriptSegment]] = []
        for seg in segments[1:]:
            if (seg.start_sec - cur_group[-1].end_sec) >= section_gap_sec:
                section_groups.append(cur_group)
                cur_group = [seg]
            else:
                cur_group.append(seg)
        section_groups.append(cur_group)

        for s_idx, sec_grp in enumerate(section_groups, start=1):
            label = "Opening" if s_idx == 1 else f"Section {s_idx}"
            dur_by_spk: dict[str, float] = {}
            for seg in sec_grp:
                seg.section_label = label
                dur_by_spk[seg.speaker_id] = dur_by_spk.get(seg.speaker_id, 0.0) + (
                    seg.end_sec - seg.start_sec
                )
            dom_spk = max(dur_by_spk, key=lambda spk_key: dur_by_spk[spk_key])
            mean_c = float(np.mean([g.confidence for g in sec_grp]))
            story_sections.append(
                StorySection(
                    section_id=f"sec_{s_idx:02d}",
                    label=label,
                    start_sec=sec_grp[0].start_sec,
                    end_sec=sec_grp[-1].end_sec,
                    dominant_speaker=dom_spk,
                    confidence=round(mean_c, 4),
                )
            )

    state.transcript_segments = segments
    state.story_sections = story_sections
    state.metadata["asr_backends_available"] = detect_available_asr_backends()
    state.metadata["asr_backend_report"] = asdict(asr_report)
    state.metadata["vad_threshold_db"] = round(speech_thresh_db, 2)
    state.metadata["overlap_segment_count"] = sum(1 for s in segments if s.is_overlap)

    out_dir = Path(state.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    transcript_doc = build_transcript_document(state, use_edited_timeline=False)
    state.transcript_document = transcript_doc
    transcript_path = save_transcript(transcript_doc, out_dir / "transcript.json")
    state.artifacts["transcript_json"] = str(transcript_path)

    # 6. Emit validated Timeline Command API v1 batch with analysis markers
    builder = TimelineCommandBuilder(
        project_id=state.project_id,
        sequence_id=state.sequence_id,
        expected_revision=state.revision,
        agent_id="transcribe_diarise_agent",
        agent_role="transcription_and_diarisation",
        transaction_label="Transcript, Diarisation & Section Markers",
        requires_approval=False,
        dry_run=state.dry_run,
        fps_num=state.fps_num,
        fps_den=state.fps_den,
    )

    if setup_delay_sec >= 0.40:
        builder.add_operation(
            "add_marker",
            in_frame=0,
            out_frame=state.sec_to_frame(setup_delay_sec),
            timeline_start=0,
            params={
                "category": "setup_trim",
                "category_index": 1,
                "frame": 0,
                "label": f"Setup Delay Detected ({setup_delay_sec:.2f}s)",
                "setup_delay_sec": setup_delay_sec,
            },
            confidence=0.95,
            rationale="Leading pre-dialogue setup delay detected for non-destructive trimming",
        )

    for take_idx, iv in enumerate(detected_interruptions, start=2):
        f_start = state.sec_to_frame(iv.start_sec)
        f_end = state.sec_to_frame(iv.end_sec)
        builder.add_operation(
            "add_marker",
            in_frame=f_start,
            out_frame=f_end,
            timeline_start=f_end,
            params={
                "category": "take_boundary",
                "category_index": 1,
                "frame": f_end,
                "take_number": take_idx,
                "interruption_start_sec": iv.start_sec,
                "interruption_end_sec": iv.end_sec,
                "interruption_duration_sec": round(iv.duration_sec, 4),
                "label": f"Take {take_idx} Boundary (after {iv.duration_sec:.1f}s interruption)",
            },
            confidence=0.95,
            rationale=(
                f"Detected mid-recording interruption ({iv.duration_sec:.1f}s >= "
                f"{interruption_gap_sec:.1f}s) before Take {take_idx}"
            ),
        )

    for sec in story_sections:
        sec_f = state.sec_to_frame(sec.start_sec)
        builder.add_operation(
            "add_marker",
            in_frame=sec_f,
            out_frame=state.sec_to_frame(sec.end_sec),
            timeline_start=sec_f,
            params={
                "category": "section",
                "category_index": 2,
                "frame": sec_f,
                "label": f"{sec.label} ({sec.dominant_speaker})",
                "section_id": sec.section_id,
                "backend_id": asr_report.backend_id,
                "is_neural_inference": asr_report.is_neural_inference,
                "transcript_quality": asr_report.transcript_quality,
            },
            confidence=sec.confidence,
            rationale=f"Story section '{sec.label}' identified from dialogue pacing",
        )

    for seg in segments:
        if seg.is_low_confidence or seg.is_overlap:
            seg_f = state.sec_to_frame(seg.start_sec)
            lbl = (
                f"Overlap Dialogue: {' & '.join(seg.overlap_speakers)} ({seg.confidence:.2f})"
                if seg.is_overlap
                else f"Review Diarisation: {seg.speaker_id} ({seg.confidence:.2f})"
            )
            builder.add_operation(
                "add_marker",
                in_frame=seg_f,
                out_frame=state.sec_to_frame(seg.end_sec),
                timeline_start=seg_f,
                params={
                    "category": "review",
                    "category_index": 5,
                    "frame": seg_f,
                    "label": lbl,
                    "segment_id": seg.segment_id,
                    "is_overlap": seg.is_overlap,
                    "overlap_speakers": list(seg.overlap_speakers),
                },
                confidence=seg.confidence,
                rationale=(
                    f"Overlapping speech detected across {', '.join(seg.overlap_speakers)}"
                    if seg.is_overlap
                    else (
                        f"Segment '{seg.segment_id}' confidence {seg.confidence:.2f} "
                        f"below review threshold {review_thresh:.2f}"
                    )
                ),
            )

    # Guarantee at least one valid operation in the batch even on minimal input
    if builder.operation_count == 0:
        builder.add_operation(
            "add_marker",
            in_frame=0,
            out_frame=1,
            timeline_start=0,
            params={
                "category": "section",
                "category_index": 2,
                "frame": 0,
                "label": "Programme Start",
                "backend_id": asr_report.backend_id,
                "is_neural_inference": asr_report.is_neural_inference,
                "transcript_quality": asr_report.transcript_quality,
            },
            confidence=0.9,
            rationale="Default programme start marker from transcription stage",
        )

    report_progress(
        0.98,
        f"Diarised {len(segments)} segments across {len(story_sections)} sections "
        f"(backend={asr_report.backend_id}, neural={asr_report.is_neural_inference})",
    )
    return builder.build()
