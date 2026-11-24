"""Bang On Pro Autonomous Multi-Agent AI Production Engine.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from bangonpro.ai_engine.commands import (
    TimelineCommandBuilder,
    TimelineCommandValidationError,
    validate_timeline_command_batch,
)
from bangonpro.ai_engine.intent import parse_production_instruction
from bangonpro.ai_engine.orchestrator import (
    ActivityLogEntry,
    ApprovalRejectedError,
    CancellationToken,
    ImmutableActivityLog,
    PipelineCancelledError,
    ProductionOrchestrator,
    StageDependencyError,
    StageSpec,
)
from bangonpro.ai_engine.pipeline import (
    build_default_orchestrator,
    map_inputs_to_profile_cameras,
    run_production_pipeline,
)
from bangonpro.ai_engine.project_builder import (
    CompiledTimeline,
    compile_command_batches,
    emit_kdenlive_xml,
    emit_otio_timeline,
    render_kdenlive_project_with_melt,
)
from bangonpro.ai_engine.state import ProjectAnalysisState

__all__ = [
    "ActivityLogEntry",
    "ApprovalRejectedError",
    "CancellationToken",
    "CompiledTimeline",
    "ImmutableActivityLog",
    "PipelineCancelledError",
    "ProductionOrchestrator",
    "ProjectAnalysisState",
    "StageDependencyError",
    "StageSpec",
    "TimelineCommandBuilder",
    "TimelineCommandValidationError",
    "build_default_orchestrator",
    "compile_command_batches",
    "emit_kdenlive_xml",
    "emit_otio_timeline",
    "map_inputs_to_profile_cameras",
    "parse_production_instruction",
    "render_kdenlive_project_with_melt",
    "run_production_pipeline",
    "validate_timeline_command_batch",
]
