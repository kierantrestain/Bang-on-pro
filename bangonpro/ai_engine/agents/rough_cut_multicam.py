"""Scene Detection, Non-Destructive Rough Cut, and Multicam Angle-Switching Agent.

Combines PySceneDetect visual cut analysis with dialogue/silence structure from
``transcribe_diarise`` to assemble a Show-Profile-compliant multicamera rough
cut, emitting validated Timeline Command API v1 operations (``create_sequence``,
``create_track``, ``select_camera``, ``place_clip``, ``add_marker``).

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

from typing import Any, Callable

import numpy as np

from bangonpro.ai_engine.commands import TimelineCommandBuilder
from bangonpro.ai_engine.orchestrator import CancellationToken
from bangonpro.ai_engine.state import (
    EditShot,
    ProjectAnalysisState,
    TimeInterval,
    TranscriptSegment,
)


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
    except Exception:  # noqa: BLE001
        pass

    # Deterministic OpenCV histogram difference fallback
    try:
        import cv2

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return [], "fallback_unavailable"
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        prev_hist = None
        cuts: list[float] = []
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
                    cuts.append(round(frame_idx / fps, 4))
            prev_hist = hist
            frame_idx += 1
        cap.release()
        return cuts, "opencv_hist_fallback"
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
    """Compute kept master-timeline segments after setup-delay & silence trimming."""
    rules = state.profile["editing_rules"]
    keep_pad = float(rules.get("keep_padding_sec", 0.2))
    trim_setup = bool(rules.get("trim_setup_delay", True)) and state.intent.remove_setup_delay
    remove_silence = bool(state.intent.remove_silence)

    start_bound = 0.0
    if trim_setup and state.setup_delay_sec >= 0.40:
        start_bound = max(0.0, state.setup_delay_sec - keep_pad)

    end_bound = common_duration
    if remove_silence:
        for iv in state.silence_intervals:
            if iv.label == "trailing_silence":
                candidate_end = min(common_duration, iv.start_sec + keep_pad)
                if candidate_end > start_bound + 0.5:
                    end_bound = candidate_end

    if end_bound <= start_bound + 0.25:
        return [TimeInterval(start_sec=0.0, end_sec=max(0.5, common_duration), label="kept_full")]

    # Excise interior silence spans exceeding (2 * keep_pad + 0.15s)
    excise_spans: list[tuple[float, float]] = []
    if remove_silence:
        for iv in state.silence_intervals:
            if iv.label != "silence":
                continue
            cut_start = max(start_bound, iv.start_sec + keep_pad)
            cut_end = min(end_bound, iv.end_sec - keep_pad)
            if (cut_end - cut_start) >= 0.15:
                excise_spans.append((cut_start, cut_end))

    excise_spans.sort()

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
    """Return ``(wide_cam_id, reaction_cam_id, speaker_to_cam_map)`` from registered assets."""
    assets = state.media_assets
    ref_cam = state.sync_result.reference_camera_id if state.sync_result else next(iter(assets))

    wide_cam = next(
        (cid for cid, a in assets.items() if a.camera_role in ("wide", "desk")),
        ref_cam,
    )
    reaction_cam = next(
        (cid for cid, a in assets.items() if a.camera_role in ("reaction", "broll")),
        wide_cam,
    )

    speaker_to_cam: dict[str, str] = {}
    # Prefer direct speaker close-up roles over reaction/wide
    role_rank = {"host": 0, "guest": 0, "panel": 0, "desk": 1, "reaction": 2, "wide": 3, "broll": 4}
    for cid, asset in sorted(assets.items(), key=lambda kv: (role_rank.get(kv[1].camera_role, 5), kv[1].priority)):
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
        off = int(round(state.sync_result.offsets_sec.get(cid, 0.0) * sr))
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
    min_shot = float(rules["min_shot_sec"])
    max_shot = float(rules["max_shot_sec"])
    reaction_interval = float(rules.get("reaction_cut_interval_sec", max_shot * 0.6))
    review_thresh = float(rules.get("review_confidence_threshold", 0.72))

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
                "speaker_id": "Studio",
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
                # Assign small pre-turn padding to incoming speaker or wide
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
            cam_id = speaker_to_cam.get(seg.speaker_id, seg.camera_id)
            if cam_id not in state.media_assets:
                cam_id = wide_cam
            raw_spans.append(
                {
                    "start": seg_s,
                    "end": seg_e,
                    "camera_id": cam_id,
                    "speaker_id": seg.speaker_id,
                    "confidence": seg.confidence,
                    "reason": f"active_speaker_{seg.speaker_id.lower().replace(' ', '_')}",
                }
            )
            cursor = seg_e

        if iv.end_sec > cursor + 0.08 and raw_spans:
            raw_spans[-1]["end"] = iv.end_sec

    # 2. Merge adjacent spans on the same camera (keeping dominant span's reason and preserving low-confidence review alerts)
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

    # 3. Coalesce micro-cuts shorter than min_shot_sec (unless only 1 span exists)
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

    # 4. Jump-cut avoidance across silence splice: if interval begins on the exact same
    #    camera that ended the previous interval AND duration allows a brief wide/reaction bridge,
    #    insert a clean cutaway opener (or switch first shot if wide differs).
    if (
        prev_end_camera is not None
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

    # 5. Enforce max_shot_sec & periodic reaction/wide cutaways
    paced: list[dict[str, Any]] = []
    cutaway_toggle = 0
    for sp in coalesced:
        cur_s = sp["start"]
        sp_end = sp["end"]
        base_cam = sp["camera_id"]

        while (sp_end - cur_s) > max_shot + 0.05:
            # Choose cut point respecting reaction_cut_interval_sec & applause peaks
            target_cut = cur_s + min(max_shot, max(min_shot, reaction_interval))
            for ap_t in applause_times:
                if (cur_s + min_shot) <= ap_t <= (cur_s + max_shot - min_shot):
                    target_cut = ap_t
                    break

            # Ensure remaining tail after cutaway still leaves >= min_shot
            cutaway_dur = max(min_shot, min(reaction_interval * 0.45, max_shot * 0.4))
            if (sp_end - target_cut) < min_shot:
                break

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

            cutaway_end = min(sp_end, round(target_cut + cutaway_dur, 4))
            # Avoid leaving a tiny tail shorter than min_shot
            if 0.0 < (sp_end - cutaway_end) < min_shot:
                cutaway_end = sp_end

            alt_cam = reaction_cam if (cutaway_toggle % 2 == 0 and reaction_cam != base_cam) else wide_cam
            if alt_cam == base_cam:
                alt_cam = next((c for c in state.media_assets if c != base_cam), base_cam)
            cutaway_toggle += 1

            paced.append(
                {
                    "start": round(target_cut, 4),
                    "end": cutaway_end,
                    "camera_id": alt_cam,
                    "speaker_id": sp["speaker_id"],
                    "confidence": round(min(0.88, sp["confidence"]), 4),
                    "reason": "periodic_reaction_or_wide_cutaway",
                }
            )
            cur_s = cutaway_end

        if (sp_end - cur_s) > 0.08:
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

    # Flag low-confidence shots for human review
    for sp in paced:
        sp["needs_review"] = float(sp["confidence"]) < review_thresh

    return paced


def run_rough_cut_multicam_agent(
    state: ProjectAnalysisState,
    token: CancellationToken,
    report_progress: Callable[[float, str], None],
) -> dict[str, Any]:
    """Detect visual scenes and assemble a non-destructive multicam rough cut."""
    token.check("rough_cut_multicam start")
    if state.sync_result is None or not state.media_assets:
        raise RuntimeError("ingest_sync and transcribe_diarise must complete before rough_cut_multicam")

    ref_cam = state.sync_result.reference_camera_id
    ref_asset = state.media_assets[ref_cam]
    common_duration = state.sync_result.common_duration_sec or ref_asset.duration_sec

    # 1. Visual scene detection on reference camera
    report_progress(0.15, "Running visual scene detection")
    scene_cuts, scene_backend = detect_visual_scene_cuts(ref_asset.file_path)
    state.scene_cuts_sec = scene_cuts
    state.metadata["scene_detect_backend"] = scene_backend
    token.check("after scene detection")

    # 2. Compute kept intervals (trim setup delay & reduce dead air)
    report_progress(0.35, "Computing kept intervals (setup-delay & dead-air reduction)")
    kept_intervals = _compute_kept_master_intervals(state, common_duration)
    state.kept_master_intervals = kept_intervals

    # 3. Plan speaker-aware multicamera switching across kept intervals
    report_progress(0.55, "Planning multicamera angle switches")
    wide_cam, reaction_cam, speaker_to_cam = _resolve_camera_roles(state)
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

    # 4. Convert planned shots into frame-accurate EditShot objects
    fps = state.fps
    shots: list[EditShot] = []
    timeline_cursor_frame = 0

    for idx, sp in enumerate(planned_raw_shots, start=1):
        m_in = float(sp["start"])
        m_out = float(sp["end"])
        desired_frames = max(1, int(round((m_out - m_in) * fps)))

        cam_id = sp["camera_id"]
        v_asset = state.media_assets[cam_id]
        v_offset = state.sync_result.offsets_sec.get(cam_id, 0.0)

        # Pick active speaker's mic for A1 audio so cutaways keep uninterrupted voice
        audio_cam_id = speaker_to_cam.get(sp["speaker_id"], ref_cam)
        if audio_cam_id not in state.media_assets or state.media_assets[audio_cam_id].audio_codec is None:
            audio_cam_id = ref_cam
        a_asset = state.media_assets[audio_cam_id]
        a_offset = state.sync_result.offsets_sec.get(audio_cam_id, 0.0)

        v_src_in_sec = max(0.0, min(v_asset.duration_sec - (1.0 / fps), m_in + v_offset))
        a_src_in_sec = max(0.0, min(a_asset.duration_sec - (1.0 / fps), m_in + a_offset))

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

    # 5. Emit validated Timeline Command API v1 batch
    report_progress(0.80, f"Emitting Timeline Command API v1 batch for {len(shots)} shots")
    builder = TimelineCommandBuilder(
        project_id=state.project_id,
        sequence_id=state.sequence_id,
        expected_revision=state.revision,
        agent_id="rough_cut_multicam_agent",
        agent_role="rough_cut_and_multicam_director",
        transaction_label="Assemble Multicam Rough Cut",
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
            "setup_delay_trimmed_sec": state.setup_delay_sec if state.intent.remove_setup_delay else 0.0,
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

    report_progress(0.98, f"Assembled {len(shots)} shots ({timeline_cursor_frame / fps:.2f}s edited)")
    return builder.build()
