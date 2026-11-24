"""Structured shared metadata store for Bang On Pro AI Production Engine.

All AI agents communicate exclusively through typed metadata in
``ProjectAnalysisState`` and emit validated Timeline Command API v1 payloads.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class TimeInterval:
    """Closed-open time interval in seconds on the synchronised master timeline."""

    start_sec: float
    end_sec: float
    label: str = ""
    confidence: float = 1.0

    @property
    def duration_sec(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)


@dataclass
class MediaAsset:
    """Verified, strictly read-only source media asset metadata."""

    media_ref: str
    camera_id: str
    camera_role: str
    speaker_label: str | None
    file_path: str
    sha256: str
    file_size_bytes: int
    duration_sec: float
    width: int
    height: int
    fps_num: int
    fps_den: int
    video_codec: str
    audio_codec: str | None
    audio_sample_rate: int
    audio_channels: int
    priority: int = 1
    verified: bool = True

    @property
    def fps(self) -> float:
        return float(self.fps_num) / float(self.fps_den) if self.fps_den else 25.0


@dataclass
class SyncResult:
    """Multicamera audio cross-correlation synchronisation measurements."""

    reference_camera_id: str
    method: str
    sample_rate: int
    offsets_sec: dict[str, float] = field(default_factory=dict)
    offsets_project_frames: dict[str, int] = field(default_factory=dict)
    offsets_native_frames: dict[str, int] = field(default_factory=dict)
    confidences: dict[str, float] = field(default_factory=dict)
    peak_snr: dict[str, float] = field(default_factory=dict)
    common_duration_sec: float = 0.0
    warnings: list[str] = field(default_factory=list)


@dataclass
class TranscriptSegment:
    """Timestamped speaker-diarised transcript segment on the master sync timeline."""

    segment_id: str
    start_sec: float
    end_sec: float
    speaker_id: str
    camera_id: str
    text: str
    confidence: float
    is_low_confidence: bool
    energy_db: float
    spectral_centroid_hz: float = 0.0
    section_label: str = "Main"
    backend: str = "deterministic_multimic_vad"
    speaker_name: str = ""
    is_overlap: bool = False
    overlap_speakers: list[str] = field(default_factory=list)
    is_filler_or_silence: bool = False
    deleted: bool = False
    words: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.speaker_name:
            self.speaker_name = self.speaker_id


@dataclass
class StorySection:
    """Detected editorial section / topic block on the master sync timeline."""

    section_id: str
    label: str
    start_sec: float
    end_sec: float
    dominant_speaker: str
    confidence: float = 0.85


@dataclass
class EditShot:
    """Single non-destructive multicam edit decision on the output timeline."""

    shot_id: str
    camera_id: str
    media_ref: str
    audio_media_ref: str
    source_in_sec: float
    source_out_sec: float
    audio_source_in_sec: float
    audio_source_out_sec: float
    timeline_in_sec: float
    timeline_out_sec: float
    source_in_frame: int
    source_out_frame: int
    audio_source_in_frame: int
    audio_source_out_frame: int
    timeline_in_frame: int
    timeline_out_frame: int
    master_sync_in_sec: float
    master_sync_out_sec: float
    speaker_id: str
    reason: str
    confidence: float
    needs_review: bool = False


@dataclass
class AudioFilterSpec:
    """Declarative MLT & FFmpeg audio restoration filter specification."""

    filter_id: str
    mlt_service: str
    mlt_params: dict[str, Any]
    ffmpeg_filter: str
    track_id: str = "A1"
    enabled: bool = True
    rationale: str = ""


@dataclass
class CaptionCue:
    """Edited-timeline caption cue."""

    index: int
    start_sec: float
    end_sec: float
    speaker_id: str
    text: str
    confidence: float = 1.0


@dataclass
class LowerThirdSpec:
    """Lower-third graphic specification on the edited timeline."""

    graphic_id: str
    title: str
    subtitle: str
    start_sec: float
    duration_sec: float
    start_frame: int
    out_frame: int
    accent_hex: str
    kdenlive_title_xml: str


@dataclass
class ReviewMarker:
    """Editorial review, chapter, or QC marker on the edited timeline."""

    marker_id: str
    timeline_sec: float
    timeline_frame: int
    label: str
    category: str  # "review" | "section" | "qc_warning" | "qc_error" | "setup_trim"
    confidence: float
    rationale: str


@dataclass
class QCIssue:
    """Single technical QC finding."""

    code: str
    severity: str  # "info" | "warning" | "error"
    message: str
    timeline_sec: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class QCReport:
    """Technical quality control summary for an assembled timeline."""

    passed: bool = True
    issues: list[QCIssue] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProductionIntent:
    """Parsed intent from natural-language user instructions."""

    raw_instruction: str = ""
    sync_cameras: bool = True
    remove_setup_delay: bool = True
    remove_silence: bool = True
    build_rough_cut: bool = True
    multicam_switch: bool = True
    restore_audio: bool = True
    add_captions: bool = True
    add_lower_thirds: bool = True
    run_qc: bool = True
    export_video: bool = False
    profile_hint: str | None = None
    parsed_actions: list[str] = field(default_factory=list)


class ProjectAnalysisState:
    """Versioned shared metadata store passed across AI production stages.

    Every stage records structured metadata and commits a new version so the
    orchestrator can snapshot progress, emit immutable logs, and guarantee
    traceability from raw media analysis to Timeline Command API v1 batches.
    """

    def __init__(
        self,
        project_id: str,
        sequence_id: str,
        profile: dict[str, Any],
        output_dir: str,
        instruction: str = "",
        intent: ProductionIntent | None = None,
        dry_run: bool = False,
        rnnoise_model_path: str | None = None,
    ) -> None:
        self.project_id: str = project_id
        self.sequence_id: str = sequence_id
        self.profile: dict[str, Any] = copy.deepcopy(profile)
        self.output_dir: str = output_dir
        self.instruction: str = instruction
        self.intent: ProductionIntent = intent or ProductionIntent(raw_instruction=instruction)
        self.dry_run: bool = dry_run
        self.rnnoise_model_path: str | None = rnnoise_model_path

        # Monotonic timeline revision & state version counters
        self.revision: int = 0
        self.state_version: int = 0
        self.version_history: list[dict[str, Any]] = []

        # Structured stage outputs
        self.input_camera_files: dict[str, str] = {}
        self.media_assets: dict[str, MediaAsset] = {}
        self.missing_media: list[dict[str, Any]] = []
        self.sync_result: SyncResult | None = None

        # Transient in-memory PCM cache (16 kHz mono float32, excluded from JSON)
        self.audio_pcm_cache: dict[str, Any] = {}
        self.audio_sample_rate: int = 16000

        self.transcript_backend: str = "deterministic_multimic_vad"
        self.transcript_segments: list[TranscriptSegment] = []
        self.transcript_document: dict[str, Any] | None = None
        self.silence_intervals: list[TimeInterval] = []
        self.interruption_intervals: list[TimeInterval] = []
        self.setup_delay_sec: float = 0.0
        self.story_sections: list[StorySection] = []

        self.scene_cuts_sec: list[float] = []
        self.kept_master_intervals: list[TimeInterval] = []
        self.rough_cut_shots: list[EditShot] = []

        self.audio_filters: list[AudioFilterSpec] = []
        self.ffmpeg_audio_filtergraph: str = ""
        self.captions: list[CaptionCue] = []
        self.lower_thirds: list[LowerThirdSpec] = []
        self.qc_report: QCReport = QCReport()
        self.review_markers: list[ReviewMarker] = []

        # Validated Timeline Command API v1 batches emitted by agents
        self.command_batches: list[dict[str, Any]] = []
        self.artifacts: dict[str, str] = {}
        self.stage_timings_sec: dict[str, float] = {}
        self.metadata: dict[str, Any] = {}

    @property
    def fps_num(self) -> int:
        return int(self.profile["video_spec"]["fps_num"])

    @property
    def fps_den(self) -> int:
        return int(self.profile["video_spec"]["fps_den"])

    @property
    def fps(self) -> float:
        return float(self.fps_num) / float(self.fps_den)

    def sec_to_frame(self, sec: float) -> int:
        return max(0, int(round(sec * self.fps)))

    def frame_to_sec(self, frame: int) -> float:
        return float(frame) / self.fps

    def bump_version(self, stage: str, summary: str) -> int:
        self.state_version += 1
        self.version_history.append(
            {
                "version": self.state_version,
                "stage": stage,
                "revision": self.revision,
                "summary": summary,
            }
        )
        return self.state_version

    def add_review_marker(
        self,
        timeline_sec: float,
        label: str,
        category: str,
        confidence: float,
        rationale: str,
    ) -> ReviewMarker:
        marker = ReviewMarker(
            marker_id=f"mkr_{len(self.review_markers) + 1:04d}",
            timeline_sec=round(max(0.0, timeline_sec), 4),
            timeline_frame=self.sec_to_frame(max(0.0, timeline_sec)),
            label=label,
            category=category,
            confidence=round(min(1.0, max(0.0, confidence)), 4),
            rationale=rationale,
        )
        self.review_markers.append(marker)
        return marker

    def to_dict(self) -> dict[str, Any]:
        """Serialize state metadata (excluding raw audio arrays) to a JSON-safe dict."""
        return {
            "project_id": self.project_id,
            "sequence_id": self.sequence_id,
            "profile_id": self.profile.get("profile_id"),
            "revision": self.revision,
            "state_version": self.state_version,
            "dry_run": self.dry_run,
            "instruction": self.instruction,
            "intent": asdict(self.intent),
            "media_assets": {k: asdict(v) for k, v in self.media_assets.items()},
            "missing_media": list(self.missing_media),
            "sync_result": asdict(self.sync_result) if self.sync_result else None,
            "transcript_backend": self.transcript_backend,
            "transcript_segments": [asdict(s) for s in self.transcript_segments],
            "silence_intervals": [asdict(i) for i in self.silence_intervals],
            "interruption_intervals": [asdict(i) for i in self.interruption_intervals],
            "setup_delay_sec": self.setup_delay_sec,
            "story_sections": [asdict(s) for s in self.story_sections],
            "scene_cuts_sec": list(self.scene_cuts_sec),
            "kept_master_intervals": [asdict(i) for i in self.kept_master_intervals],
            "rough_cut_shots": [asdict(s) for s in self.rough_cut_shots],
            "audio_filters": [asdict(f) for f in self.audio_filters],
            "ffmpeg_audio_filtergraph": self.ffmpeg_audio_filtergraph,
            "captions": [asdict(c) for c in self.captions],
            "lower_thirds": [asdict(lt) for lt in self.lower_thirds],
            "qc_report": asdict(self.qc_report),
            "review_markers": [asdict(m) for m in self.review_markers],
            "command_batches_count": len(self.command_batches),
            "artifacts": dict(self.artifacts),
            "stage_timings_sec": dict(self.stage_timings_sec),
            "version_history": list(self.version_history),
        }
