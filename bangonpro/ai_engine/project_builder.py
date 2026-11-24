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

    @property
    def fps(self) -> float:
        return float(self.fps_num) / float(self.fps_den)

    @property
    def total_frames(self) -> int:
        max_end = 0
        for lst in (self.v1_clips, self.a1_clips, self.v2_clips, self.a2_clips):
            for c in lst:
                max_end = max(max_end, c.timeline_end_inclusive + 1)
        return max(1, max_end)


def compile_command_batches(command_batches: list[dict[str, Any]]) -> CompiledTimeline:
    """Validate and deterministically replay Timeline Command API v1 batches into a CompiledTimeline."""
    if not command_batches:
        raise ValueError("Cannot compile project from empty command_batches list")

    first = validate_timeline_command_batch(command_batches[0])
    compiled = CompiledTimeline(
        project_id=first["project_id"],
        sequence_id=first["sequence_id"],
    )

    for batch in command_batches:
        validate_timeline_command_batch(batch)
        for op in batch["operations"]:
            op_type = op["type"]
            params = op.get("params", {}) or {}
            fr = op.get("frame_range", {}) or {}

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
                compiled.captions.append(
                    {
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

    compiled.v1_clips.sort(key=lambda c: c.timeline_start)
    compiled.v2_clips.sort(key=lambda c: c.timeline_start)
    compiled.a1_clips.sort(key=lambda c: c.timeline_start)
    compiled.a2_clips.sort(key=lambda c: c.timeline_start)
    return compiled


def _find_compiled_clip(compiled: CompiledTimeline, clip_id: str) -> CompiledClip | None:
    for lst in (compiled.v1_clips, compiled.v2_clips, compiled.a1_clips, compiled.a2_clips):
        for clip in lst:
            if clip.clip_id == clip_id:
                return clip
    return None


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
    filters: list[dict[str, Any]] = [
        {
            "mlt_service": "mask_start",
            "kdenlive_id": "mask_start_shape",
            "properties": {
                "filter": "shape",
                "filter.resource": str(matte),
                "filter.mix": 100,
                "filter.softness": round(min(1.0, max(0.0, float(params.get("feather", 0.0)) / 100.0)), 3),
                "filter.invert": 1 if params.get("invert") else 0,
                "filter.use_luminance": 1,
            },
            "agent_id": params.get("agent_id"),
        }
    ]
    masked = params.get("masked_effect") or {}
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
            if meta.get("sha256"):
                _add_prop(chain, "kdenlive:file_hash", meta["sha256"])
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
            for k, v in flt["mlt_params"].items():
                _add_prop(f_el, k, v)

    # 6. Video Track V1 (Multicam Program)
    pl_v1 = ET.SubElement(mlt, "playlist", {"id": "playlist_v1"})
    _populate_playlist_with_clips(pl_v1, compiled.v1_clips, video_chain_map, kdenlive_id_map)
    ET.SubElement(mlt, "playlist", {"id": "playlist_v1_b"})

    tr_v1 = ET.SubElement(mlt, "tractor", {"id": "tractor_v1", "in": "0", "out": str(last_frame)})
    _add_prop(tr_v1, "kdenlive:track_name", "Multicam Program V1")
    _add_prop(tr_v1, "kdenlive:trackheight", 67)
    _add_prop(tr_v1, "kdenlive:timeline_active", 1)
    ET.SubElement(tr_v1, "track", {"hide": "audio", "producer": "playlist_v1"})
    ET.SubElement(tr_v1, "track", {"hide": "audio", "producer": "playlist_v1_b"})

    # 7. Video Track V2 (Graphics & Lower Thirds)
    pl_v2 = ET.SubElement(mlt, "playlist", {"id": "playlist_v2"})
    _populate_playlist_with_clips(pl_v2, compiled.v2_clips, title_prod_map, kdenlive_id_map)
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


def render_kdenlive_project_with_melt(
    kdenlive_path: str | Path,
    output_mp4_path: str | Path,
    *,
    video_codec: str = "libx264",
    audio_codec: str = "aac",
    video_bitrate: str = "6M",
    audio_bitrate: str = "192k",
    preset: str = "ultrafast",
) -> dict[str, Any]:
    """Execute a real verified render of a ``.kdenlive`` project using ``melt`` + ``ffprobe``."""
    src_xml = Path(kdenlive_path).resolve()
    out_mp4 = Path(output_mp4_path).resolve()
    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
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
