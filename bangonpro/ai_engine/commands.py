"""Timeline Command API v1 builder and JSON Schema validator.

All Bang On Pro AI agents emit structured Timeline Command API v1 batches
validated strictly against ``bangonpro/schemas/timeline_command_v1.schema.json``.
Agents never mutate raw timeline structures directly.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


class TimelineCommandValidationError(ValueError):
    """Raised when a Timeline Command API v1 payload fails schema or semantic validation."""


def get_timeline_command_schema_path() -> Path:
    """Return path to ``bangonpro/schemas/timeline_command_v1.schema.json``."""
    return Path(__file__).resolve().parent.parent / "schemas" / "timeline_command_v1.schema.json"


@lru_cache(maxsize=1)
def _get_command_validator() -> Draft202012Validator:
    schema_path = get_timeline_command_schema_path()
    with schema_path.open("r", encoding="utf-8") as fh:
        schema = json.load(fh)
    return Draft202012Validator(schema)


def validate_timeline_command_batch(batch: dict[str, Any]) -> dict[str, Any]:
    """Validate a Timeline Command API v1 batch against JSON Schema + semantic invariants."""
    if not isinstance(batch, dict):
        raise TimelineCommandValidationError("Timeline command batch must be a JSON object.")

    validator = _get_command_validator()
    errors = sorted(validator.iter_errors(batch), key=lambda err: list(err.path))
    if errors:
        formatted = "; ".join(
            f"{'.'.join(str(p) for p in err.path) or '<root>'}: {err.message}"
            for err in errors
        )
        raise TimelineCommandValidationError(
            f"Timeline Command API v1 validation failed: {formatted}"
        )

    seen_op_ids: set[str] = set()
    for op in batch["operations"]:
        op_id = op["op_id"]
        if op_id in seen_op_ids:
            raise TimelineCommandValidationError(f"Duplicate op_id in batch: {op_id}")
        seen_op_ids.add(op_id)

        fr = op.get("frame_range")
        if fr is not None:
            in_f = fr.get("in_frame")
            out_f = fr.get("out_frame")
            if in_f is not None and out_f is not None and out_f < in_f:
                raise TimelineCommandValidationError(
                    f"Operation '{op_id}' has out_frame ({out_f}) < in_frame ({in_f})"
                )

    return batch


class TimelineCommandBuilder:
    """Fluent builder for constructing schema-validated Timeline Command API v1 batches."""

    def __init__(
        self,
        *,
        project_id: str,
        sequence_id: str,
        expected_revision: int,
        agent_id: str,
        agent_role: str,
        transaction_label: str = "AI Edit Batch",
        requires_approval: bool = False,
        dry_run: bool = False,
        fps_num: int = 25,
        fps_den: int = 1,
        request_id: str | None = None,
    ) -> None:
        self.project_id = project_id
        self.sequence_id = sequence_id
        self.expected_revision = int(expected_revision)
        self.agent_id = agent_id
        self.agent_role = agent_role
        self.transaction_label = transaction_label
        self.requires_approval = bool(requires_approval)
        self.dry_run = bool(dry_run)
        self.fps_num = int(fps_num)
        self.fps_den = int(fps_den)
        self.request_id = request_id or f"req_{uuid.uuid4().hex[:12]}"
        self._operations: list[dict[str, Any]] = []

    def add_operation(
        self,
        op_type: str,
        *,
        media_ref: str | None = None,
        clip_id: str | None = None,
        track_id: str | None = None,
        in_frame: int | None = None,
        out_frame: int | None = None,
        timeline_start: int | None = None,
        params: dict[str, Any] | None = None,
        confidence: float = 1.0,
        rationale: str = "",
        op_id: str | None = None,
    ) -> dict[str, Any]:
        """Append a single Timeline Command operation to the batch."""
        op: dict[str, Any] = {
            "op_id": op_id or f"{self.agent_id}_op_{len(self._operations) + 1:04d}",
            "type": op_type,
            "confidence": round(min(1.0, max(0.0, float(confidence))), 4),
        }
        if media_ref is not None:
            op["media_ref"] = media_ref
        if clip_id is not None:
            op["clip_id"] = clip_id
        if track_id is not None:
            op["track_id"] = track_id
        if any(v is not None for v in (in_frame, out_frame, timeline_start)):
            frame_range: dict[str, int] = {
                "fps_num": self.fps_num,
                "fps_den": self.fps_den,
            }
            if in_frame is not None:
                frame_range["in_frame"] = max(0, int(in_frame))
            if out_frame is not None:
                frame_range["out_frame"] = max(
                    frame_range.get("in_frame", 0), int(out_frame)
                )
            if timeline_start is not None:
                frame_range["timeline_start"] = max(0, int(timeline_start))
            op["frame_range"] = frame_range
        if params is not None:
            op["params"] = params
        if rationale:
            op["rationale"] = rationale

        self._operations.append(op)
        return op

    @property
    def operation_count(self) -> int:
        return len(self._operations)

    def build(self) -> dict[str, Any]:
        """Assemble and strictly validate the Timeline Command API v1 batch."""
        batch: dict[str, Any] = {
            "api_version": "1.0",
            "request_id": self.request_id,
            "project_id": self.project_id,
            "sequence_id": self.sequence_id,
            "expected_revision": self.expected_revision,
            "actor": {
                "agent_id": self.agent_id,
                "role": self.agent_role,
                "requires_approval": self.requires_approval,
            },
            "transaction_label": self.transaction_label,
            "dry_run": self.dry_run,
            "operations": list(self._operations),
        }
        return validate_timeline_command_batch(batch)
