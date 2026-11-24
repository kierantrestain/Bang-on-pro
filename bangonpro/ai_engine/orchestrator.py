"""Multi-Agent DAG Orchestrator & Immutable Activity Log for Bang On Pro.

Coordinates specialist AI agents through a validated DAG pipeline with:
- Topological dependency scheduling & cycle detection
- Thread-safe cooperative cancellation (``cancel()`` / ``CancellationToken``)
- Bounded retries for transient errors
- Explicit human approval checkpoints before committing command batches
- Strict Timeline Command API v1 JSON Schema validation on every agent output
- Append-only, immutable JSON activity logging

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bangonpro.ai_engine.commands import (
    TimelineCommandValidationError,
    validate_timeline_command_batch,
)
from bangonpro.ai_engine.state import ProjectAnalysisState


class PipelineCancelledError(RuntimeError):
    """Raised when an AI production pipeline is cancelled by user or controller."""


class ApprovalRejectedError(RuntimeError):
    """Raised when a human approval checkpoint rejects an AI command batch."""


class StageDependencyError(ValueError):
    """Raised when the stage DAG has missing or cyclic dependencies."""


class CancellationToken:
    """Thread-safe cooperative cancellation token passed to every agent stage."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: str = ""

    def cancel(self, reason: str = "Cancelled by user") -> None:
        with self._lock:
            if not self._event.is_set():
                self._reason = reason
                self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def check(self, context: str = "") -> None:
        """Raise ``PipelineCancelledError`` immediately if cancellation was requested."""
        if self._event.is_set():
            suffix = f" during {context}" if context else ""
            raise PipelineCancelledError(
                f"AI pipeline cancelled{suffix}: {self.reason or 'cancelled'}"
            )


@dataclass(frozen=True)
class ActivityLogEntry:
    """Immutable audit record of a single orchestrator event."""

    seq: int
    timestamp_utc: str
    stage: str
    event: str
    attempt: int
    duration_sec: float
    state_version: int
    revision: int
    details: dict[str, Any] = field(default_factory=dict)


class ImmutableActivityLog:
    """Append-only activity log whose emitted records are frozen and tamper-evident."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[ActivityLogEntry] = []

    def append(
        self,
        *,
        stage: str,
        event: str,
        state: ProjectAnalysisState,
        attempt: int = 1,
        duration_sec: float = 0.0,
        details: dict[str, Any] | None = None,
    ) -> ActivityLogEntry:
        with self._lock:
            entry = ActivityLogEntry(
                seq=len(self._entries) + 1,
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                stage=stage,
                event=event,
                attempt=attempt,
                duration_sec=round(max(0.0, duration_sec), 4),
                state_version=state.state_version,
                revision=state.revision,
                details=dict(details or {}),
            )
            self._entries.append(entry)
            return entry

    def entries(self) -> tuple[ActivityLogEntry, ...]:
        """Return an immutable tuple snapshot of all log entries."""
        with self._lock:
            return tuple(self._entries)

    def to_list(self) -> list[dict[str, Any]]:
        return [asdict(e) for e in self.entries()]

    def write_json(self, path: str | Path) -> Path:
        out_path = Path(path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            json.dump(self.to_list(), fh, indent=2)
        return out_path


ProgressCallback = Callable[[str, float, str], None]
ApprovalCallback = Callable[[str, ProjectAnalysisState, dict[str, Any] | None], bool]
StageHandler = Callable[
    [ProjectAnalysisState, CancellationToken, Callable[[float, str], None]],
    dict[str, Any] | list[dict[str, Any]] | None,
]


@dataclass(frozen=True)
class StageSpec:
    """Declarative DAG stage registration for ``ProductionOrchestrator``."""

    name: str
    handler: StageHandler
    depends_on: tuple[str, ...] = ()
    max_retries: int = 1
    retry_delay_sec: float = 0.05
    requires_approval: bool = False
    description: str = ""


class ProductionOrchestrator:
    """DAG task scheduler governing Bang On Pro's specialist AI agents."""

    def __init__(self, *, stages: list[StageSpec] | None = None) -> None:
        self._stages: dict[str, StageSpec] = {}
        self._token = CancellationToken()
        self.activity_log = ImmutableActivityLog()
        if stages:
            for spec in stages:
                self.register_stage(spec)

    def register_stage(self, spec: StageSpec) -> None:
        if spec.name in self._stages:
            raise StageDependencyError(f"Duplicate stage registration: '{spec.name}'")
        self._stages[spec.name] = spec

    def cancel(self, reason: str = "User requested cancellation") -> None:
        """Request cooperative cancellation of the running pipeline."""
        self._token.cancel(reason)

    def is_cancelled(self) -> bool:
        return self._token.is_cancelled

    @property
    def cancellation_token(self) -> CancellationToken:
        return self._token

    def resolve_execution_order(self) -> list[StageSpec]:
        """Return a deterministic topological order of registered DAG stages."""
        for name, spec in self._stages.items():
            for dep in spec.depends_on:
                if dep not in self._stages:
                    raise StageDependencyError(
                        f"Stage '{name}' depends on unknown stage '{dep}'"
                    )

        in_degree: dict[str, int] = {
            name: len(spec.depends_on) for name, spec in self._stages.items()
        }
        dependents: dict[str, list[str]] = {name: [] for name in self._stages}
        for name, spec in self._stages.items():
            for dep in spec.depends_on:
                dependents[dep].append(name)

        # Preserve registration order among zero-in-degree nodes for determinism
        ready = deque(name for name in self._stages if in_degree[name] == 0)
        ordered: list[StageSpec] = []

        while ready:
            current = ready.popleft()
            ordered.append(self._stages[current])
            for nxt in dependents[current]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    ready.append(nxt)

        if len(ordered) != len(self._stages):
            cyclic = sorted(set(self._stages) - {s.name for s in ordered})
            raise StageDependencyError(
                f"Cyclic dependency detected among stages: {', '.join(cyclic)}"
            )

        return ordered

    def execute(
        self,
        state: ProjectAnalysisState,
        *,
        progress_callback: ProgressCallback | None = None,
        approval_callback: ApprovalCallback | None = None,
    ) -> ProjectAnalysisState:
        """Execute all DAG stages in topological order and persist audit artifacts."""
        ordered_stages = self.resolve_execution_order()
        output_dir = Path(state.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        pipeline_start = time.perf_counter()
        self.activity_log.append(
            stage="orchestrator",
            event="pipeline_started",
            state=state,
            details={
                "project_id": state.project_id,
                "sequence_id": state.sequence_id,
                "profile_id": state.profile.get("profile_id"),
                "stages": [s.name for s in ordered_stages],
                "dry_run": state.dry_run,
            },
        )

        total_stages = max(1, len(ordered_stages))

        try:
            for idx, spec in enumerate(ordered_stages):
                self._token.check(context=f"before stage '{spec.name}'")
                stage_start = time.perf_counter()

                def _report_progress(local_frac: float, msg: str, _idx: int = idx, _name: str = spec.name) -> None:
                    clamped = min(1.0, max(0.0, float(local_frac)))
                    overall = (_idx + clamped) / total_stages
                    if progress_callback is not None:
                        progress_callback(_name, round(overall, 4), msg)

                _report_progress(0.0, f"Starting {spec.name}")

                emitted: dict[str, Any] | list[dict[str, Any]] | None = None
                attempts_allowed = max(1, spec.max_retries + 1)
                last_exc: Exception | None = None

                for attempt in range(1, attempts_allowed + 1):
                    self._token.check(context=f"stage '{spec.name}' attempt {attempt}")
                    self.activity_log.append(
                        stage=spec.name,
                        event="stage_started" if attempt == 1 else "stage_retry",
                        state=state,
                        attempt=attempt,
                        details={"description": spec.description},
                    )
                    try:
                        emitted = spec.handler(state, self._token, _report_progress)
                        last_exc = None
                        break
                    except (
                        PipelineCancelledError,
                        ApprovalRejectedError,
                        TimelineCommandValidationError,
                    ):
                        raise
                    except Exception as exc:  # noqa: BLE001
                        last_exc = exc
                        self.activity_log.append(
                            stage=spec.name,
                            event="stage_attempt_failed",
                            state=state,
                            attempt=attempt,
                            duration_sec=time.perf_counter() - stage_start,
                            details={"error": str(exc), "error_type": type(exc).__name__},
                        )
                        if attempt < attempts_allowed:
                            if spec.retry_delay_sec > 0:
                                time.sleep(spec.retry_delay_sec)
                            continue
                        raise

                if last_exc is not None:
                    raise last_exc

                self._token.check(context=f"after stage '{spec.name}'")

                # Normalize and validate emitted Timeline Command API v1 batches
                batches: list[dict[str, Any]] = []
                if isinstance(emitted, dict):
                    batches = [emitted]
                elif isinstance(emitted, list):
                    batches = [b for b in emitted if b is not None]

                for batch in batches:
                    validate_timeline_command_batch(batch)
                    if batch["expected_revision"] != state.revision:
                        raise TimelineCommandValidationError(
                            f"Stage '{spec.name}' emitted expected_revision={batch['expected_revision']} "
                            f"but current state.revision={state.revision}"
                        )

                    needs_approval = bool(
                        spec.requires_approval
                        or batch["actor"].get("requires_approval", False)
                    )
                    if needs_approval and approval_callback is not None:
                        self.activity_log.append(
                            stage=spec.name,
                            event="approval_requested",
                            state=state,
                            details={
                                "request_id": batch["request_id"],
                                "operations": len(batch["operations"]),
                            },
                        )
                        approved = bool(approval_callback(spec.name, state, batch))
                        if not approved:
                            self.activity_log.append(
                                stage=spec.name,
                                event="approval_rejected",
                                state=state,
                                details={"request_id": batch["request_id"]},
                            )
                            raise ApprovalRejectedError(
                                f"Approval rejected at stage '{spec.name}' for batch '{batch['request_id']}'"
                            )
                        self.activity_log.append(
                            stage=spec.name,
                            event="approval_granted",
                            state=state,
                            details={"request_id": batch["request_id"]},
                        )

                    state.command_batches.append(batch)
                    state.revision += 1
                    self.activity_log.append(
                        stage=spec.name,
                        event="command_batch_validated",
                        state=state,
                        details={
                            "request_id": batch["request_id"],
                            "agent_id": batch["actor"]["agent_id"],
                            "operation_count": len(batch["operations"]),
                            "new_revision": state.revision,
                        },
                    )

                stage_elapsed = time.perf_counter() - stage_start
                state.stage_timings_sec[spec.name] = round(stage_elapsed, 4)
                state.bump_version(spec.name, f"Completed {spec.name} in {stage_elapsed:.3f}s")
                self.activity_log.append(
                    stage=spec.name,
                    event="stage_completed",
                    state=state,
                    duration_sec=stage_elapsed,
                    details={"batches_committed": len(batches)},
                )
                _report_progress(1.0, f"Completed {spec.name}")

            total_elapsed = time.perf_counter() - pipeline_start
            state.stage_timings_sec["total_pipeline"] = round(total_elapsed, 4)
            self.activity_log.append(
                stage="orchestrator",
                event="pipeline_completed",
                state=state,
                duration_sec=total_elapsed,
                details={
                    "total_batches": len(state.command_batches),
                    "final_revision": state.revision,
                },
            )
            self._persist_audit_files(state)
            return state

        except PipelineCancelledError as exc:
            self.activity_log.append(
                stage="orchestrator",
                event="pipeline_cancelled",
                state=state,
                duration_sec=time.perf_counter() - pipeline_start,
                details={"reason": str(exc)},
            )
            self._persist_audit_files(state)
            raise
        except Exception as exc:
            self.activity_log.append(
                stage="orchestrator",
                event="pipeline_failed",
                state=state,
                duration_sec=time.perf_counter() - pipeline_start,
                details={"error": str(exc), "error_type": type(exc).__name__},
            )
            self._persist_audit_files(state)
            raise

    def _persist_audit_files(self, state: ProjectAnalysisState) -> None:
        out_dir = Path(state.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        log_path = self.activity_log.write_json(out_dir / "activity_log.json")
        state.artifacts["activity_log_json"] = str(log_path)

        cmds_path = out_dir / "timeline_commands.json"
        with cmds_path.open("w", encoding="utf-8") as fh:
            json.dump(
                {
                    "api_version": "1.0",
                    "project_id": state.project_id,
                    "sequence_id": state.sequence_id,
                    "final_revision": state.revision,
                    "batches": state.command_batches,
                },
                fh,
                indent=2,
            )
        state.artifacts["timeline_commands_json"] = str(cmds_path)

        state_path = out_dir / "analysis_state.json"
        with state_path.open("w", encoding="utf-8") as fh:
            json.dump(state.to_dict(), fh, indent=2)
        state.artifacts["analysis_state_json"] = str(state_path)
