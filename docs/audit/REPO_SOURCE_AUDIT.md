# Bang On Pro v1.1.0 — Repository-Wide Source Audit & Verification Report

- **Generated (UTC):** `2026-11-24T20:43:26.763815+00:00`
- **V1.0.0 Immutable Baseline Commit:** `1f7e2df8e3e797638e966ece5559a30e084c2041`
- **Audited Branch:** `bangonpro/integration` (`bba0dfe0cb719a7d73e7ecde5d5c146a2bab4d3c`)
- **Machine-Readable Registers:**
  - Inventory: `docs/audit/repository_inventory.json`
  - Coverage Register: `docs/audit/audit_register.json`
  - Defect Register: `docs/audit/defect_register.json`
  - V1.0 Static Baseline: `docs/audit/static_analysis_baseline.json`

## 1. Repository Inventory & Review Coverage (Automated vs Manual Separated)

Manual line-by-line review is strictly tracked per file and line range. Unmodified upstream Kdenlive subsystems receive automated compilation and static analysis only and are **never** claimed as manually reviewed.

| Scope | Tracked Files | Text Lines | Automated Scan Files (%) | Manual Review Files (%) | Manual Reviewed Lines (%) |
|---|---:|---:|---:|---:|---:|
| **Bang On Pro (First-Party)** | 111 | 72,858 | 93 (83.78%) | 81 (72.97%) | 52,737 (72.38%) |
| **Upstream Kdenlive 24.08.3** | 1505 | 1,570,416 | 545 (36.21%) | 13 (0.86%) | 4,678 (0.3%) |
| **Third-Party Bundled** | 34 | 3,872 | 17 (50.0%) | 0 (0.0%) | 0 (0.0%) |
| **Total Tracked Repository** | **1650** | **1,647,146** | **655 (39.7%)** | **94 (5.7%)** | **57,415 (3.49%)** |

## 2. Static Analysis, Sanitizers & Test Verification (Before vs After)

| Check | V1.0.0 Baseline | V1.1.0 Verified Result |
|---|---|---|
| `cppcheck --library=qt` (`src/bangonpro/` — 20 files) | 1 finding (`PremierePresetBrowser.cpp:73`) | **0 findings** |
| `clang-tidy` (`bugprone-*`, `clang-analyzer-*`, `performance-*` on Bang On Pro C++ files) | Not run | **42 benign perf/widening notices (0 defects)** |
| `ASan + UBSan` (`bangonpro_test_timeline_command_engine`) | Not run | **20/20 PASS** (0 memory/UB errors, includes fuzzing) |
| Native C++ `ctest` suites | 3 suites | **5/5 suites PASS (100%)** |
| `mypy --ignore-missing-imports bangonpro/` (42 files) | 50+ type errors | **0 errors** |
| `ruff check --select=E9,F63,F7,F82 bangonpro/ tests/bangonpro/` | Not gated in CI | **0 errors (strictly gated with `--require-ruff`)** |
| Python QA, Security, Compliance & Project Integrity (`run_all_checks.py`) | 52 checks | **81 checks PASS (6 static + 75 pytest, 0 fail, 0 skip)** |

## 3. Confirmed Defects Resolved

- **10 inherited upstream Kdenlive defects fixed** (`UP-001`..`UP-010`): null dereferences in `AbstractTreeModel::checkConsistency()`, `ClipModel` lifecycle, `GraphicsSceneRectMove::mouseDoubleClickEvent()` (plus broken bitmask precedence), `MonitorProxy::extractFrame()`, `TimelineModel::getTrackPosition()` uncaught `std::out_of_range`, `ProjectClip::TimeWarpInfo` uninitialized struct & unsafe `strcmp`, `ProxyTask` ignored non-zero process exits / image save errors / div-by-zero / raw temp-file leak, plus hardened `RenderWidget`, `KdenliveDoc`, and `ProjectManager` lifecycle safety.
- **15 Bang On Pro defects fixed** (`BOP-001`..`BOP-015`): stale-proposal revision bypass, partial-approval broken dependency validation, live-timeline-mutating A/B compare replaced with strictly isolated clone preview, C++/Python `timeline_state.json` schema mismatch & in-place editing of open projects, narrow intent routing + clarification flow, local Model Manager UI & backend, real Faster-Whisper PyAV crash & CLI summary reporting, real MobileSAM ONNX weight discovery & rank-3 HWC tensor support, subprocess environment secret allowlisting & exit-code enforcement, shortcut action registration & zero-conflict verification, Roto backend label persistence, control-character fuzz hardening, 50+ `mypy` errors, sustained hardware-attributed benchmarking, and `--require-ruff` CI gating.

## 4. Real Local Neural AI Model Verification (Non-Fallback Proof)

Both local neural AI subsystems were executed end-to-end on real media through Bang On Pro's production entrypoints (in an isolated Python venv with real weights, without touching fallback paths):

1. **Faster-Whisper Tiny (`faster-whisper 1.2.1` / CTranslate2)** via `bangonpro.ai_engine.cli`:
   - Input: `espeak-ng` spoken English WAV muxed into 640×360 H.264/AAC MP4.
   - Result: `asr_backend_used="faster_whisper"`, `is_neural_inference=true`, `fallback_used=false`, `transcript_quality="neural_asr"`, `actual_inference_executed=true`, 2 recognized English transcript segments + 2 SRT/ASS captions, `qc_passed=true`.
2. **MobileSAM ONNX (`onnxruntime 1.32.0`)** via `bangonpro.roto_studio.mask_engine.segment_frame_detailed`:
   - Weights: `mobile_sam_image_encoder.onnx` (27 MB) + `sam_mask_decoder_single.onnx` (16 MB).
   - Result: `backend_id="mobile_sam_onnx"`, `is_neural_inference=true`, `fallback_used=false`, `quality_status="neural_ok"`, `confidence=1.0`, `foreground_alpha_mean=0.999993`, `background_alpha_mean=0.000001`, elapsed `3.578s`.

## 5. Explicit Exclusions & Open External-Input Blockers

1. **Proprietary Premiere Archive (`EXT-001`)**: `effects pr.zip` and `Bang_on_pro_premiere_preset_audit.json` are external proprietary inputs not present in the repository. Bang On Pro ships a verified 64-preset GPL-3.0 starter library (45 Exact Validated, 19 Approximated, 0 Missing Dep, 0 Unsupported) and full local `.prfpset`/`.zip`/`.rar` import pipeline.
2. **Developer ID Notarization & Bare-Metal Mac Telemetry (`EXT-002`)**: Apple Developer signing secrets are not configured in CI (release DMG is ad-hoc signed with explicit `SIGNING_STATUS.txt`), and macOS CI benchmarks are labeled as virtualized `macos-15` Apple Silicon runners rather than physical bare-metal M-series machines.
3. **Untouched Upstream Kdenlive Informational Warnings (`UP-DEFERRED`)**: 317 non-release-blocking `cppcheck` informational/style/performance items in unmodified upstream Kdenlive files are preserved untouched to avoid destabilizing upstream KDE 24.08 subsystems.
