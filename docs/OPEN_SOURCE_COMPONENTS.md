# Bang On Pro — Open-Source Components, Licences & Governance Matrix

**Project Licence:** `GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL` (inherited from upstream Kdenlive `release/24.08`, preserved in [`COPYING`](../COPYING) and [`LICENSES/`](../LICENSES/)).

This document provides the audited evaluation matrix of all core and optional open-source components used by **Bang On Pro**, including pinned version ranges, SPDX licence identifiers, GPL-3.0 compatibility analysis, integration status, ArtCraft selective-integration attribution, and local model-storage guidance for low-storage Macs.

---

## 1. Core Editor, Media Engine & Optional AI Evaluation Matrix

| Component | Pinned Version / Range | SPDX Licence | GPL-3.0 Compatibility | Integration Status | Role in Bang On Pro |
|---|---|---|---|---|---|
| **Kdenlive** | `24.08.3` (`release/24.08`) | `GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL` | **Native (Primary Licence)** | **Integrated Core** | Upstream NLE engine, timeline model, undo/redo (`QUndoStack`), autosave, crash recovery, `.kdenlive` project format |
| **Qt** | `5.15.x` (Linux CI) / `6.6+` (macOS `arm64`) | `LGPL-3.0-only` / `GPL-3.0-only` | **Compatible** (LGPL-3.0 & GPL-3.0) | **Integrated Core** | Cross-platform C++ GUI, QML timeline view, multimedia, threading, concurrent job queue |
| **KDE Frameworks** | `KF5 5.115+` (Linux CI) / `KF6 6.0+` (macOS `arm64`) | `LGPL-2.1-or-later` / `LGPL-3.0-only` / `BSD-2-Clause` | **Compatible** (all LGPL/BSD modules compatible with GPL-3.0) | **Integrated Core** | KConfig, KXmlGui, KIO, KArchive, KNewStuff, KCoreAddons, Breeze icons, KCrash |
| **MLT Framework** | `>= 7.22.0` (verified `7.22.0`) | `LGPL-2.1-or-later` (core) / `GPL-3.0-or-later` (GPL modules) | **Compatible** (`LGPL-2.1+` upgrades cleanly to `GPL-3.0`; GPL modules match) | **Integrated Core** | Frame-accurate timeline composition, filter graph, transitions, `melt` headless renderer |
| **FFmpeg** | `>= 6.1` (Ubuntu `6.1.1`; macOS `arm64` Craft/Homebrew `6.x`/`7.x`) | `GPL-3.0-or-later` (when built with `--enable-gpl --enable-version3`; **never** `--enable-nonfree`) | **Compatible** (strictly GPL/LGPL build; `--enable-nonfree` is prohibited) | **Integrated Core** | Demux/decode/encode, audio analysis, Apple VideoToolbox hardware encode/decode on macOS |
| **frei0r-plugins** | `>= 1.8.0` (verified `1.8.0`) | `GPL-2.0-or-later` | **Compatible** (`GPL-2.0-or-later` permits distribution under `GPL-3.0`) | **Integrated Core** | Real-time video effects, compositing (`frei0r.cairoblend`), Premiere preset mapping targets |
| **OpenTimelineIO** | `>= 0.17.0, < 1.0.0` | `Apache-2.0` | **Compatible** (FSF confirms Apache-2.0 is compatible with GPL-3.0) | **Integrated Python Runtime** | Editorial timeline interchange (`.otio`) across NLEs and AI rough-cut pipelines |
| **PySceneDetect** | `>= 0.6.4, < 1.0.0` | `BSD-3-Clause` | **Compatible** (permissive BSD-3-Clause) | **Integrated Python Runtime** | Shot boundary & scene-change detection for multicam/rough-cut assembly |
| **OpenCV** | `>= 4.9.0, < 6.0.0` (`opencv-python-headless`) | `Apache-2.0` | **Compatible** (Apache-2.0 is compatible with GPL-3.0) | **Integrated / Optional Vision Runtime** | Optical-flow mask propagation (`Farneback`), rotoscoping contour refinement, matting, and frame-level QC |
| **RNNoise** | `xiph/rnnoise` (via FFmpeg `arnndn` filter / system `librnnoise`) | `BSD-3-Clause` | **Compatible** (permissive BSD-3-Clause) | **Integrated / System FFmpeg Filter** | Low-latency, CPU-friendly neural speech denoising for podcast & studio voice tracks |
| **ArtCraft (Selective Integration)** | `storytold/artcraft` (architecture patterns & selected tooling) | `MIT OR Apache-2.0` | **Compatible** (both MIT and Apache-2.0 are compatible with GPL-3.0) | **Selective Integration (Code/Patterns Only)** | Masking/rotoscoping studio UX patterns, keyframe workflow inspiration; third-party media asset packs strictly excluded (see Section 5) |
| **Auto-Editor** | Pinned opt-in: `auto-editor==24w36a` (or `>= 24.0,<26.0`) | `Unlicense` | **Compatible** (Public Domain dedication) | **Optional Opt-In Plugin** | Automated silence/dead-air detection & pacing engine (native DSP fallback built in) |
| **WhisperX / faster-whisper** | Pinned opt-in: `faster-whisper>=1.0.3,<2.0.0`, `whisperx>=3.1.5,<4.0.0` | `BSD-4-Clause` (WhisperX) / `MIT` (faster-whisper & OpenAI Whisper) | **See Section 2, Note 4** (`faster-whisper` MIT is directly GPL-3.0 compatible; WhisperX invoked out-of-process as optional CLI/subprocess plugin) | **Optional Opt-In Plugin** | Word-level speech transcription, speaker diarization, caption timing |
| **SAM2 / MobileSAM** | Pinned opt-in: `SAM-2 >= 1.0` (`facebookresearch/sam2`) / `MobileSAM` (`ChaoningZhang/MobileSAM`) | `Apache-2.0` | **Compatible** (Apache-2.0 is compatible with GPL-3.0) | **Optional Opt-In Plugin** | Interactive object rotoscoping, subject isolation, and dynamic mask tracking (MPS FP16 on 16 GB+ Macs or ONNX INT8 CPU fallback on 8 GB Macs) |

### Core Python Runtime & Verification Dependencies (`requirements-bangonpro.txt`)

| Package | Pinned Range | SPDX Licence | GPL-3.0 Compatibility | Role |
|---|---|---|---|---|
| **NumPy** | `>= 1.26.0, < 3.0.0` | `BSD-3-Clause` | **Compatible** | Waveform envelope, RMS/LUFS loudness, cross-correlation sync math |
| **SciPy** | `>= 1.13.0, < 2.0.0` | `BSD-3-Clause` | **Compatible** | Signal processing, FFT cross-correlation multicam sync, filtering |
| **defusedxml** | `>= 0.7.1, < 1.0.0` | `PSF-2.0` | **Compatible** | Hardened XML parser blocking XXE, external DTDs, and entity expansion bombs |
| **jsonschema** | `>= 4.22.0, < 5.0.0` | `MIT` | **Compatible** | Draft 2020-12 contract validation for Timeline Command API, Show Profiles, and Preset Reports |
| **pytest** | `>= 8.2.0, < 9.0.0` | `MIT` | **Compatible** | Automated QA, security regression, compliance, and project-integrity test runner |
| **Pillow / PyYAML** *(packaging & CI)* | `Pillow >= 10.0.0`, `PyYAML >= 6.0` | `MIT-CMU` / `MIT` | **Compatible** | Alpha mask PNG/image rendering, CI workflow validation |

---

## 2. GPL-3.0 Licence Compatibility Notes

1. **Upstream Kdenlive Preservation (`COPYING` & `LICENSES/`):**
   - Bang On Pro is a direct derivative work of Kdenlive (`release/24.08`). All upstream copyright headers, `COPYING` (GNU GPL v3.0), and the `LICENSES/` directory are retained unmodified and bundled inside macOS `.app` distributions (`Contents/Resources/COPYING` and `Contents/Resources/LICENSES/`).
2. **Apache-2.0 (`OpenTimelineIO`, `SAM2`, `MobileSAM`, `OpenCV`, `ArtCraft`):**
   - Per the Free Software Foundation licence compatibility matrix, **Apache-2.0 is one-way compatible with GPL-3.0** (combined works are distributed under GPL-3.0, preserving Apache-2.0 `NOTICE` attributions). Note that Apache-2.0 is *not* compatible with GPL-2.0-only, which is why Bang On Pro explicitly targets **GPL-3.0-only / GPL-3.0-or-later**.
3. **FFmpeg Build Hygiene:**
   - Packaged builds MUST link against FFmpeg compiled without `--enable-nonfree` (e.g., excluding non-free FDK-AAC or proprietary CUDA SDK stubs). On Apple Silicon macOS, Apple's system `VideoToolbox.framework` is accessed as a standard operating system library under the GPL-3.0 System Library Exception (Section 1).
4. **WhisperX (`BSD-4-Clause`) vs `faster-whisper` (`MIT`):**
   - `faster-whisper` (MIT) and OpenAI Whisper (MIT) are directly compatible with GPL-3.0.
   - Upstream `whisperx` carries a 4-clause BSD licence (which includes the advertising clause incompatible with linking into a GPL-3.0 work). Therefore, WhisperX is **never** vendored or statically/in-process linked into Bang On Pro; when opted in by the user, it is installed separately in an isolated user-managed Python environment and invoked strictly as an external subprocess exchanging JSON transcripts on disk.
   - Similarly, pyannote speaker-diarization model weights require a user's own Hugging Face token and acceptance of upstream model terms; Bang On Pro never bundles gated model weights.

---

## 3. Optional ML Backends & Low-Storage Mac Model Management

To keep the Bang On Pro installer compact and usable on base-model **256 GB / 8 GB Apple Silicon Macs**, multi-gigabyte ML runtimes and weights are **excluded** from the default `.dmg` and `requirements-bangonpro.txt`.

### Storage Footprint Tiers & Unified-Memory Recommendations

| Tier / Mac RAM | Recommended Models & Quantization | Approximate Disk Footprint |
|---|---|---|
| **Tier 0 — Core Editor (Default `.dmg`)** | Self-contained Bang On Pro `.app` (Kdenlive + Qt6 + KF6 + MLT 7 + FFmpeg/VideoToolbox + frei0r + Breeze + embedded Python 3.12 runtime) | ~400–650 MB |
| **8 GB Apple Silicon (Base M1/M2/M3)** | `faster-whisper` `tiny`/`small` (`int8`, CPU) + `MobileSAM` / `sam2.1_hiera_tiny` (`onnx_int8`, CPU fallback) | +450 MB – 1.2 GB |
| **16 GB Apple Silicon** | `faster-whisper` `small`/`medium` (`int8`, CPU) + `sam2.1_hiera_small` (`fp16` MPS with `onnx_int8` CPU fallback) | +1.6 – 2.6 GB |
| **24 GB Apple Silicon** | `faster-whisper` `medium` (`int8`) + `sam2.1_hiera_small` (`fp16` MPS) | +2.6 – 3.2 GB |
| **32 GB+ Apple Silicon (Pro/Max/Ultra)** | `faster-whisper` / Whisper `large-v3` (`fp16` MPS) + `sam2.1_hiera_base_plus` (`fp16` MPS) | +4.0 – 5.5 GB |

### Relocating Model & Proxy Caches to an External SSD

Bang On Pro honours configurable cache directories so large AI weights and proxy renders never fill the internal macOS boot drive:

```bash
# Default locations (internal SSD, gitignored in repo)
export BANGONPRO_MODEL_DIR="$HOME/.cache/bangonpro/models"
export BANGONPRO_PROXY_CACHE="$HOME/.cache/bangonpro/proxies"

# Recommended for 256 GB Macs: relocate onto an external Thunderbolt/USB-C SSD
export BANGONPRO_MODEL_DIR="/Volumes/StudioSSD/BangOnPro/models"
export BANGONPRO_PROXY_CACHE="/Volumes/StudioSSD/BangOnPro/proxies"
export HF_HOME="/Volumes/StudioSSD/BangOnPro/huggingface"
```

### Offline & Deterministic Operation
- All Tier 0 editorial workflows (manual editing, multicam waveform sync, RMS silence detection, PySceneDetect cuts, RNNoise/FFmpeg audio mastering, Premiere `.prfpset` parsing/mapping, OpenCV optical-flow masking, and MLT rendering) run **100% locally and offline** with zero cloud API calls or mandatory downloads.

---

## 4. Third-Party Proprietary Assets Policy (Premiere `.prfpset` Packs)

- Commercial or community Adobe Premiere Pro preset archives (e.g., `effects pr.zip`, `*pr.zip`, `*.prfpset`) are **never committed, vendored, or redistributed** in the Bang On Pro repository.
- `.gitignore` explicitly blocks `effects pr.zip`, `*pr.zip`, and `*.prfpset`, and `tests/bangonpro/test_security_and_compliance.py` continuously scans the Git tree to fail CI if any `.prfpset` or proprietary preset archive is accidentally staged.
- Users import their own lawfully obtained preset packs locally at runtime; Bang On Pro parses them through a hardened zip-slip/XXE-protected pipeline and preserves original attribution and unsupported-effect metadata without stripping creator credits.

---

## 5. ArtCraft (`storytold/artcraft`) Selective Integration Attribution & Exclusion Notice

- **Upstream Project:** ArtCraft — <https://github.com/storytold/artcraft>
- **Upstream Licence:** Dual-licensed under **`MIT OR Apache-2.0`** (full licence texts preserved in [`LICENSES/MIT.txt`](../LICENSES/MIT.txt) and [`LICENSES/Apache-2.0.txt`](../LICENSES/Apache-2.0.txt), and bundled in macOS distributions under `Contents/Resources/LICENSES/ARTCRAFT_NOTICE.txt`).
- **Selective Integration Scope:** Only clean open-source architectural concepts and GPL-3.0-compatible code patterns (such as interactive rotoscoping studio workflows, matte/mask layering, and node/timeline ergonomics) are selectively integrated or adapted under Bang On Pro's GPL-3.0 licence with attribution preserved.
- **Strict Third-Party Asset Exclusion Policy:** Non-code third-party ArtCraft media asset folders — specifically **`sound/`**, **`memes/`**, **`skybox/`**, **`pose/`**, and **`characters_gif/`** — carry separate third-party attributions/licences and are **strictly excluded** from Bang On Pro. Automated compliance checks in `tests/bangonpro/test_security_and_compliance.py` continuously verify that zero files from those excluded ArtCraft asset directories exist anywhere in the Bang On Pro repository or packages.
