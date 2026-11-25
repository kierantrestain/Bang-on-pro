"""Permanent AI Production Department: specialist agent registry & capability matrix.

These are the *production* agents that operate inside Bang On Pro (not the
development agents that build the application). Each agent declares its
responsibilities, dependencies, the shared artefacts it consumes/produces, its
compute tier (deterministic / local neural / optional external LLM) and how its
output relates to the native timeline, so the Executive Editing Agent can
activate only the agents a job needs.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Automation modes (Section 3 of the V1.2 brief)
MODE_MANUAL = "manual"
MODE_ASSISTED = "assisted"
MODE_AUTONOMOUS_FIRST_CUT = "autonomous_first_cut"
MODE_FULL_PRODUCTION_DRAFT = "full_production_draft"
AUTOMATION_MODES: tuple[str, ...] = (
    MODE_MANUAL,
    MODE_ASSISTED,
    MODE_AUTONOMOUS_FIRST_CUT,
    MODE_FULL_PRODUCTION_DRAFT,
)
AUTONOMOUS_MODES = frozenset({MODE_AUTONOMOUS_FIRST_CUT, MODE_FULL_PRODUCTION_DRAFT})

# Output kinds drive dependency-aware rejection semantics
KIND_PLANNING = "planning"  # no timeline mutations
KIND_EDITORIAL = "editorial"  # changes timeline structure
KIND_FINISHING = "finishing"  # attaches to clips placed by editorial agents
KIND_ANNOTATION = "annotation"  # markers / review notes only
KIND_DELIVERY = "delivery"  # project emission & deliverables

TIER_DETERMINISTIC = "deterministic"
TIER_LOCAL_NEURAL = "local_neural_optional"
TIER_EXTERNAL_LLM = "external_llm_optional"


@dataclass(frozen=True)
class AgentDefinition:
    agent_id: str
    display_name: str
    responsibilities: str
    depends_on: tuple[str, ...]
    kind: str
    compute_tier: str
    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    engine: str = ""
    version: str = "1.2.0"
    implementation_notes: str = ""
    limitations: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["depends_on"] = list(self.depends_on)
        d["consumes"] = list(self.consumes)
        d["produces"] = list(self.produces)
        d["limitations"] = list(self.limitations)
        return d


AGENT_DEFINITIONS: tuple[AgentDefinition, ...] = (
    AgentDefinition(
        agent_id="executive",
        display_name="Executive Editing Agent",
        responsibilities="Interpret the brief, choose agents for the job, plan, delegate and verify the finished draft.",
        depends_on=(),
        kind=KIND_PLANNING,
        compute_tier=TIER_EXTERNAL_LLM,
        consumes=("brief", "profile", "project_context"),
        produces=("production_plan", "production_summary"),
        engine="planner.interpret_instruction + department activation rules",
        implementation_notes="Deterministic rule-based brief parsing by default; an LLM provider is used only when configured, consented and within budget.",
    ),
    AgentDefinition(
        agent_id="learning",
        display_name="Learning Agent",
        responsibilities="Apply human-approved production preferences; record corrections and propose profile improvements.",
        depends_on=("executive",),
        kind=KIND_PLANNING,
        compute_tier=TIER_DETERMINISTIC,
        consumes=("profile", "approved_preferences", "editor_decisions"),
        produces=("effective_profile", "preference_suggestions"),
        engine="department.learning.ProfilePreferenceStore",
        implementation_notes="Never modifies code, model weights or approval rules; suggestions require explicit approval and are versioned with rollback.",
    ),
    AgentDefinition(
        agent_id="media_ingest",
        display_name="Media Ingest Agent",
        responsibilities="Inspect, label and validate source media; detect missing media, mixed frame rates and insufficient storage.",
        depends_on=("learning",),
        kind=KIND_PLANNING,
        compute_tier=TIER_DETERMINISTIC,
        consumes=("input_media",),
        produces=("media_inventory", "sha256_cache", "probe_cache"),
        engine="ingest_sync.probe_media_file + compute_file_sha256 (shared memo + persistent cache)",
    ),
    AgentDefinition(
        agent_id="synchronisation",
        display_name="Synchronisation Agent",
        responsibilities="Align cameras, microphones and independent recorders; import media into the sequence.",
        depends_on=("media_ingest",),
        kind=KIND_EDITORIAL,
        compute_tier=TIER_DETERMINISTIC,
        consumes=("media_inventory",),
        produces=("sync_result", "import_media_ops"),
        engine="ingest_sync.run_ingest_sync_agent (GCC-PHAT cross-correlation)",
    ),
    AgentDefinition(
        agent_id="editorial_intelligence",
        display_name="Editorial Intelligence Agent",
        responsibilities="Transcribe, diarise and understand speech, sections and important moments (highlights).",
        depends_on=("synchronisation",),
        kind=KIND_ANNOTATION,
        compute_tier=TIER_LOCAL_NEURAL,
        consumes=("sync_result",),
        produces=("transcript", "story_sections", "highlights", "silence_intervals"),
        engine="transcribe_diarise.run_transcribe_diarise_agent (faster-whisper when installed, else VAD) + highlight scoring",
        limitations=(
            "Without a local Whisper model the transcript contains VAD speech regions only (no words); this is reported explicitly.",
        ),
    ),
    AgentDefinition(
        agent_id="multicam_director",
        display_name="Multicamera Director",
        responsibilities="Resolve speaker-to-camera coverage, reaction, wide-reset and preferred-angle rules for the cut.",
        depends_on=("editorial_intelligence",),
        kind=KIND_PLANNING,
        compute_tier=TIER_DETERMINISTIC,
        consumes=("transcript", "media_inventory", "effective_profile"),
        produces=("camera_plan",),
        engine="department camera planner feeding rough_cut_multicam switching engine",
        implementation_notes="Angle selection is executed by the shared multicam switching engine using the director's camera plan; coverage is audited by QC.",
    ),
    AgentDefinition(
        agent_id="rough_cut",
        display_name="Rough Cut Agent",
        responsibilities="Assemble the edit, remove setup delays, dead air and silences, and apply pacing rules.",
        depends_on=("multicam_director",),
        kind=KIND_EDITORIAL,
        compute_tier=TIER_DETERMINISTIC,
        consumes=("camera_plan", "silence_intervals", "transcript"),
        produces=("rough_cut_shots", "place_clip_ops"),
        engine="rough_cut_multicam.run_rough_cut_multicam_agent",
    ),
    AgentDefinition(
        agent_id="audio_engineer",
        display_name="Audio Engineer",
        responsibilities="Clean dialogue, balance microphones, denoise and normalise loudness.",
        depends_on=("rough_cut",),
        kind=KIND_FINISHING,
        compute_tier=TIER_DETERMINISTIC,
        consumes=("rough_cut_shots", "per_camera_gain_trim_db"),
        produces=("audio_filter_ops",),
        engine="audio_graphics_qc.prepare_audio_restoration + emit_audio_ops (MLT/FFmpeg loudnorm, afftdn/RNNoise)",
    ),
    AgentDefinition(
        agent_id="colourist",
        display_name="Colourist",
        responsibilities="Match cameras, correct exposure and apply approved colour treatments (LUTs).",
        depends_on=("rough_cut",),
        kind=KIND_FINISHING,
        compute_tier=TIER_DETERMINISTIC,
        consumes=("media_inventory", "rough_cut_shots", "colour_rules"),
        produces=("colour_effect_ops", "colour_report"),
        engine="department frame statistics -> native lift_gamma_gain (+ avfilter.lut3d) effects",
        limitations=("Global per-camera matching from sampled frames; no shot-by-shot secondary grading.",),
    ),
    AgentDefinition(
        agent_id="captions",
        display_name="Captions Agent",
        responsibilities="Generate, correct and format subtitles on the edited timeline.",
        depends_on=("rough_cut",),
        kind=KIND_FINISHING,
        compute_tier=TIER_LOCAL_NEURAL,
        consumes=("transcript", "rough_cut_shots", "caption_style"),
        produces=("captions_srt", "captions_ass", "caption_ops"),
        engine="audio_graphics_qc.prepare_captions + emit_caption_ops",
        limitations=("Refuses to publish placeholder captions when only a VAD (non-neural) transcript exists.",),
    ),
    AgentDefinition(
        agent_id="graphics",
        display_name="Graphics Agent",
        responsibilities="Apply intros, outros, title cards, branding and lower thirds.",
        depends_on=("rough_cut",),
        kind=KIND_FINISHING,
        compute_tier=TIER_DETERMINISTIC,
        consumes=("transcript", "graphics_rules"),
        produces=("lower_third_ops", "title_card_ops"),
        engine="audio_graphics_qc.prepare_lower_thirds + native kdenlivetitle generator",
    ),
    AgentDefinition(
        agent_id="vfx",
        display_name="VFX Agent",
        responsibilities="Apply masks, segmentation, background blur/replacement and selective effects where requested.",
        depends_on=("rough_cut",),
        kind=KIND_FINISHING,
        compute_tier=TIER_LOCAL_NEURAL,
        consumes=("rough_cut_shots", "vfx_rules"),
        produces=("mask_ops", "matte_files"),
        engine="roto_studio.mask_engine.segment_frame_detailed (SAM2/MobileSAM when installed, else labelled fallback)",
        limitations=(
            "Department VFX applies a static per-camera matte from a representative frame; temporal propagation/tracking is done in Roto Studio.",
        ),
    ),
    AgentDefinition(
        agent_id="quality_control",
        display_name="Quality Control Agent",
        responsibilities="Detect audio, visual, continuity, synchronisation, caption and editorial problems.",
        depends_on=("audio_engineer", "colourist", "captions", "graphics", "vfx"),
        kind=KIND_ANNOTATION,
        compute_tier=TIER_DETERMINISTIC,
        consumes=("rough_cut_shots", "captions", "sync_result", "media_inventory"),
        produces=("qc_report", "qc_marker_ops"),
        engine="audio_graphics_qc.run_technical_qc + department editorial/continuity checks",
    ),
    AgentDefinition(
        agent_id="distribution",
        display_name="Distribution Agent",
        responsibilities="Emit the editable project, render masters, highlights and social versions with verification.",
        depends_on=("quality_control",),
        kind=KIND_DELIVERY,
        compute_tier=TIER_DETERMINISTIC,
        consumes=("all_batches", "highlights", "distribution_rules"),
        produces=("kdenlive_project", "otio_timeline", "master_render", "social_versions", "delivery_manifest"),
        engine="project_builder.run_project_emit_export_stage + ffmpeg derivative renders",
        implementation_notes="Never publishes or uploads; publishing is an approval-gated action outside autonomous modes.",
    ),
)

AGENTS_BY_ID: dict[str, AgentDefinition] = {a.agent_id: a for a in AGENT_DEFINITIONS}
AGENT_ORDER: tuple[str, ...] = tuple(a.agent_id for a in AGENT_DEFINITIONS)


def get_agent(agent_id: str) -> AgentDefinition:
    try:
        return AGENTS_BY_ID[agent_id]
    except KeyError as exc:
        raise KeyError(f"Unknown production agent '{agent_id}'. Known: {', '.join(AGENT_ORDER)}") from exc


def transitive_dependents(agent_id: str) -> list[str]:
    """Return all agents that (directly or indirectly) depend on ``agent_id``, in run order."""
    found: set[str] = set()
    frontier = [agent_id]
    while frontier:
        cur = frontier.pop()
        for a in AGENT_DEFINITIONS:
            if cur in a.depends_on and a.agent_id not in found:
                found.add(a.agent_id)
                frontier.append(a.agent_id)
    return [a for a in AGENT_ORDER if a in found]


def capability_matrix() -> list[dict[str, Any]]:
    return [a.to_dict() for a in AGENT_DEFINITIONS]
