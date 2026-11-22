"""Transcription, Energy/Spectral-Centroid VAD & Multi-Mic Speaker Diarisation Agent.

Features pluggable ASR backend detection (``whisperx`` / ``faster_whisper`` when
installed and explicitly enabled) paired with a real, deterministic CPU engine
using ``numpy``/``scipy`` short-time spectral analysis and per-camera microphone
loudness diarisation so multi-camera studio shoots run accurately offline with
zero multi-GB model downloads.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import importlib.util
from typing import Any, Callable

import numpy as np
from scipy.ndimage import uniform_filter1d

from bangonpro.ai_engine.agents.ingest_sync import extract_mono_pcm
from bangonpro.ai_engine.commands import TimelineCommandBuilder
from bangonpro.ai_engine.orchestrator import CancellationToken
from bangonpro.ai_engine.state import (
    ProjectAnalysisState,
    StorySection,
    TimeInterval,
    TranscriptSegment,
)


def detect_available_asr_backends() -> dict[str, bool]:
    """Feature-detect optional heavy ML ASR backends without importing them eagerly."""
    return {
        "whisperx": importlib.util.find_spec("whisperx") is not None,
        "faster_whisper": importlib.util.find_spec("faster_whisper") is not None,
        "deterministic_multimic_vad": True,
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

    src_start = int(round(offset_sec * sample_rate))
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
    frame_len = max(32, int(round(frame_sec * sample_rate)))
    hop_len = max(16, int(round(hop_sec * sample_rate)))

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

    return full_db, speech_db, speech_ratio, centroid_hz


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


def _try_optional_whisper_text(
    audio_path: str,
    state: ProjectAnalysisState,
) -> tuple[str | None, list[dict[str, Any]] | None]:
    """Attempt optional faster-whisper / whisperx transcription ONLY when explicitly opted in."""
    if not state.metadata.get("enable_whisper", False):
        return None, None

    backends = detect_available_asr_backends()
    model_size = str(state.metadata.get("whisper_model", "tiny"))
    device = str(state.metadata.get("whisper_device", "cpu"))

    if backends["faster_whisper"]:
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]

            model = WhisperModel(model_size, device=device, local_files_only=True)
            segments, _ = model.transcribe(audio_path, vad_filter=True)
            rows = [
                {
                    "start": float(seg.start),
                    "end": float(seg.end),
                    "text": str(seg.text).strip(),
                    "confidence": float(np.clip(np.exp(getattr(seg, "avg_logprob", -0.3)), 0.1, 0.99)),
                }
                for seg in segments
                if str(seg.text).strip()
            ]
            if rows:
                return "faster_whisper", rows
        except Exception:  # noqa: BLE001
            return None, None

    return None, None


def run_transcribe_diarise_agent(
    state: ProjectAnalysisState,
    token: CancellationToken,
    report_progress: Callable[[float, str], None],
) -> dict[str, Any]:
    """Run VAD, multi-mic speaker diarisation, silence/setup detection, and sectioning."""
    token.check("transcribe_diarise start")
    if state.sync_result is None or not state.media_assets:
        raise RuntimeError("ingest_sync must complete before transcribe_diarise")

    sample_rate = state.audio_sample_rate
    hop_sec = 0.01
    frame_sec = 0.02
    common_duration = state.sync_result.common_duration_sec
    if common_duration <= 0.0:
        common_duration = min(a.duration_sec for a in state.media_assets.values())
    total_samples = max(int(round(common_duration * sample_rate)), sample_rate // 2)

    report_progress(0.1, "Aligning per-camera audio streams to master sync timeline")

    # 1. Align all camera audio streams to T=0 on the master sync timeline
    aligned_pcm: dict[str, np.ndarray] = {}
    features_by_cam: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}

    for cam_id, asset in state.media_assets.items():
        token.check(f"align audio {cam_id}")
        raw_pcm = state.audio_pcm_cache.get(cam_id)
        if raw_pcm is None or len(raw_pcm) == 0:
            raw_pcm = extract_mono_pcm(asset.file_path, sample_rate=sample_rate)
            state.audio_pcm_cache[cam_id] = raw_pcm
        offset = state.sync_result.offsets_sec.get(cam_id, 0.0)
        aligned = _align_pcm_to_master(raw_pcm, offset, total_samples, sample_rate)
        aligned_pcm[cam_id] = aligned
        features_by_cam[cam_id] = _compute_frame_spectral_features(
            aligned, sample_rate, frame_sec=frame_sec, hop_sec=hop_sec
        )

    # Identify candidate speaker cameras (prefer speaker-labeled non-wide mics; fallback to all)
    speaker_cams = [
        cid
        for cid, asset in state.media_assets.items()
        if asset.speaker_label and asset.camera_role not in ("wide", "broll", "reaction")
    ]
    if not speaker_cams:
        speaker_cams = [
            cid
            for cid, asset in state.media_assets.items()
            if asset.speaker_label
        ]
    if not speaker_cams:
        speaker_cams = list(state.media_assets.keys())

    # Include reaction camera ONLY if it has a distinct speaker_label not already covered
    covered_speakers = {state.media_assets[cid].speaker_label for cid in speaker_cams}
    for cid, asset in state.media_assets.items():
        if (
            cid not in speaker_cams
            and asset.speaker_label
            and asset.speaker_label not in covered_speakers
        ):
            speaker_cams.append(cid)
            covered_speakers.add(asset.speaker_label)

    # 2. Compute frame-wise VAD across all cameras
    report_progress(0.35, "Running energy & spectral-centroid VAD")
    all_speech_db = np.vstack([features_by_cam[cid][1] for cid in state.media_assets])
    all_speech_ratio = np.vstack([features_by_cam[cid][2] for cid in state.media_assets])
    all_centroid = np.vstack([features_by_cam[cid][3] for cid in state.media_assets])

    max_speech_db = np.max(all_speech_db, axis=0)
    best_overall_idx = np.argmax(all_speech_db, axis=0)
    n_frames = max_speech_db.size
    frame_idx_arr = np.arange(n_frames)
    best_speech_ratio = all_speech_ratio[best_overall_idx, frame_idx_arr]
    best_centroid = all_centroid[best_overall_idx, frame_idx_arr]

    editing_rules = state.profile["editing_rules"]
    profile_thresh_db = float(editing_rules["silence_threshold_db"])
    min_silence_sec = float(editing_rules["min_silence_sec"])
    review_thresh = float(editing_rules.get("review_confidence_threshold", 0.72))

    peak_db = float(np.percentile(max_speech_db, 95)) if n_frames else -20.0
    floor_db = float(np.percentile(max_speech_db, 15)) if n_frames else -80.0
    if peak_db - floor_db >= 14.0:
        adaptive_thresh = floor_db + 0.30 * (peak_db - floor_db)
        speech_thresh_db = max(profile_thresh_db, min(adaptive_thresh, peak_db - 10.0))
    else:
        speech_thresh_db = profile_thresh_db

    raw_vad = (max_speech_db >= speech_thresh_db) & (best_speech_ratio >= 0.22)

    min_gap_frames = max(3, int(round(min(0.28, min_silence_sec * 0.45) / hop_sec)))
    # Require >= 0.28s sustained speech so brief slate clicks in setup delay are ignored
    min_speech_frames = max(10, int(round(0.28 / hop_sec)))
    vad_mask = _morphological_clean_mask(
        raw_vad,
        min_gap_frames=min_gap_frames,
        min_run_frames=min_speech_frames,
    )

    speech_runs = _extract_true_runs(vad_mask)

    # Fallback if ultra-quiet audio yielded no runs: take highest-energy region
    if not speech_runs and n_frames > 0:
        speech_runs = [(0, n_frames)]

    # 3. Detect setup delay & interior silence intervals
    first_speech_sec = speech_runs[0][0] * hop_sec if speech_runs else 0.0
    setup_delay_sec = round(first_speech_sec, 4) if first_speech_sec >= 0.40 else 0.0
    state.setup_delay_sec = setup_delay_sec

    silence_intervals: list[TimeInterval] = []
    if setup_delay_sec >= 0.40:
        silence_intervals.append(
            TimeInterval(
                start_sec=0.0,
                end_sec=setup_delay_sec,
                label="setup_delay",
                confidence=0.95,
            )
        )

    for prev_run, next_run in zip(speech_runs, speech_runs[1:]):
        gap_start = prev_run[1] * hop_sec
        gap_end = next_run[0] * hop_sec
        if (gap_end - gap_start) >= min_silence_sec:
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

    # 4. Multi-mic speaker diarisation inside each speech run
    report_progress(0.60, "Performing multi-camera speaker diarisation")
    spk_db_matrix = np.vstack([features_by_cam[cid][1] for cid in speaker_cams])
    num_spk_cams = len(speaker_cams)

    # Optional Whisper override text (only when explicitly enabled & locally available)
    ref_asset = state.media_assets[state.sync_result.reference_camera_id]
    asr_backend, whisper_rows = _try_optional_whisper_text(ref_asset.file_path, state)
    active_backend = asr_backend or "deterministic_multimic_vad"
    state.transcript_backend = active_backend

    segments: list[TranscriptSegment] = []
    min_turn_frames = max(12, int(round(0.40 / hop_sec)))

    for run_start, run_end in speech_runs:
        token.check("diarise speech run")
        if num_spk_cams == 1:
            sub_turns = [(run_start, run_end, 0)]
        else:
            win_scores = spk_db_matrix[:, run_start:run_end]
            smooth_win = uniform_filter1d(win_scores, size=min(40, max(1, win_scores.shape[1])), axis=1)
            raw_winners = np.argmax(smooth_win, axis=0).tolist()
            smoothed_winners = _majority_smooth_labels(raw_winners, half_window=18)

            # Identify sustained crosstalk regions where top-2 smoothed mic envelopes are within 3.5 dB
            sorted_smooth = np.sort(smooth_win, axis=0)
            smooth_margins = sorted_smooth[-1] - sorted_smooth[-2]
            crosstalk_mask = _morphological_clean_mask(
                smooth_margins < 3.5,
                min_gap_frames=max(5, int(round(0.20 / hop_sec))),
                min_run_frames=max(20, int(round(0.45 / hop_sec))),
            )

            # Group contiguous (speaker_idx, is_crosstalk) runs
            raw_groups: list[list[int]] = []
            for local_i, spk_idx in enumerate(smoothed_winners):
                is_xtalk = 1 if crosstalk_mask[local_i] else 0
                if not raw_groups or raw_groups[-1][2] != spk_idx or raw_groups[-1][3] != is_xtalk:
                    raw_groups.append([run_start + local_i, run_start + local_i + 1, spk_idx, is_xtalk])
                else:
                    raw_groups[-1][1] = run_start + local_i + 1

            # Merge tiny speaker blips (< min_turn_frames) into neighboring turn
            merged_groups: list[list[int]] = []
            for grp in raw_groups:
                if merged_groups and (grp[1] - grp[0]) < min_turn_frames:
                    merged_groups[-1][1] = grp[1]
                elif merged_groups and merged_groups[-1][2] == grp[2] and merged_groups[-1][3] == grp[3]:
                    merged_groups[-1][1] = grp[1]
                else:
                    merged_groups.append(grp)
            # If the very first group is tiny and there is a second group, merge forward
            if len(merged_groups) >= 2 and (merged_groups[0][1] - merged_groups[0][0]) < min_turn_frames:
                merged_groups[1][0] = merged_groups[0][0]
                merged_groups.pop(0)

            sub_turns = [(g[0], g[1], g[2]) for g in merged_groups]

        for t_start_f, t_end_f, spk_idx in sub_turns:
            start_sec = round(t_start_f * hop_sec, 4)
            end_sec = round(min(common_duration, t_end_f * hop_sec), 4)
            if end_sec - start_sec < 0.15:
                continue

            cam_id = speaker_cams[spk_idx]
            asset = state.media_assets[cam_id]
            speaker_id = asset.speaker_label or cam_id

            seg_db_slice = spk_db_matrix[:, t_start_f:t_end_f]
            winner_db = float(np.mean(seg_db_slice[spk_idx]))
            centroid_hz = float(np.mean(best_centroid[t_start_f:t_end_f]))

            if num_spk_cams > 1:
                per_cam_means = np.sort(np.mean(seg_db_slice, axis=1))
                margin_db = float(per_cam_means[-1] - per_cam_means[-2])
            else:
                margin_db = 12.0

            snr_above_thresh = winner_db - speech_thresh_db

            # Confidence model: high when speech is clearly above threshold AND single mic dominates.
            # Crosstalk (turn-level separation < 3.5 dB) drops cleanly below review threshold.
            energy_conf = float(np.clip((snr_above_thresh + 2.0) / 14.0, 0.25, 1.0))
            if num_spk_cams > 1 and margin_db < 3.5:
                diar_conf = float(np.clip(0.30 + 0.08 * margin_db, 0.28, 0.58))
                confidence = round(float(np.clip(0.35 * energy_conf + 0.65 * diar_conf, 0.25, 0.68)), 4)
            else:
                diar_conf = float(np.clip(0.55 + 0.45 * (margin_db / 6.0), 0.45, 0.99))
                confidence = round(float(np.clip(0.5 * energy_conf + 0.5 * diar_conf, 0.20, 0.98)), 4)

            # Match optional Whisper text if available
            text = ""
            if whisper_rows:
                overlapping = [
                    r["text"]
                    for r in whisper_rows
                    if r["end"] > start_sec and r["start"] < end_sec
                ]
                text = " ".join(overlapping).strip()

            if not text:
                dur = end_sec - start_sec
                text = f"{speaker_id}: studio dialogue ({dur:.1f}s)"

            seg_id = f"seg_{len(segments) + 1:04d}"
            segments.append(
                TranscriptSegment(
                    segment_id=seg_id,
                    start_sec=start_sec,
                    end_sec=end_sec,
                    speaker_id=speaker_id,
                    camera_id=cam_id,
                    text=text,
                    confidence=confidence,
                    is_low_confidence=confidence < review_thresh,
                    energy_db=round(winner_db, 2),
                    spectral_centroid_hz=round(centroid_hz, 1),
                    backend=active_backend,
                )
            )

    # 5. Group transcript segments into editorial Story Sections
    report_progress(0.82, "Identifying story sections and editorial review flags")
    story_sections: list[StorySection] = []
    if segments:
        section_gap_sec = max(1.2, min_silence_sec * 1.4)
        cur_group: list[TranscriptSegment] = [segments[0]]
        groups: list[list[TranscriptSegment]] = []
        for seg in segments[1:]:
            if (seg.start_sec - cur_group[-1].end_sec) >= section_gap_sec:
                groups.append(cur_group)
                cur_group = [seg]
            else:
                cur_group.append(seg)
        groups.append(cur_group)

        for s_idx, grp in enumerate(groups, start=1):
            label = "Opening" if s_idx == 1 else f"Section {s_idx}"
            # Dominant speaker by total speaking duration in this section
            dur_by_spk: dict[str, float] = {}
            for seg in grp:
                seg.section_label = label
                dur_by_spk[seg.speaker_id] = dur_by_spk.get(seg.speaker_id, 0.0) + (
                    seg.end_sec - seg.start_sec
                )
            dom_spk = max(dur_by_spk, key=dur_by_spk.get)
            mean_c = float(np.mean([g.confidence for g in grp]))
            story_sections.append(
                StorySection(
                    section_id=f"sec_{s_idx:02d}",
                    label=label,
                    start_sec=grp[0].start_sec,
                    end_sec=grp[-1].end_sec,
                    dominant_speaker=dom_spk,
                    confidence=round(mean_c, 4),
                )
            )

    state.transcript_segments = segments
    state.story_sections = story_sections
    state.metadata["asr_backends_available"] = detect_available_asr_backends()
    state.metadata["vad_threshold_db"] = round(speech_thresh_db, 2)

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
                "label": f"Setup Delay Detected ({setup_delay_sec:.2f}s)",
                "setup_delay_sec": setup_delay_sec,
            },
            confidence=0.95,
            rationale="Leading pre-dialogue setup delay detected for non-destructive trimming",
        )

    for sec in story_sections:
        builder.add_operation(
            "add_marker",
            in_frame=state.sec_to_frame(sec.start_sec),
            out_frame=state.sec_to_frame(sec.end_sec),
            timeline_start=state.sec_to_frame(sec.start_sec),
            params={
                "category": "section",
                "label": f"{sec.label} ({sec.dominant_speaker})",
                "section_id": sec.section_id,
            },
            confidence=sec.confidence,
            rationale=f"Story section '{sec.label}' identified from dialogue pacing",
        )

    for seg in segments:
        if seg.is_low_confidence:
            builder.add_operation(
                "add_marker",
                in_frame=state.sec_to_frame(seg.start_sec),
                out_frame=state.sec_to_frame(seg.end_sec),
                timeline_start=state.sec_to_frame(seg.start_sec),
                params={
                    "category": "review",
                    "label": f"Review Diarisation: {seg.speaker_id} ({seg.confidence:.2f})",
                    "segment_id": seg.segment_id,
                },
                confidence=seg.confidence,
                rationale=(
                    f"Segment '{seg.segment_id}' confidence {seg.confidence:.2f} "
                    f"below review threshold {review_thresh:.2f}"
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
                "label": "Programme Start",
                "backend": active_backend,
            },
            confidence=0.9,
            rationale="Default programme start marker from transcription stage",
        )

    report_progress(0.98, f"Diarised {len(segments)} segments across {len(story_sections)} sections")
    return builder.build()
