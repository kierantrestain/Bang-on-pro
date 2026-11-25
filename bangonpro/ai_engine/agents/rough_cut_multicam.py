"""Scene Detection, Non-Destructive Rough Cut, and Multicam Angle-Switching Agent.

Combines PySceneDetect visual cut analysis with dialogue/silence structure from
``transcribe_diarise`` to assemble a Show-Profile-compliant multicamera rough
cut honoring ``camera_roles``, ``speaker_camera_map``, ``wide_shot_interval_sec``,
``reaction_shot_frequency``, ``min_shot_duration_sec``, ``max_shot_duration_sec``,
``intro_outro_rules``, mixed FPS, independent audio recorders, overlapping speech,
interrupted recordings, and missing cameras.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import numpy as np

from bangonpro.ai_engine.commands import TimelineCommandBuilder
from bangonpro.ai_engine.orchestrator import CancellationToken
from bangonpro.ai_engine.state import (
    EditShot,
    ProjectAnalysisState,
    TimeInterval,
    TranscriptSegment,
)
from bangonpro.show_profiles.loader import get_effective_profile_settings

_LOGGER = logging.getLogger(__name__)


def detect_visual_scene_cuts(
    video_path: str,
    *,
    threshold: float = 27.0,
) -> tuple[list[float], str]:
    """Detect visual scene-cut timestamps using ``scenedetect`` with OpenCV fallback."""
    try:
        from scenedetect import ContentDetector, SceneManager, open_video

        video = open_video(video_path)
        manager = SceneManager()
        manager.add_detector(ContentDetector(threshold=threshold))
        manager.detect_scenes(video=video, show_progress=False)
        scene_list = manager.get_scene_list()
        cuts = [
            round(float(start.get_seconds()), 4)
            for start, _ in scene_list[1:]
            if float(start.get_seconds()) > 0.0
        ]
        return cuts, "scenedetect_content_detector"
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("scenedetect unavailable or failed (%s); falling back to OpenCV", exc)

    # Deterministic OpenCV histogram difference fallback
    try:
        import cv2

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return [], "fallback_unavailable"
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        prev_hist = None
        fallback_cuts: list[float] = []
        frame_idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            hist = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
            cv2.normalize(hist, hist)
            if prev_hist is not None:
                diff = cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA)
                if diff > 0.45:
                    fallback_cuts.append(round(frame_idx / fps, 4))
            prev_hist = hist
            frame_idx += 1
        cap.release()
        return fallback_cuts, "opencv_hist_fallback"
    except Exception:  # noqa: BLE001
        return [], "none"


def map_master_to_edited_sec(
    master_sec: float,
    kept_intervals: list[TimeInterval],
) -> float | None:
    """Map a timestamp on the master sync timeline to the edited rough-cut timeline."""
    cursor = 0.0
    for iv in kept_intervals:
        if iv.start_sec <= master_sec <= iv.end_sec:
            return round(cursor + (master_sec - iv.start_sec), 4)
        cursor += iv.duration_sec
    return None


def _compute_kept_master_intervals(
    state: ProjectAnalysisState,
    common_duration: float,
) -> list[TimeInterval]:
    """Compute kept master-timeline segments after setup-delay, silence & interruption trimming."""
    rules = state.profile["editing_rules"]
    keep_pad = float(rules.get("keep_padding_sec", 0.2))
    trim_setup = bool(rules.get("trim_setup_delay", True)) and state.intent.remove_setup_delay
    remove_silence = bool(state.intent.remove_silence)
    excise_interruptions = bool(rules.get("excise_interruption_gaps", True)) and (
        state.intent.remove_silence or state.intent.build_rough_cut
    )

    range_start = 0.0
    range_end = common_duration
    if state.intent.time_range_sec is not None:
        r0, r1 = state.intent.time_range_sec
        range_start = max(0.0, min(common_duration, float(r0)))
        range_end = max(range_start, min(common_duration, float(r1)))

    start_bound = range_start
    if trim_setup and state.setup_delay_sec >= 0.40:
        start_bound = max(range_start, state.setup_delay_sec - keep_pad)

    end_bound = range_end
    if remove_silence:
        for iv in state.silence_intervals:
            if iv.label == "trailing_silence":
                candidate_end = min(range_end, iv.start_sec + keep_pad)
                if candidate_end > start_bound + 0.5:
                    end_bound = candidate_end

    if end_bound <= start_bound + 0.25:
        return [
            TimeInterval(
                start_sec=round(range_start, 4),
                end_sec=round(max(range_start + 0.5, range_end), 4),
                label="kept_full",
            )
        ]

    # Excise interior silence spans AND detected recording interruptions (when enabled)
    excise_spans: list[tuple[float, float]] = []
    if remove_silence:
        for iv in state.silence_intervals:
            if iv.label != "silence":
                continue
            cut_start = max(start_bound, iv.start_sec + keep_pad)
            cut_end = min(end_bound, iv.end_sec - keep_pad)
            if (cut_end - cut_start) >= 0.15:
                excise_spans.append((cut_start, cut_end))

    if excise_interruptions:
        for iv in getattr(state, "interruption_intervals", []):
            cut_start = max(start_bound, iv.start_sec + keep_pad)
            cut_end = min(end_bound, iv.end_sec - keep_pad)
            if (cut_end - cut_start) >= 0.25:
                excise_spans.append((cut_start, cut_end))

    # Merge overlapping excise spans
    excise_spans.sort()
    merged_excise: list[tuple[float, float]] = []
    for cs, ce in excise_spans:
        if merged_excise and cs <= merged_excise[-1][1] + 0.05:
            merged_excise[-1] = (merged_excise[-1][0], max(merged_excise[-1][1], ce))
        else:
            merged_excise.append((cs, ce))
    excise_spans = merged_excise

    kept: list[TimeInterval] = []
    cursor = start_bound
    for cut_s, cut_e in excise_spans:
        if cut_s > cursor + 0.10:
            kept.append(
                TimeInterval(
                    start_sec=round(cursor, 4),
                    end_sec=round(cut_s, 4),
                    label=f"kept_{len(kept) + 1}",
                )
            )
        cursor = max(cursor, cut_e)

    if end_bound > cursor + 0.10:
        kept.append(
            TimeInterval(
                start_sec=round(cursor, 4),
                end_sec=round(end_bound, 4),
                label=f"kept_{len(kept) + 1}",
            )
        )

    if not kept:
        kept.append(
            TimeInterval(
                start_sec=round(start_bound, 4),
                end_sec=round(end_bound, 4),
                label="kept_1",
            )
        )
    return kept


def _dominant_segment_for_window(
    start_sec: float,
    end_sec: float,
    segments: list[TranscriptSegment],
) -> TranscriptSegment | None:
    best_seg: TranscriptSegment | None = None
    best_overlap = 0.0
    for seg in segments:
        ov = min(end_sec, seg.end_sec) - max(start_sec, seg.start_sec)
        if ov > best_overlap:
            best_overlap = ov
            best_seg = seg
    return best_seg


def _resolve_camera_roles(state: ProjectAnalysisState) -> tuple[str, str, dict[str, str]]:
    """Return ``(wide_cam_id, reaction_cam_id, speaker_to_cam_map)`` from profile & available video assets."""
    effective = get_effective_profile_settings(state.profile)
    profile_roles: dict[str, str] = effective["camera_roles"]
    profile_spk_map: dict[str, str] = effective["speaker_camera_map"]

    # Exclude audio-only independent recorders from video switching candidates
    video_assets = {
        cid: asset
        for cid, asset in state.media_assets.items()
        if asset.has_video and asset.camera_role != "audio_recorder"
    }
    if not video_assets:
        video_assets = dict(state.media_assets)

    ref_cam = (
        state.sync_result.reference_camera_id
        if (state.sync_result and state.sync_result.reference_camera_id in video_assets)
        else next(iter(video_assets))
    )

    def _effective_role(cid: str) -> str:
        return str(profile_roles.get(cid, video_assets[cid].camera_role))

    cam_prefs = state.profile.get("camera_preferences") or {}
    preferred_open_role = str(cam_prefs.get("default_opening_role", "wide"))

    wide_cam = next(
        (cid for cid in video_assets if _effective_role(cid) == preferred_open_role),
        next(
            (cid for cid in video_assets if _effective_role(cid) in ("wide", "desk")),
            ref_cam,
        ),
    )
    reaction_cam = next(
        (cid for cid in video_assets if _effective_role(cid) in ("reaction", "broll")),
        wide_cam,
    )

    speaker_to_cam: dict[str, str] = {}
    # 1. Explicit speaker_camera_map from profile (only if that camera is actually available!)
    for spk, cid in profile_spk_map.items():
        if cid in video_assets:
            speaker_to_cam[spk] = cid

    # 2. Fill remaining speakers from registered video assets in role priority order
    role_rank = {
        "host": 0,
        "guest": 0,
        "panel": 0,
        "desk": 1,
        "reaction": 2,
        "wide": 3,
        "broll": 4,
    }
    for cid, asset in sorted(
        video_assets.items(),
        key=lambda kv: (role_rank.get(_effective_role(kv[0]), 5), kv[1].priority),
    ):
        if asset.speaker_label and asset.speaker_label not in speaker_to_cam:
            speaker_to_cam[asset.speaker_label] = cid

    return wide_cam, reaction_cam, speaker_to_cam


def _detect_high_energy_applause_times(
    state: ProjectAnalysisState,
    common_duration: float,
) -> list[float]:
    """For gameshow/sports profiles, detect multi-mic energy peaks for reaction switching."""
    if state.profile.get("format_type") not in ("gameshow", "sports_discussion"):
        return []
    if not state.audio_pcm_cache or state.sync_result is None:
        return []

    sr = state.audio_sample_rate
    hop = max(1, int(0.1 * sr))
    n_steps = max(1, int(common_duration / 0.1))
    energy_stack = []
    for cid, pcm in state.audio_pcm_cache.items():
        if pcm is None or len(pcm) < hop:
            continue
        off = round(state.sync_result.offsets_sec.get(cid, 0.0) * sr)
        vals = np.zeros(n_steps, dtype=np.float32)
        for i in range(n_steps):
            s = off + i * hop
            e = s + hop
            if s >= 0 and e <= len(pcm):
                vals[i] = float(np.sqrt(np.mean(pcm[s:e] ** 2) + 1e-12))
        energy_stack.append(vals)

    if len(energy_stack) < 2:
        return []
    mean_env = np.mean(np.vstack(energy_stack), axis=0)
    thresh = float(np.percentile(mean_env, 88))
    if thresh <= 1e-5:
        return []

    peaks: list[float] = []
    last_t = -10.0
    for i, v in enumerate(mean_env):
        t = i * 0.1
        if v >= thresh and (t - last_t) >= 2.5:
            peaks.append(round(t, 2))
            last_t = t
    return peaks


def _choose_cutaway_camera(
    *,
    base_cam: str,
    wide_cam: str,
    reaction_cam: str,
    available_video_cams: list[str],
    cutaway_idx: int,
    reaction_freq: float,
    time_since_wide: float,
    wide_interval_sec: float,
) -> tuple[str, str]:
    """Pick wide vs reaction cutaway camera honoring ``wide_shot_interval_sec`` & ``reaction_shot_frequency``."""
    # Force wide establishing return whenever wide_shot_interval_sec has elapsed
    if wide_cam != base_cam and time_since_wide >= wide_interval_sec:
        return wide_cam, "periodic_wide_shot_interval"

    # Respect reaction_shot_frequency in [0.0, 1.0]
    if reaction_freq <= 0.0:
        prefer_reaction = False
    elif reaction_freq >= 1.0:
        prefer_reaction = True
    else:
        # Deterministic Bresenham-style cadence matching exact reaction_shot_frequency
        prev_hits = int(cutaway_idx * reaction_freq)
        curr_hits = int((cutaway_idx + 1) * reaction_freq)
        prefer_reaction = curr_hits > prev_hits

    if prefer_reaction and reaction_cam != base_cam:
        return reaction_cam, "periodic_reaction_cutaway"
    if wide_cam != base_cam:
        return wide_cam, "periodic_wide_cutaway"
    alt = next((c for c in available_video_cams if c != base_cam), base_cam)
    return alt, "periodic_alternate_angle_cutaway"


def _plan_shots_for_kept_interval(
    iv: TimeInterval,
    *,
    state: ProjectAnalysisState,
    wide_cam: str,
    reaction_cam: str,
    speaker_to_cam: dict[str, str],
    applause_times: list[float],
    prev_end_camera: str | None,
) -> list[dict[str, Any]]:
    """Plan speaker-aware multicamera shots inside a single kept master interval."""
    rules = state.profile["editing_rules"]
    effective = get_effective_profile_settings(state.profile)
    cam_prefs = state.profile.get("camera_preferences") or {}

    min_shot = float(effective["min_shot_duration_sec"])
    max_shot = float(effective["max_shot_duration_sec"])
    wide_interval = float(effective["wide_shot_interval_sec"])
    reaction_freq = float(effective["reaction_shot_frequency"])
    reaction_interval = float(rules.get("reaction_cut_interval_sec", max_shot * 0.6))
    review_thresh = float(rules.get("review_confidence_threshold", 0.72))
    overlap_to_wide = bool(
        rules.get("overlap_cut_to_wide", cam_prefs.get("prefer_wide_on_overlap", True))
    )
    avoid_jump_cuts = bool(cam_prefs.get("avoid_jump_cuts", True))

    available_video_cams = [
        cid
        for cid, a in state.media_assets.items()
        if a.has_video and a.camera_role != "audio_recorder"
    ] or list(state.media_assets.keys())

    # 1. Gather raw speaker-aligned spans inside [iv.start_sec, iv.end_sec]
    overlapping = [
        seg
        for seg in state.transcript_segments
        if seg.end_sec > iv.start_sec + 0.05 and seg.start_sec < iv.end_sec - 0.05
    ]

    raw_spans: list[dict[str, Any]] = []
    if not overlapping or not state.intent.multicam_switch:
        raw_spans.append(
            {
                "start": iv.start_sec,
                "end": iv.end_sec,
                "camera_id": wide_cam,
                "speaker_id": overlapping[0].speaker_id if overlapping else "Studio",
                "confidence": 0.85,
                "reason": "establishing_wide_shot",
            }
        )
    else:
        cursor = iv.start_sec
        for seg in overlapping:
            seg_s = max(iv.start_sec, seg.start_sec)
            seg_e = min(iv.end_sec, seg.end_sec)
            if seg_s > cursor + 0.08:
                pad_cam = speaker_to_cam.get(seg.speaker_id, wide_cam)
                raw_spans.append(
                    {
                        "start": cursor,
                        "end": seg_s,
                        "camera_id": pad_cam,
                        "speaker_id": seg.speaker_id,
                        "confidence": max(0.75, seg.confidence),
                        "reason": "dialogue_padding",
                    }
                )
            if getattr(seg, "is_overlap", False) and overlap_to_wide and wide_cam in available_video_cams:
                cam_id = wide_cam
                shot_reason = "overlapping_speech_wide_shot"
            else:
                cam_id = speaker_to_cam.get(seg.speaker_id, seg.camera_id)
                if cam_id not in available_video_cams:
                    cam_id = wide_cam
                shot_reason = f"active_speaker_{seg.speaker_id.lower().replace(' ', '_')}"
            raw_spans.append(
                {
                    "start": seg_s,
                    "end": seg_e,
                    "camera_id": cam_id,
                    "speaker_id": seg.speaker_id,
                    "confidence": seg.confidence,
                    "reason": shot_reason,
                }
            )
            cursor = seg_e

        if iv.end_sec > cursor + 0.08 and raw_spans:
            raw_spans[-1]["end"] = iv.end_sec

    # 2. Merge adjacent spans on the same camera
    merged: list[dict[str, Any]] = []
    for sp in raw_spans:
        if merged and merged[-1]["camera_id"] == sp["camera_id"]:
            w1 = max(0.01, merged[-1]["end"] - merged[-1]["start"])
            w2 = max(0.01, sp["end"] - sp["start"])
            if w2 > w1:
                merged[-1]["reason"] = sp["reason"]
            merged[-1]["confidence"] = round(
                min(merged[-1]["confidence"], sp["confidence"])
                if (merged[-1]["confidence"] < review_thresh or sp["confidence"] < review_thresh)
                else (merged[-1]["confidence"] * w1 + sp["confidence"] * w2) / (w1 + w2),
                4,
            )
            merged[-1]["end"] = sp["end"]
        else:
            merged.append(dict(sp))

    # 3. Coalesce micro-cuts shorter than min_shot_duration_sec (unless only 1 span exists)
    coalesced: list[dict[str, Any]] = []
    for sp in merged:
        dur = sp["end"] - sp["start"]
        if coalesced and dur < min_shot:
            coalesced[-1]["end"] = sp["end"]
            coalesced[-1]["confidence"] = round(
                min(coalesced[-1]["confidence"], sp["confidence"]), 4
            )
        else:
            coalesced.append(sp)

    if len(coalesced) >= 2 and (coalesced[0]["end"] - coalesced[0]["start"]) < min_shot:
        coalesced[1]["start"] = coalesced[0]["start"]
        coalesced.pop(0)

    # 4. Jump-cut avoidance across silence splice
    if (
        avoid_jump_cuts
        and prev_end_camera is not None
        and coalesced
        and coalesced[0]["camera_id"] == prev_end_camera
        and wide_cam != prev_end_camera
        and (coalesced[0]["end"] - coalesced[0]["start"]) >= (2.0 * min_shot)
    ):
        bridge_dur = min(min_shot + 0.4, (coalesced[0]["end"] - coalesced[0]["start"]) * 0.35)
        split_t = round(coalesced[0]["start"] + bridge_dur, 4)
        first = dict(coalesced[0])
        first["end"] = split_t
        first["camera_id"] = wide_cam
        first["reason"] = "jump_cut_bridge_wide"
        coalesced[0]["start"] = split_t
        coalesced.insert(0, first)

    # 5. Enforce max_shot_duration_sec, wide_shot_interval_sec & reaction_shot_frequency cutaways
    paced: list[dict[str, Any]] = []
    cutaway_idx = 0
    time_since_wide = 0.0

    for sp in coalesced:
        cur_s = sp["start"]
        sp_end = sp["end"]
        base_cam = sp["camera_id"]

        if base_cam == wide_cam:
            time_since_wide = 0.0

        while (sp_end - cur_s) > max_shot + 0.05:
            target_cut = cur_s + min(max_shot, max(min_shot, reaction_interval))
            for ap_t in applause_times:
                if (cur_s + min_shot) <= ap_t <= (cur_s + max_shot - min_shot):
                    target_cut = ap_t
                    break

            cutaway_dur = max(min_shot, min(reaction_interval * 0.45, max_shot * 0.4))
            if (sp_end - target_cut) < min_shot:
                break

            first_dur = round(target_cut - cur_s, 4)
            paced.append(
                {
                    "start": cur_s,
                    "end": round(target_cut, 4),
                    "camera_id": base_cam,
                    "speaker_id": sp["speaker_id"],
                    "confidence": sp["confidence"],
                    "reason": sp["reason"],
                }
            )
            if base_cam == wide_cam:
                time_since_wide = 0.0
            else:
                time_since_wide += first_dur

            cutaway_end = min(sp_end, round(target_cut + cutaway_dur, 4))
            if 0.0 < (sp_end - cutaway_end) < min_shot:
                cutaway_end = sp_end

            alt_cam, cutaway_reason = _choose_cutaway_camera(
                base_cam=base_cam,
                wide_cam=wide_cam,
                reaction_cam=reaction_cam,
                available_video_cams=available_video_cams,
                cutaway_idx=cutaway_idx,
                reaction_freq=reaction_freq,
                time_since_wide=time_since_wide,
                wide_interval_sec=wide_interval,
            )
            cutaway_idx += 1

            paced.append(
                {
                    "start": round(target_cut, 4),
                    "end": cutaway_end,
                    "camera_id": alt_cam,
                    "speaker_id": sp["speaker_id"],
                    "confidence": round(min(0.88, sp["confidence"]), 4),
                    "reason": cutaway_reason,
                }
            )
            if alt_cam == wide_cam:
                time_since_wide = 0.0
            else:
                time_since_wide += round(cutaway_end - target_cut, 4)
            cur_s = cutaway_end

        if (sp_end - cur_s) > 0.08:
            rem_dur = sp_end - cur_s
            # Honor wide_shot_interval_sec across consecutive close-up turns
            if (
                base_cam != wide_cam
                and wide_cam in available_video_cams
                and (time_since_wide + rem_dur) >= wide_interval
                and rem_dur >= (2.0 * min_shot)
            ):
                wide_dur = min(max(min_shot, min_shot + 0.5), rem_dur - min_shot)
                split_pt = round(sp_end - wide_dur, 4)
                paced.append(
                    {
                        "start": round(cur_s, 4),
                        "end": split_pt,
                        "camera_id": base_cam,
                        "speaker_id": sp["speaker_id"],
                        "confidence": sp["confidence"],
                        "reason": sp["reason"],
                    }
                )
                paced.append(
                    {
                        "start": split_pt,
                        "end": round(sp_end, 4),
                        "camera_id": wide_cam,
                        "speaker_id": sp["speaker_id"],
                        "confidence": round(min(0.90, sp["confidence"]), 4),
                        "reason": "periodic_wide_shot_interval",
                    }
                )
                time_since_wide = 0.0
            else:
                paced.append(
                    {
                        "start": round(cur_s, 4),
                        "end": round(sp_end, 4),
                        "camera_id": base_cam,
                        "speaker_id": sp["speaker_id"],
                        "confidence": sp["confidence"],
                        "reason": sp["reason"],
                    }
                )
                if base_cam == wide_cam:
                    time_since_wide = 0.0
                else:
                    time_since_wide += rem_dur

    for sp in paced:
        sp["needs_review"] = float(sp["confidence"]) < review_thresh

    return paced


def _apply_intro_outro_rules(
    planned_shots: list[dict[str, Any]],
    *,
    state: ProjectAnalysisState,
    wide_cam: str,
) -> list[dict[str, Any]]:
    """Apply Show Profile ``intro_outro_rules`` (opening/closing wide shots) without violating ``min_shot_duration_sec``."""
    if not planned_shots or not state.intent.multicam_switch:
        return planned_shots
    effective = get_effective_profile_settings(state.profile)
    io_rules = effective["intro_outro_rules"]
    min_shot = float(effective["min_shot_duration_sec"])
    shots = [dict(s) for s in planned_shots]

    open_on_wide = bool(io_rules.get("open_on_wide", False))
    opening_sec = float(io_rules.get("opening_wide_sec", 0.0))
    if (
        open_on_wide
        and opening_sec >= min_shot
        and shots[0]["camera_id"] != wide_cam
        and (shots[0]["end"] - shots[0]["start"]) >= (opening_sec + min_shot)
    ):
        split_t = round(shots[0]["start"] + opening_sec, 4)
        opener = dict(shots[0])
        opener["end"] = split_t
        opener["camera_id"] = wide_cam
        opener["reason"] = "intro_opening_wide_shot"
        shots[0]["start"] = split_t
        shots.insert(0, opener)

    close_on_wide = bool(io_rules.get("close_on_wide", False))
    closing_sec = float(io_rules.get("closing_wide_sec", 0.0))
    if (
        close_on_wide
        and closing_sec >= min_shot
        and shots[-1]["camera_id"] != wide_cam
        and (shots[-1]["end"] - shots[-1]["start"]) >= (closing_sec + min_shot)
    ):
        split_t = round(shots[-1]["end"] - closing_sec, 4)
        closer = dict(shots[-1])
        closer["start"] = split_t
        closer["camera_id"] = wide_cam
        closer["reason"] = "outro_closing_wide_shot"
        shots[-1]["end"] = split_t
        shots.append(closer)

    return shots


def run_rough_cut_multicam_agent(
    state: ProjectAnalysisState,
    token: CancellationToken,
    report_progress: Callable[[float, str], None],
) -> dict[str, Any] | None:
    """Detect visual scenes and assemble a non-destructive multicam rough cut when requested."""
    token.check("rough_cut_multicam start")

    # Enforce strict editorial scoping: skip entirely when cuts/silence/setup edits are not requested
    needs_cut_assembly = any(
        (
            state.intent.build_rough_cut,
            state.intent.multicam_switch,
            state.intent.remove_silence,
            state.intent.remove_setup_delay,
        )
    )
    if not needs_cut_assembly:
        report_progress(1.0, "Skipping rough cut / multicam switching (not requested by instruction scope)")
        return None

    if state.sync_result is None or not state.media_assets:
        raise RuntimeError("ingest_sync and transcribe_diarise must complete before rough_cut_multicam")

    wide_cam, reaction_cam, speaker_to_cam = _resolve_camera_roles(state)
    video_ref_cam = (
        state.sync_result.reference_camera_id
        if state.media_assets[state.sync_result.reference_camera_id].has_video
        else wide_cam
    )
    ref_asset = state.media_assets[video_ref_cam]
    common_duration = state.sync_result.common_duration_sec or ref_asset.duration_sec

    # 1. Visual scene detection on reference video camera
    report_progress(0.15, "Running visual scene detection")
    if ref_asset.has_video:
        scene_cuts, scene_backend = detect_visual_scene_cuts(ref_asset.file_path)
    else:
        scene_cuts, scene_backend = ([], "audio_only_skipped")
    state.scene_cuts_sec = scene_cuts
    state.metadata["scene_detect_backend"] = scene_backend
    token.check("after scene detection")

    # 2. Compute kept intervals (trim setup delay & reduce dead air)
    report_progress(0.35, "Computing kept intervals (setup-delay & dead-air reduction)")
    kept_intervals = _compute_kept_master_intervals(state, common_duration)
    state.kept_master_intervals = kept_intervals

    # 3. Plan speaker-aware multicamera switching across kept intervals
    report_progress(0.55, "Planning multicamera angle switches")
    applause_times = _detect_high_energy_applause_times(state, common_duration)

    planned_raw_shots: list[dict[str, Any]] = []
    prev_cam: str | None = None
    for iv in kept_intervals:
        token.check("plan interval shots")
        iv_shots = _plan_shots_for_kept_interval(
            iv,
            state=state,
            wide_cam=wide_cam,
            reaction_cam=reaction_cam,
            speaker_to_cam=speaker_to_cam,
            applause_times=applause_times,
            prev_end_camera=prev_cam,
        )
        planned_raw_shots.extend(iv_shots)
        if iv_shots:
            prev_cam = iv_shots[-1]["camera_id"]

    planned_raw_shots = _apply_intro_outro_rules(
        planned_raw_shots,
        state=state,
        wide_cam=wide_cam,
    )

    # Prefer synced independent audio recorder for A1 dialogue master when present
    recorder_ids = [
        cid
        for cid, a in state.media_assets.items()
        if (a.is_audio_only or a.camera_role == "audio_recorder") and a.audio_codec is not None
    ]
    master_recorder_id = recorder_ids[0] if recorder_ids else None
    audio_fallback_cam = (
        master_recorder_id
        or (
            state.sync_result.reference_camera_id
            if state.media_assets[state.sync_result.reference_camera_id].audio_codec is not None
            else video_ref_cam
        )
    )

    # 4. Convert planned shots into frame-accurate EditShot objects
    fps = state.fps
    shots: list[EditShot] = []
    timeline_cursor_frame = 0

    for idx, sp in enumerate(planned_raw_shots, start=1):
        m_in = float(sp["start"])
        m_out = float(sp["end"])
        desired_frames = max(1, round((m_out - m_in) * fps))

        cam_id = sp["camera_id"]
        v_asset = state.media_assets[cam_id]

        # Pick master independent recorder if present, else active speaker's mic for A1
        if master_recorder_id is not None:
            audio_cam_id = master_recorder_id
        else:
            audio_cam_id = speaker_to_cam.get(sp["speaker_id"], audio_fallback_cam)
            if (
                audio_cam_id not in state.media_assets
                or state.media_assets[audio_cam_id].audio_codec is None
            ):
                audio_cam_id = audio_fallback_cam
        a_asset = state.media_assets[audio_cam_id]

        # V1.2.2: drift/segment-aware mapping (identical to m_in + offset when no drift is measured)
        v_src_in_sec = max(0.0, min(v_asset.duration_sec - (1.0 / fps), state.sync_result.source_time(cam_id, m_in)))
        a_src_in_sec = max(0.0, min(a_asset.duration_sec - (1.0 / fps), state.sync_result.source_time(audio_cam_id, m_in)))

        v_src_in_frame = state.sec_to_frame(v_src_in_sec)
        a_src_in_frame = state.sec_to_frame(a_src_in_sec)

        v_max_frames = max(1, state.sec_to_frame(v_asset.duration_sec) - v_src_in_frame)
        a_max_frames = max(1, state.sec_to_frame(a_asset.duration_sec) - a_src_in_frame)
        dur_frames = max(1, min(desired_frames, v_max_frames, a_max_frames))

        v_src_out_frame = v_src_in_frame + dur_frames - 1
        a_src_out_frame = a_src_in_frame + dur_frames - 1

        tl_in_frame = timeline_cursor_frame
        tl_out_frame = timeline_cursor_frame + dur_frames - 1
        timeline_cursor_frame = tl_out_frame + 1

        shot = EditShot(
            shot_id=f"shot_{idx:04d}",
            camera_id=cam_id,
            media_ref=v_asset.media_ref,
            audio_media_ref=a_asset.media_ref,
            source_in_sec=round(v_src_in_frame / fps, 4),
            source_out_sec=round((v_src_out_frame + 1) / fps, 4),
            audio_source_in_sec=round(a_src_in_frame / fps, 4),
            audio_source_out_sec=round((a_src_out_frame + 1) / fps, 4),
            timeline_in_sec=round(tl_in_frame / fps, 4),
            timeline_out_sec=round((tl_out_frame + 1) / fps, 4),
            source_in_frame=v_src_in_frame,
            source_out_frame=v_src_out_frame,
            audio_source_in_frame=a_src_in_frame,
            audio_source_out_frame=a_src_out_frame,
            timeline_in_frame=tl_in_frame,
            timeline_out_frame=tl_out_frame,
            master_sync_in_sec=round(m_in, 4),
            master_sync_out_sec=round(m_out, 4),
            speaker_id=sp["speaker_id"],
            reason=sp["reason"],
            confidence=float(sp["confidence"]),
            needs_review=bool(sp["needs_review"]),
        )
        shots.append(shot)

    state.rough_cut_shots = shots
    state.metadata["master_audio_source_id"] = audio_fallback_cam

    # 5. Emit validated Timeline Command API v1 batch
    report_progress(0.80, f"Emitting Timeline Command API v1 batch for {len(shots)} shots")
    builder = TimelineCommandBuilder(
        project_id=state.project_id,
        sequence_id=state.sequence_id,
        expected_revision=state.revision,
        agent_id="rough_cut_multicam_agent",
        agent_role="rough_cut_and_multicam_director",
        transaction_label=(
            "Assemble Multicam Rough Cut"
            if state.intent.multicam_switch
            else "Trim Silence & Setup Delay"
        ),
        requires_approval=False,
        dry_run=state.dry_run,
        fps_num=state.fps_num,
        fps_den=state.fps_den,
    )

    spec = state.profile["video_spec"]
    builder.add_operation(
        "create_sequence",
        params={
            "profile_id": state.profile["profile_id"],
            "display_name": state.profile["display_name"],
            "width": spec["width"],
            "height": spec["height"],
            "fps_num": spec["fps_num"],
            "fps_den": spec["fps_den"],
            "color_space": spec.get("color_space", "bt709"),
            "setup_delay_trimmed_sec": state.setup_delay_sec
            if state.intent.remove_setup_delay
            else 0.0,
            "total_frames": timeline_cursor_frame,
        },
        confidence=1.0,
        rationale=f"Create master sequence for Show Profile '{state.profile['profile_id']}'",
    )

    for track_id, track_type, track_name in (
        ("V1", "video", "Multicam Program V1"),
        ("V2", "video", "Graphics & Lower Thirds V2"),
        ("A1", "audio", "Dialogue Master A1"),
        ("A2", "audio", "Music Bed A2"),
    ):
        builder.add_operation(
            "create_track",
            track_id=track_id,
            params={"track_type": track_type, "name": track_name},
            confidence=1.0,
            rationale=f"Create {track_type} track {track_id}",
        )

    for shot in shots:
        if state.intent.multicam_switch:
            builder.add_operation(
                "select_camera",
                media_ref=shot.media_ref,
                clip_id=shot.shot_id,
                track_id="V1",
                in_frame=shot.source_in_frame,
                out_frame=shot.source_out_frame,
                timeline_start=shot.timeline_in_frame,
                params={
                    "camera_id": shot.camera_id,
                    "speaker_id": shot.speaker_id,
                    "reason": shot.reason,
                    "master_sync_in_sec": shot.master_sync_in_sec,
                    "master_sync_out_sec": shot.master_sync_out_sec,
                },
                confidence=shot.confidence,
                rationale=f"Switch to {shot.camera_id} for {shot.speaker_id} ({shot.reason})",
            )
        if state.media_assets[shot.camera_id].has_video:
            builder.add_operation(
                "place_clip",
                media_ref=shot.media_ref,
                clip_id=f"{shot.shot_id}_v",
                track_id="V1",
                in_frame=shot.source_in_frame,
                out_frame=shot.source_out_frame,
                timeline_start=shot.timeline_in_frame,
                params={"camera_id": shot.camera_id, "stream": "video"},
                confidence=shot.confidence,
                rationale=f"Place video shot {shot.shot_id} ({shot.camera_id}) on V1",
            )
        builder.add_operation(
            "place_clip",
            media_ref=shot.audio_media_ref,
            clip_id=f"{shot.shot_id}_a",
            track_id="A1",
            in_frame=shot.audio_source_in_frame,
            out_frame=shot.audio_source_out_frame,
            timeline_start=shot.timeline_in_frame,
            params={"speaker_id": shot.speaker_id, "stream": "audio"},
            confidence=shot.confidence,
            rationale=f"Place synced dialogue audio for {shot.shot_id} on A1",
        )

        if shot.needs_review:
            marker = state.add_review_marker(
                timeline_sec=shot.timeline_in_sec,
                label=f"Review Cut: {shot.camera_id} ({shot.confidence:.2f})",
                category="review",
                confidence=shot.confidence,
                rationale=f"Uncertain cut on {shot.shot_id} ({shot.reason}, conf={shot.confidence:.2f})",
            )
            builder.add_operation(
                "add_marker",
                track_id="V1",
                in_frame=marker.timeline_frame,
                out_frame=marker.timeline_frame,
                timeline_start=marker.timeline_frame,
                params={
                    "marker_id": marker.marker_id,
                    "category": marker.category,
                    "label": marker.label,
                    "shot_id": shot.shot_id,
                },
                confidence=shot.confidence,
                rationale=marker.rationale,
            )

    # Map story sections onto edited timeline markers
    for sec in state.story_sections:
        edited_t = map_master_to_edited_sec(sec.start_sec, kept_intervals)
        if edited_t is not None:
            marker = state.add_review_marker(
                timeline_sec=edited_t,
                label=f"{sec.label}: {sec.dominant_speaker}",
                category="section",
                confidence=sec.confidence,
                rationale=f"Story section '{sec.label}' mapped to edited timeline",
            )
            builder.add_operation(
                "add_marker",
                track_id="V1",
                in_frame=marker.timeline_frame,
                out_frame=marker.timeline_frame,
                timeline_start=marker.timeline_frame,
                params={
                    "marker_id": marker.marker_id,
                    "category": marker.category,
                    "label": marker.label,
                    "section_id": sec.section_id,
                },
                confidence=sec.confidence,
                rationale=marker.rationale,
            )

    report_progress(
        0.98, f"Assembled {len(shots)} shots ({timeline_cursor_frame / fps:.2f}s edited)"
    )
    return builder.build()
