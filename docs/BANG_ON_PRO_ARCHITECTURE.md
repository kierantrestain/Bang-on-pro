# Bang On Pro v1.0.0 — Architecture & Module Reference

## Overview

**Bang On Pro** (`v1.0.0`) is an AI-native professional non-linear video editor built directly on **Kdenlive** (`release/24.08`, v24.08.3, `GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL`), **Qt** (dual Qt5/KF5 on Linux CI & Qt6/KF6 on Apple Silicon macOS), **MLT** (`>= 7.22.0`), and **FFmpeg** (with Apple VideoToolbox hardware acceleration on `arm64` macOS).

Upstream tracking: `upstream` → `https://github.com/KDE/kdenlive.git` (`release/24.08`).
All Kdenlive manual editing workflows, native `.kdenlive` XML project compatibility, `QUndoStack` undo/redo, autosave, crash recovery, and GPL-3.0 licensing are preserved intact.

## Module Architecture

| Module | Owned Paths | Responsibilities |
|---|---|---|
| **Editor Core & Timeline API** | `src/bangonpro/timeline_api/`, `bangonpro/schemas/timeline_command_v1.schema.json` | Single authorized transactional timeline writer (`TimelineCommandEngine` + `KdenliveTimelineBridge` + `TimelineCommandServer`), revision-checked optimistic concurrency, atomic rollback, per-operation selective approval (`approveBatchWithSelection`), native Kdenlive `QUndoStack` integration, and Stage Two operations (`apply_mask`, `add_transition`, `update_caption`, `delete_caption`, `add_lower_third`, `set_clip_speed`). |
| **AI Production & Transcription** | `bangonpro/ai_engine/`, `bangonpro/show_profiles/` | Multi-agent production orchestrator (ingest/sync with mixed FPS & missing media resilience, streaming spectral VAD/ASR transcription & editable `transcript.json`, overlapping-speech & interruption-aware multicamera switching, per-camera RMS gain-staging + RNNoise/loudnorm + lower thirds + QC), custom show profile persistence, and local-first + consent-gated external AI provider registry. |
| **Premiere Compatibility Engine** | `bangonpro/premiere_compat/`, `src/bangonpro/effects_browser/` | Hardened zip-slip/XXE `.prfpset` importer, 60+ legal built-in native presets covering all 58 Premiere effect families, real visual before/after PNG previews, inline parameter & keyframe editing, favourites/custom stack JSON persistence, and honest compatibility classification (`exact_validated`, `approximated`, `missing_dependency`, `unsupported`). |
| **Stage Two Roto & ArtCraft 2D Studio** | `bangonpro/roto_studio/`, `src/bangonpro/roto_ui/`, `bangonpro/schemas/roto_mask_v1.schema.json` | Interactive AI (+/- point) & manual (polygon, cubic Bézier, freehand, brush) rotoscoping, feather/expand/guided hair-edge refinement, multi-mask boolean combinations, bidirectional optical-flow propagation with occlusion handling & manual keyframe overrides, 4 preview modes, native MLT matte/spline export, and license-clean selective ArtCraft (`MIT OR Apache-2.0`) 2D Layer Compositor & Storyboard Animatic studio. |
| **Professional Studio UI & Workspaces** | `src/bangonpro/ui/`, `src/bangonpro/BangOnProIntegration.*`, `src/main.cpp` | Unified dark studio theme (`BangOnProTheme`), 9 dedicated workspaces (`Edit`, `Multicam`, `AI First Cut`, `Effects`, `Colour`, `Audio`, `Captions`, `Export`, `Roto & 2D`) with `WorkspaceSwitcherBar` and `QSettings` layout persistence, Premiere Pro / DaVinci Resolve / Kdenlive Classic shortcut presets, collapsible AI Assistant dock with per-change checkboxes, interactive `TranscriptEditorWidget`, `ShowProfileEditorDialog`, and `ExternalProviderConsentDialog`. |
| **Apple Silicon, Packaging & QA** | `bangonpro/macos/`, `packaging/macos/`, `.github/workflows/`, `tests/bangonpro/` | Native `macos-15` (`arm64`) KDE CraftMaster Qt6/KF6 build & self-contained `.app`/`.dmg` packaging with recursive `@rpath`/Mach-O dylib relocation, embedded Python 3.12 runtime, VideoToolbox profiles, hardware-aware quantization recommendations, bounded cache & model storage manager, comprehensive real benchmarks, static security AST checks, and 5 native C++ CTest suites. |

## Core Architectural Invariants

1. **Single Authorized Timeline Writer (`TimelineCommandEngine`)**: All AI, transcript, preset, rotoscope, and storyboard timeline edits execute through Draft 2020-12 schema-validated `timeline_command_v1` JSON batches with optimistic revision checks, atomic rollback, optional human approval (with per-change accept/reject), and native undo/redo.
2. **Non-Destructive Media & Reversible Edits**: Source media files are opened strictly read-only (verified by SHA-256 checksums before and after pipeline runs).
3. **Local-First & Consent-Gated External Services**: Zero paid external AI APIs are required for editing, transcription, masking, or 2D compositing; any external provider transmission requires explicit user opt-in consent.
4. **Licence & Asset Hygiene**: GPL-3.0 upstream Kdenlive licensing is preserved; ArtCraft (`MIT OR Apache-2.0`) code concepts are credited with zero excluded third-party ArtCraft media assets; proprietary `.prfpset`/`.zip` archives are gitignored and never redistributed.
