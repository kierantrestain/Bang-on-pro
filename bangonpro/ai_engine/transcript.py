"""Rich Editable Transcript linked to Timeline Positions, Speakers & Timeline Command API v1.

Provides load/save, speaker renaming, text editing, segment deletion toggling,
transcript search, and deterministic diffing of edited transcripts into schema-
validated Timeline Command API v1 batches (captions + clip cuts/trims/splits).

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import copy
import json
import re
from fractions import Fraction
from pathlib import Path
from typing import Any

from bangonpro.ai_engine.commands import (
    TimelineCommandBuilder,
    get_supported_command_types,
    validate_timeline_command_batch,
)
from bangonpro.ai_engine.state import ProjectAnalysisState

TRANSCRIPT_SCHEMA_VERSION = "1.0"

_FILLER_TOKENS: frozenset[str] = frozenset(
    {
        "um",
        "umm",
        "uh",
        "uhh",
        "er",
        "erm",
        "ah",
        "ahh",
        "hmm",
        "hm",
        "mm",
        "mhm",
        "uh-huh",
    }
)

_SILENCE_MARKER_RE = re.compile(
    r"^\s*(\[(silence|pause|dead\s*air|interruption|setup\s*delay|filler)\]|…+|\.+)\s*$",
    re.IGNORECASE,
)


def _sec_to_frame(sec: float, fps: float) -> int:
    return max(0, int(round(max(0.0, float(sec)) * float(fps))))


def _fps_to_rational(fps: float) -> tuple[int, int]:
    if fps <= 0:
        return (25, 1)
    # Recognize exact NTSC broadcast rates
    for num, den in ((24000, 1001), (30000, 1001), (60000, 1001)):
        if abs(fps - (num / den)) < 0.002:
            return (num, den)
    frac = Fraction(fps).limit_denominator(1001)
    return (max(1, int(frac.numerator)), max(1, int(frac.denominator)))


def is_filler_or_silence_text(text: str) -> bool:
    """Return True if segment text represents a filler utterance or silence marker."""
    cleaned = (text or "").strip()
    if not cleaned:
        return True
    if _SILENCE_MARKER_RE.match(cleaned):
        return True
    # Strip punctuation and check whether all words are filler interjections
    tokens = [re.sub(r"[^a-zA-Z'-]+", "", w).lower() for w in cleaned.split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return True
    return all(t in _FILLER_TOKENS for t in tokens)


def build_words_for_segment(
    text: str,
    start_sec: float,
    end_sec: float,
    fps: float,
    *,
    confidence: float = 0.9,
    explicit_words: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build frame-accurate word timing entries spanning ``[start_sec, end_sec]``."""
    safe_start = max(0.0, float(start_sec))
    safe_end = max(safe_start, float(end_sec))
    safe_fps = float(fps) if fps > 0 else 25.0
    base_conf = round(min(1.0, max(0.0, float(confidence))), 4)

    if explicit_words:
        normalized: list[dict[str, Any]] = []
        for entry in explicit_words:
            w_text = str(entry.get("word", "")).strip()
            if not w_text:
                continue
            w_s = max(safe_start, float(entry.get("start_sec", safe_start)))
            w_e = max(w_s, min(safe_end, float(entry.get("end_sec", w_s))))
            s_f = _sec_to_frame(w_s, safe_fps)
            e_f = max(s_f, _sec_to_frame(w_e, safe_fps))
            w_conf = round(min(1.0, max(0.0, float(entry.get("confidence", base_conf)))), 4)
            normalized.append(
                {
                    "word": w_text,
                    "start_sec": round(w_s, 4),
                    "end_sec": round(w_e, 4),
                    "start_frame": s_f,
                    "end_frame": e_f,
                    "confidence": w_conf,
                }
            )
        if normalized:
            return normalized

    raw_tokens = [tok for tok in (text or "").strip().split() if tok]
    if not raw_tokens:
        return []

    dur = max(0.01, safe_end - safe_start)
    weights = [max(1, len(re.sub(r"[^A-Za-z0-9]", "", tok))) for tok in raw_tokens]
    total_weight = float(sum(weights))

    words: list[dict[str, Any]] = []
    cursor = safe_start
    for idx, (tok, weight) in enumerate(zip(raw_tokens, weights)):
        w_dur = dur * (weight / total_weight)
        w_end = safe_end if idx == len(raw_tokens) - 1 else min(safe_end, cursor + w_dur)
        s_f = _sec_to_frame(cursor, safe_fps)
        e_f = max(s_f, _sec_to_frame(w_end, safe_fps))
        words.append(
            {
                "word": tok,
                "start_sec": round(cursor, 4),
                "end_sec": round(w_end, 4),
                "start_frame": s_f,
                "end_frame": e_f,
                "confidence": base_conf,
            }
        )
        cursor = w_end
    return words


def validate_transcript(transcript: dict[str, Any]) -> dict[str, Any]:
    """Validate structural invariants of a Bang On Pro ``transcript.json`` document."""
    if not isinstance(transcript, dict):
        raise ValueError("Transcript must be a JSON object")
    for req_key in ("schema_version", "project_id", "sequence_id", "fps", "speakers", "segments"):
        if req_key not in transcript:
            raise ValueError(f"Transcript missing required field '{req_key}'")
    if float(transcript["fps"]) <= 0:
        raise ValueError("Transcript 'fps' must be > 0")
    if not isinstance(transcript["speakers"], list):
        raise ValueError("Transcript 'speakers' must be a list")
    if not isinstance(transcript["segments"], list):
        raise ValueError("Transcript 'segments' must be a list")

    seen_seg_ids: set[str] = set()
    for seg in transcript["segments"]:
        if not isinstance(seg, dict):
            raise ValueError("Every transcript segment must be a JSON object")
        seg_id = str(seg.get("segment_id", "")).strip()
        if not seg_id:
            raise ValueError("Transcript segment missing non-empty 'segment_id'")
        if seg_id in seen_seg_ids:
            raise ValueError(f"Duplicate segment_id in transcript: '{seg_id}'")
        seen_seg_ids.add(seg_id)
        if float(seg.get("end_sec", 0.0)) < float(seg.get("start_sec", 0.0)):
            raise ValueError(f"Segment '{seg_id}' has end_sec < start_sec")
        if int(seg.get("end_frame", 0)) < int(seg.get("start_frame", 0)):
            raise ValueError(f"Segment '{seg_id}' has end_frame < start_frame")

    return transcript


def build_transcript_document(
    state: ProjectAnalysisState,
    *,
    use_edited_timeline: bool = False,
) -> dict[str, Any]:
    """Construct the structured ``transcript.json`` document linked to timeline frames & speakers."""
    fps = float(state.fps)

    # 1. Assign deterministic speaker_id ("spk_1", "spk_2", ...) for every known speaker
    speaker_by_name: dict[str, dict[str, Any]] = {}
    speakers_list: list[dict[str, Any]] = []

    def _register_speaker(display_name: str, camera_id: str) -> str:
        key = display_name.strip() or camera_id or "Speaker"
        if key not in speaker_by_name:
            spk_id = f"spk_{len(speakers_list) + 1}"
            rec = {
                "speaker_id": spk_id,
                "display_name": key,
                "camera_id": camera_id,
            }
            speaker_by_name[key] = rec
            speakers_list.append(rec)
        return speaker_by_name[key]["speaker_id"]

    # Seed from media assets in deterministic priority order
    for cam_id, asset in state.media_assets.items():
        if asset.speaker_label:
            _register_speaker(asset.speaker_label, cam_id)

    for seg in state.transcript_segments:
        name = seg.speaker_name or seg.speaker_id
        _register_speaker(name, seg.camera_id)

    # 2. Build segments (either on master sync timeline or mapped onto edited rough-cut timeline)
    doc_segments: list[dict[str, Any]] = []
    edited_duration = state.rough_cut_shots[-1].timeline_out_sec if state.rough_cut_shots else 0.0

    def _find_overlapping_clips(ed_start_f: int, ed_end_f: int) -> list[dict[str, Any]]:
        if not state.rough_cut_shots:
            return []
        clips: list[dict[str, Any]] = []
        for shot in state.rough_cut_shots:
            shot_s = shot.timeline_in_frame
            shot_e = shot.timeline_out_frame + 1
            if shot_e > ed_start_f and shot_s < ed_end_f:
                clips.append(
                    {
                        "clip_id": f"{shot.shot_id}_v",
                        "track_id": "V1",
                        "timeline_start": shot_s,
                        "timeline_end": shot_e,
                        "in_frame": shot.source_in_frame,
                        "out_frame": shot.source_out_frame + 1,
                    }
                )
                clips.append(
                    {
                        "clip_id": f"{shot.shot_id}_a",
                        "track_id": "A1",
                        "timeline_start": shot_s,
                        "timeline_end": shot_e,
                        "in_frame": shot.audio_source_in_frame,
                        "out_frame": shot.audio_source_out_frame + 1,
                    }
                )
        return clips

    for seg in state.transcript_segments:
        display_name = seg.speaker_name or seg.speaker_id
        spk_id = _register_speaker(display_name, seg.camera_id)
        overlap_names = list(seg.overlap_speakers) if seg.is_overlap else []

        if use_edited_timeline and state.kept_master_intervals and state.rough_cut_shots:
            # Map segment across kept intervals onto edited timeline
            cursor = 0.0
            part_idx = 0
            for iv in state.kept_master_intervals:
                ov_s = max(seg.start_sec, iv.start_sec)
                ov_e = min(seg.end_sec, iv.end_sec)
                if (ov_e - ov_s) >= 0.15:
                    ed_s = round(cursor + (ov_s - iv.start_sec), 4)
                    ed_e = round(min(edited_duration, cursor + (ov_e - iv.start_sec)), 4)
                    if ed_e > ed_s + 0.05:
                        part_idx += 1
                        seg_id_out = seg.segment_id if part_idx == 1 else f"{seg.segment_id}_p{part_idx}"
                        s_f = _sec_to_frame(ed_s, fps)
                        e_f = max(s_f + 1, _sec_to_frame(ed_e, fps))
                        words = build_words_for_segment(
                            seg.text,
                            ed_s,
                            ed_e,
                            fps,
                            confidence=seg.confidence,
                        )
                        entry: dict[str, Any] = {
                            "segment_id": seg_id_out,
                            "speaker_id": spk_id,
                            "speaker_name": display_name,
                            "camera_id": seg.camera_id,
                            "start_sec": ed_s,
                            "end_sec": ed_e,
                            "start_frame": s_f,
                            "end_frame": e_f,
                            "text": seg.text,
                            "confidence": round(float(seg.confidence), 4),
                            "is_overlap": bool(seg.is_overlap),
                            "overlap_speakers": overlap_names,
                            "is_filler_or_silence": bool(
                                seg.is_filler_or_silence or is_filler_or_silence_text(seg.text)
                            ),
                            "deleted": bool(seg.deleted),
                            "words": words,
                        }
                        linked_clips = _find_overlapping_clips(s_f, e_f)
                        if linked_clips:
                            entry["linked_clips"] = linked_clips
                        doc_segments.append(entry)
                cursor += iv.duration_sec
        else:
            s_f = _sec_to_frame(seg.start_sec, fps)
            e_f = max(s_f + 1, _sec_to_frame(seg.end_sec, fps))
            words = build_words_for_segment(
                seg.text,
                seg.start_sec,
                seg.end_sec,
                fps,
                confidence=seg.confidence,
                explicit_words=seg.words or None,
            )
            doc_segments.append(
                {
                    "segment_id": seg.segment_id,
                    "speaker_id": spk_id,
                    "speaker_name": display_name,
                    "camera_id": seg.camera_id,
                    "start_sec": round(float(seg.start_sec), 4),
                    "end_sec": round(float(seg.end_sec), 4),
                    "start_frame": s_f,
                    "end_frame": e_f,
                    "text": seg.text,
                    "confidence": round(float(seg.confidence), 4),
                    "is_overlap": bool(seg.is_overlap),
                    "overlap_speakers": overlap_names,
                    "is_filler_or_silence": bool(
                        seg.is_filler_or_silence or is_filler_or_silence_text(seg.text)
                    ),
                    "deleted": bool(seg.deleted),
                    "words": words,
                }
            )

    doc: dict[str, Any] = {
        "schema_version": TRANSCRIPT_SCHEMA_VERSION,
        "project_id": state.project_id,
        "sequence_id": state.sequence_id,
        "fps": round(fps, 6),
        "speakers": speakers_list,
        "segments": doc_segments,
    }
    return validate_transcript(doc)


def load_transcript(path: str | Path) -> dict[str, Any]:
    """Load and validate a ``transcript.json`` file from disk."""
    src = Path(path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Transcript file not found: {src}")
    data = json.loads(src.read_text(encoding="utf-8"))
    return validate_transcript(data)


def save_transcript(transcript: dict[str, Any], path: str | Path) -> Path:
    """Validate and write a ``transcript.json`` file to disk."""
    validated = validate_transcript(transcript)
    dest = Path(path).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(validated, indent=2) + "\n", encoding="utf-8")
    return dest.resolve()


def rename_speaker(
    transcript: dict[str, Any],
    speaker_id_or_name: str,
    new_display_name: str,
) -> dict[str, Any]:
    """Return a copy of ``transcript`` with the specified speaker renamed across metadata & segments."""
    cleaned_new = (new_display_name or "").strip()
    if not cleaned_new:
        raise ValueError("new_display_name must be a non-empty string")

    updated = copy.deepcopy(validate_transcript(transcript))
    target_key = (speaker_id_or_name or "").strip()
    matched_speaker: dict[str, Any] | None = None
    for spk in updated["speakers"]:
        if spk.get("speaker_id") == target_key or spk.get("display_name") == target_key:
            matched_speaker = spk
            break

    if matched_speaker is None:
        raise KeyError(f"Speaker '{speaker_id_or_name}' not found in transcript")

    old_name = str(matched_speaker.get("display_name", ""))
    spk_id = str(matched_speaker["speaker_id"])
    matched_speaker["display_name"] = cleaned_new

    for seg in updated["segments"]:
        if seg.get("speaker_id") == spk_id or seg.get("speaker_name") == old_name:
            seg["speaker_name"] = cleaned_new
        if seg.get("overlap_speakers"):
            seg["overlap_speakers"] = [
                cleaned_new if s in (old_name, spk_id) else s
                for s in seg["overlap_speakers"]
            ]

    return updated


def edit_segment_text(
    transcript: dict[str, Any],
    segment_id: str,
    new_text: str,
) -> dict[str, Any]:
    """Return a copy of ``transcript`` with updated segment text and recomputed word timings."""
    updated = copy.deepcopy(validate_transcript(transcript))
    fps = float(updated["fps"])
    for seg in updated["segments"]:
        if seg["segment_id"] == segment_id:
            cleaned = str(new_text).strip()
            seg["text"] = cleaned
            seg["is_filler_or_silence"] = is_filler_or_silence_text(cleaned)
            seg["words"] = build_words_for_segment(
                cleaned,
                float(seg["start_sec"]),
                float(seg["end_sec"]),
                fps,
                confidence=float(seg.get("confidence", 0.95)),
            )
            return updated
    raise KeyError(f"Segment '{segment_id}' not found in transcript")


def toggle_segment_deleted(
    transcript: dict[str, Any],
    segment_id: str,
    deleted: bool | None = None,
) -> dict[str, Any]:
    """Return a copy of ``transcript`` with ``segment.deleted`` toggled or explicitly set."""
    updated = copy.deepcopy(validate_transcript(transcript))
    for seg in updated["segments"]:
        if seg["segment_id"] == segment_id:
            if deleted is None:
                seg["deleted"] = not bool(seg.get("deleted", False))
            else:
                seg["deleted"] = bool(deleted)
            return updated
    raise KeyError(f"Segment '{segment_id}' not found in transcript")


def search_transcript(
    transcript: dict[str, Any],
    query: str,
    *,
    speaker: str | None = None,
    include_deleted: bool = False,
    case_sensitive: bool = False,
) -> list[dict[str, Any]]:
    """Search transcript segments by text substring and optional speaker filter."""
    validated = validate_transcript(transcript)
    needle = (query or "").strip()
    needle_cmp = needle if case_sensitive else needle.lower()
    spk_filter = speaker.strip().lower() if speaker else None

    matches: list[dict[str, Any]] = []
    for seg in validated["segments"]:
        if not include_deleted and bool(seg.get("deleted", False)):
            continue
        if spk_filter is not None:
            seg_spk_id = str(seg.get("speaker_id", "")).lower()
            seg_spk_name = str(seg.get("speaker_name", "")).lower()
            if spk_filter not in (seg_spk_id, seg_spk_name):
                continue

        seg_text = str(seg.get("text", ""))
        haystack = seg_text if case_sensitive else seg_text.lower()
        if needle_cmp and needle_cmp not in haystack:
            continue

        matched_words = [
            w
            for w in seg.get("words", [])
            if not needle_cmp
            or (
                needle_cmp in str(w.get("word", ""))
                if case_sensitive
                else needle_cmp in str(w.get("word", "")).lower()
            )
        ]
        hit = copy.deepcopy(seg)
        hit["matched_words"] = matched_words
        matches.append(hit)
    return matches


def _emit_deleted_segment_clip_ops(
    builder: TimelineCommandBuilder,
    seg: dict[str, Any],
    *,
    clip_map: list[dict[str, Any]] | None = None,
) -> None:
    """Emit ``split_clip`` / ``trim_clip`` / ``delete_clip`` operations excising a deleted segment."""
    seg_id = str(seg["segment_id"])
    seg_start = int(seg["start_frame"])
    seg_end = max(seg_start + 1, int(seg["end_frame"]))
    seg_conf = float(seg.get("confidence", 0.95))

    candidates = clip_map if clip_map is not None else seg.get("linked_clips")
    if candidates:
        matched_any = False
        for clip in candidates:
            c_id = str(clip["clip_id"])
            t_id = str(clip.get("track_id", "V1"))
            c_tl_start = int(clip["timeline_start"])
            c_tl_end = int(clip["timeline_end"])
            c_src_in = int(clip.get("in_frame", 0))
            c_src_out = int(clip.get("out_frame", c_src_in + max(1, c_tl_end - c_tl_start)))

            ov_s = max(seg_start, c_tl_start)
            ov_e = min(seg_end, c_tl_end)
            if ov_e <= ov_s:
                continue
            matched_any = True

            # Case 1: Deleted segment covers entire clip -> delete_clip
            if ov_s <= c_tl_start and ov_e >= c_tl_end:
                builder.add_operation(
                    "delete_clip",
                    clip_id=c_id,
                    track_id=t_id,
                    in_frame=c_src_in,
                    out_frame=max(c_src_in + 1, c_src_out),
                    timeline_start=c_tl_start,
                    params={
                        "segment_id": seg_id,
                        "ripple": True,
                        "deleted_range_frames": [seg_start, seg_end],
                    },
                    confidence=seg_conf,
                    rationale=f"Delete clip '{c_id}' covered by deleted transcript segment '{seg_id}'",
                )
            # Case 2: Deleted segment trims leading edge of clip -> trim_clip (left edge)
            elif ov_s <= c_tl_start < ov_e < c_tl_end:
                trim_delta = ov_e - c_tl_start
                new_src_in = c_src_in + trim_delta
                new_src_out = max(new_src_in + 1, c_src_out)
                builder.add_operation(
                    "trim_clip",
                    clip_id=c_id,
                    track_id=t_id,
                    in_frame=new_src_in,
                    out_frame=new_src_out,
                    timeline_start=ov_e,
                    params={
                        "segment_id": seg_id,
                        "right": False,
                        "duration_frames": new_src_out - new_src_in,
                        "trimmed_range_frames": [ov_s, ov_e],
                    },
                    confidence=seg_conf,
                    rationale=f"Trim leading frames [{ov_s}, {ov_e}) of clip '{c_id}' for deleted segment '{seg_id}'",
                )
            # Case 3: Deleted segment trims trailing edge of clip -> trim_clip (right edge)
            elif c_tl_start < ov_s < c_tl_end <= ov_e:
                keep_dur = max(1, ov_s - c_tl_start)
                new_src_out = c_src_in + keep_dur
                builder.add_operation(
                    "trim_clip",
                    clip_id=c_id,
                    track_id=t_id,
                    in_frame=c_src_in,
                    out_frame=new_src_out,
                    timeline_start=c_tl_start,
                    params={
                        "segment_id": seg_id,
                        "right": True,
                        "duration_frames": keep_dur,
                        "trimmed_range_frames": [ov_s, ov_e],
                    },
                    confidence=seg_conf,
                    rationale=f"Trim trailing frames [{ov_s}, {ov_e}) of clip '{c_id}' for deleted segment '{seg_id}'",
                )
            # Case 4: Deleted segment is strictly inside clip -> split_clip x2 + delete_clip middle
            else:
                mid_clip_id = f"{c_id}_{seg_id}_mid"
                right_clip_id = f"{c_id}_{seg_id}_right"
                builder.add_operation(
                    "split_clip",
                    clip_id=c_id,
                    track_id=t_id,
                    timeline_start=ov_s,
                    params={
                        "segment_id": seg_id,
                        "split_frame": ov_s,
                        "new_clip_id": mid_clip_id,
                    },
                    confidence=seg_conf,
                    rationale=f"Split clip '{c_id}' at frame {ov_s} (start of deleted segment '{seg_id}')",
                )
                builder.add_operation(
                    "split_clip",
                    clip_id=mid_clip_id,
                    track_id=t_id,
                    timeline_start=ov_e,
                    params={
                        "segment_id": seg_id,
                        "split_frame": ov_e,
                        "new_clip_id": right_clip_id,
                    },
                    confidence=seg_conf,
                    rationale=f"Split clip '{mid_clip_id}' at frame {ov_e} (end of deleted segment '{seg_id}')",
                )
                builder.add_operation(
                    "delete_clip",
                    clip_id=mid_clip_id,
                    track_id=t_id,
                    in_frame=c_src_in + (ov_s - c_tl_start),
                    out_frame=c_src_in + (ov_e - c_tl_start),
                    timeline_start=ov_s,
                    params={
                        "segment_id": seg_id,
                        "ripple": True,
                        "deleted_range_frames": [ov_s, ov_e],
                    },
                    confidence=seg_conf,
                    rationale=f"Delete split interior clip '{mid_clip_id}' corresponding to deleted segment '{seg_id}'",
                )
        if matched_any:
            return

    # Fallback when standalone transcript has no explicit clip_map: emit V1/A1 clip deletions
    for track_id, suffix in (("V1", "v"), ("A1", "a")):
        clip_id = f"{seg_id}_{suffix}"
        builder.add_operation(
            "delete_clip",
            clip_id=clip_id,
            track_id=track_id,
            in_frame=seg_start,
            out_frame=seg_end,
            timeline_start=seg_start,
            params={
                "segment_id": seg_id,
                "ripple": True,
                "deleted_range_frames": [seg_start, seg_end],
            },
            confidence=seg_conf,
            rationale=f"Delete {track_id} clip range [{seg_start}, {seg_end}) for deleted transcript segment '{seg_id}'",
        )


def build_transcript_edit_command_batch(
    original_transcript: dict[str, Any],
    edited_transcript: dict[str, Any],
    *,
    expected_revision: int = 0,
    project_id: str | None = None,
    sequence_id: str | None = None,
    agent_id: str = "transcript_editor_agent",
    requires_approval: bool = True,
    dry_run: bool = False,
    clip_map: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Diff ``original_transcript`` vs ``edited_transcript`` into a validated Timeline Command API v1 batch.

    Emits caption updates/additions/deletions (using native ``update_caption`` /
    ``delete_caption`` when permitted by the active Timeline Command schema, or
    schema-valid ``add_caption`` / ``add_marker`` equivalents otherwise) together
    with ``split_clip`` / ``trim_clip`` / ``delete_clip`` operations for deleted
    transcript segments, all annotated with confidence scores and rationales.
    """
    orig = validate_transcript(original_transcript)
    edited = validate_transcript(edited_transcript)

    fps = float(edited.get("fps") or orig.get("fps") or 25.0)
    fps_num, fps_den = _fps_to_rational(fps)
    supported_ops = get_supported_command_types()
    has_update_caption = "update_caption" in supported_ops
    has_delete_caption = "delete_caption" in supported_ops

    builder = TimelineCommandBuilder(
        project_id=project_id or str(edited["project_id"]),
        sequence_id=sequence_id or str(edited["sequence_id"]),
        expected_revision=expected_revision,
        agent_id=agent_id,
        agent_role="transcript_based_editor",
        transaction_label="Apply Transcript Text, Speaker & Deletion Edits",
        requires_approval=requires_approval,
        dry_run=dry_run,
        fps_num=fps_num,
        fps_den=fps_den,
    )

    orig_speakers = {s["speaker_id"]: s.get("display_name", "") for s in orig["speakers"]}
    edited_speakers = {s["speaker_id"]: s.get("display_name", "") for s in edited["speakers"]}
    renamed_speaker_ids = {
        sid
        for sid, new_name in edited_speakers.items()
        if sid in orig_speakers and orig_speakers[sid] != new_name
    }

    orig_segs = {seg["segment_id"]: seg for seg in orig["segments"]}
    edited_segs = {seg["segment_id"]: seg for seg in edited["segments"]}

    # 1. Handle removed segments (present in original, absent in edited)
    for seg_id, orig_seg in orig_segs.items():
        if seg_id not in edited_segs and not bool(orig_seg.get("deleted", False)):
            s_f = int(orig_seg["start_frame"])
            e_f = max(s_f + 1, int(orig_seg["end_frame"]))
            conf = float(orig_seg.get("confidence", 0.95))
            if has_delete_caption:
                builder.add_operation(
                    "delete_caption",
                    track_id="V2",
                    in_frame=s_f,
                    out_frame=e_f,
                    timeline_start=s_f,
                    params={
                        "action": "delete_caption",
                        "caption_id": f"cap_{seg_id}",
                        "segment_id": seg_id,
                    },
                    confidence=conf,
                    rationale=f"Delete caption for removed transcript segment '{seg_id}'",
                )
            else:
                builder.add_operation(
                    "add_marker",
                    track_id="V2",
                    in_frame=s_f,
                    out_frame=e_f,
                    timeline_start=s_f,
                    params={
                        "action": "delete_caption",
                        "category": "caption_delete",
                        "caption_id": f"cap_{seg_id}",
                        "segment_id": seg_id,
                        "label": f"Delete Caption: {seg_id}",
                    },
                    confidence=conf,
                    rationale=f"Remove caption for deleted transcript segment '{seg_id}'",
                )
            _emit_deleted_segment_clip_ops(builder, orig_seg, clip_map=clip_map)

    # 2. Handle modified, deleted, restored, and newly added segments
    for seg_id, new_seg in edited_segs.items():
        s_f = int(new_seg["start_frame"])
        e_f = max(s_f + 1, int(new_seg["end_frame"]))
        conf = float(new_seg.get("confidence", 0.95))
        new_deleted = bool(new_seg.get("deleted", False))
        speaker_name = str(new_seg.get("speaker_name") or new_seg.get("speaker_id", ""))

        if seg_id not in orig_segs:
            if new_deleted:
                continue
            builder.add_operation(
                "add_caption",
                track_id="V2",
                in_frame=s_f,
                out_frame=e_f,
                timeline_start=s_f,
                params={
                    "action": "add_caption",
                    "caption_id": f"cap_{seg_id}",
                    "segment_id": seg_id,
                    "speaker_id": new_seg.get("speaker_id", ""),
                    "speaker": speaker_name,
                    "text": new_seg.get("text", ""),
                    "duration_frames": e_f - s_f,
                },
                confidence=conf,
                rationale=f"Add new caption for inserted transcript segment '{seg_id}' ({speaker_name})",
            )
            continue

        old_seg = orig_segs[seg_id]
        old_deleted = bool(old_seg.get("deleted", False))

        # Newly deleted segment (deleted: False -> True)
        if not old_deleted and new_deleted:
            if has_delete_caption:
                builder.add_operation(
                    "delete_caption",
                    track_id="V2",
                    in_frame=s_f,
                    out_frame=e_f,
                    timeline_start=s_f,
                    params={
                        "action": "delete_caption",
                        "caption_id": f"cap_{seg_id}",
                        "segment_id": seg_id,
                    },
                    confidence=conf,
                    rationale=f"Delete caption for transcript segment '{seg_id}' marked deleted",
                )
            else:
                builder.add_operation(
                    "add_marker",
                    track_id="V2",
                    in_frame=s_f,
                    out_frame=e_f,
                    timeline_start=s_f,
                    params={
                        "action": "delete_caption",
                        "category": "caption_delete",
                        "caption_id": f"cap_{seg_id}",
                        "segment_id": seg_id,
                        "label": f"Delete Caption: {seg_id}",
                    },
                    confidence=conf,
                    rationale=f"Remove caption for transcript segment '{seg_id}' marked deleted",
                )
            _emit_deleted_segment_clip_ops(builder, new_seg, clip_map=clip_map)
            continue

        # Restored segment (deleted: True -> False)
        if old_deleted and not new_deleted:
            builder.add_operation(
                "add_caption",
                track_id="V2",
                in_frame=s_f,
                out_frame=e_f,
                timeline_start=s_f,
                params={
                    "action": "add_caption",
                    "caption_id": f"cap_{seg_id}",
                    "segment_id": seg_id,
                    "speaker_id": new_seg.get("speaker_id", ""),
                    "speaker": speaker_name,
                    "text": new_seg.get("text", ""),
                    "duration_frames": e_f - s_f,
                },
                confidence=conf,
                rationale=f"Restore caption for undeleted transcript segment '{seg_id}'",
            )
            continue

        # Active segment text / speaker / timing edit
        if new_deleted:
            continue

        text_changed = str(old_seg.get("text", "")) != str(new_seg.get("text", ""))
        speaker_changed = (
            str(old_seg.get("speaker_name", "")) != speaker_name
            or str(old_seg.get("speaker_id", "")) != str(new_seg.get("speaker_id", ""))
            or str(new_seg.get("speaker_id", "")) in renamed_speaker_ids
        )
        timing_changed = (
            int(old_seg.get("start_frame", 0)) != s_f
            or int(old_seg.get("end_frame", 0)) != e_f
        )

        if text_changed or speaker_changed or timing_changed:
            reasons: list[str] = []
            if text_changed:
                reasons.append("text edited")
            if speaker_changed:
                reasons.append(f"speaker updated to '{speaker_name}'")
            if timing_changed:
                reasons.append(f"timing adjusted to [{s_f}, {e_f})")

            op_type = "update_caption" if has_update_caption else "add_caption"
            builder.add_operation(
                op_type,
                track_id="V2",
                in_frame=s_f,
                out_frame=e_f,
                timeline_start=s_f,
                params={
                    "action": "update_caption",
                    "replace_existing": True,
                    "caption_id": f"cap_{seg_id}",
                    "segment_id": seg_id,
                    "speaker_id": new_seg.get("speaker_id", ""),
                    "speaker": speaker_name,
                    "text": new_seg.get("text", ""),
                    "previous_text": old_seg.get("text", ""),
                    "duration_frames": e_f - s_f,
                },
                confidence=conf,
                rationale=f"Update caption for segment '{seg_id}' ({', '.join(reasons)})",
            )

    if builder.operation_count == 0:
        raise ValueError("No transcript edits detected between original and edited transcripts")

    return validate_timeline_command_batch(builder.build())
