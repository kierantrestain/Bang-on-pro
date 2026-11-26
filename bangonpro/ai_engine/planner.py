"""Structured AI Command Planner & Active-Project Context Engine for Bang On Pro.

Separates AI editing into four explicit, testable stages:
1. **Instruction interpretation** — deterministic local intent parsing
   (``intent.py``) with optional consent-gated LLM plan interpretation.
2. **Active-project context ingestion** — hardened ``defusedxml`` parsing of
   existing ``.kdenlive`` XML projects and ``timeline_state.json`` snapshots so
   the AI understands existing clips, tracks, duration, markers, and speakers
   without forcing users to re-import project media.
3. **Deterministic proposal planning** — generates schema-validated
   ``timeline_command_v1`` operation batches strictly scoped to user intent with
   confidence scores and editorial rationales.
4. **Plan validation & execution status reporting** — verifies JSON Schema
   conformance, target grounding, and strict editorial scope invariants.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import defusedxml.ElementTree as SafeET

from bangonpro.ai_engine.agents.audio_graphics_qc import build_audio_restoration_chain
from bangonpro.ai_engine.commands import (
    TimelineCommandBuilder,
    TimelineCommandValidationError,
    validate_timeline_command_batch,
)
from bangonpro.ai_engine.intent import _classify_scope, parse_production_instruction
from bangonpro.ai_engine.providers import AiProviderRegistry, ConsentRecord
from bangonpro.ai_engine.state import ProductionIntent
from bangonpro.show_profiles.loader import get_effective_profile_settings

LlmInterpreterFn = Callable[[str, dict[str, Any]], dict[str, Any]]

_ACTION_FLAGS: tuple[str, ...] = (
    "sync_cameras", "transcribe", "remove_setup_delay", "remove_silence", "build_rough_cut", "multicam_switch",
    "restore_audio", "add_captions", "add_lower_thirds", "run_qc", "export_video",
)

_ALLOWED_LLM_INTENT_KEYS: frozenset[str] = frozenset(
    {
        "sync_cameras",
        "transcribe",
        "remove_setup_delay",
        "remove_silence",
        "build_rough_cut",
        "multicam_switch",
        "restore_audio",
        "add_captions",
        "add_lower_thirds",
        "run_qc",
        "export_video",
        "needs_clarification",
        "clarification_question",
        "profile_hint",
        "target_clips",
        "target_tracks",
        "time_range_sec",
        "target_speakers",
        "loudness_target_lufs",
        "silence_threshold_db",
        "min_pause_sec",
    }
)

_SCOPE_FORBIDDEN_OPS: dict[str, frozenset[str]] = {
    "audio_only": frozenset(
        {
            "sync_clips",
            "select_camera",
            "place_clip",
            "split_clip",
            "trim_clip",
            "delete_clip",
            "add_caption",
            "add_lower_third",
            "export_sequence",
        }
    ),
    "captions_only": frozenset(
        {
            "sync_clips",
            "select_camera",
            "place_clip",
            "split_clip",
            "trim_clip",
            "delete_clip",
            "add_audio_filter",
            "set_audio_level",
            "add_lower_third",
            "export_sequence",
        }
    ),
    "graphics_only": frozenset(
        {
            "sync_clips",
            "select_camera",
            "split_clip",
            "trim_clip",
            "delete_clip",
            "add_audio_filter",
            "set_audio_level",
            "add_caption",
            "export_sequence",
        }
    ),
    "silence_only": frozenset(
        {
            "sync_clips",
            "select_camera",
            "add_audio_filter",
            "set_audio_level",
            "add_caption",
            "add_lower_third",
            "export_sequence",
        }
    ),
}


@dataclass
class ProjectClipInfo:
    """Single timeline clip extracted from an active ``.kdenlive`` or ``timeline_state.json`` project."""

    clip_id: str
    track_id: str
    producer_id: str
    media_ref: str | None
    file_path: str | None
    clip_name: str
    in_frame: int
    out_frame: int
    timeline_start: int
    timeline_end: int
    duration_frames: int
    duration_sec: float
    clip_kind: str = "av"  # "av" | "video" | "audio" | "kdenlivetitle" | "color"
    filters: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProjectTrackInfo:
    """Track metadata extracted from an active project."""

    track_id: str
    name: str
    track_type: str  # "video" | "audio"
    playlist_id: str = ""
    clip_count: int = 0
    filters: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProjectContext:
    """Normalized active-project context consumed by the AI planner and pipeline."""

    source_kind: str = "empty"  # "kdenlive_xml" | "timeline_state_json" | "inputs_only" | "empty"
    source_path: str | None = None
    project_id: str = "active_project"
    sequence_id: str = "seq_main"
    revision: int = 0
    profile_id: str | None = None
    width: int = 1920
    height: int = 1080
    fps_num: int = 25
    fps_den: int = 1
    duration_frames: int = 0
    duration_sec: float = 0.0
    tracks: list[ProjectTrackInfo] = field(default_factory=list)
    clips: list[ProjectClipInfo] = field(default_factory=list)
    media_resources: dict[str, dict[str, Any]] = field(default_factory=dict)
    markers: list[dict[str, Any]] = field(default_factory=list)
    speakers: list[str] = field(default_factory=list)
    transcript_segments: list[dict[str, Any]] = field(default_factory=list)
    audio_filters: list[dict[str, Any]] = field(default_factory=list)
    subtitles: list[str] = field(default_factory=list)
    transcript_path: str | None = None

    @property
    def fps(self) -> float:
        return float(self.fps_num) / float(self.fps_den) if self.fps_den > 0 else 25.0

    def extract_existing_media_inputs(self) -> dict[str, str]:
        """Return ``{camera_id: file_path}`` for all project media files present on disk."""
        resolved: dict[str, str] = {}
        seen_paths: set[str] = set()
        for idx, (res_id, meta) in enumerate(self.media_resources.items(), start=1):
            raw_path = str(meta.get("file_path") or "").strip()
            if not raw_path or raw_path == "black":
                continue
            p = Path(raw_path)
            if not p.is_file():
                continue
            abs_p = str(p.resolve())
            if abs_p in seen_paths:
                continue
            seen_paths.add(abs_p)
            cam_id = str(meta.get("camera_id") or meta.get("clip_name") or res_id or f"cam_{idx}")
            # Normalize safe camera_id key if clip_name has file extension
            cam_key = Path(cam_id).stem if "." in cam_id and "/" not in cam_id else cam_id
            if cam_key in resolved:
                cam_key = f"{cam_key}_{idx}"
            resolved[cam_key] = abs_p
        return resolved

    def summary(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "source_path": self.source_path,
            "project_id": self.project_id,
            "sequence_id": self.sequence_id,
            "revision": self.revision,
            "profile_id": self.profile_id,
            "resolution": f"{self.width}x{self.height}",
            "fps": round(self.fps, 4),
            "duration_frames": self.duration_frames,
            "duration_sec": round(self.duration_sec, 4),
            "track_ids": [t.track_id for t in self.tracks],
            "clip_count": len(self.clips),
            "media_resource_count": len(self.media_resources),
            "marker_count": len(self.markers),
            "speakers": list(self.speakers),
            "transcript_segment_count": len(self.transcript_segments),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "source_path": self.source_path,
            "project_id": self.project_id,
            "sequence_id": self.sequence_id,
            "revision": self.revision,
            "profile_id": self.profile_id,
            "width": self.width,
            "height": self.height,
            "fps_num": self.fps_num,
            "fps_den": self.fps_den,
            "fps": round(self.fps, 6),
            "duration_frames": self.duration_frames,
            "duration_sec": round(self.duration_sec, 4),
            "tracks": [t.to_dict() for t in self.tracks],
            "clips": [c.to_dict() for c in self.clips],
            "media_resources": dict(self.media_resources),
            "markers": list(self.markers),
            "speakers": list(self.speakers),
            "transcript_segments": list(self.transcript_segments),
            "audio_filters": list(self.audio_filters),
            "subtitles": list(self.subtitles),
            "transcript_path": self.transcript_path,
        }


@dataclass
class InstructionInterpretation:
    """Output of Stage 1 (instruction interpretation)."""

    intent: ProductionIntent
    interpreter_backend: str = "local_deterministic_intent_parser"
    used_llm: bool = False
    consent_record: dict[str, Any] | None = None
    raw_llm_payload: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        intent_d = asdict(self.intent)
        if intent_d.get("time_range_sec") is not None:
            intent_d["time_range_sec"] = list(intent_d["time_range_sec"])
        return {
            "intent": intent_d,
            "interpreter_backend": self.interpreter_backend,
            "used_llm": self.used_llm,
            "consent_record": self.consent_record,
        }


@dataclass
class EditProposal:
    """Output of Stage 3 (deterministic proposal planning)."""

    status: str  # "planned" | "needs_clarification"
    scope: str
    command_batch: dict[str, Any] | None = None
    clarification_question: str = ""
    warnings: list[str] = field(default_factory=list)
    operations_summary: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PlanExecutionReport:
    """Output of Stage 4 (plan validation & execution status reporting)."""

    status: str  # "planned" | "executed" | "needs_clarification" | "validation_failed"
    valid: bool
    scope: str
    needs_clarification: bool
    clarification_question: str
    interpreter_backend: str
    operation_count: int
    operation_types: list[str]
    validation_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    intent: dict[str, Any] = field(default_factory=dict)
    context_summary: dict[str, Any] = field(default_factory=dict)
    command_batch: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Stage 1: Instruction Interpretation (Local + Optional Consent-Gated LLM)
# ---------------------------------------------------------------------------


def shared_reasoning_interpreter() -> LlmInterpreterFn | None:
    """Instruction interpreter backed by the department's shared provider router, or ``None``.

    The AI Assistant uses the same authorised CLIs, priority order, failover, cooldowns and daily
    budget as the 14 production agents; there is no second provider system. ``None`` when no
    provider is authorised and installed, so the local rules stay the only interpreter.
    """
    from bangonpro.ai_engine.mcp.reasoning import CliReasoner, ReasonerError

    try:
        router = CliReasoner()
    except ReasonerError:
        return None

    def interpret(instruction: str, ctx_summary: dict[str, Any]) -> dict[str, Any]:
        def prompt(policy: dict[str, Any]) -> str:
            return (
                "You interpret one video-editing instruction for Bang On Pro's AI Assistant. Reply with ONE JSON object and nothing else. "
                "Allowed keys (omit any you do not need): booleans sync_cameras, transcribe, remove_setup_delay, remove_silence, build_rough_cut, "
                "multicam_switch, restore_audio, add_captions, add_lower_thirds, run_qc, export_video, needs_clarification; strings "
                "clarification_question, profile_hint; lists of strings target_clips, target_tracks, target_speakers; time_range_sec [start, end] "
                "in seconds; numbers loudness_target_lufs, silence_threshold_db, min_pause_sec. If any requested detail cannot be expressed with these fields, "
                'set needs_clarification true and ask one short question.\n'
                f"Project: {json.dumps({k: v for k, v in ctx_summary.items() if k != 'source_path'} if policy.get('metadata') is True else {}, default=str)[:1500]}\nInstruction: {instruction}"
            )
        out = router.plan(prompt, purpose="interpret", agent="executive")
        return {k: v for k, v in out.items() if k in _ALLOWED_LLM_INTENT_KEYS}

    interpret.provider_id = "shared_reasoning_router"  # type: ignore[attr-defined]
    return interpret


def validate_llm_plan_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate an optional LLM interpreter JSON response against the strict intent schema."""
    if not isinstance(payload, dict):
        raise ValueError("LLM interpretation payload must be a JSON object")  # noqa: TRY004
    unknown = set(payload.keys()) - _ALLOWED_LLM_INTENT_KEYS
    if unknown:
        raise ValueError(
            f"LLM interpretation payload contains disallowed keys: {sorted(unknown)}"
        )
    bool_keys = (
        "sync_cameras",
        "transcribe",
        "remove_setup_delay",
        "remove_silence",
        "build_rough_cut",
        "multicam_switch",
        "restore_audio",
        "add_captions",
        "add_lower_thirds",
        "run_qc",
        "export_video",
        "needs_clarification",
    )
    for k in bool_keys:
        if k in payload and not isinstance(payload[k], bool):
            raise ValueError(f"LLM interpretation field '{k}' must be a boolean")
    if "time_range_sec" in payload and payload["time_range_sec"] is not None:
        tr = payload["time_range_sec"]
        if not isinstance(tr, (list, tuple)) or len(tr) != 2 or float(tr[1]) <= float(tr[0]):
            raise ValueError("LLM interpretation field 'time_range_sec' must be [start, end] with end > start")
    return payload


def interpret_instruction(
    instruction: str,
    *,
    context: ProjectContext | None = None,
    export_override: bool | None = None,
    allow_default_empty: bool = False,
    provider_config: dict[str, Any] | None = None,
    llm_interpreter: LlmInterpreterFn | None = None,
    audit_log: Any = None,
) -> InstructionInterpretation:
    """Stage 1: Interpret user instruction locally, or via consent-gated LLM provider."""
    base_intent = parse_production_instruction(
        instruction,
        export_override=export_override,
        allow_default_empty=allow_default_empty,
    )

    consent_record: ConsentRecord | None = None
    call_llm = shared_router = False
    router_note: str | None = None
    provider_id = "local_deterministic"
    if provider_config:
        registry = AiProviderRegistry()
        provider_id = str(provider_config.get("provider_id", "local_deterministic"))
        consent_granted = bool(provider_config.get("consent_granted", False))
        payload_summary = str(
            provider_config.get(
                "payload_summary",
                f"AI instruction interpretation for '{instruction[:80]}'",
            )
        )
        consent_record = registry.require_external_consent(
            provider_id,
            consent_granted=consent_granted,
            payload_summary=payload_summary,
            audit_log=audit_log,
            data_categories=("instruction_text", "project_context_summary"),
        )

        call_llm = llm_interpreter is not None
    elif llm_interpreter is not None and base_intent.needs_clarification:
        # The shared reasoning router (the editor's authorised CLIs, one budget, automatic failover) is
        # asked only when the local rules could not interpret the instruction. It enforces saved privacy consent.
        provider_id = str(getattr(llm_interpreter, "provider_id", "shared_reasoning_router"))
        call_llm = True
        shared_router = True

    if call_llm and llm_interpreter is not None:
        ctx_summary = context.summary() if context is not None else {}
        try:
            raw_llm = validate_llm_plan_payload(llm_interpreter(instruction, ctx_summary))
        except Exception as exc:  # noqa: BLE001 - an unavailable or unusable router answer keeps the local interpretation
            if not shared_router:
                raise
            raw_llm = None
            router_note = f"{type(exc).__name__}: {exc}"[:300]
        if raw_llm is not None:
            merged = ProductionIntent(
                raw_instruction=instruction.strip(),
                sync_cameras=bool(raw_llm.get("sync_cameras", base_intent.sync_cameras)),
                transcribe=bool(raw_llm.get("transcribe", base_intent.transcribe)),
                remove_setup_delay=bool(
                    raw_llm.get("remove_setup_delay", base_intent.remove_setup_delay)
                ),
                remove_silence=bool(raw_llm.get("remove_silence", base_intent.remove_silence)),
                build_rough_cut=bool(raw_llm.get("build_rough_cut", base_intent.build_rough_cut)),
                multicam_switch=bool(raw_llm.get("multicam_switch", base_intent.multicam_switch)),
                restore_audio=bool(raw_llm.get("restore_audio", base_intent.restore_audio)),
                add_captions=bool(raw_llm.get("add_captions", base_intent.add_captions)),
                add_lower_thirds=bool(
                    raw_llm.get("add_lower_thirds", base_intent.add_lower_thirds)
                ),
                run_qc=bool(raw_llm.get("run_qc", base_intent.run_qc)),
                export_video=(
                    bool(export_override)
                    if export_override is not None
                    else bool(raw_llm.get("export_video", base_intent.export_video))
                ),
                needs_clarification=bool(
                    raw_llm.get("needs_clarification", base_intent.needs_clarification)
                ),
                clarification_question=str(
                    raw_llm.get("clarification_question", base_intent.clarification_question)
                ),
                profile_hint=raw_llm.get("profile_hint", base_intent.profile_hint),
                parsed_actions=list(base_intent.parsed_actions),
                target_clips=list(raw_llm.get("target_clips", base_intent.target_clips)),
                target_tracks=list(raw_llm.get("target_tracks", base_intent.target_tracks)),
                time_range_sec=(
                    (float(raw_llm["time_range_sec"][0]), float(raw_llm["time_range_sec"][1]))
                    if raw_llm.get("time_range_sec") is not None
                    else base_intent.time_range_sec
                ),
                target_speakers=list(
                    raw_llm.get("target_speakers", base_intent.target_speakers)
                ),
                loudness_target_lufs=raw_llm.get(
                    "loudness_target_lufs", base_intent.loudness_target_lufs
                ),
                silence_threshold_db=raw_llm.get(
                    "silence_threshold_db", base_intent.silence_threshold_db
                ),
                min_pause_sec=raw_llm.get("min_pause_sec", base_intent.min_pause_sec),
                intent_confidence=0.9 if not raw_llm.get("needs_clarification") else 0.0,
                scope="clarification" if raw_llm.get("needs_clarification") else base_intent.scope,
            )
            if shared_router and "needs_clarification" not in raw_llm and any(bool(getattr(merged, k)) for k in _ACTION_FLAGS):
                # The router answered with actions: it understood what the rules could not.
                merged = replace(merged, needs_clarification=False, clarification_question="")
            if not merged.needs_clarification:
                # The interpreted flags define the actions and the editorial scope, not the rules' empty parse.
                flags = {k: bool(getattr(merged, k)) for k in _ACTION_FLAGS}
                merged = replace(merged, parsed_actions=[k for k in _ACTION_FLAGS if flags[k]], scope=_classify_scope(**flags))
            return InstructionInterpretation(
                intent=merged,
                interpreter_backend=f"llm_provider:{provider_id}",
                used_llm=True,
                consent_record=consent_record.to_dict() if consent_record else None,
                raw_llm_payload=raw_llm,
            )

    return InstructionInterpretation(
        intent=base_intent,
        interpreter_backend="local_deterministic_intent_parser" + (f" (shared reasoning unavailable: {router_note})" if router_note else ""),
        used_llm=False,
        consent_record=consent_record.to_dict() if consent_record else None,
    )


# ---------------------------------------------------------------------------
# Stage 2: Active-Project Context Ingestion (.kdenlive XML & timeline_state.json)
# ---------------------------------------------------------------------------


def _parse_mlt_time_to_frames(raw_val: str | None, fps: float) -> int:
    """Parse MLT integer frames OR ``HH:MM:SS.mmm`` / ``HH:MM:SS:FF`` timecodes to frame int."""
    if raw_val is None:
        return 0
    s = str(raw_val).strip()
    if not s:
        return 0
    if re.fullmatch(r"-?\d+", s):
        return max(0, int(s))
    if ":" in s:
        parts = s.split(":")
        try:
            if len(parts) == 3:
                hrs = int(parts[0])
                mins = int(parts[1])
                secs = float(parts[2])
                return max(0, round((hrs * 3600.0 + mins * 60.0 + secs) * fps))
            if len(parts) == 4:
                hrs = int(parts[0])
                mins = int(parts[1])
                secs_i = int(parts[2])
                frames_i = int(parts[3])
                return max(0, round((hrs * 3600 + mins * 60 + secs_i) * fps) + frames_i)
        except ValueError:
            return 0
    try:
        return max(0, round(float(s) * fps))
    except ValueError:
        return 0


def _props_map(element: Any) -> dict[str, str]:
    props: dict[str, str] = {}
    for prop in element.findall("property"):
        name = prop.attrib.get("name", "")
        if name:
            props[name] = (prop.text or "").strip()
    return props


def _resolve_resource_path(resource: str, project_root: Path) -> str:
    if not resource or resource == "black":
        return resource
    p = Path(resource)
    if p.is_absolute():
        return str(p)
    candidate = (project_root / p).resolve()
    return str(candidate)


def _normalize_track_id(track_name: str, is_audio: bool, idx: int) -> str:
    m = re.search(r"\b([VA]\d+)\b", track_name or "", re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return f"{'A' if is_audio else 'V'}{idx}"


def ingest_kdenlive_project(kdenlive_path: str | Path) -> ProjectContext:
    """Safely ingest an existing ``.kdenlive`` MLT XML project using ``defusedxml``."""
    xml_path = Path(kdenlive_path).expanduser().resolve()
    if not xml_path.is_file():
        raise FileNotFoundError(f"Kdenlive project file not found: {xml_path}")

    root = SafeET.parse(str(xml_path)).getroot()
    if root.tag != "mlt":
        raise ValueError(f"Expected <mlt> root in '{xml_path}', found <{root.tag}>")

    mlt_root_dir = Path(root.attrib.get("root") or xml_path.parent)
    if not mlt_root_dir.is_dir():
        mlt_root_dir = xml_path.parent

    profile_el = root.find("profile")
    width = int(profile_el.attrib.get("width", "1920")) if profile_el is not None else 1920
    height = int(profile_el.attrib.get("height", "1080")) if profile_el is not None else 1080
    fps_num = int(profile_el.attrib.get("frame_rate_num", "25")) if profile_el is not None else 25
    fps_den = int(profile_el.attrib.get("frame_rate_den", "1")) if profile_el is not None else 1
    fps = float(fps_num) / float(fps_den) if fps_den > 0 else 25.0

    # 1. Index all <producer> and <chain> nodes
    producers: dict[str, dict[str, Any]] = {}
    media_resources: dict[str, dict[str, Any]] = {}

    for node in list(root.findall("chain")) + list(root.findall("producer")):
        pid = node.attrib.get("id", "")
        if not pid:
            continue
        props = _props_map(node)
        service = props.get("mlt_service", "")
        raw_res = props.get("resource", "")
        resolved_res = (
            _resolve_resource_path(raw_res, mlt_root_dir)
            if service not in ("color", "kdenlivetitle") and raw_res
            else raw_res
        )
        clip_name = props.get("kdenlive:clipname") or (Path(resolved_res).name if resolved_res else pid)
        in_f = _parse_mlt_time_to_frames(node.attrib.get("in"), fps)
        out_f = _parse_mlt_time_to_frames(node.attrib.get("out"), fps)
        length_f = _parse_mlt_time_to_frames(props.get("length"), fps) or max(1, out_f - in_f + 1)

        info = {
            "producer_id": pid,
            "mlt_service": service,
            "file_path": resolved_res if service not in ("color", "kdenlivetitle") else None,
            "clip_name": clip_name,
            "kdenlive_id": props.get("kdenlive:id"),
            "file_hash": props.get("kdenlive:file_hash"),
            "duration_frames": length_f,
            "duration_sec": round(length_f / fps, 4),
            "xmldata": props.get("xmldata", ""),
            "exists_on_disk": bool(resolved_res and Path(resolved_res).is_file())
            if service not in ("color", "kdenlivetitle")
            else False,
        }
        producers[pid] = info

        if info["file_path"] and service not in ("color", "kdenlivetitle"):
            # Deduplicate bin/audio/video mirror chains by file_path
            existing_key = next(
                (k for k, v in media_resources.items() if v.get("file_path") == info["file_path"]),
                None,
            )
            if existing_key is None:
                res_key = f"media_{props.get('kdenlive:clipname') or pid}"
                media_resources[res_key] = {
                    "media_ref": res_key,
                    "camera_id": props.get("kdenlive:clipname") or Path(info["file_path"]).stem,
                    "clip_name": clip_name,
                    "file_path": info["file_path"],
                    "kdenlive_id": props.get("kdenlive:id"),
                    "sha256": props.get("kdenlive:file_hash"),
                    "duration_sec": info["duration_sec"],
                    "exists_on_disk": info["exists_on_disk"],
                }

    # 2. Extract document metadata, guides/markers, and playlists
    playlists_by_id: dict[str, Any] = {}
    project_id = xml_path.stem
    sequence_id = "seq_main"
    profile_id: str | None = None
    markers: list[dict[str, Any]] = []
    speakers_set: list[str] = []

    def _add_speaker(name: str) -> None:
        cleaned = name.strip()
        if cleaned and cleaned not in speakers_set:
            speakers_set.append(cleaned)

    for pl in root.findall("playlist"):
        pl_id = pl.attrib.get("id", "")
        playlists_by_id[pl_id] = pl
        if pl_id == "main_bin":
            props = _props_map(pl)
            project_id = props.get("kdenlive:docproperties.documentid") or project_id
            profile_id = props.get("kdenlive:docproperties.bangonpro_profile") or profile_id
            doc_guides = props.get("kdenlive:docproperties.guides")
            if doc_guides:
                try:
                    for g in json.loads(doc_guides):
                        pos = int(g.get("pos", 0))
                        markers.append(
                            {
                                "frame": pos,
                                "time_sec": round(pos / fps, 4),
                                "label": str(g.get("comment", "")),
                                "type": int(g.get("type", 0)),
                            }
                        )
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass

    # 3. Inspect tractors for tracks, track filters, sequence guides, and subtitles
    tracks: list[ProjectTrackInfo] = []
    clips: list[ProjectClipInfo] = []
    audio_filters: list[dict[str, Any]] = []
    subtitles: list[str] = []
    audio_idx = 0
    video_idx = 0
    max_timeline_frame = 0

    for tr in root.findall("tractor"):
        tr_id = tr.attrib.get("id", "")
        props = _props_map(tr)

        # Sequence-level guides & subtitles
        seq_guides = props.get("kdenlive:sequenceproperties.guides")
        if seq_guides:
            sequence_id = props.get("kdenlive:clipname") or tr_id or sequence_id
            try:
                for g in json.loads(seq_guides):
                    pos = int(g.get("pos", 0))
                    markers.append(
                        {
                            "frame": pos,
                            "time_sec": round(pos / fps, 4),
                            "label": str(g.get("comment", "")),
                            "type": int(g.get("type", 0)),
                        }
                    )
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        for flt in tr.findall("filter"):
            f_props = _props_map(flt)
            svc = f_props.get("mlt_service", "")
            if svc == "avfilter.subtitles" and f_props.get("av.filename"):
                subtitles.append(f_props["av.filename"])

        # Track-level tractor (has kdenlive:track_name or playlist tracks)
        track_name = props.get("kdenlive:track_name", "")
        if not track_name:
            continue

        is_audio = props.get("kdenlive:audio_track") == "1"
        if is_audio:
            audio_idx += 1
            track_id = _normalize_track_id(track_name, True, audio_idx)
        else:
            video_idx += 1
            track_id = _normalize_track_id(track_name, False, video_idx)

        tr_filters: list[dict[str, Any]] = []
        for flt in tr.findall("filter"):
            f_props = _props_map(flt)
            svc = f_props.get("mlt_service", "")
            if svc:
                f_rec = {
                    "filter_id": flt.attrib.get("id", ""),
                    "track_id": track_id,
                    "mlt_service": svc,
                    "params": {k: v for k, v in f_props.items() if k != "mlt_service"},
                }
                tr_filters.append(f_rec)
                if is_audio:
                    audio_filters.append(f_rec)

        track_els = tr.findall("track")
        primary_pl_id = track_els[0].attrib.get("producer", "") if track_els else ""
        track_clip_count = 0

        for t_el in track_els:
            pl_ref = t_el.attrib.get("producer", "")
            pl_node = playlists_by_id.get(pl_ref)
            if pl_node is None:
                continue
            cursor = 0
            for child in list(pl_node):
                if child.tag == "blank":
                    cursor += _parse_mlt_time_to_frames(child.attrib.get("length"), fps)
                elif child.tag == "entry":
                    prod_ref = child.attrib.get("producer", "")
                    prod_info = producers.get(prod_ref, {})
                    in_f = _parse_mlt_time_to_frames(child.attrib.get("in"), fps)
                    out_f = _parse_mlt_time_to_frames(child.attrib.get("out"), fps)
                    dur_f = max(1, out_f - in_f + 1)
                    tl_start = cursor
                    tl_end = cursor + dur_f
                    cursor = tl_end
                    max_timeline_frame = max(max_timeline_frame, tl_end)

                    entry_filters: list[dict[str, Any]] = []
                    for e_flt in child.findall("filter"):
                        ef_props = _props_map(e_flt)
                        if ef_props.get("mlt_service"):
                            entry_filters.append(
                                {
                                    "filter_id": e_flt.attrib.get("id", ""),
                                    "mlt_service": ef_props["mlt_service"],
                                    "params": {
                                        k: v for k, v in ef_props.items() if k != "mlt_service"
                                    },
                                }
                            )

                    svc = str(prod_info.get("mlt_service", ""))
                    clip_kind = (
                        "kdenlivetitle"
                        if svc == "kdenlivetitle"
                        else ("audio" if is_audio else "video")
                    )
                    clip_name = str(prod_info.get("clip_name") or prod_ref)
                    if clip_kind == "kdenlivetitle" and clip_name.startswith("Lower Third:"):
                        _add_speaker(clip_name.split(":", 1)[1])

                    media_ref = next(
                        (
                            k
                            for k, v in media_resources.items()
                            if v.get("file_path") == prod_info.get("file_path")
                        ),
                        None,
                    )
                    track_clip_count += 1
                    clips.append(
                        ProjectClipInfo(
                            clip_id=f"{track_id.lower()}_clip_{track_clip_count:03d}",
                            track_id=track_id,
                            producer_id=prod_ref,
                            media_ref=media_ref,
                            file_path=prod_info.get("file_path"),
                            clip_name=clip_name,
                            in_frame=in_f,
                            out_frame=out_f,
                            timeline_start=tl_start,
                            timeline_end=tl_end,
                            duration_frames=dur_f,
                            duration_sec=round(dur_f / fps, 4),
                            clip_kind=clip_kind,
                            filters=entry_filters,
                        )
                    )

        tracks.append(
            ProjectTrackInfo(
                track_id=track_id,
                name=track_name,
                track_type="audio" if is_audio else "video",
                playlist_id=primary_pl_id,
                clip_count=track_clip_count,
                filters=tr_filters,
            )
        )

    # 4. Ingest sibling transcript.json if present next to .kdenlive
    transcript_segments: list[dict[str, Any]] = []
    transcript_path: str | None = None
    sibling_transcript = xml_path.parent / "transcript.json"
    if sibling_transcript.is_file():
        try:
            t_doc = json.loads(sibling_transcript.read_text(encoding="utf-8"))
            transcript_path = str(sibling_transcript)
            for spk in t_doc.get("speakers", []):
                _add_speaker(str(spk.get("display_name") or spk.get("speaker_id", "")))
            transcript_segments = list(t_doc.get("segments", []))
        except (OSError, json.JSONDecodeError):
            pass

    # Infer speakers from section markers if still empty
    for m in markers:
        lbl = str(m.get("label", ""))
        if ":" in lbl and lbl.split(":", 1)[0].strip().lower() in ("opening", "section 2", "section 3"):
            _add_speaker(lbl.split(":", 1)[1])

    duration_sec = round(max_timeline_frame / fps, 4) if max_timeline_frame > 0 else 0.0
    return ProjectContext(
        source_kind="kdenlive_xml",
        source_path=str(xml_path),
        project_id=project_id,
        sequence_id=sequence_id,
        revision=0,
        profile_id=profile_id,
        width=width,
        height=height,
        fps_num=fps_num,
        fps_den=fps_den,
        duration_frames=max_timeline_frame,
        duration_sec=duration_sec,
        tracks=tracks,
        clips=clips,
        media_resources=media_resources,
        markers=markers,
        speakers=speakers_set,
        transcript_segments=transcript_segments,
        audio_filters=audio_filters,
        subtitles=subtitles,
        transcript_path=transcript_path,
    )


def ingest_timeline_state_json(state_path: str | Path) -> ProjectContext:
    """Ingest an active project ``timeline_state.json`` or ``analysis_state.json`` snapshot."""
    json_path = Path(state_path).expanduser().resolve()
    if not json_path.is_file():
        raise FileNotFoundError(f"Project state JSON not found: {json_path}")

    data = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Project state JSON must be an object")  # noqa: TRY004

    fps_num = int(data.get("fps_num", 25))
    fps_den = int(data.get("fps_den", 1))
    fps_val = float(data.get("fps", float(fps_num) / float(fps_den)))
    if fps_val > 0 and "fps_num" not in data:
        fps_num = round(fps_val)
        fps_den = 1

    tracks: list[ProjectTrackInfo] = []
    clips: list[ProjectClipInfo] = []
    media_resources: dict[str, dict[str, Any]] = {}
    speakers: list[str] = []

    def _add_spk(name: str) -> None:
        c = name.strip()
        if c and c not in speakers:
            speakers.append(c)

    # Support C++ TimelineCommandEngine::stateSnapshotJson() (nested "state" dict),
    # flat timeline_state.json (tracks + clips), AND analysis_state.json (media_assets + rough_cut_shots).
    state_body = data.get("state") if isinstance(data.get("state"), dict) else data
    if not isinstance(state_body, dict):
        state_body = data

    for mb in state_body.get("media_bin", []):
        if not isinstance(mb, dict):
            continue
        m_ref = str(mb.get("media_ref") or "").strip()
        if not m_ref:
            continue
        fp = str(mb.get("source_path") or mb.get("file_path") or "")
        media_resources[m_ref] = {
            "media_ref": m_ref,
            "camera_id": str(mb.get("name") or m_ref),
            "clip_name": str(mb.get("name") or m_ref),
            "file_path": fp,
            "duration_frames": int(mb.get("duration_frames", 0)),
            "exists_on_disk": bool(fp and Path(fp).is_file()),
        }

    for tr in state_body.get("tracks", []):
        tracks.append(
            ProjectTrackInfo(
                track_id=str(tr.get("track_id", tr.get("id", "V1"))),
                name=str(tr.get("name", tr.get("track_id", "Track"))),
                track_type=str(tr.get("track_type", tr.get("kind", tr.get("type", "video")))),
                clip_count=int(tr.get("clip_count", 0)),
                filters=list(tr.get("filters", tr.get("effects", []))),
            )
        )

    for c_idx, clip in enumerate(state_body.get("clips", []), start=1):
        fr = clip.get("frame_range") if isinstance(clip.get("frame_range"), dict) else {}
        in_f = int(clip.get("in_frame", fr.get("in_frame", 0)))
        out_f = int(clip.get("out_frame", fr.get("out_frame", in_f)))
        tl_s = int(clip.get("timeline_start", fr.get("timeline_start", 0)))
        dur_f = max(1, int(clip.get("duration_frames", max(1, out_f - in_f))))
        tl_e = int(clip.get("timeline_end", tl_s + dur_f))
        m_ref_raw = clip.get("media_ref")
        resolved_fp = clip.get("file_path")
        if not resolved_fp and m_ref_raw and str(m_ref_raw) in media_resources:
            resolved_fp = media_resources[str(m_ref_raw)].get("file_path") or None
        clips.append(
            ProjectClipInfo(
                clip_id=str(clip.get("clip_id", f"clip_{c_idx:03d}")),
                track_id=str(clip.get("track_id", "V1")),
                producer_id=str(clip.get("producer_id", "")),
                media_ref=str(m_ref_raw) if m_ref_raw is not None else None,
                file_path=str(resolved_fp) if resolved_fp else None,
                clip_name=str(clip.get("clip_name", clip.get("name", clip.get("clip_id", f"clip_{c_idx:03d}")))),
                in_frame=in_f,
                out_frame=out_f,
                timeline_start=tl_s,
                timeline_end=tl_e,
                duration_frames=dur_f,
                duration_sec=round(dur_f / max(1.0, fps_val), 4),
                clip_kind=str(clip.get("clip_kind", "av")),
                filters=list(clip.get("filters", clip.get("effects", []))),
            )
        )
        if clip.get("file_path"):
            m_ref = str(clip.get("media_ref") or f"media_{clip.get('clip_id', c_idx)}")
            media_resources.setdefault(
                m_ref,
                {
                    "media_ref": m_ref,
                    "camera_id": str(clip.get("camera_id") or clip.get("clip_name") or clip.get("name") or m_ref),
                    "clip_name": str(clip.get("clip_name") or clip.get("name") or m_ref),
                    "file_path": str(clip["file_path"]),
                    "exists_on_disk": Path(str(clip["file_path"])).is_file(),
                },
            )

    # analysis_state.json compatibility
    for cam_id, asset in (data.get("media_assets") or {}).items():
        m_ref = str(asset.get("media_ref", f"media_{cam_id}"))
        fp = str(asset.get("file_path", ""))
        media_resources[m_ref] = {
            "media_ref": m_ref,
            "camera_id": cam_id,
            "clip_name": cam_id,
            "file_path": fp,
            "sha256": asset.get("sha256"),
            "duration_sec": asset.get("duration_sec", 0.0),
            "exists_on_disk": bool(fp and Path(fp).is_file()),
        }
        if asset.get("speaker_label"):
            _add_spk(str(asset["speaker_label"]))

    if not clips and data.get("rough_cut_shots"):
        for shot in data["rough_cut_shots"]:
            tl_s = int(shot.get("timeline_in_frame", 0))
            tl_e = int(shot.get("timeline_out_frame", 0)) + 1
            dur_f = max(1, tl_e - tl_s)
            clips.append(
                ProjectClipInfo(
                    clip_id=f"{shot['shot_id']}_v",
                    track_id="V1",
                    producer_id=str(shot.get("camera_id", "")),
                    media_ref=shot.get("media_ref"),
                    file_path=None,
                    clip_name=str(shot.get("camera_id", shot["shot_id"])),
                    in_frame=int(shot.get("source_in_frame", 0)),
                    out_frame=int(shot.get("source_out_frame", 0)),
                    timeline_start=tl_s,
                    timeline_end=tl_e,
                    duration_frames=dur_f,
                    duration_sec=round(dur_f / max(1.0, fps_val), 4),
                    clip_kind="video",
                )
            )
            if shot.get("speaker_id"):
                _add_spk(str(shot["speaker_id"]))

    if not tracks and clips:
        seen_t: dict[str, int] = {}
        for c in clips:
            seen_t[c.track_id] = seen_t.get(c.track_id, 0) + 1
        for tid, cnt in seen_t.items():
            tracks.append(
                ProjectTrackInfo(
                    track_id=tid,
                    name=tid,
                    track_type="audio" if tid.startswith("A") else "video",
                    clip_count=cnt,
                )
            )

    for spk in data.get("speakers", []):
        if isinstance(spk, dict):
            _add_spk(str(spk.get("display_name") or spk.get("speaker_id", "")))
        else:
            _add_spk(str(spk))

    transcript_segments = list(data.get("transcript_segments", []))
    for seg in transcript_segments:
        if isinstance(seg, dict) and seg.get("speaker_id"):
            _add_spk(str(seg.get("speaker_name") or seg["speaker_id"]))

    max_f = max((c.timeline_end for c in clips), default=int(data.get("duration_frames", 0)))
    dur_sec = float(data.get("duration_sec", round(max_f / max(1.0, fps_val), 4)))

    return ProjectContext(
        source_kind="timeline_state_json",
        source_path=str(json_path),
        project_id=str(data.get("project_id", json_path.stem)),
        sequence_id=str(data.get("sequence_id") or state_body.get("active_sequence_id") or "seq_main"),
        revision=int(data.get("revision", 0)),
        profile_id=data.get("profile_id"),
        width=int(data.get("width", 1920)),
        height=int(data.get("height", 1080)),
        fps_num=fps_num,
        fps_den=fps_den,
        duration_frames=max_f,
        duration_sec=round(dur_sec, 4),
        tracks=tracks,
        clips=clips,
        media_resources=media_resources,
        markers=list(state_body.get("markers") or data.get("markers") or data.get("review_markers") or []),
        speakers=speakers,
        transcript_segments=transcript_segments,
        audio_filters=list(data.get("audio_filters", [])),
        subtitles=list(state_body.get("captions") or data.get("subtitles") or []),
        transcript_path=data.get("transcript_path"),
    )


def ingest_project_context(
    *,
    kdenlive_project: str | Path | None = None,
    project_state: str | Path | None = None,
    inputs: dict[str, str | Path] | list[str | Path] | None = None,
    project_id: str = "active_project",
    sequence_id: str = "seq_main",
) -> ProjectContext:
    """Stage 2: Ingest active project context from ``.kdenlive``, ``timeline_state.json``, or inputs."""
    if kdenlive_project is not None:
        return ingest_kdenlive_project(kdenlive_project)
    if project_state is not None:
        return ingest_timeline_state_json(project_state)

    media_resources: dict[str, dict[str, Any]] = {}
    if inputs:
        if isinstance(inputs, dict):
            items = [(str(k), str(v)) for k, v in inputs.items()]
        else:
            items = []
            for idx, item in enumerate(inputs, start=1):
                raw = str(item)
                if "=" in raw and not Path(raw).exists():
                    cid, path_s = raw.split("=", 1)
                    items.append((cid.strip(), path_s.strip()))
                else:
                    items.append((f"cam_{idx}", raw))
        for cid, raw_p in items:
            p = Path(raw_p).expanduser()
            media_resources[f"media_{cid}"] = {
                "media_ref": f"media_{cid}",
                "camera_id": cid,
                "clip_name": p.name,
                "file_path": str(p.resolve()) if p.exists() else raw_p,
                "exists_on_disk": p.is_file(),
            }

    return ProjectContext(
        source_kind="inputs_only" if media_resources else "empty",
        project_id=project_id,
        sequence_id=sequence_id,
        media_resources=media_resources,
    )


# ---------------------------------------------------------------------------
# Stage 3: Deterministic Proposal Planning
# ---------------------------------------------------------------------------


def _clip_matches_targets(
    clip: ProjectClipInfo,
    intent: ProductionIntent,
    fps: float,
) -> bool:
    if intent.target_tracks and clip.track_id not in intent.target_tracks:
        return False
    if intent.target_clips:
        haystack = f"{clip.clip_id} {clip.clip_name} {clip.producer_id} {clip.file_path or ''}".lower()
        if not any(t.lower() in haystack for t in intent.target_clips):
            return False
    if intent.time_range_sec is not None:
        r0_f = round(intent.time_range_sec[0] * fps)
        r1_f = round(intent.time_range_sec[1] * fps)
        if clip.timeline_end <= r0_f or clip.timeline_start >= r1_f:
            return False
    return True


def build_edit_proposal(
    interpretation: InstructionInterpretation,
    context: ProjectContext,
    profile: dict[str, Any],
    *,
    expected_revision: int | None = None,
    dry_run: bool = True,
    rnnoise_model_path: str | None = None,
) -> EditProposal:
    """Stage 3: Deterministically plan schema-valid ``timeline_command_v1`` operations for active context."""
    intent = interpretation.intent
    if intent.needs_clarification:
        return EditProposal(
            status="needs_clarification",
            scope="clarification",
            command_batch=None,
            clarification_question=intent.clarification_question,
            warnings=[],
            operations_summary=[],
        )

    effective = get_effective_profile_settings(profile)
    fps = context.fps if context.duration_frames > 0 else (
        float(profile["video_spec"]["fps_num"]) / float(profile["video_spec"]["fps_den"])
    )
    fps_num = context.fps_num if context.duration_frames > 0 else int(profile["video_spec"]["fps_num"])
    fps_den = context.fps_den if context.duration_frames > 0 else int(profile["video_spec"]["fps_den"])
    rev = context.revision if expected_revision is None else int(expected_revision)

    builder = TimelineCommandBuilder(
        project_id=context.project_id,
        sequence_id=context.sequence_id,
        expected_revision=rev,
        agent_id="ai_command_planner",
        agent_role="context_aware_editorial_planner",
        transaction_label=f"AI Edit Proposal ({intent.scope}): {intent.raw_instruction[:60]}",
        requires_approval=True,
        dry_run=dry_run,
        fps_num=fps_num,
        fps_den=fps_den,
    )
    warnings: list[str] = []

    existing_track_ids = {t.track_id for t in context.tracks}
    for req_track in intent.target_tracks:
        if existing_track_ids and req_track not in existing_track_ids:
            warnings.append(
                f"Requested track '{req_track}' was not found in active project tracks {sorted(existing_track_ids)}."
            )

    if intent.time_range_sec is not None and context.duration_sec > 0:
        if intent.time_range_sec[0] >= context.duration_sec:
            warnings.append(
                f"Requested time range {intent.time_range_sec} starts at/after project duration {context.duration_sec:.2f}s."
            )

    # 1. Audio restoration operations
    if intent.restore_audio:
        audio_rules = profile.get("audio_rules", {})
        target_lufs = float(
            intent.loudness_target_lufs
            if intent.loudness_target_lufs is not None
            else effective["loudness_target_lufs"]
        )
        req_audio_tracks = [t for t in intent.target_tracks if t.startswith("A")]
        if req_audio_tracks:
            audio_tracks = req_audio_tracks
        else:
            existing_audio = [t.track_id for t in context.tracks if t.track_type == "audio"]
            audio_tracks = ["A1"] if ("A1" in existing_audio or not existing_audio) else [existing_audio[0]]

        range_in_f = round(intent.time_range_sec[0] * fps) if intent.time_range_sec else None
        range_out_f = round(intent.time_range_sec[1] * fps) if intent.time_range_sec else None

        for trk in audio_tracks:
            filters, _ = build_audio_restoration_chain(
                audio_rules,
                rnnoise_model_path=rnnoise_model_path,
                target_track_id=trk,
                loudness_override_lufs=target_lufs,
                include_music_duck=(trk == "A1" and intent.scope != "audio_only"),
            )
            for flt in filters:
                if not flt.enabled:
                    continue
                builder.add_operation(
                    "add_audio_filter",
                    track_id=flt.track_id,
                    in_frame=range_in_f,
                    out_frame=range_out_f,
                    timeline_start=range_in_f,
                    params={
                        "filter_id": flt.filter_id,
                        "mlt_service": flt.mlt_service,
                        "mlt_params": flt.mlt_params,
                        "ffmpeg_filter": flt.ffmpeg_filter,
                        "target_lufs": target_lufs,
                    },
                    confidence=0.96,
                    rationale=f"{flt.rationale} on {flt.track_id}",
                )

    # 2. Captions operations
    if intent.add_captions:
        caption_style = effective["caption_style"]
        matched_segs = []
        for seg in context.transcript_segments:
            if not isinstance(seg, dict) or seg.get("deleted"):
                continue
            spk = str(seg.get("speaker_name") or seg.get("speaker_id") or "")
            if intent.target_speakers and not any(
                t.lower() in spk.lower() or spk.lower() in t.lower()
                for t in intent.target_speakers
            ):
                continue
            s_sec = float(seg.get("start_sec", 0.0))
            e_sec = float(seg.get("end_sec", s_sec + 1.0))
            if intent.time_range_sec is not None:
                if e_sec <= intent.time_range_sec[0] or s_sec >= intent.time_range_sec[1]:
                    continue
                s_sec = max(s_sec, intent.time_range_sec[0])
                e_sec = min(e_sec, intent.time_range_sec[1])
            matched_segs.append((seg, s_sec, e_sec))

        if matched_segs:
            for idx, (seg, s_sec, e_sec) in enumerate(matched_segs, start=1):
                in_f = round(s_sec * fps)
                out_f = max(in_f + 1, round(e_sec * fps))
                builder.add_operation(
                    "add_caption",
                    track_id="V2",
                    in_frame=in_f,
                    out_frame=out_f,
                    timeline_start=in_f,
                    params={
                        "index": idx,
                        "speaker_id": str(seg.get("speaker_name") or seg.get("speaker_id", "")),
                        "text": str(seg.get("text", "")),
                        "caption_style": caption_style,
                    },
                    confidence=float(seg.get("confidence", 0.85)),
                    rationale=f"Add caption cue #{idx} from active project transcript",
                )
        else:
            r_in = round(intent.time_range_sec[0] * fps) if intent.time_range_sec else 0
            r_out = (
                round(intent.time_range_sec[1] * fps)
                if intent.time_range_sec
                else max(1, context.duration_frames - 1)
            )
            builder.add_operation(
                "add_marker",
                track_id="V2",
                in_frame=r_in,
                out_frame=max(r_in, r_out),
                timeline_start=r_in,
                params={
                    "category": "caption_pending_asr",
                    "label": "Run ASR/VAD transcription to populate captions",
                    "target_speakers": list(intent.target_speakers),
                    "caption_style": caption_style,
                },
                confidence=0.88,
                rationale=(
                    "Active project has no transcript.json yet; execute transcription stage "
                    "to generate timestamped captions without altering timeline cuts"
                ),
            )

    # 3. Lower-third graphics operations
    if intent.add_lower_thirds:
        templates = effective["graphics_templates"]
        g_rules = profile.get("graphics_rules", {})
        lt_dur_f = max(1, round(float(g_rules.get("lower_third_duration_sec", 4.5)) * fps))
        speakers = intent.target_speakers or context.speakers or ["Host"]
        show_subtitle = str(profile.get("display_name", "Bang On Pro"))
        cursor_f = round(intent.time_range_sec[0] * fps) if intent.time_range_sec else round(0.5 * fps)

        for idx, spk in enumerate(speakers, start=1):
            start_f = cursor_f
            end_f = start_f + lt_dur_f - 1
            builder.add_operation(
                "add_lower_third",
                clip_id=f"lt_{idx:02d}_{spk.lower().replace(' ', '_')}",
                track_id="V2",
                in_frame=0,
                out_frame=max(0, lt_dur_f - 1),
                timeline_start=start_f,
                params={
                    "speaker_name": spk,
                    "subtitle": show_subtitle,
                    "template": str(templates.get("lower_third_template", "studio_clean_bar")),
                    "position": str(templates.get("lower_third_position", "bottom_left")),
                    "accent_hex": str(templates.get("accent_hex", "#FF5A1F")),
                    "duration_frames": lt_dur_f,
                },
                confidence=0.93,
                rationale=f"Add lower-third graphic for '{spk}' on V2 without modifying timeline cuts",
            )
            cursor_f = end_f + round(1.0 * fps)

    # 4. Sync cameras operation (when requested)
    if intent.sync_cameras:
        first_media_ref = next(iter(context.media_resources), None)
        builder.add_operation(
            "sync_clips",
            media_ref=first_media_ref,
            params={
                "method": "gcc_phat_16khz",
                "media_refs": list(context.media_resources.keys()),
                "source_kind": context.source_kind,
            },
            confidence=0.94,
            rationale=f"Synchronise {len(context.media_resources)} project media source(s) via GCC-PHAT",
        )

    # 5. Silence / setup delay trimming on active project clips
    if (intent.remove_silence or intent.remove_setup_delay) and not intent.build_rough_cut:
        sil_db = (
            intent.silence_threshold_db
            if intent.silence_threshold_db is not None
            else float(effective["silence_threshold_db"])
        )
        min_pause = (
            intent.min_pause_sec
            if intent.min_pause_sec is not None
            else float(profile["editing_rules"]["min_silence_sec"])
        )
        matching_clips = [c for c in context.clips if _clip_matches_targets(c, intent, fps)]
        if intent.remove_setup_delay and matching_clips:
            lead_clip = matching_clips[0]
            trim_frames = min(max(1, round(1.0 * fps)), max(1, lead_clip.duration_frames // 3))
            builder.add_operation(
                "trim_clip",
                clip_id=lead_clip.clip_id,
                track_id=lead_clip.track_id,
                in_frame=lead_clip.in_frame + trim_frames,
                out_frame=lead_clip.out_frame,
                timeline_start=lead_clip.timeline_start,
                params={
                    "action": "remove_setup_delay",
                    "silence_threshold_db": sil_db,
                    "right": False,
                },
                confidence=0.84,
                rationale=f"Trim leading setup delay on clip '{lead_clip.clip_id}' ({lead_clip.track_id})",
            )
        if intent.remove_silence:
            r_in = round(intent.time_range_sec[0] * fps) if intent.time_range_sec else 0
            r_out = (
                round(intent.time_range_sec[1] * fps)
                if intent.time_range_sec
                else max(1, context.duration_frames - 1)
            )
            builder.add_operation(
                "add_marker",
                track_id=(intent.target_tracks[0] if intent.target_tracks else "V1"),
                in_frame=r_in,
                out_frame=max(r_in, r_out),
                timeline_start=r_in,
                params={
                    "category": "silence_trim_plan",
                    "label": f"Remove silence < {sil_db:.1f} dB (min pause {min_pause:.2f}s)",
                    "silence_threshold_db": sil_db,
                    "min_pause_sec": min_pause,
                    "target_clip_ids": [c.clip_id for c in matching_clips],
                },
                confidence=0.90,
                rationale=(
                    f"Planned silence/dead-air excision below {sil_db:.1f} dB for pauses >= {min_pause:.2f}s "
                    "without adding unsolicited graphics, captions, or multicam changes"
                ),
            )

    # 6. Rough cut / multicam switching proposal grounded in active project media
    if intent.build_rough_cut or intent.multicam_switch:
        v_spec = profile["video_spec"]
        if context.source_kind in ("inputs_only", "empty"):
            builder.add_operation(
                "create_sequence",
                params={
                    "profile_id": profile["profile_id"],
                    "display_name": profile["display_name"],
                    "width": v_spec["width"],
                    "height": v_spec["height"],
                    "fps_num": v_spec["fps_num"],
                    "fps_den": v_spec["fps_den"],
                },
                confidence=0.95,
                rationale=f"Initialize sequence for Show Profile '{profile['profile_id']}'",
            )
        media_keys = list(context.media_resources.keys())
        if media_keys:
            first_ref = media_keys[0]
            end_f = max(1, context.duration_frames - 1) if context.duration_frames > 0 else round(5.0 * fps)
            builder.add_operation(
                "select_camera",
                media_ref=first_ref,
                track_id="V1",
                in_frame=0,
                out_frame=end_f,
                timeline_start=0,
                params={
                    "camera_id": context.media_resources[first_ref].get("camera_id", first_ref),
                    "profile_id": profile["profile_id"],
                    "min_shot_duration_sec": effective["min_shot_duration_sec"],
                    "max_shot_duration_sec": effective["max_shot_duration_sec"],
                    "wide_shot_interval_sec": effective["wide_shot_interval_sec"],
                    "reaction_shot_frequency": effective["reaction_shot_frequency"],
                },
                confidence=0.88,
                rationale=f"Plan speaker-aware multicam rough cut using profile '{profile['profile_id']}'",
            )

    # 7. Active-project QC checks
    if intent.run_qc:
        missing_files = [
            m for m in context.media_resources.values() if not m.get("exists_on_disk", False)
        ]
        for miss in missing_files:
            builder.add_operation(
                "add_marker",
                track_id="V1",
                in_frame=0,
                out_frame=0,
                timeline_start=0,
                params={
                    "category": "qc_warning",
                    "code": "MISSING_MEDIA",
                    "label": f"Offline media: {miss.get('clip_name', miss.get('file_path'))}",
                },
                confidence=1.0,
                rationale=f"Project media file missing on disk: {miss.get('file_path')}",
            )
        if builder.operation_count == 0:
            builder.add_operation(
                "add_marker",
                track_id="V1",
                in_frame=0,
                out_frame=0,
                timeline_start=0,
                params={
                    "category": "qc_passed",
                    "label": "Project QC Passed (0 missing files)",
                    "clip_count": len(context.clips),
                    "duration_sec": context.duration_sec,
                },
                confidence=0.98,
                rationale="Active project passed structural context QC checks",
            )

    # 8. Export proposal
    if intent.export_video:
        delivery = effective["delivery_spec"]
        builder.add_operation(
            "export_sequence",
            in_frame=0,
            out_frame=max(0, context.duration_frames - 1),
            timeline_start=0,
            params={
                "container": delivery.get("container", "mp4"),
                "video_codec": delivery.get("video_codec", "libx264"),
                "audio_codec": delivery.get("audio_codec", "aac"),
                "target_lufs": delivery.get("target_lufs", -16.0),
                "export_executed": False,
            },
            confidence=0.96,
            rationale="Plan sequence export conforming to Show Profile delivery_spec",
        )

    # Fallback marker if a valid recognized intent (e.g. transcribe_only) had no direct timeline op
    if builder.operation_count == 0:
        builder.add_operation(
            "add_marker",
            track_id="V1",
            in_frame=0,
            out_frame=0,
            timeline_start=0,
            params={
                "category": "analysis_request",
                "scope": intent.scope,
                "label": f"Execute {intent.scope}: {intent.raw_instruction[:64]}",
            },
            confidence=0.90,
            rationale=f"Scheduled analysis stage for scope '{intent.scope}'",
        )

    batch = builder.build()
    ops_summary = [
        {
            "op_id": op["op_id"],
            "type": op["type"],
            "track_id": op.get("track_id"),
            "confidence": op.get("confidence", 1.0),
            "rationale": op.get("rationale", ""),
        }
        for op in batch["operations"]
    ]
    return EditProposal(
        status="planned",
        scope=intent.scope,
        command_batch=batch,
        clarification_question="",
        warnings=warnings,
        operations_summary=ops_summary,
    )


# ---------------------------------------------------------------------------
# Stage 4: Plan Validation & Execution Status Reporting
# ---------------------------------------------------------------------------


def validate_edit_proposal(
    proposal: EditProposal,
    interpretation: InstructionInterpretation,
    context: ProjectContext,
) -> PlanExecutionReport:
    """Stage 4: Validate schema compliance and strict editorial scope invariants."""
    intent_d = asdict(interpretation.intent)
    if intent_d.get("time_range_sec") is not None:
        intent_d["time_range_sec"] = list(intent_d["time_range_sec"])

    if proposal.status == "needs_clarification" or interpretation.intent.needs_clarification:
        return PlanExecutionReport(
            status="needs_clarification",
            valid=True,
            scope="clarification",
            needs_clarification=True,
            clarification_question=proposal.clarification_question
            or interpretation.intent.clarification_question,
            interpreter_backend=interpretation.interpreter_backend,
            operation_count=0,
            operation_types=[],
            validation_errors=[],
            warnings=list(proposal.warnings),
            intent=intent_d,
            context_summary=context.summary(),
            command_batch=None,
        )

    errors: list[str] = []
    batch = proposal.command_batch
    if batch is None:
        errors.append("Edit proposal has no command_batch despite status='planned'.")
        return PlanExecutionReport(
            status="validation_failed",
            valid=False,
            scope=proposal.scope,
            needs_clarification=False,
            clarification_question="",
            interpreter_backend=interpretation.interpreter_backend,
            operation_count=0,
            operation_types=[],
            validation_errors=errors,
            warnings=list(proposal.warnings),
            intent=intent_d,
            context_summary=context.summary(),
            command_batch=None,
        )

    try:
        validate_timeline_command_batch(batch)
    except TimelineCommandValidationError as exc:
        errors.append(str(exc))

    op_types = [str(op["type"]) for op in batch.get("operations", [])]
    forbidden = _SCOPE_FORBIDDEN_OPS.get(proposal.scope, frozenset())
    violated = sorted(set(op_types) & forbidden)
    if violated:
        errors.append(
            f"Scope violation for '{proposal.scope}': disallowed operations emitted: {violated}"
        )

    valid = len(errors) == 0
    return PlanExecutionReport(
        status="planned" if valid else "validation_failed",
        valid=valid,
        scope=proposal.scope,
        needs_clarification=False,
        clarification_question="",
        interpreter_backend=interpretation.interpreter_backend,
        operation_count=len(op_types),
        operation_types=sorted(set(op_types)),
        validation_errors=errors,
        warnings=list(proposal.warnings),
        intent=intent_d,
        context_summary=context.summary(),
        command_batch=batch if valid else None,
    )


