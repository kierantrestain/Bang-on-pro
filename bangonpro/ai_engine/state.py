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
    video_codec: str | None
    audio_codec: str | None
    audio_sample_rate: int
    audio_channels: int
    priority: int = 1
    verified: bool = True

    @property
    def has_video(self) -> bool:
        return bool(self.video_codec)

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_codec)

    @property
    def is_audio_only(self) -> bool:
        return (not self.has_video) and self.has_audio

    @property
    def fps(self) -> float:
        if self.fps_num <= 0 or self.fps_den <= 0:
            return 0.0
        return float(self.fps_num) / float(self.fps_den)


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
    # V1.2.2: clock drift and interrupted recordings.
    # Convention: source_sec = master_sec + offset + drift_ppm * 1e-6 * master_sec, where
    # ``offset`` is the offset at master t=0 (regression intercept). ``segment_offsets``
    # optionally overrides this piecewise: [{"master_start_sec", "master_end_sec",
    # "offset_sec", "drift_ppm"}] for recordings that stopped and restarted.
    drift_ppm: dict[str, float] = field(default_factory=dict)
    segment_offsets: dict[str, list[dict[str, float]]] = field(default_factory=dict)

    def source_time(self, camera_id: str, master_sec: float) -> float:
        """Map a master-clock time to the source file's own clock (drift and segments aware)."""
        for seg in self.segment_offsets.get(camera_id, []) or []:
            if float(seg.get("master_start_sec", 0.0)) <= master_sec < float(seg.get("master_end_sec", float("inf"))):
                return master_sec + float(seg.get("offset_sec", 0.0)) + float(seg.get("drift_ppm", 0.0)) * 1e-6 * master_sec
        return master_sec + float(self.offsets_sec.get(camera_id, 0.0)) + float(self.drift_ppm.get(camera_id, 0.0)) * 1e-6 * master_sec

    def master_time(self, camera_id: str, source_sec: float) -> float:
        """Inverse of :meth:`source_time`: a time in the source file on the master clock."""
        for seg in self.segment_offsets.get(camera_id, []) or []:
            k = 1.0 + float(seg.get("drift_ppm", 0.0)) * 1e-6
            m = (source_sec - float(seg.get("offset_sec", 0.0))) / k
            if float(seg.get("master_start_sec", 0.0)) <= m < float(seg.get("master_end_sec", float("inf"))):
                return m
        k = 1.0 + float(self.drift_ppm.get(camera_id, 0.0)) * 1e-6
        return (source_sec - float(self.offsets_sec.get(camera_id, 0.0))) / k


@dataclass
class AsrBackendReport:
    """Honest runtime report for the ASR / VAD stage (never conflates DSP VAD with neural Whisper)."""

    backend_id: str = "deterministic_multimic_vad"
    model_id: str | None = None
    is_neural_inference: bool = False
    fallback_used: bool = False
    fallback_reason: str | None = None
    transcript_quality: str = "vad_speech_segments_only"
    backend_package_installed: bool = False
    model_available_on_disk: bool = False
    actual_inference_executed: bool = False
    segments_transcribed: int = 0


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
    is_neural_inference: bool = False
    transcript_quality: str = "vad_speech_segments_only"
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
    template: str = "studio_clean_bar"
    position: str = "bottom_left"


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
    """Parsed intent from natural-language user instructions.

    All mutating editorial flags default to ``False`` so actions only execute
    when explicitly requested or explicitly opted into via ``full_production()``.
    """

    raw_instruction: str = ""
    sync_cameras: bool = False
    transcribe: bool = False
    remove_setup_delay: bool = False
    remove_silence: bool = False
    build_rough_cut: bool = False
    multicam_switch: bool = False
    restore_audio: bool = False
    add_captions: bool = False
    add_lower_thirds: bool = False
    run_qc: bool = False
    export_video: bool = False
    needs_clarification: bool = False
    clarification_question: str = ""
    profile_hint: str | None = None
    parsed_actions: list[str] = field(default_factory=list)
    target_clips: list[str] = field(default_factory=list)
    target_tracks: list[str] = field(default_factory=list)
    time_range_sec: tuple[float, float] | None = None
    target_speakers: list[str] = field(default_factory=list)
    loudness_target_lufs: float | None = None
    silence_threshold_db: float | None = None
    min_pause_sec: float | None = None
    intent_confidence: float = 1.0
    scope: str = "none"

    @property
    def has_mutating_actions(self) -> bool:
        """Return True if any timeline-mutating or rendering flag is enabled."""
        return any(
            (
                self.sync_cameras,
                self.remove_setup_delay,
                self.remove_silence,
                self.build_rough_cut,
                self.multicam_switch,
                self.restore_audio,
                self.add_captions,
                self.add_lower_thirds,
                self.export_video,
            )
        )

    @classmethod
    def full_production(
        cls,
        raw_instruction: str = "",
        *,
        export_video: bool = False,
        profile_hint: str | None = None,
        parsed_actions: list[str] | None = None,
    ) -> ProductionIntent:
        """Explicitly construct a full end-to-end production workflow intent."""
        return cls(
            raw_instruction=raw_instruction,
            sync_cameras=True,
            transcribe=True,
            remove_setup_delay=True,
            remove_silence=True,
            build_rough_cut=True,
            multicam_switch=True,
            restore_audio=True,
            add_captions=True,
            add_lower_thirds=True,
            run_qc=True,
            export_video=export_video,
            needs_clarification=False,
            clarification_question="",
            profile_hint=profile_hint,
            parsed_actions=list(parsed_actions or ["default_full_production_pipeline"]),
            intent_confidence=1.0,
            scope="full_production",
        )


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
        self.asr_backend_report: AsrBackendReport = AsrBackendReport()
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
        return max(0, round(sec * self.fps))

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
        intent_dict = asdict(self.intent)
        if intent_dict.get("time_range_sec") is not None:
            intent_dict["time_range_sec"] = list(intent_dict["time_range_sec"])
        return {
            "project_id": self.project_id,
            "sequence_id": self.sequence_id,
            "profile_id": self.profile.get("profile_id"),
            "revision": self.revision,
            "state_version": self.state_version,
            "dry_run": self.dry_run,
            "instruction": self.instruction,
            "intent": intent_dict,
            "media_assets": {k: asdict(v) for k, v in self.media_assets.items()},
            "missing_media": list(self.missing_media),
            "sync_result": asdict(self.sync_result) if self.sync_result else None,
            "transcript_backend": self.transcript_backend,
            "asr_backend_report": asdict(self.asr_backend_report),
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
