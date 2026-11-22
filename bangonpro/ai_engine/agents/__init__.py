"""Bang On Pro specialist AI production agents.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from bangonpro.ai_engine.agents.audio_graphics_qc import (
    build_audio_restoration_chain,
    generate_ass_subtitles,
    generate_kdenlive_lower_third_xml,
    generate_srt_subtitles,
    run_audio_graphics_qc_agent,
    run_technical_qc,
)
from bangonpro.ai_engine.agents.experimental_vision import (
    EXPERIMENTAL_VISION_WARNING,
    ExperimentalVisionResult,
    ExperimentalVisionService,
)
from bangonpro.ai_engine.agents.ingest_sync import (
    compute_file_sha256,
    estimate_offset_gcc_phat,
    extract_mono_pcm,
    probe_media_file,
    run_ingest_sync_agent,
    verify_source_integrity,
)
from bangonpro.ai_engine.agents.rough_cut_multicam import (
    detect_visual_scene_cuts,
    run_rough_cut_multicam_agent,
)
from bangonpro.ai_engine.agents.transcribe_diarise import (
    detect_available_asr_backends,
    run_transcribe_diarise_agent,
)

__all__ = [
    "EXPERIMENTAL_VISION_WARNING",
    "ExperimentalVisionResult",
    "ExperimentalVisionService",
    "build_audio_restoration_chain",
    "compute_file_sha256",
    "detect_available_asr_backends",
    "detect_visual_scene_cuts",
    "estimate_offset_gcc_phat",
    "extract_mono_pcm",
    "generate_ass_subtitles",
    "generate_kdenlive_lower_third_xml",
    "generate_srt_subtitles",
    "probe_media_file",
    "run_audio_graphics_qc_agent",
    "run_ingest_sync_agent",
    "run_rough_cut_multicam_agent",
    "run_technical_qc",
    "run_transcribe_diarise_agent",
    "verify_source_integrity",
]
