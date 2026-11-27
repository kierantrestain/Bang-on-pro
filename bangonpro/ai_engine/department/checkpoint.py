"""Lossless checkpoint serialisation of ``ProjectAnalysisState`` for resumable jobs.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from bangonpro.ai_engine.state import (
    AsrBackendReport,
    AudioFilterSpec,
    CaptionCue,
    EditShot,
    LowerThirdSpec,
    MediaAsset,
    ProductionIntent,
    ProjectAnalysisState,
    QCIssue,
    QCReport,
    ReviewMarker,
    StorySection,
    SyncResult,
    TimeInterval,
    TranscriptSegment,
)

CHECKPOINT_HASH_KEY = "checkpoint_sha256"


def _build(cls: type, data: dict[str, Any] | None) -> Any:
    if data is None:
        return None
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def serialize_state(state: ProjectAnalysisState) -> dict[str, Any]:
    """Return a JSON-safe dict that ``deserialize_state`` can restore exactly."""
    base = state.to_dict()
    base["profile"] = copy.deepcopy(state.profile)
    base["output_dir"] = state.output_dir
    base["rnnoise_model_path"] = state.rnnoise_model_path
    base["input_camera_files"] = dict(state.input_camera_files)
    base["command_batches"] = copy.deepcopy(state.command_batches)
    base["transcript_document"] = copy.deepcopy(state.transcript_document)
    base["metadata"] = _json_safe(state.metadata)
    base["audio_sample_rate"] = state.audio_sample_rate
    base["qc_report_full"] = dataclasses.asdict(state.qc_report)
    return base


def deserialize_state(data: dict[str, Any]) -> ProjectAnalysisState:
    intent_data = dict(data.get("intent") or {})
    if intent_data.get("time_range_sec") is not None:
        intent_data["time_range_sec"] = tuple(intent_data["time_range_sec"])
    state = ProjectAnalysisState(
        project_id=data["project_id"],
        sequence_id=data["sequence_id"],
        profile=data["profile"],
        output_dir=data["output_dir"],
        instruction=data.get("instruction", ""),
        intent=_build(ProductionIntent, intent_data),
        dry_run=bool(data.get("dry_run", False)),
        rnnoise_model_path=data.get("rnnoise_model_path"),
    )
    state.revision = int(data.get("revision", 0))
    state.state_version = int(data.get("state_version", 0))
    state.version_history = list(data.get("version_history", []))
    state.input_camera_files = dict(data.get("input_camera_files", {}))
    state.media_assets = {k: _build(MediaAsset, v) for k, v in (data.get("media_assets") or {}).items()}
    state.missing_media = list(data.get("missing_media", []))
    state.sync_result = _build(SyncResult, data.get("sync_result"))
    state.transcript_backend = data.get("transcript_backend", state.transcript_backend)
    state.asr_backend_report = _build(AsrBackendReport, data.get("asr_backend_report")) or AsrBackendReport()
    state.transcript_segments = [_build(TranscriptSegment, s) for s in data.get("transcript_segments", [])]
    state.transcript_document = data.get("transcript_document")
    state.silence_intervals = [_build(TimeInterval, i) for i in data.get("silence_intervals", [])]
    state.interruption_intervals = [_build(TimeInterval, i) for i in data.get("interruption_intervals", [])]
    state.setup_delay_sec = float(data.get("setup_delay_sec", 0.0))
    state.story_sections = [_build(StorySection, s) for s in data.get("story_sections", [])]
    state.scene_cuts_sec = list(data.get("scene_cuts_sec", []))
    state.kept_master_intervals = [_build(TimeInterval, i) for i in data.get("kept_master_intervals", [])]
    state.rough_cut_shots = [_build(EditShot, s) for s in data.get("rough_cut_shots", [])]
    state.audio_filters = [_build(AudioFilterSpec, f) for f in data.get("audio_filters", [])]
    state.ffmpeg_audio_filtergraph = data.get("ffmpeg_audio_filtergraph", "")
    state.captions = [_build(CaptionCue, c) for c in data.get("captions", [])]
    state.lower_thirds = [_build(LowerThirdSpec, lt) for lt in data.get("lower_thirds", [])]
    qc = data.get("qc_report_full") or data.get("qc_report") or {}
    state.qc_report = QCReport(
        passed=bool(qc.get("passed", True)),
        issues=[_build(QCIssue, i) for i in qc.get("issues", [])],
        metrics=dict(qc.get("metrics", {})),
    )
    state.review_markers = [_build(ReviewMarker, m) for m in data.get("review_markers", [])]
    state.command_batches = list(data.get("command_batches", []))
    state.artifacts = dict(data.get("artifacts", {}))
    state.stage_timings_sec = dict(data.get("stage_timings_sec", {}))
    state.metadata = dict(data.get("metadata", {}))
    state.audio_sample_rate = int(data.get("audio_sample_rate", 16000))
    return state


def write_json_atomic(path: str | Path, payload: Any) -> Path:
    """Write UTF-8 JSON to a unique sibling temp file, fsync, then atomically replace ``path``."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, indent=2, default=str) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".tmp", dir=str(out.parent))
    tmp_path = Path(tmp_name)
    closed = False
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        closed = True
        os.replace(tmp_path, out)
    except BaseException:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        tmp_path.unlink(missing_ok=True)
        raise
    return out


def compute_checkpoint_sha256(data: dict[str, Any]) -> str:
    """Compute canonical SHA-256 hex digest of a checkpoint payload excluding ``checkpoint_sha256``."""
    body = {k: v for k, v in data.items() if k != CHECKPOINT_HASH_KEY}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def write_checkpoint_atomic(path: str | Path, payload: dict[str, Any]) -> Path:
    """Attach ``checkpoint_sha256`` and write a checkpoint atomically with fsync."""
    body = {k: v for k, v in payload.items() if k != CHECKPOINT_HASH_KEY}
    # Round-trip through JSON defaults once so the hashed representation matches what is read back.
    normalised = json.loads(json.dumps(body, default=str))
    normalised[CHECKPOINT_HASH_KEY] = compute_checkpoint_sha256(normalised)
    return write_json_atomic(path, normalised)


def verify_checkpoint_integrity(data: dict[str, Any]) -> bool:
    """Verify checkpoint structural validity and (when present) its SHA-256 digest.

    Backward-compatible: V1.2-V1.6 checkpoints without ``checkpoint_sha256`` return True
    when structurally valid.
    """
    if not isinstance(data, dict):
        return False
    agent_id = data.get("agent_id")
    agent_index = data.get("agent_index")
    state = data.get("state")
    if not isinstance(agent_id, str) or not agent_id:
        return False
    if isinstance(agent_index, bool) or not isinstance(agent_index, int):
        return False
    if not isinstance(state, dict):
        return False
    for required_key in ("project_id", "sequence_id", "profile", "output_dir"):
        if required_key not in state:
            return False
    if CHECKPOINT_HASH_KEY not in data or data[CHECKPOINT_HASH_KEY] is None:
        return True
    recorded = data.get(CHECKPOINT_HASH_KEY)
    if not isinstance(recorded, str) or len(recorded) != 64:
        return False
    return recorded == compute_checkpoint_sha256(data)
