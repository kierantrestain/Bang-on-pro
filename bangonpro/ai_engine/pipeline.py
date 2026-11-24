"""High-level Bang On Pro AI Production Pipeline entrypoint.

Wires all specialist agents into a validated DAG inside ``ProductionOrchestrator``:
    ingest_sync -> transcribe_diarise -> rough_cut_multicam -> audio_graphics_qc -> project_emit_export

Integrates the 4-stage AI Command Planner (``planner.py``) so instructions can
operate either on raw media ``inputs`` or directly on an active ``.kdenlive`` /
``timeline_state.json`` project context without forcing media re-import, with
strict editorial scoping and safe clarification handling.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from bangonpro.ai_engine.agents.audio_graphics_qc import run_audio_graphics_qc_agent
from bangonpro.ai_engine.agents.ingest_sync import run_ingest_sync_agent
from bangonpro.ai_engine.agents.rough_cut_multicam import run_rough_cut_multicam_agent
from bangonpro.ai_engine.agents.transcribe_diarise import run_transcribe_diarise_agent
from bangonpro.ai_engine.orchestrator import (
    ApprovalCallback,
    ProductionOrchestrator,
    ProgressCallback,
    StageSpec,
)
from bangonpro.ai_engine.planner import (
    LlmInterpreterFn,
    build_edit_proposal,
    ingest_project_context,
    interpret_instruction,
    validate_edit_proposal,
)
from bangonpro.ai_engine.project_builder import run_project_emit_export_stage
from bangonpro.ai_engine.state import ProjectAnalysisState
from bangonpro.show_profiles.loader import load_show_profile


def build_default_orchestrator(*, require_edit_approval: bool = False) -> ProductionOrchestrator:
    """Create a ``ProductionOrchestrator`` registered with all 5 production DAG stages."""
    return ProductionOrchestrator(
        stages=[
            StageSpec(
                name="ingest_sync",
                handler=run_ingest_sync_agent,
                depends_on=(),
                max_retries=1,
                description="Verify media streams via ffprobe, SHA-256 hash, and GCC-PHAT multicam sync",
            ),
            StageSpec(
                name="transcribe_diarise",
                handler=run_transcribe_diarise_agent,
                depends_on=("ingest_sync",),
                max_retries=1,
                description="Run optional Whisper ASR or honest DSP VAD, per-camera diarisation & section detection",
            ),
            StageSpec(
                name="rough_cut_multicam",
                handler=run_rough_cut_multicam_agent,
                depends_on=("transcribe_diarise",),
                max_retries=1,
                requires_approval=require_edit_approval,
                description="Detect scenes and assemble non-destructive speaker-aware multicam rough cut",
            ),
            StageSpec(
                name="audio_graphics_qc",
                handler=run_audio_graphics_qc_agent,
                depends_on=("rough_cut_multicam",),
                max_retries=1,
                description="Build audio restoration chain, .srt/.ass captions, lower thirds, and run QC",
            ),
            StageSpec(
                name="project_emit_export",
                handler=run_project_emit_export_stage,
                depends_on=("audio_graphics_qc",),
                max_retries=1,
                description="Emit Kdenlive .kdenlive + OpenTimelineIO .otio and optionally render via melt",
            ),
        ]
    )


def map_inputs_to_profile_cameras(
    inputs: dict[str, str | Path] | list[str | Path],
    profile: dict[str, Any],
) -> dict[str, str]:
    """Map user input files to camera IDs defined in the Show Profile."""
    if isinstance(inputs, dict):
        return {str(cam_id): str(Path(p).resolve()) for cam_id, p in inputs.items()}

    profile_cam_ids = [cam["camera_id"] for cam in profile.get("cameras", [])]
    mapped: dict[str, str] = {}
    for idx, raw_item in enumerate(inputs):
        raw_str = str(raw_item)
        if "=" in raw_str and not Path(raw_str).exists():
            cam_id, path_part = raw_str.split("=", 1)
            mapped[cam_id.strip()] = str(Path(path_part.strip()).resolve())
        else:
            cam_id = profile_cam_ids[idx] if idx < len(profile_cam_ids) else f"cam_{idx + 1}"
            mapped[cam_id] = str(Path(raw_str).resolve())
    return mapped


def run_production_pipeline(
    *,
    inputs: dict[str, str | Path] | list[str | Path] | None = None,
    output_dir: str | Path,
    profile_ref: str | Path | None = None,
    instruction: str = "",
    export_video: bool | None = None,
    dry_run: bool = False,
    project_id: str | None = None,
    sequence_id: str | None = None,
    rnnoise_model_path: str | None = None,
    require_edit_approval: bool = False,
    provider_config: dict[str, Any] | None = None,
    progress_callback: ProgressCallback | None = None,
    approval_callback: ApprovalCallback | None = None,
    orchestrator: ProductionOrchestrator | None = None,
    kdenlive_project: str | Path | None = None,
    project_state: str | Path | None = None,
    plan_only: bool = False,
    allow_default_empty: bool = False,
    model_dir: str | Path | None = None,
    asr_model_id: str | None = None,
    llm_interpreter: LlmInterpreterFn | None = None,
) -> tuple[ProjectAnalysisState, ProductionOrchestrator]:
    """Execute or plan the Bang On Pro AI production pipeline end-to-end."""
    # Stage 2: Active project context ingestion (when editing existing .kdenlive / timeline_state.json)
    context = ingest_project_context(
        kdenlive_project=kdenlive_project,
        project_state=project_state,
        inputs=inputs,
        project_id=project_id or f"bop_{uuid.uuid4().hex[:10]}",
        sequence_id=sequence_id or "seq_main",
    )

    # Stage 1: Instruction interpretation (enforces ExternalConsentError for external providers)
    interpretation = interpret_instruction(
        instruction,
        context=context,
        export_override=export_video,
        allow_default_empty=allow_default_empty,
        provider_config=provider_config,
        llm_interpreter=llm_interpreter,
    )
    intent = interpretation.intent

    chosen_profile_ref = (
        profile_ref
        or intent.profile_hint
        or context.profile_id
        or "podcast_multicam"
    )
    profile = load_show_profile(chosen_profile_ref)

    orch = orchestrator or build_default_orchestrator(
        require_edit_approval=require_edit_approval
    )

    resolved_project_id = project_id or context.project_id or f"bop_{uuid.uuid4().hex[:10]}"
    resolved_sequence_id = sequence_id or context.sequence_id or "seq_main"

    state = ProjectAnalysisState(
        project_id=resolved_project_id,
        sequence_id=resolved_sequence_id,
        profile=profile,
        output_dir=str(Path(output_dir).resolve()),
        instruction=instruction,
        intent=intent,
        dry_run=dry_run or plan_only,
        rnnoise_model_path=rnnoise_model_path,
    )
    state.revision = context.revision
    state.metadata["project_context"] = context.summary()
    state.metadata["interpreter_backend"] = interpretation.interpreter_backend
    if model_dir is not None:
        state.metadata["model_dir"] = str(Path(model_dir).expanduser().resolve())
    if asr_model_id is not None:
        state.metadata["asr_model_id"] = asr_model_id
    if provider_config:
        state.metadata["provider_config"] = {
            "provider_id": str(provider_config.get("provider_id", "local_deterministic")),
            "consent_granted": bool(provider_config.get("consent_granted", False)),
        }

    out_path = Path(state.output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # Safe clarification gate: NEVER execute mutating stages on unknown/ambiguous/empty prompts
    if intent.needs_clarification:
        state.metadata["execution_status"] = "needs_clarification"
        state.metadata["clarification_question"] = intent.clarification_question
        orch.activity_log.append(
            stage="planner",
            event="clarification_required",
            state=state,
            details={
                "instruction": instruction,
                "clarification_question": intent.clarification_question,
            },
        )
        orch._persist_audit_files(state)
        return state, orch

    # Plan-only mode: emit validated timeline_command_v1 proposal without running media render
    if plan_only:
        proposal = build_edit_proposal(
            interpretation,
            context,
            profile,
            expected_revision=state.revision,
            dry_run=True,
            rnnoise_model_path=rnnoise_model_path,
        )
        plan_report = validate_edit_proposal(proposal, interpretation, context)
        state.metadata["execution_status"] = plan_report.status
        state.metadata["plan_report"] = plan_report.to_dict()
        if plan_report.valid and plan_report.command_batch is not None:
            state.command_batches.append(plan_report.command_batch)
            state.revision += 1

        plan_file = out_path / "edit_plan.json"
        plan_file.write_text(json.dumps(plan_report.to_dict(), indent=2) + "\n", encoding="utf-8")
        state.artifacts["edit_plan_json"] = str(plan_file)
        orch.activity_log.append(
            stage="planner",
            event="edit_plan_generated",
            state=state,
            details={
                "status": plan_report.status,
                "scope": plan_report.scope,
                "operation_count": plan_report.operation_count,
                "operation_types": plan_report.operation_types,
            },
        )
        orch._persist_audit_files(state)
        return state, orch

    # Resolve media inputs from explicit inputs OR active project context (no re-import required)
    if inputs:
        state.input_camera_files = map_inputs_to_profile_cameras(inputs, profile)
    else:
        derived_inputs = context.extract_existing_media_inputs()
        if derived_inputs:
            derived_map: dict[str, str | Path] = dict(derived_inputs)
            state.input_camera_files = map_inputs_to_profile_cameras(derived_map, profile)

    # If operating purely on a context snapshot with no local media files on disk,
    # execute the validated context-aware proposal cleanly without failing on missing --inputs.
    if not state.input_camera_files and context.source_kind in ("kdenlive_xml", "timeline_state_json"):
        proposal = build_edit_proposal(
            interpretation,
            context,
            profile,
            expected_revision=state.revision,
            dry_run=dry_run,
            rnnoise_model_path=rnnoise_model_path,
        )
        plan_report = validate_edit_proposal(proposal, interpretation, context)
        state.metadata["execution_status"] = "executed_context_plan" if plan_report.valid else "validation_failed"
        state.metadata["plan_report"] = plan_report.to_dict()
        if plan_report.valid and plan_report.command_batch is not None:
            state.command_batches.append(plan_report.command_batch)
            state.revision += 1
        plan_file = out_path / "edit_plan.json"
        plan_file.write_text(json.dumps(plan_report.to_dict(), indent=2) + "\n", encoding="utf-8")
        state.artifacts["edit_plan_json"] = str(plan_file)
        orch.activity_log.append(
            stage="planner",
            event="context_plan_executed",
            state=state,
            details={
                "source_kind": context.source_kind,
                "operation_count": plan_report.operation_count,
            },
        )
        orch._persist_audit_files(state)
        return state, orch

    if not state.input_camera_files:
        raise ValueError(
            "No input media files available. Provide --inputs or pass an active "
            "--kdenlive-project / --project-state containing media."
        )

    orch.execute(
        state,
        progress_callback=progress_callback,
        approval_callback=approval_callback,
    )
    state.metadata["execution_status"] = "executed"

    # Guarantee editable transcript.json artifact is emitted when transcription/rough-cut/captions ran
    if "transcript_json" not in state.artifacts and (
        intent.transcribe or intent.add_captions or intent.build_rough_cut
    ):
        from bangonpro.ai_engine.transcript import build_transcript_document, save_transcript

        doc = state.transcript_document or build_transcript_document(state)
        state.transcript_document = doc
        transcript_path = Path(state.output_dir) / "transcript.json"
        save_transcript(doc, transcript_path)
        state.artifacts["transcript_json"] = str(transcript_path)

    return state, orch
