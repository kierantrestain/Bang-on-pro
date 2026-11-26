"""Executable Kdenlive/MLT (.kdenlive), OpenTimelineIO (.otio), and Melt Export Builder.

Strictly replays validated Timeline Command API v1 batches (never mutating raw
timelines outside validated commands) to generate:
1. A native Kdenlive 24.08 / MLT 7.22 ``.kdenlive`` XML project that opens in
   Kdenlive and renders directly via ``melt``.
2. A standards-compliant OpenTimelineIO ``.otio`` interchange timeline.
3. Optional real MP4 export via ``melt`` with ``ffprobe`` stream verification.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

import opentimelineio as otio

from bangonpro.ai_engine.agents.audio_graphics_qc import (
    generate_kdenlive_lower_third_xml,
)
from bangonpro.ai_engine.agents.ingest_sync import probe_media_file
from bangonpro.ai_engine.commands import (
    TimelineCommandBuilder,
    validate_timeline_command_batch,
)
from bangonpro.ai_engine.orchestrator import CancellationToken
from bangonpro.ai_engine.state import ProjectAnalysisState

_GUIDE_CATEGORY_MAP: dict[str, int] = {
    "setup_trim": 1,
    "section": 2,
    "review": 5,
    "qc_warning": 6,
    "qc_error": 8,
}

_KDENLIVE_GUIDE_CATEGORIES = [
    {"color": "#9b59b6", "comment": "General", "index": 0},
    {"color": "#3daee9", "comment": "Setup Trim", "index": 1},
    {"color": "#1abc9c", "comment": "Story Section", "index": 2},
    {"color": "#1cdc9a", "comment": "Approved", "index": 3},
    {"color": "#c9ce3b", "comment": "Audio Note", "index": 4},
    {"color": "#fdbc4b", "comment": "Editorial Review", "index": 5},
    {"color": "#f39c1f", "comment": "QC Warning", "index": 6},
    {"color": "#f47750", "comment": "Graphics", "index": 7},
    {"color": "#da4453", "comment": "QC Error", "index": 8},
]


@dataclass
class CompiledClip:
    clip_id: str
    track_id: str
    media_ref: str | None
    in_frame: int
    out_frame: int
    timeline_start: int
    params: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    rationale: str = ""
    # Native per-clip MLT filters (V1.2 Colourist / VFX agents) emitted as editable
    # Kdenlive effect-stack ``<filter>`` elements inside the clip's playlist entry.
    filters: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration_frames(self) -> int:
        return max(1, self.out_frame - self.in_frame + 1)

    @property
    def timeline_end_inclusive(self) -> int:
        return self.timeline_start + self.duration_frames - 1


@dataclass
class CompiledTimeline:
    project_id: str
    sequence_id: str
    profile_id: str = "podcast_multicam"
    display_name: str = "Bang On Pro Sequence"
    width: int = 1920
    height: int = 1080
    fps_num: int = 25
    fps_den: int = 1
    color_space: str = "bt709"
    media_registry: dict[str, dict[str, Any]] = field(default_factory=dict)
    tracks: dict[str, dict[str, Any]] = field(default_factory=dict)
    v1_clips: list[CompiledClip] = field(default_factory=list)
    v2_clips: list[CompiledClip] = field(default_factory=list)
    a1_clips: list[CompiledClip] = field(default_factory=list)
    a2_clips: list[CompiledClip] = field(default_factory=list)
    audio_filters: list[dict[str, Any]] = field(default_factory=list)
    captions: list[dict[str, Any]] = field(default_factory=list)
    ass_subtitle_path: str | None = None
    srt_subtitle_path: str | None = None
    markers: list[dict[str, Any]] = field(default_factory=list)
    unresolved_effects: list[dict[str, Any]] = field(default_factory=list)
    # V1.4: same-track dissolves (incoming clip on V1's b-lane + a native luma mix) and caption edits
    v1b_clips: list[CompiledClip] = field(default_factory=list)
    transitions: list[dict[str, Any]] = field(default_factory=list)
    captions_edited: bool = False

    @property
    def fps(self) -> float:
        return float(self.fps_num) / float(self.fps_den)

    @property
    def total_frames(self) -> int:
        max_end = 0
        for lst in (self.v1_clips, self.a1_clips, self.v2_clips, self.a2_clips, self.v1b_clips):
            for c in lst:
                max_end = max(max_end, c.timeline_end_inclusive + 1)
        return max(1, max_end)

    def track_lists(self) -> dict[str, list[CompiledClip]]:
        return {"V1": self.v1_clips, "V2": self.v2_clips, "A1": self.a1_clips, "A2": self.a2_clips, "V1b": self.v1b_clips}

    def find(self, clip_id: str | None) -> tuple[str, CompiledClip] | None:
        if not clip_id:
            return None
        for tid, lst in self.track_lists().items():
            for c in lst:
                if c.clip_id == clip_id:
                    return tid, c
        return None

    def media_frames(self, media_ref: str | None) -> int:
        meta = self.media_registry.get(media_ref or "") or {}
        if meta.get("is_image"):
            return 10**9
        return max(1, round(float(meta.get("duration_sec", 0.0) or 0.0) * self.fps)) if meta.get("duration_sec") else 10**9


def compile_command_batches(command_batches: list[dict[str, Any]], op_results: list[dict[str, Any]] | None = None) -> CompiledTimeline:
    """Validate and deterministically replay Timeline Command API v1 batches into a CompiledTimeline.

    V1.4: structural operations (``move_clip``, ``trim_clip``, ``split_clip``, ``delete_clip``,
    ``set_audio_level``, ``set_keyframe``, ``set_effect_param``, ``add_transition``,
    ``update_caption``, ``delete_caption``) are replayed too, so an agent's edit to a headless draft
    is a real change in the emitted project. Frame ranges here are inclusive (``out_frame`` is the
    last frame shown), as in every department batch. When ``op_results`` is given, one
    ``{"op_id", "type", "applied", "message"}`` entry per operation is appended (invalid structural
    operations are skipped and reported, never half-applied).
    """
    if not command_batches:
        raise ValueError("Cannot compile project from empty command_batches list")

    first = validate_timeline_command_batch(command_batches[0])
    compiled = CompiledTimeline(
        project_id=first["project_id"],
        sequence_id=first["sequence_id"],
    )

    for batch in command_batches:
        replay_batch(compiled, batch, op_results)
    for lst in compiled.track_lists().values():
        lst.sort(key=lambda c: c.timeline_start)
    return compiled


def replay_batch(compiled: CompiledTimeline, batch: dict[str, Any], op_results: list[dict[str, Any]] | None = None) -> CompiledTimeline:
    """Validate one batch and replay it onto ``compiled`` (incremental form of ``compile_command_batches``)."""
    validate_timeline_command_batch(batch)
    for op in batch["operations"]:
        op_type = op["type"]
        params = op.get("params", {}) or {}
        fr = op.get("frame_range", {}) or {}

        if op_type in _STRUCTURAL_OPS:
            ok, msg = _apply_structural_op(compiled, op_type, op, params, fr)
            if op_results is not None:
                op_results.append({"op_id": op["op_id"], "type": op_type, "applied": ok, "message": msg})
            continue
        if op_results is not None:
            op_results.append({"op_id": op["op_id"], "type": op_type, "applied": True, "message": ""})

        if op_type == "import_media":
            media_ref = op.get("media_ref") or f"media_{len(compiled.media_registry) + 1}"
            compiled.media_registry[media_ref] = dict(params)

        elif op_type == "create_sequence":
            compiled.profile_id = str(params.get("profile_id", compiled.profile_id))
            compiled.display_name = str(params.get("display_name", compiled.display_name))
            compiled.width = int(params.get("width", compiled.width))
            compiled.height = int(params.get("height", compiled.height))
            compiled.fps_num = int(params.get("fps_num", compiled.fps_num))
            compiled.fps_den = int(params.get("fps_den", compiled.fps_den))
            compiled.color_space = str(params.get("color_space", compiled.color_space))

        elif op_type == "create_track":
            track_id = op.get("track_id") or f"T{len(compiled.tracks) + 1}"
            compiled.tracks[track_id] = dict(params)

        elif op_type == "place_clip":
            track_id = op.get("track_id") or "V1"
            clip = CompiledClip(
                clip_id=op.get("clip_id") or op["op_id"],
                track_id=track_id,
                media_ref=op.get("media_ref"),
                in_frame=int(fr.get("in_frame", 0)),
                out_frame=int(fr.get("out_frame", 0)),
                timeline_start=int(fr.get("timeline_start", 0)),
                params=dict(params),
                confidence=float(op.get("confidence", 1.0)),
                rationale=str(op.get("rationale", "")),
            )
            if track_id == "V1":
                compiled.v1_clips.append(clip)
            elif track_id == "V2":
                compiled.v2_clips.append(clip)
            elif track_id == "A1":
                compiled.a1_clips.append(clip)
            elif track_id == "A2":
                compiled.a2_clips.append(clip)

        elif op_type in ("add_effect", "apply_mask"):
            target_id = op.get("clip_id")
            target = _find_compiled_clip(compiled, target_id) if target_id else None
            if target is not None:
                target.filters.extend(_effect_op_to_filters(op_type, params))
            else:
                compiled.unresolved_effects.append(dict(op))

        elif op_type == "add_lower_third":
            dur_f = max(
                1,
                int(
                    params.get(
                        "duration_frames",
                        max(1, int(fr.get("out_frame", 99)) - int(fr.get("in_frame", 0)) + 1),
                    )
                ),
            )
            title = str(params.get("speaker_name", params.get("title", "Speaker")))
            subtitle = str(params.get("subtitle", compiled.display_name))
            accent = str(params.get("accent_hex", "#FF5A1F"))
            position = str(params.get("position", "bottom_left"))
            xml_str = str(
                params.get("kdenlive_title_xml")
                or generate_kdenlive_lower_third_xml(
                    title=title,
                    subtitle=subtitle,
                    width=compiled.width,
                    height=compiled.height,
                    accent_hex=accent,
                    duration_frames=dur_f,
                    position=position,
                )
            )
            compiled.v2_clips.append(
                CompiledClip(
                    clip_id=op.get("clip_id") or op["op_id"],
                    track_id=op.get("track_id") or "V2",
                    media_ref=None,
                    in_frame=0,
                    out_frame=max(0, dur_f - 1),
                    timeline_start=int(fr.get("timeline_start", fr.get("in_frame", 0))),
                    params={
                        "clip_kind": "kdenlivetitle",
                        "title": title,
                        "subtitle": subtitle,
                        "accent_hex": accent,
                        "kdenlive_title_xml": xml_str,
                    },
                    confidence=float(op.get("confidence", 0.95)),
                    rationale=str(op.get("rationale", "")),
                )
            )

        elif op_type == "add_audio_filter":
            compiled.audio_filters.append(
                {
                    "track_id": op.get("track_id") or "A1",
                    "filter_id": params.get("filter_id") or op["op_id"],
                    "mlt_service": params.get("mlt_service", ""),
                    "mlt_params": dict(params.get("mlt_params", {})),
                    "ffmpeg_filter": params.get("ffmpeg_filter", ""),
                    "rationale": str(op.get("rationale", "")),
                }
            )

        elif op_type == "add_caption":
            if params.get("text") and (op.get("clip_id") or params.get("caption_id")):
                compiled.captions_edited = True  # an agent's own caption, not the Captions Agent's file
            compiled.captions.append(
                {
                    "caption_style": params.get("caption_style"),
                    "caption_id": str(op.get("clip_id") or params.get("caption_id") or f"cap_{len(compiled.captions) + 1:04d}"),
                    "in_frame": int(fr.get("in_frame", 0)),
                    "out_frame": int(fr.get("out_frame", 0)),
                    "speaker_id": params.get("speaker_id", ""),
                    "text": params.get("text", ""),
                }
            )
            if params.get("ass_path"):
                compiled.ass_subtitle_path = str(params["ass_path"])
            if params.get("srt_path"):
                compiled.srt_subtitle_path = str(params["srt_path"])

        elif op_type == "add_marker":
            if op.get("track_id") in ("V1", None):
                compiled.markers.append(
                    {
                        "frame": int(fr.get("timeline_start", fr.get("in_frame", 0))),
                        "label": str(params.get("label", op.get("rationale", "Marker"))),
                        "category": str(params.get("category", "review")),
                        "confidence": float(op.get("confidence", 1.0)),
                        "rationale": str(op.get("rationale", "")),
                    }
                )
    for lst in compiled.track_lists().values():
        lst.sort(key=lambda c: c.timeline_start)
    return compiled


_STRUCTURAL_OPS = frozenset(
    {"move_clip", "trim_clip", "split_clip", "delete_clip", "set_audio_level", "set_keyframe", "set_effect_param", "add_transition", "update_caption", "delete_caption"}
)


def _overlaps(compiled: CompiledTimeline, track_id: str, start: int, end_incl: int, ignore: set[str]) -> str | None:
    for c in compiled.track_lists().get(track_id, []):
        if c.clip_id in ignore:
            continue
        if start <= c.timeline_end_inclusive and end_incl >= c.timeline_start:
            return c.clip_id
    return None


def _target_filter(clip: CompiledClip, params: dict[str, Any]) -> dict[str, Any] | None:
    if "filter_index" in params:
        idx = int(params["filter_index"])
        return clip.filters[idx] if 0 <= idx < len(clip.filters) else None
    want = str(params.get("kdenlive_id") or params.get("effect_id") or params.get("mlt_service") or "")
    for flt in reversed(clip.filters):
        if want and want in (flt.get("kdenlive_id"), flt.get("mlt_service")):
            return flt
    return None


def _keyframe_string(keyframes: list[dict[str, Any]]) -> str:
    """MLT animation syntax: ``frame=value`` (linear), ``frame~=value`` (smooth), ``frame|=value`` (hold)."""
    sep = {"linear": "=", "smooth": "~=", "hold": "|=", "discrete": "|="}
    parts = []
    for kf in sorted(keyframes, key=lambda k: int(k["frame"])):
        parts.append(f"{int(kf['frame'])}{sep.get(str(kf.get('interpolation', 'linear')), '=')}{kf['value']}")
    return ";".join(parts)


def _apply_structural_op(compiled: CompiledTimeline, op_type: str, op: dict[str, Any], params: dict[str, Any], fr: dict[str, Any]) -> tuple[bool, str]:
    """Apply one structural edit to the compiled draft; returns ``(applied, message)``. Never half-applies."""
    if op_type in ("update_caption", "delete_caption"):
        cid = str(op.get("clip_id") or params.get("caption_id") or "")
        idx = next((i for i, c in enumerate(compiled.captions) if c.get("caption_id") == cid), None)
        if idx is None:
            return False, f"Caption '{cid}' does not exist"
        compiled.captions_edited = True
        if op_type == "delete_caption":
            compiled.captions.pop(idx)
            return True, f"Deleted caption '{cid}'"
        cap = compiled.captions[idx]
        new_in, new_out = int(fr.get("in_frame", cap["in_frame"])), int(fr.get("out_frame", cap["out_frame"]))
        if new_out < new_in:
            return False, "Caption out_frame precedes in_frame"
        cap.update({"in_frame": new_in, "out_frame": new_out})
        if "text" in params:
            cap["text"] = str(params["text"])
        if "style" in params:
            cap["style"] = dict(params["style"])
        return True, f"Updated caption '{cid}'"

    found = compiled.find(op.get("clip_id"))
    if found is None:
        return False, f"Clip '{op.get('clip_id')}' does not exist"
    track_id, clip = found
    lists = compiled.track_lists()

    if op_type == "move_clip":
        target = str(op.get("track_id") or track_id)
        if target not in lists or (target == "V1b" and track_id != "V1b"):
            return False, f"Target track '{target}' does not exist"
        start = int(params.get("timeline_start", fr.get("timeline_start", clip.timeline_start)))
        if start < 0:
            return False, "timeline_start must be non-negative"
        end = start + clip.duration_frames - 1
        hit = None if params.get("allow_overlap") else _overlaps(compiled, target, start, end, {clip.clip_id})
        if hit:
            return False, f"Move destination overlaps clip '{hit}' on track '{target}'"
        lists[track_id].remove(clip)
        for trn in compiled.transitions:  # a dissolve travels with its incoming clip
            if trn.get("clip_id") == clip.clip_id:
                trn["in_frame"] += start - clip.timeline_start
                trn["out_frame"] += start - clip.timeline_start
        clip.track_id, clip.timeline_start = target, start
        lists[target].append(clip)
        lists[target].sort(key=lambda c: c.timeline_start)
        return True, f"Moved clip '{clip.clip_id}' to {target} frame {start}"

    if op_type == "trim_clip":
        new_in = int(params.get("in_frame", fr.get("in_frame", clip.in_frame)))
        new_out = int(params.get("out_frame", fr.get("out_frame", clip.out_frame)))
        if "duration_frames" in params:
            new_out = new_in + int(params["duration_frames"]) - 1
        start = int(params.get("timeline_start", fr.get("timeline_start", clip.timeline_start)))
        if new_in < 0 or new_out < new_in or start < 0:
            return False, f"trim_clip requires 0 <= in_frame <= out_frame (got in={new_in}, out={new_out})"
        if new_out >= compiled.media_frames(clip.media_ref) and not params.get("allow_extend"):
            return False, f"Trim out_frame {new_out} exceeds the media length of '{clip.media_ref}'"
        hit = _overlaps(compiled, track_id, start, start + (new_out - new_in), {clip.clip_id})
        if hit:
            return False, f"Trim would overlap clip '{hit}' on {track_id}"
        clip.in_frame, clip.out_frame, clip.timeline_start = new_in, new_out, start
        return True, f"Trimmed clip '{clip.clip_id}' to source [{new_in}, {new_out}] at {start}"

    if op_type == "split_clip":
        split = int(params.get("split_frame", params.get("timeline_frame", fr.get("timeline_start", -1))))
        if not clip.timeline_start < split <= clip.timeline_end_inclusive:
            return False, f"Split frame {split} is outside clip '{clip.clip_id}' ({clip.timeline_start}, {clip.timeline_end_inclusive}]"
        right_id = str(params.get("new_clip_id") or f"{clip.clip_id}_r{split}")
        if compiled.find(right_id) is not None:
            return False, f"Clip id '{right_id}' already exists"
        left_len = split - clip.timeline_start
        right = CompiledClip(
            clip_id=right_id, track_id=clip.track_id, media_ref=clip.media_ref, in_frame=clip.in_frame + left_len, out_frame=clip.out_frame,
            timeline_start=split, params=dict(clip.params), confidence=clip.confidence, rationale=clip.rationale, filters=[dict(f, properties=dict(f.get("properties") or {})) for f in clip.filters],
        )
        clip.out_frame = clip.in_frame + left_len - 1
        lists[track_id].append(right)
        lists[track_id].sort(key=lambda c: c.timeline_start)
        return True, f"Split '{clip.clip_id}' at {split}; right part '{right_id}'"

    if op_type == "delete_clip":
        lists[track_id].remove(clip)
        compiled.transitions = [t for t in compiled.transitions if t.get("clip_id") != clip.clip_id]
        return True, f"Deleted clip '{clip.clip_id}'"

    if op_type == "set_audio_level":
        kfs = params.get("keyframes")
        level: Any = _keyframe_string([{"frame": k["frame"], "value": float(k["gain_db"]), "interpolation": k.get("interpolation", "linear")} for k in kfs]) if kfs else float(params.get("gain_db", 0.0))
        existing = next((f for f in clip.filters if f.get("kdenlive_id") == "volume" and f.get("mlt_service") == "volume"), None)
        if existing is None:
            clip.filters.append({"mlt_service": "volume", "kdenlive_id": "volume", "properties": {"level": level}, "agent_id": params.get("agent_id")})
        else:
            existing["properties"]["level"] = level
        return True, f"Audio level of '{clip.clip_id}' set to {level} dB"

    if op_type in ("set_effect_param", "set_keyframe"):
        flt = _target_filter(clip, params)
        if flt is None:
            return False, f"No matching effect on '{clip.clip_id}' (give kdenlive_id or filter_index)"
        prop = str(params.get("property") or params.get("param") or "")
        if not prop:
            return False, "property is required"
        if op_type == "set_effect_param":
            flt.setdefault("properties", {})[prop] = params.get("value")
        else:
            kfs = params.get("keyframes") or []
            if not kfs or any(int(k.get("frame", -1)) < 0 or int(k["frame"]) >= clip.duration_frames for k in kfs):
                return False, f"keyframes must be clip-relative frames inside 0..{clip.duration_frames - 1}"
            flt.setdefault("properties", {})[prop] = _keyframe_string(kfs)
        return True, f"Set {prop} on '{clip.clip_id}' ({flt.get('kdenlive_id')})"

    if op_type == "add_transition":
        kind = str(params.get("kind") or params.get("transition_id") or "dissolve")
        d = int(params.get("duration_frames", 12))
        if d < 1 or d >= clip.duration_frames:
            return False, f"Transition length {d} must be shorter than the clip ({clip.duration_frames} frames)"
        video = track_id in ("V1", "V2", "V1b")
        fade = {"fade_in": ("fade_from_black", "fadein", f"0=0;{d}=1", f"0=-60;{d}=0"),
                "fade_out": ("fade_to_black", "fadeout", f"{clip.duration_frames - 1 - d}=1;{clip.duration_frames - 1}=0", f"{clip.duration_frames - 1 - d}=0;{clip.duration_frames - 1}=-60")}
        if kind in fade:
            vid_id, aud_id, vkf, akf = fade[kind]
            if video:
                clip.filters.append({"mlt_service": "brightness", "kdenlive_id": vid_id, "properties": {"level": vkf, "alpha": 1}, "agent_id": params.get("agent_id")})
            else:
                clip.filters.append({"mlt_service": "volume", "kdenlive_id": aud_id, "properties": {"level": akf}, "agent_id": params.get("agent_id")})
            return True, f"{kind} of {d} frames on '{clip.clip_id}'"
        if kind == "dissolve":
            if track_id != "V1":
                return False, "Dissolves are made between adjacent clips on V1"
            prev = next((c for c in compiled.v1_clips if c.timeline_end_inclusive + 1 == clip.timeline_start), None)
            if prev is None:
                return False, f"'{clip.clip_id}' has no clip ending where it starts on V1"
            if clip.in_frame < d or prev.duration_frames <= d:
                return False, f"Not enough handle before '{clip.clip_id}' for a {d}-frame dissolve"
            lists["V1"].remove(clip)
            cut = clip.timeline_start
            clip.in_frame -= d
            clip.timeline_start = cut - d
            clip.track_id = "V1b"
            lists["V1b"].append(clip)
            compiled.transitions.append({"kind": "dissolve", "clip_id": clip.clip_id, "in_frame": cut - d, "out_frame": cut - 1, "agent_id": params.get("agent_id")})
            return True, f"Dissolve of {d} frames into '{clip.clip_id}'"
        return False, f"Unknown transition kind '{kind}' (dissolve, fade_in, fade_out)"
    return False, f"Unsupported operation '{op_type}'"


def kdenlive_file_hash(path: str | Path) -> tuple[str, int] | None:
    """Replicate Kdenlive ``ProjectClip::calculateHash``: MD5 of the whole file when <= 2 MB,
    otherwise MD5 of the first 1 MB + last 1 MB; returns ``(hex digest, file size)``."""
    try:
        p = Path(path)
        size = p.stat().st_size
        md5 = hashlib.md5()  # noqa: S324 - identity hash mandated by Kdenlive's project format, not security
        with p.open("rb") as fh:
            if size > 2_000_000:
                md5.update(fh.read(1_000_000))
                fh.seek(size - 1_000_000)
                md5.update(fh.read())
            else:
                md5.update(fh.read())
        return md5.hexdigest(), size
    except OSError:
        return None


def _find_compiled_clip(compiled: CompiledTimeline, clip_id: str) -> CompiledClip | None:
    for lst in (compiled.v1_clips, compiled.v2_clips, compiled.a1_clips, compiled.a2_clips):
        for clip in lst:
            if clip.clip_id == clip_id:
                return clip
    return None


MATTE_THRESHOLD = 50  # MLT shape threshold (percent luma) for binary subject mattes


def _effect_op_to_filters(op_type: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate ``add_effect`` / ``apply_mask`` params into native MLT filter specs.

    ``add_effect`` params: ``mlt_service``, optional ``kdenlive_id`` and ``properties``.
    ``apply_mask`` with a ``matte_path`` becomes Kdenlive's native mask workflow:
    ``mask_start`` (shape matte) -> optional masked effect -> ``mask_apply``.
    """
    if op_type == "add_effect":
        # Accept both the native C++ bridge keys (effect_id / parameters) and MLT keys.
        service = str(params.get("mlt_service") or params.get("effect_id") or "")
        if not service:
            return []
        return [
            {
                "mlt_service": service,
                "kdenlive_id": str(params.get("kdenlive_id") or params.get("effect_id") or service),
                "properties": dict(params.get("properties") or params.get("parameters") or {}),
                "agent_id": params.get("agent_id"),
            }
        ]
    matte = params.get("matte_path")
    if not matte:
        return []
    masked = params.get("masked_effect") or {}
    softness = round(min(1.0, max(0.0, float(params.get("feather", 0.0)) / 100.0)), 3)
    # Mattes are white on the subject. MLT's luma shape makes luma above the threshold transparent, so the
    # threshold is the midpoint (100 keeps everything) and the selection is the white region unless the
    # operation is inverted ("outside the subject", e.g. blur the background). Rendered and pixel-checked.
    mlt_invert = 0 if params.get("invert") else 1
    if not masked.get("mlt_service"):
        # No effect to limit: the matte isolates the subject (alpha), the same native "shape" filter the
        # C++ Timeline Command bridge applies for this operation.
        return [
            {
                "mlt_service": "shape",
                "kdenlive_id": "shape",
                "properties": {"resource": str(matte), "mix": MATTE_THRESHOLD, "softness": softness, "invert": mlt_invert, "use_luminance": 1},
                "agent_id": params.get("agent_id"),
            }
        ]
    filters: list[dict[str, Any]] = [
        {
            "mlt_service": "mask_start",
            "kdenlive_id": "mask_start-shape",
            "properties": {
                "filter": "shape",
                "filter.resource": str(matte),
                "filter.mix": MATTE_THRESHOLD,
                "filter.softness": softness,
                "filter.invert": mlt_invert,
                "filter.use_luminance": 1,
            },
            "agent_id": params.get("agent_id"),
        }
    ]
    if masked.get("mlt_service"):
        filters.append(
            {
                "mlt_service": str(masked["mlt_service"]),
                "kdenlive_id": str(masked.get("kdenlive_id") or masked["mlt_service"]),
                "properties": dict(masked.get("properties") or {}),
                "agent_id": params.get("agent_id"),
            }
        )
    filters.append(
        {
            "mlt_service": "mask_apply",
            "kdenlive_id": "mask_apply",
            "properties": {"transition": "frei0r.composition"},
            "agent_id": params.get("agent_id"),
        }
    )
    return filters


def _add_prop(parent: ET.Element, name: str, value: Any) -> ET.Element:
    prop = ET.SubElement(parent, "property", {"name": name})
    prop.text = str(value)
    return prop


def _populate_playlist_with_clips(
    playlist: ET.Element,
    clips: list[CompiledClip],
    producer_map: dict[str, str],
    kdenlive_id_map: dict[str, int],
) -> None:
    """Emit `<blank>` spacers and `<entry>` items into an MLT `<playlist>`."""
    cursor = 0
    for clip in clips:
        if clip.timeline_start > cursor:
            gap_frames = clip.timeline_start - cursor
            ET.SubElement(playlist, "blank", {"length": str(gap_frames)})
            cursor = clip.timeline_start

        if clip.params.get("clip_kind") == "kdenlivetitle":
            prod_id = producer_map[clip.clip_id]
            k_id = kdenlive_id_map[clip.clip_id]
        else:
            if not clip.media_ref or clip.media_ref not in producer_map:
                continue
            prod_id = producer_map[clip.media_ref]
            k_id = kdenlive_id_map[clip.media_ref]

        entry = ET.SubElement(
            playlist,
            "entry",
            {
                "producer": prod_id,
                "in": str(clip.in_frame),
                "out": str(clip.out_frame),
            },
        )
        _add_prop(entry, "kdenlive:id", k_id)
        for f_idx, flt in enumerate(clip.filters):
            f_el = ET.SubElement(
                entry,
                "filter",
                {"id": f"{clip.clip_id}_fx{f_idx}", "in": str(clip.in_frame), "out": str(clip.out_frame)},
            )
            _add_prop(f_el, "mlt_service", flt["mlt_service"])
            _add_prop(f_el, "kdenlive_id", flt.get("kdenlive_id") or flt["mlt_service"])
            for p_name, p_val in (flt.get("properties") or {}).items():
                _add_prop(f_el, str(p_name), p_val)
            if flt.get("agent_id"):
                _add_prop(f_el, "kdenlive:bangonpro_agent", flt["agent_id"])
        cursor = clip.timeline_start + clip.duration_frames


def emit_kdenlive_xml(
    compiled: CompiledTimeline,
    output_path: str | Path,
) -> Path:
    """Emit a real Kdenlive 24.08 / MLT 7.22 ``.kdenlive`` XML project file."""
    out_path = Path(output_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    total_frames = compiled.total_frames
    last_frame = max(0, total_frames - 1)
    seq_uuid = f"{{{uuid.uuid5(uuid.NAMESPACE_URL, f'{compiled.project_id}/{compiled.sequence_id}')}}}"

    dar = Fraction(compiled.width, compiled.height).limit_denominator(100)
    colorspace_code = "709" if "709" in compiled.color_space else "601"

    mlt = ET.Element(
        "mlt",
        {
            "LC_NUMERIC": "C",
            "producer": "main_bin",
            "version": "7.22.0",
            "root": str(out_path.parent),
        },
    )

    ET.SubElement(
        mlt,
        "profile",
        {
            "description": f"Bang On Pro {compiled.width}x{compiled.height} {compiled.fps_num}/{compiled.fps_den} fps",
            "width": str(compiled.width),
            "height": str(compiled.height),
            "progressive": "1",
            "sample_aspect_num": "1",
            "sample_aspect_den": "1",
            "display_aspect_num": str(dar.numerator),
            "display_aspect_den": str(dar.denominator),
            "frame_rate_num": str(compiled.fps_num),
            "frame_rate_den": str(compiled.fps_den),
            "colorspace": colorspace_code,
        },
    )

    # 1. Media chains (separate bin, audio-track, and video-track chains as Kdenlive does)
    bin_chain_map: dict[str, str] = {}
    audio_chain_map: dict[str, str] = {}
    video_chain_map: dict[str, str] = {}
    kdenlive_id_map: dict[str, int] = {}

    next_k_id = 2
    for idx, (media_ref, meta) in enumerate(compiled.media_registry.items()):
        file_path = str(meta["file_path"])
        dur_frames = max(1, round(float(meta.get("duration_sec", 1.0)) * compiled.fps))
        out_f = max(0, dur_frames - 1)
        k_id = next_k_id
        next_k_id += 1
        kdenlive_id_map[media_ref] = k_id
        is_audio_only = bool(meta.get("is_audio_only") or meta.get("video_codec") is None)
        if meta.get("is_image"):
            # V1.4: a supplied still (logo, score card, headline image) as Kdenlive's native image clip.
            img_id = f"image_{idx}"
            prod = ET.SubElement(mlt, "producer", {"id": img_id, "in": "0", "out": str(out_f)})
            _add_prop(prod, "length", dur_frames)
            _add_prop(prod, "eof", "pause")
            _add_prop(prod, "resource", file_path)
            _add_prop(prod, "ttl", 25)
            _add_prop(prod, "mlt_service", "qimage")
            _add_prop(prod, "kdenlive:id", k_id)
            _add_prop(prod, "kdenlive:clipname", meta.get("name") or Path(file_path).name)
            _add_prop(prod, "kdenlive:clip_type", 5)
            _add_prop(prod, "kdenlive:folderid", -1)
            bin_chain_map[media_ref] = img_id
            video_chain_map[media_ref] = img_id
            continue

        chain_specs: list[tuple[str, dict[str, str], str]] = [
            ("bin", bin_chain_map, f"chain_bin_{idx}"),
            ("audio", audio_chain_map, f"chain_a_{idx}"),
        ]
        if not is_audio_only:
            chain_specs.append(("video", video_chain_map, f"chain_v_{idx}"))

        for kind, target_map, suffix in chain_specs:
            target_map[media_ref] = suffix
            chain = ET.SubElement(mlt, "chain", {"id": suffix, "out": str(out_f)})
            _add_prop(chain, "length", dur_frames)
            _add_prop(chain, "eof", "pause")
            _add_prop(chain, "resource", file_path)
            _add_prop(chain, "mlt_service", "avformat")
            _add_prop(chain, "kdenlive:id", k_id)
            _add_prop(chain, "kdenlive:clipname", meta.get("camera_id", media_ref))
            _add_prop(chain, "kdenlive:folderid", -1)
            _add_prop(chain, "kdenlive:clip_type", 1 if is_audio_only else 0)
            k_hash = kdenlive_file_hash(file_path)
            if k_hash is not None:
                # Kdenlive's own clip identity (ProjectClip::calculateHash); a SHA-256 here made Kdenlive
                # flag every generated clip as "changed - reload" when the project was opened (V1.2 fix).
                _add_prop(chain, "kdenlive:file_hash", k_hash[0])
                _add_prop(chain, "kdenlive:file_size", k_hash[1])
            if meta.get("sha256"):
                _add_prop(chain, "kdenlive:bangonpro_sha256", meta["sha256"])
            if kind == "audio":
                _add_prop(chain, "video_index", -1)
                _add_prop(chain, "set.test_audio", 0)
                _add_prop(chain, "set.test_image", 1)
            elif kind == "video":
                _add_prop(chain, "audio_index", -1)
                _add_prop(chain, "set.test_audio", 1)
                _add_prop(chain, "set.test_image", 0)

    # 2. Lower-third kdenlivetitle producers for V2
    title_prod_map: dict[str, str] = {}
    for idx, clip in enumerate(compiled.v2_clips):
        if clip.params.get("clip_kind") != "kdenlivetitle":
            continue
        prod_id = f"title_prod_{idx}"
        title_prod_map[clip.clip_id] = prod_id
        k_id = next_k_id
        next_k_id += 1
        kdenlive_id_map[clip.clip_id] = k_id

        prod = ET.SubElement(
            mlt,
            "producer",
            {"id": prod_id, "in": "0", "out": str(clip.out_frame)},
        )
        _add_prop(prod, "length", clip.duration_frames)
        _add_prop(prod, "eof", "pause")
        _add_prop(prod, "resource", "")
        _add_prop(prod, "mlt_service", "kdenlivetitle")
        _add_prop(prod, "xmldata", clip.params.get("kdenlive_title_xml", ""))
        _add_prop(prod, "kdenlive:id", k_id)
        _add_prop(prod, "kdenlive:clipname", f"Lower Third: {clip.params.get('title', clip.clip_id)}")
        _add_prop(prod, "kdenlive:clip_type", 2)
        _add_prop(prod, "kdenlive:folderid", -1)
        _add_prop(prod, "set.test_audio", 1)
        _add_prop(prod, "set.test_image", 0)

    # 3. Black background track producer
    black_prod = ET.SubElement(
        mlt, "producer", {"id": "black_track", "in": "0", "out": str(last_frame)}
    )
    _add_prop(black_prod, "length", total_frames)
    _add_prop(black_prod, "eof", "continue")
    _add_prop(black_prod, "resource", "black")
    _add_prop(black_prod, "aspect_ratio", 1)
    _add_prop(black_prod, "mlt_service", "color")
    _add_prop(black_prod, "kdenlive:playlistid", "black_track")
    _add_prop(black_prod, "mlt_image_format", "rgba")
    _add_prop(black_prod, "set.test_audio", 0)

    # 4. Audio Track A2 (Music Bed)
    pl_a2 = ET.SubElement(mlt, "playlist", {"id": "playlist_a2"})
    _add_prop(pl_a2, "kdenlive:audio_track", 1)
    _populate_playlist_with_clips(pl_a2, compiled.a2_clips, audio_chain_map, kdenlive_id_map)
    pl_a2_b = ET.SubElement(mlt, "playlist", {"id": "playlist_a2_b"})
    _add_prop(pl_a2_b, "kdenlive:audio_track", 1)

    tr_a2 = ET.SubElement(mlt, "tractor", {"id": "tractor_a2", "in": "0", "out": str(last_frame)})
    _add_prop(tr_a2, "kdenlive:audio_track", 1)
    _add_prop(tr_a2, "kdenlive:track_name", "Music Bed A2")
    _add_prop(tr_a2, "kdenlive:trackheight", 67)
    _add_prop(tr_a2, "kdenlive:timeline_active", 1)
    ET.SubElement(tr_a2, "track", {"hide": "video", "producer": "playlist_a2"})
    ET.SubElement(tr_a2, "track", {"hide": "video", "producer": "playlist_a2_b"})
    for flt in compiled.audio_filters:
        if flt["track_id"] == "A2" and flt["mlt_service"]:
            f_el = ET.SubElement(tr_a2, "filter", {"id": flt["filter_id"]})
            _add_prop(f_el, "mlt_service", flt["mlt_service"])
            _add_prop(f_el, "kdenlive_id", flt["mlt_service"])
            for k, v in flt["mlt_params"].items():
                _add_prop(f_el, k, v)

    # 5. Audio Track A1 (Dialogue Master + Restoration Chain)
    pl_a1 = ET.SubElement(mlt, "playlist", {"id": "playlist_a1"})
    _add_prop(pl_a1, "kdenlive:audio_track", 1)
    _populate_playlist_with_clips(pl_a1, compiled.a1_clips, audio_chain_map, kdenlive_id_map)
    pl_a1_b = ET.SubElement(mlt, "playlist", {"id": "playlist_a1_b"})
    _add_prop(pl_a1_b, "kdenlive:audio_track", 1)

    tr_a1 = ET.SubElement(mlt, "tractor", {"id": "tractor_a1", "in": "0", "out": str(last_frame)})
    _add_prop(tr_a1, "kdenlive:audio_track", 1)
    _add_prop(tr_a1, "kdenlive:track_name", "Dialogue Master A1")
    _add_prop(tr_a1, "kdenlive:trackheight", 67)
    _add_prop(tr_a1, "kdenlive:timeline_active", 1)
    ET.SubElement(tr_a1, "track", {"hide": "video", "producer": "playlist_a1"})
    ET.SubElement(tr_a1, "track", {"hide": "video", "producer": "playlist_a1_b"})
    for flt in compiled.audio_filters:
        if flt["track_id"] == "A1" and flt["mlt_service"]:
            f_el = ET.SubElement(tr_a1, "filter", {"id": flt["filter_id"]})
            _add_prop(f_el, "mlt_service", flt["mlt_service"])
            _add_prop(f_el, "kdenlive_id", flt["mlt_service"])
            for k, v in flt["mlt_params"].items():
                _add_prop(f_el, k, v)

    # 6. Video Track V1 (Multicam Program)
    pl_v1 = ET.SubElement(mlt, "playlist", {"id": "playlist_v1"})
    _populate_playlist_with_clips(pl_v1, compiled.v1_clips, video_chain_map, kdenlive_id_map)
    pl_v1_b = ET.SubElement(mlt, "playlist", {"id": "playlist_v1_b"})
    _populate_playlist_with_clips(pl_v1_b, compiled.v1b_clips, video_chain_map, kdenlive_id_map)

    tr_v1 = ET.SubElement(mlt, "tractor", {"id": "tractor_v1", "in": "0", "out": str(last_frame)})
    _add_prop(tr_v1, "kdenlive:track_name", "Multicam Program V1")
    _add_prop(tr_v1, "kdenlive:trackheight", 67)
    _add_prop(tr_v1, "kdenlive:timeline_active", 1)
    ET.SubElement(tr_v1, "track", {"hide": "audio", "producer": "playlist_v1"})
    ET.SubElement(tr_v1, "track", {"hide": "audio", "producer": "playlist_v1_b"})
    for t_idx, trn in enumerate(t for t in compiled.transitions if t.get("kind") == "dissolve"):
        # Kdenlive same-track mix: the incoming clip sits on the track's second playlist and a luma
        # transition dissolves into it; after the mix the upper playlist simply shows that clip.
        mix = ET.SubElement(tr_v1, "transition", {"id": f"mix_v1_{t_idx}", "in": str(trn["in_frame"]), "out": str(trn["out_frame"])})
        _add_prop(mix, "a_track", 0)
        _add_prop(mix, "b_track", 1)
        _add_prop(mix, "mlt_service", "luma")
        _add_prop(mix, "kdenlive_id", "luma")
        _add_prop(mix, "kdenlive:mixcut", trn["out_frame"] - trn["in_frame"] + 1)
        if trn.get("agent_id"):
            _add_prop(mix, "kdenlive:bangonpro_agent", trn["agent_id"])

    # 7. Video Track V2 (Graphics & Lower Thirds)
    pl_v2 = ET.SubElement(mlt, "playlist", {"id": "playlist_v2"})
    # V1.4: V2 carries supplied B-roll and still graphics as well as generated titles.
    _populate_playlist_with_clips(pl_v2, compiled.v2_clips, {**video_chain_map, **title_prod_map}, kdenlive_id_map)
    ET.SubElement(mlt, "playlist", {"id": "playlist_v2_b"})

    tr_v2 = ET.SubElement(mlt, "tractor", {"id": "tractor_v2", "in": "0", "out": str(last_frame)})
    _add_prop(tr_v2, "kdenlive:track_name", "Graphics & Lower Thirds V2")
    _add_prop(tr_v2, "kdenlive:trackheight", 67)
    _add_prop(tr_v2, "kdenlive:timeline_active", 1)
    ET.SubElement(tr_v2, "track", {"hide": "audio", "producer": "playlist_v2"})
    ET.SubElement(tr_v2, "track", {"hide": "audio", "producer": "playlist_v2_b"})

    # 8. Main Sequence Tractor (compositing + audio mix + guides + subtitles)
    seq_tr = ET.SubElement(
        mlt, "tractor", {"id": "sequence_tractor", "in": "0", "out": str(last_frame)}
    )
    _add_prop(seq_tr, "kdenlive:uuid", seq_uuid)
    _add_prop(seq_tr, "kdenlive:clipname", compiled.display_name)
    _add_prop(seq_tr, "kdenlive:duration", total_frames)
    _add_prop(seq_tr, "kdenlive:maxduration", total_frames)
    _add_prop(seq_tr, "kdenlive:clip_type", 0)
    _add_prop(seq_tr, "kdenlive:id", next_k_id)
    _add_prop(seq_tr, "kdenlive:sequenceproperties.activeTrack", 2)
    _add_prop(seq_tr, "kdenlive:sequenceproperties.audioTarget", 1)
    _add_prop(seq_tr, "kdenlive:sequenceproperties.videoTarget", 2)
    _add_prop(seq_tr, "kdenlive:sequenceproperties.tracks", 4)
    _add_prop(seq_tr, "kdenlive:sequenceproperties.tracksCount", 4)
    _add_prop(seq_tr, "kdenlive:sequenceproperties.hasAudio", 1)
    _add_prop(seq_tr, "kdenlive:sequenceproperties.hasVideo", 1)

    guides_json = [
        {
            "comment": m["label"],
            "pos": int(m["frame"]),
            "type": _GUIDE_CATEGORY_MAP.get(m["category"], 5),
        }
        for m in compiled.markers
    ]
    _add_prop(seq_tr, "kdenlive:sequenceproperties.guides", json.dumps(guides_json, indent=2))

    # Track stack: 0=black_track, 1=A2, 2=A1, 3=V1, 4=V2
    ET.SubElement(seq_tr, "track", {"producer": "black_track"})
    ET.SubElement(seq_tr, "track", {"producer": "tractor_a2"})
    ET.SubElement(seq_tr, "track", {"producer": "tractor_a1"})
    ET.SubElement(seq_tr, "track", {"producer": "tractor_v1"})
    ET.SubElement(seq_tr, "track", {"producer": "tractor_v2"})

    for idx, b_track in enumerate((1, 2)):
        tr = ET.SubElement(seq_tr, "transition", {"id": f"transition_mix_{idx}"})
        _add_prop(tr, "a_track", 0)
        _add_prop(tr, "b_track", b_track)
        _add_prop(tr, "mlt_service", "mix")
        _add_prop(tr, "kdenlive_id", "mix")
        _add_prop(tr, "internal_added", 237)
        _add_prop(tr, "always_active", 1)
        _add_prop(tr, "accepts_blanks", 1)
        _add_prop(tr, "sum", 1)

    for idx, b_track in enumerate((3, 4)):
        tr = ET.SubElement(seq_tr, "transition", {"id": f"transition_qtblend_{idx}"})
        _add_prop(tr, "a_track", 0)
        _add_prop(tr, "b_track", b_track)
        _add_prop(tr, "compositing", 0)
        _add_prop(tr, "distort", 0)
        _add_prop(tr, "rotate_center", 0)
        _add_prop(tr, "mlt_service", "qtblend")
        _add_prop(tr, "internal_added", 237)
        _add_prop(tr, "always_active", 1)

    if compiled.ass_subtitle_path and Path(compiled.ass_subtitle_path).is_file():
        sub_flt = ET.SubElement(seq_tr, "filter", {"id": "filter_subtitles"})
        _add_prop(sub_flt, "mlt_service", "avfilter.subtitles")
        _add_prop(sub_flt, "av.filename", str(Path(compiled.ass_subtitle_path).resolve()))
        _add_prop(sub_flt, "av.alpha", 1)
        _add_prop(sub_flt, "internal_added", 237)

    # 9. Kdenlive main_bin playlist
    main_bin = ET.SubElement(mlt, "playlist", {"id": "main_bin"})
    _add_prop(main_bin, "kdenlive:docproperties.activetimeline", seq_uuid)
    _add_prop(main_bin, "kdenlive:docproperties.opensequences", seq_uuid)
    _add_prop(main_bin, "kdenlive:docproperties.uuid", seq_uuid)
    _add_prop(main_bin, "kdenlive:docproperties.audioChannels", 2)
    _add_prop(main_bin, "kdenlive:docproperties.compositing", 1)
    _add_prop(main_bin, "kdenlive:docproperties.documentid", compiled.project_id)
    _add_prop(main_bin, "kdenlive:docproperties.kdenliveversion", "24.08.0")
    _add_prop(main_bin, "kdenlive:docproperties.version", "1.1")
    _add_prop(main_bin, "kdenlive:docproperties.bangonpro_profile", compiled.profile_id)
    _add_prop(
        main_bin,
        "kdenlive:docproperties.guidesCategories",
        json.dumps(_KDENLIVE_GUIDE_CATEGORIES, indent=2),
    )
    _add_prop(main_bin, "xml_retain", 1)

    for media_ref, chain_id in bin_chain_map.items():
        dur_f = max(
            1,
            round(float(compiled.media_registry[media_ref].get("duration_sec", 1.0)) * compiled.fps),
        )
        ET.SubElement(
            main_bin,
            "entry",
            {"producer": chain_id, "in": "0", "out": str(max(0, dur_f - 1))},
        )
    for clip in compiled.v2_clips:
        if clip.clip_id in title_prod_map:
            ET.SubElement(
                main_bin,
                "entry",
                {
                    "producer": title_prod_map[clip.clip_id],
                    "in": "0",
                    "out": str(clip.out_frame),
                },
            )
    ET.SubElement(
        main_bin,
        "entry",
        {"producer": "sequence_tractor", "in": "0", "out": str(last_frame)},
    )

    # 10. Top-level Kdenlive Project Tractor (executed directly by `melt`)
    proj_tr = ET.SubElement(
        mlt, "tractor", {"id": "project_tractor", "in": "0", "out": str(last_frame)}
    )
    _add_prop(proj_tr, "kdenlive:projectTractor", 1)
    ET.SubElement(
        proj_tr,
        "track",
        {"producer": "sequence_tractor", "in": "0", "out": str(last_frame)},
    )

    ET.indent(mlt, space=" ")
    tree = ET.ElementTree(mlt)
    tree.write(str(out_path), encoding="utf-8", xml_declaration=True)
    return out_path


def emit_otio_timeline(
    compiled: CompiledTimeline,
    output_path: str | Path,
) -> Path:
    """Emit a standards-compliant OpenTimelineIO ``.otio`` timeline file."""
    out_path = Path(output_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fps = compiled.fps
    timeline = otio.schema.Timeline(
        name=compiled.display_name,
        global_start_time=otio.opentime.RationalTime(0, fps),
        metadata={
            "bangonpro": {
                "project_id": compiled.project_id,
                "sequence_id": compiled.sequence_id,
                "profile_id": compiled.profile_id,
                "width": compiled.width,
                "height": compiled.height,
                "fps_num": compiled.fps_num,
                "fps_den": compiled.fps_den,
                "ass_subtitle_path": compiled.ass_subtitle_path,
                "srt_subtitle_path": compiled.srt_subtitle_path,
            }
        },
    )

    # Video Track V1
    v1_track = otio.schema.Track(name="V1_Multicam", kind=otio.schema.TrackKind.Video)
    cursor = 0
    for clip in compiled.v1_clips:
        if clip.timeline_start > cursor:
            v1_track.append(
                otio.schema.Gap(
                    source_range=otio.opentime.TimeRange(
                        otio.opentime.RationalTime(0, fps),
                        otio.opentime.RationalTime(clip.timeline_start - cursor, fps),
                    )
                )
            )
            cursor = clip.timeline_start

        meta = compiled.media_registry.get(clip.media_ref or "", {})
        media_url = str(meta.get("file_path", ""))
        avail_dur = round(float(meta.get("duration_sec", clip.duration_frames / fps)) * fps)
        ext_ref = otio.schema.ExternalReference(
            target_url=media_url,
            available_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(0, fps),
                otio.opentime.RationalTime(max(1, avail_dur), fps),
            ),
            metadata={"sha256": meta.get("sha256", ""), "camera_id": meta.get("camera_id", "")},
        )
        otio_clip = otio.schema.Clip(
            name=f"{clip.clip_id}_{meta.get('camera_id', 'cam')}",
            media_reference=ext_ref,
            source_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(clip.in_frame, fps),
                otio.opentime.RationalTime(clip.duration_frames, fps),
            ),
            metadata={
                "bangonpro": {
                    "camera_id": clip.params.get("camera_id", ""),
                    "confidence": clip.confidence,
                    "rationale": clip.rationale,
                }
            },
        )
        v1_track.append(otio_clip)
        cursor = clip.timeline_start + clip.duration_frames

    # Attach review/section/QC markers to V1 track
    marker_color_map = {
        "section": otio.schema.MarkerColor.GREEN,
        "review": otio.schema.MarkerColor.ORANGE,
        "qc_warning": otio.schema.MarkerColor.YELLOW,
        "qc_error": otio.schema.MarkerColor.RED,
        "setup_trim": otio.schema.MarkerColor.CYAN,
    }
    for m in compiled.markers:
        v1_track.markers.append(
            otio.schema.Marker(
                name=m["label"],
                marked_range=otio.opentime.TimeRange(
                    otio.opentime.RationalTime(int(m["frame"]), fps),
                    otio.opentime.RationalTime(1, fps),
                ),
                color=marker_color_map.get(m["category"], otio.schema.MarkerColor.ORANGE),
                metadata={
                    "category": m["category"],
                    "confidence": m["confidence"],
                    "rationale": m["rationale"],
                },
            )
        )
    timeline.tracks.append(v1_track)

    # Graphics Track V2
    v2_track = otio.schema.Track(name="V2_Graphics", kind=otio.schema.TrackKind.Video)
    cursor = 0
    for clip in compiled.v2_clips:
        if clip.timeline_start > cursor:
            v2_track.append(
                otio.schema.Gap(
                    source_range=otio.opentime.TimeRange(
                        otio.opentime.RationalTime(0, fps),
                        otio.opentime.RationalTime(clip.timeline_start - cursor, fps),
                    )
                )
            )
            cursor = clip.timeline_start

        gen_ref = otio.schema.GeneratorReference(
            name=clip.clip_id,
            generator_kind="kdenlivetitle",
            parameters={
                "title": clip.params.get("title", ""),
                "subtitle": clip.params.get("subtitle", ""),
                "accent_hex": clip.params.get("accent_hex", "#FF5A1F"),
            },
        )
        v2_track.append(
            otio.schema.Clip(
                name=f"LowerThird_{clip.params.get('title', clip.clip_id)}",
                media_reference=gen_ref,
                source_range=otio.opentime.TimeRange(
                    otio.opentime.RationalTime(0, fps),
                    otio.opentime.RationalTime(clip.duration_frames, fps),
                ),
            )
        )
        cursor = clip.timeline_start + clip.duration_frames
    timeline.tracks.append(v2_track)

    # Audio Track A1
    a1_track = otio.schema.Track(name="A1_Dialogue", kind=otio.schema.TrackKind.Audio)
    cursor = 0
    for clip in compiled.a1_clips:
        if clip.timeline_start > cursor:
            a1_track.append(
                otio.schema.Gap(
                    source_range=otio.opentime.TimeRange(
                        otio.opentime.RationalTime(0, fps),
                        otio.opentime.RationalTime(clip.timeline_start - cursor, fps),
                    )
                )
            )
            cursor = clip.timeline_start
        meta = compiled.media_registry.get(clip.media_ref or "", {})
        avail_dur = round(float(meta.get("duration_sec", clip.duration_frames / fps)) * fps)
        ext_ref = otio.schema.ExternalReference(
            target_url=str(meta.get("file_path", "")),
            available_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(0, fps),
                otio.opentime.RationalTime(max(1, avail_dur), fps),
            ),
        )
        a1_track.append(
            otio.schema.Clip(
                name=clip.clip_id,
                media_reference=ext_ref,
                source_range=otio.opentime.TimeRange(
                    otio.opentime.RationalTime(clip.in_frame, fps),
                    otio.opentime.RationalTime(clip.duration_frames, fps),
                ),
            )
        )
        cursor = clip.timeline_start + clip.duration_frames

    for flt in compiled.audio_filters:
        if flt["track_id"] == "A1":
            a1_track.effects.append(
                otio.schema.Effect(
                    name=flt["filter_id"],
                    effect_name=flt["mlt_service"],
                    metadata={
                        "mlt_params": flt["mlt_params"],
                        "ffmpeg_filter": flt["ffmpeg_filter"],
                    },
                )
            )
    timeline.tracks.append(a1_track)

    otio.adapters.write_to_file(timeline, str(out_path))
    return out_path


def melt_env() -> dict[str, str]:
    """Environment for a melt subprocess: OpenCV's wheel (imported by scenedetect) points
    QT_QPA_PLATFORM_PLUGIN_PATH at its own bundled Qt plugins, which MLT's Qt module cannot load."""
    env = {k: v for k, v in os.environ.items() if k not in ("QT_QPA_PLATFORM_PLUGIN_PATH", "QT_QPA_FONTDIR", "QT_PLUGIN_PATH")}
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    return env


def render_kdenlive_project_with_melt(
    kdenlive_path: str | Path,
    output_mp4_path: str | Path,
    *,
    video_codec: str = "libx264",
    audio_codec: str = "aac",
    video_bitrate: str = "6M",
    audio_bitrate: str = "192k",
    preset: str = "ultrafast",
    consumer_props: list[str] | None = None,
) -> dict[str, Any]:
    """Execute a real verified render of a ``.kdenlive`` project using ``melt`` + ``ffprobe``.

    ``consumer_props`` adds melt consumer properties, e.g. ``["width=360", "height=640"]`` for a
    fast review proxy of the same project.
    """
    src_xml = Path(kdenlive_path).resolve()
    out_mp4 = Path(output_mp4_path).resolve()
    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    env = melt_env()
    if os.name == "posix" and not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        env["DISPLAY"] = ":99"

    cmd = [
        "melt",
        str(src_xml),
        "-consumer",
        f"avformat:{out_mp4}",
        f"vcodec={video_codec}",
        f"vb={video_bitrate}",
        f"preset={preset}",
        "pix_fmt=yuv420p",
        f"acodec={audio_codec}",
        f"ab={audio_bitrate}",
        "ar=48000",
        "ac=2",
        *(consumer_props or []),
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"melt render failed (rc={proc.returncode}): {proc.stderr[-1000:]}"
        )

    if not out_mp4.is_file() or out_mp4.stat().st_size == 0:
        raise RuntimeError(f"melt completed but output file is missing or empty: {out_mp4}")

    probe = probe_media_file(out_mp4)
    if probe["audio_codec"] is None:
        raise RuntimeError(f"Rendered MP4 missing audio stream: {out_mp4}")
    return probe


def run_project_emit_export_stage(
    state: ProjectAnalysisState,
    token: CancellationToken,
    report_progress: Callable[[float, str], None],
) -> dict[str, Any] | None:
    """Compile validated command batches to ``.kdenlive`` + ``.otio`` and optionally render via ``melt``."""
    token.check("project_emit_export start")
    if not state.command_batches:
        report_progress(1.0, "No command batches to compile; skipping project emission")
        return None

    out_dir = Path(state.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    report_progress(0.15, "Replaying validated Timeline Command API v1 batches")
    compiled = compile_command_batches(state.command_batches)

    report_progress(0.35, "Emitting Kdenlive/MLT .kdenlive project XML")
    kdenlive_path = emit_kdenlive_xml(compiled, out_dir / f"{state.sequence_id}.kdenlive")
    state.artifacts["kdenlive_project"] = str(kdenlive_path)

    token.check("after kdenlive xml emit")
    report_progress(0.55, "Emitting OpenTimelineIO .otio project")
    otio_path = emit_otio_timeline(compiled, out_dir / f"{state.sequence_id}.otio")
    state.artifacts["otio_timeline"] = str(otio_path)

    export_presets = state.profile.get("export_presets", [])
    delivery_spec = state.profile.get("delivery_spec") or {}
    preset_cfg = (
        export_presets[0]
        if export_presets
        else {
            "name": "delivery_spec_default",
            "container": delivery_spec.get("container", "mp4"),
            "video_codec": delivery_spec.get("video_codec", "libx264"),
            "audio_codec": delivery_spec.get("audio_codec", "aac"),
            "video_bitrate": delivery_spec.get("video_bitrate", "8M"),
            "audio_bitrate": delivery_spec.get("audio_bitrate", "192k"),
        }
    )

    export_probe: dict[str, Any] | None = None
    rendered_path: Path | None = None

    if state.intent.export_video and not state.dry_run:
        token.check("before melt export")
        report_progress(0.70, "Rendering master MP4 via melt")
        ext = str(preset_cfg.get("container", "mp4")).lstrip(".")
        rendered_path = out_dir / f"{state.sequence_id}_export.{ext}"
        export_probe = render_kdenlive_project_with_melt(
            kdenlive_path,
            rendered_path,
            video_codec=str(preset_cfg.get("video_codec", "libx264")),
            audio_codec=str(preset_cfg.get("audio_codec", "aac")),
            video_bitrate=str(preset_cfg.get("video_bitrate", "6M")),
            audio_bitrate=str(preset_cfg.get("audio_bitrate", "192k")),
        )
        state.artifacts["rendered_video"] = str(rendered_path)
        state.metadata["export_probe"] = export_probe

    builder = TimelineCommandBuilder(
        project_id=state.project_id,
        sequence_id=state.sequence_id,
        expected_revision=state.revision,
        agent_id="project_builder_agent",
        agent_role="project_emitter_and_exporter",
        transaction_label="Emit Kdenlive/OTIO Project & Export Sequence",
        requires_approval=False,
        dry_run=state.dry_run,
        fps_num=state.fps_num,
        fps_den=state.fps_den,
    )

    builder.add_operation(
        "export_sequence",
        in_frame=0,
        out_frame=max(0, compiled.total_frames - 1),
        timeline_start=0,
        params={
            "kdenlive_path": str(kdenlive_path),
            "otio_path": str(otio_path),
            "rendered_video_path": str(rendered_path) if rendered_path else None,
            "export_executed": bool(rendered_path is not None),
            "preset_name": preset_cfg.get("name", "default_mp4"),
            "export_probe": export_probe,
        },
        confidence=1.0,
        rationale=(
            "Emitted Kdenlive XML + OTIO timeline"
            + (
                " and rendered verified MP4 via melt"
                if rendered_path
                else " (export not requested / dry run)"
            )
        ),
    )

    report_progress(0.98, "Project emission complete")
    return builder.build()
