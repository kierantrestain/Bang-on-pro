"""High-level Bang On Pro AI Production Pipeline entrypoint.

Wires all specialist agents into a validated DAG inside ``ProductionOrchestrator``:
    ingest_sync -> transcribe_diarise -> rough_cut_multicam -> audio_graphics_qc -> project_emit_export

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from bangonpro.ai_engine.agents.audio_graphics_qc import run_audio_graphics_qc_agent
from bangonpro.ai_engine.agents.ingest_sync import run_ingest_sync_agent
from bangonpro.ai_engine.agents.rough_cut_multicam import run_rough_cut_multicam_agent
from bangonpro.ai_engine.agents.transcribe_diarise import run_transcribe_diarise_agent
from bangonpro.ai_engine.intent import parse_production_instruction
from bangonpro.ai_engine.orchestrator import (
    ApprovalCallback,
    ProductionOrchestrator,
    ProgressCallback,
    StageSpec,
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
                description="Run VAD, per-camera speaker diarisation, setup-delay & section detection",
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
    inputs: dict[str, str | Path] | list[str | Path],
    output_dir: str | Path,
    profile_ref: str | Path | None = None,
    instruction: str = "",
    export_video: bool | None = None,
    dry_run: bool = False,
    project_id: str | None = None,
    sequence_id: str = "seq_main",
    rnnoise_model_path: str | None = None,
    require_edit_approval: bool = False,
    progress_callback: ProgressCallback | None = None,
    approval_callback: ApprovalCallback | None = None,
    orchestrator: ProductionOrchestrator | None = None,
) -> tuple[ProjectAnalysisState, ProductionOrchestrator]:
    """Execute the complete Bang On Pro AI production pipeline end-to-end."""
    intent = parse_production_instruction(instruction, export_override=export_video)
    chosen_profile_ref = profile_ref or intent.profile_hint or "podcast_multicam"
    profile = load_show_profile(chosen_profile_ref)

    state = ProjectAnalysisState(
        project_id=project_id or f"bop_{uuid.uuid4().hex[:10]}",
        sequence_id=sequence_id,
        profile=profile,
        output_dir=str(Path(output_dir).resolve()),
        instruction=instruction,
        intent=intent,
        dry_run=dry_run,
        rnnoise_model_path=rnnoise_model_path,
    )
    state.input_camera_files = map_inputs_to_profile_cameras(inputs, profile)

    orch = orchestrator or build_default_orchestrator(
        require_edit_approval=require_edit_approval
    )
    orch.execute(
        state,
        progress_callback=progress_callback,
        approval_callback=approval_callback,
    )
    return state, orch
