# Bang On Pro — Architecture & Multi-Agent Module Boundaries

## Overview

**Bang On Pro** is an AI-native professional non-linear video editor built directly on **Kdenlive** (`release/24.08`, GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL), **Qt** (dual Qt5/KF5 on Linux CI & Qt6/KF6 on Apple Silicon macOS), and **MLT** (`>= 7.22.0`).

Upstream tracking: `upstream` → `https://github.com/KDE/kdenlive.git` (`release/24.08`).
All Kdenlive core manual editing workflows, project `.kdenlive` XML compatibility, autosave, crash recovery, and GPL-3.0 licensing are preserved intact.

## Module Ownership & Worktree Boundaries

| Agent | Role | Branch / Worktree | Owned Paths |
|---|---|---|---|
| **Agent 1** | Architect / Integration Lead | `bangonpro/integration` | `docs/`, `bangonpro/schemas/`, `src/bangonpro/BangOnProContracts.h`, `src/CMakeLists.txt`, `src/mainwindow.*`, `src/main.cpp` |
| **Agent 2** | Editor Core (C++/Qt/MLT) | `feat/editor-core-timeline-api` | `src/bangonpro/timeline_api/` |
| **Agent 3** | AI Production & Workflow | `feat/ai-production-pipeline` | `bangonpro/ai_engine/`, `bangonpro/show_profiles/` |
| **Agent 4** | Effects & Premiere Compat | `feat/premiere-preset-engine` | `bangonpro/premiere_compat/`, `src/bangonpro/effects_browser/` |
| **Agent 5** | Performance & Interface | `feat/ui-macos-perf` | `src/bangonpro/ui/`, `bangonpro/macos/` |
| **Agent 6** | QA, Security & Packaging | `feat/qa-security-packaging` | `.github/workflows/`, `packaging/macos/`, `tests/bangonpro/` |

## Core Architectural Invariants

1. **Single Authorized Timeline Writer (`TimelineTransactionWriter`)**:
   - AI agents never directly mutate UI widgets or simulate mouse/keyboard events.
   - All timeline edits flow through `bangonpro/schemas/timeline_command_v1.schema.json` into `BangOnPro::TimelineCommandEngine`, enforcing project/sequence targeting, optimistic concurrency via `expected_revision`, atomic rollback on failure, Kdenlive `QUndoStack` integration, and optional human approval gates (`PendingApproval`).
2. **Non-Destructive Media & Reversible Edits**:
   - Source media files are opened strictly read-only. Automated tests verify SHA-256 checksums before and after every pipeline execution.
   - Every AI-assembled cut outputs a standard, fully editable `.kdenlive` MLT XML project (and optional OpenTimelineIO `.otio` interchange).
3. **Hardened Premiere Preset Compatibility**:
   - All `.prfpset` archives are extracted with zip-slip/zip-bomb guards and parsed via `defusedxml` (XXE/DTD disabled).
   - Every preset is catalogued with an explicit status (`exact_validated`, `approximated`, `unsupported`, `missing_dependency`), dependency report (LUTs, fonts, codecs, proprietary plugins), and preserved raw XML metadata so unsupported presets remain browsable.
   - Third-party proprietary preset ZIP files are gitignored and never redistributed in the repository.
4. **Open-Source Component Governance**:
   - Core C++ editor: Kdenlive (GPL-3.0) + MLT (LGPL-2.1/GPL-3.0) + Qt + KDE Frameworks.
   - Python AI/Interchange layer: NumPy (BSD-3), SciPy (BSD-3), defusedxml (PSF-2.0), jsonschema (MIT), OpenTimelineIO (Apache-2.0), PySceneDetect (BSD-3).
   - Optional pluggable backends (pinned, opt-in): WhisperX / faster-whisper (MIT), Auto-Editor (Unlicense/Public Domain), SAM2 (Apache-2.0), RNNoise (BSD-3 via FFmpeg `arnndn` / `xiph/rnnoise`).
