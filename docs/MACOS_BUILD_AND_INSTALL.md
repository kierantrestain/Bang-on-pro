# Bang On Pro — Apple Silicon (macOS `arm64`) Build & Installation Guide (V1.1.0)

This guide covers installing **Bang On Pro** on Apple Silicon Macs (M1, M2, M3, M4) from versioned GitHub Releases / GitHub Actions builds, or compiling a reproducible, fully self-contained native `arm64` `.app` bundle and `.dmg` installer locally.

---

## 1. System Requirements

- **Hardware:** Apple Silicon Mac (`arm64` — M1 / M2 / M3 / M4 family). Intel `x86_64` Macs are not the primary target of the `arm64` packaging scripts.
- **Operating System:** macOS **13.0 Ventura** or newer (CI builds, relocates, signs, DMG-verifies, and benchmarks natively on **macOS 15 Sequoia `macos-15`**).
- **Disk Space:**
  - Installing the self-contained `.dmg`: ~500–700 MB free (**zero manual Homebrew, Python, or terminal setup required**).
  - Building locally from source with KDE CraftMaster (`macos-arm-clang` Qt6/KF6 binary cache): ~10–15 GB free (can be placed on an external APFS SSD via `CRAFT_ROOT`).
  - Optional AI models (Whisper / SAM2 / MobileSAM): managed visually through the in-app **GUI Model Manager** — see [RAM-Tier & External SSD Storage Guidance](OPEN_SOURCE_COMPONENTS.md#3-optional-ml-backends-gui-model-manager--low-storage-mac-storage-management).
- **Hardware Video Acceleration:** Bundled FFmpeg utilizes Apple **VideoToolbox** (`h264_videotoolbox`, `hevc_videotoolbox`, `prores_videotoolbox`) for hardware-accelerated decoding, proxy generation, and export. MLT 7.x effect processing executes on multi-threaded ARM64 NEON CPU slices (MLT 7.x does not ship native Metal filters; deprecated OpenGL/Movit is intentionally avoided for stability).

---

## 2. Option A: Install Prebuilt Self-Contained `.dmg` (Zero Terminal Setup)

Every version tag (`v*`, e.g. `v1.1.0`) publishes a **GitHub Release** with the native Apple Silicon installer attached. Every push to `main`, `bangonpro/**`, or manual dispatch also builds, DMG-verifies, and benchmarks the installer via [`.github/workflows/build-macos-arm64.yml`](../.github/workflows/build-macos-arm64.yml) on GitHub's native **`macos-15`** (`arm64`) runner.

### What Is Bundled Inside `Bang On Pro.app`
`Bang On Pro.app` is a **100% self-contained** native Apple Silicon application bundle:
- `Contents/MacOS/Bang On Pro` — Native `arm64` Mach-O editor binary (Qt 6 + KF6, DBus-free)
- `Contents/MacOS/kdenlive_render`, `melt`, `ffmpeg`, `ffprobe` — Self-contained rendering & VideoToolbox media stack
- `Contents/Frameworks/` — Relocated Qt 6 frameworks, KDE Frameworks 6 dylibs, MLT, FFmpeg, and transitive dependencies (verified zero leaked `/Users/...`, `/opt/homebrew`, or `CraftRoot` paths in both `otool -L` and `LC_RPATH`)
- `Contents/PlugIns/` — Qt 6 plugins (`platforms`, `styles`, `imageformats`, `iconengines`, `multimedia`, `tls`, `networkinformation`, `kf6`), MLT 7 modules (`mlt/`), and frei0r effects (`frei0r-1/`)
- `Contents/Resources/` — MLT 7 profiles/presets (`mlt/`), Breeze icons (`icons/`), QML modules (`qml/`), `bangonpro/` Python engine, embedded self-contained Python 3.12 virtualenv (`python-env/`, locked via [`requirements-bangonpro.lock`](../requirements-bangonpro.lock)), and `LICENSES/` (GPL-3.0 `COPYING`, upstream `LICENSES/`, `OPEN_SOURCE_COMPONENTS.md`, and `ARTCRAFT_NOTICE.txt`)

### Download & Install Steps
1. Download the release assets from the **Releases** page (or latest green **Build macOS Apple Silicon (arm64)** Actions run):
   - `BangOnPro-<version>-arm64.dmg` — Native `arm64` macOS disk image
   - `SHA256SUMS.txt` — Cryptographic SHA-256 checksum of the `.dmg`
   - `SIGNING_STATUS.txt` — Explicit record of signing, notarization, and `dmg_verified=true` status
   - `BUILD_VERIFICATION.log` — Strict non-masked verification log covering both the staged `.app` AND the mounted DMG clean-copied into an isolated Applications directory (`plutil`, `lipo`, `codesign --verify --deep --strict`, `otool -L` + `LC_RPATH` audit, credential/asset hygiene, VideoToolbox, `melt -query producers` `avformat`, embedded Python CLI, and headless `--version`)
   - `benchmark_report.md` & `benchmark_results.json` — Sustained 10s+ 1080p/4K/multicam/AI/roto benchmark report with transparent `host_tier` attribution
2. Verify the download checksum before mounting:
   ```bash
   cd ~/Downloads
   shasum -a 256 -c SHA256SUMS.txt
   cat SIGNING_STATUS.txt
   ```
3. Double-click `BangOnPro-<version>-arm64.dmg` and drag **`Bang On Pro.app`** into **`/Applications`**.
4. Launch **Bang On Pro** from Applications / Spotlight. If `SIGNING_STATUS.txt` reports `signing_state=adhoc_signed_not_notarized`, follow [Section 4 (Gatekeeper Instructions)](#4-code-signing-notarization--gatekeeper-instructions) on first launch.
5. **Optional Local AI Models (Zero Terminal Setup):** Open **Bang On Pro → AI Model & Storage Manager** inside the app to inspect your Mac's RAM tier, install/verify optional local Whisper or SAM2 models, clear caches safely, or move caches/models to an external SSD with zero terminal commands.

---

## 3. Option B: Local Reproducible Build on an Apple Silicon Mac

### 3.1 Install Prerequisites

1. **Xcode Command Line Tools:**
   ```bash
   xcode-select --install
   ```
2. **Homebrew Base Tooling (`arm64` in `/opt/homebrew`):**
   ```bash
   brew install cmake ninja ccache pkg-config create-dmg python@3.12
   ```
3. **KDE Frameworks 6 (KF6) + Qt 6 + MLT 7 + FFmpeg via KDE CraftMaster (`master` Qt 6 Binary Cache):**
   KDE Craft merged its `qt6` branch into `master` and distributes prebuilt Apple Silicon Qt 6 / KF6 / MLT 7 / FFmpeg binary caches at `https://files.kde.org/craft/Qt6/`. Use KDE's official `CraftMaster.py` with [`packaging/macos/CraftConfig.ini`](../packaging/macos/CraftConfig.ini):
   ```bash
   export CRAFT_ROOT="$HOME/CraftRoot"
   git clone --depth=1 https://invent.kde.org/packaging/craftmaster.git --branch=master /tmp/craftmaster

   python3 /tmp/craftmaster/CraftMaster.py \
     --config packaging/macos/CraftConfig.ini \
     --variables "Root=$CRAFT_ROOT" \
     --targets macos-arm-clang \
     --setup

   python3 /tmp/craftmaster/CraftMaster.py \
     --config packaging/macos/CraftConfig.ini \
     --variables "Root=$CRAFT_ROOT" \
     --targets macos-arm-clang \
     -c -i --options virtual.ignored=True --update craft

   # Optional: overlay Bang On Pro's Craft blueprint
   BLUEPRINT_DIR="$CRAFT_ROOT/macos-arm-clang/etc/blueprints/locations/craft-blueprints-kde/kde/kdemultimedia/bangonpro"
   mkdir -p "$BLUEPRINT_DIR"
   cp packaging/macos/craft_blueprint.py "$BLUEPRINT_DIR/bangonpro.py"

   # Install prebuilt binary-cached Qt6/KF6/MLT/FFmpeg dependencies
   python3 /tmp/craftmaster/CraftMaster.py \
     --config packaging/macos/CraftConfig.ini \
     --variables "Root=$CRAFT_ROOT" \
     --targets macos-arm-clang \
     -c --install-deps kdenlive
   ```

### 3.2 Run the Reproducible Packaging Script

```bash
chmod +x packaging/macos/build_macos_arm64.sh packaging/macos/bootstrap_ai_runtime.sh
./packaging/macos/build_macos_arm64.sh \
  --mode craft \
  --craft-root "$HOME/CraftRoot/macos-arm-clang" \
  --version 1.1.0 \
  --output-dir dist/macos-arm64
```
*(Note: `--craft-root "$HOME/CraftRoot"` is also accepted — the script automatically detects the `macos-arm-clang` CraftMaster target subdirectory.)*

What `build_macos_arm64.sh` performs step-by-step:
1. Confirms the host is native `Darwin` + `arm64`.
2. Verifies `Qt6`, `KF6CoreAddons`, `ECM`, `MLT >= 7.22.0`, `frei0r`, `ditto`, and `FFmpeg` (checking Apple **VideoToolbox** availability).
3. Configures and compiles via CMake + Ninja (`RelWithDebInfo`, `-DCMAKE_OSX_ARCHITECTURES=arm64`, `-DQT_MAJOR_VERSION=6`, `-DBUILD_WITH_QT6=ON`, `-DUSE_DBUS=OFF`) and executes Bang On Pro's C++ CTest unit tests.
4. Assembles `dist/macos-arm64/Bang On Pro.app` using [`packaging/macos/Info.plist.in`](../packaging/macos/Info.plist.in) (`com.bangonpro.editor`), embeds Qt 6 frameworks & plugins (`macdeployqt`), MLT 7 modules/data, frei0r plugins, Breeze icons, `bangonpro/` Python modules, self-contained `python-env/` installed strictly from [`requirements-bangonpro.lock`](../requirements-bangonpro.lock) via [`packaging/macos/bootstrap_ai_runtime.sh`](../packaging/macos/bootstrap_ai_runtime.sh) (with credential-scrubbed environment isolation), `COPYING`, `LICENSES/`, `OPEN_SOURCE_COMPONENTS.md`, and `ARTCRAFT_NOTICE.txt`.
5. Runs `relocate_macho_deps` recursively across all Mach-O executables, frameworks, plugins, and QML libraries — stripping build/Craft/Homebrew `LC_RPATH` entries, normalizing plugin `LC_ID_DYLIB` install names, and rewriting every non-system dependency strictly to `@rpath` / `@executable_path/../Frameworks`.
6. Applies **Developer ID** signing with Hardened Runtime (if configured) or falls back cleanly to **ad-hoc signing** (`codesign --force --deep --sign -`) with [`packaging/macos/entitlements.plist`](../packaging/macos/entitlements.plist).
7. Packages `dist/macos-arm64/BangOnPro-<version>-arm64.dmg` via `create-dmg` (with automatic `hdiutil` fallback) and writes `SHA256SUMS.txt`.
8. Executes **strict, non-masking verification** (no `|| true` or exit-code-dropping pipes) on BOTH the staged `.app` AND the actual packaged `.dmg`: verifies `SHA256SUMS.txt`, mounts the `.dmg` read-only with `hdiutil attach`, clean-copies `Bang On Pro.app` into a private temporary Applications folder outside the build tree via `ditto`, detaches the `.dmg`, and launches `Bang On Pro --version`, `melt -query producers` (`avformat`), `ffmpeg`, bundled Python AI CLI, `plutil`, `lipo`, `codesign --verify --deep --strict`, `otool -L` + `LC_RPATH`, and credential/asset hygiene checks inside a sanitized `env -i` environment.

### 3.3 Optional: Local Developer ID Signing & Notarization

If you hold an Apple Developer Program membership (`Developer ID Application` certificate installed in your login keychain and an `xcrun notarytool` keychain profile):

```bash
./packaging/macos/build_macos_arm64.sh \
  --mode craft \
  --craft-root "$HOME/CraftRoot/macos-arm-clang" \
  --version 1.1.0 \
  --sign-identity "Developer ID Application: Your Name (TEAMID1234)" \
  --notary-profile "bangonpro-notary" \
  --output-dir dist/macos-arm64
```

### 3.4 Running Sustained Benchmarks on Your Mac

Run the sustained 10-second 1080p/4K/multicam/AI/roto benchmark suite on physical bare-metal Apple Silicon hardware:

```bash
"/Applications/Bang On Pro.app/Contents/Resources/python-env/bin/python3" \
  -m bangonpro.macos.benchmark \
  --sustained \
  --duration 10.0 \
  --duration-4k 10.0 \
  --output-dir ./benchmarks-bare-metal
```

---

## 4. Code Signing, Notarization & Gatekeeper Instructions

Bang On Pro is completely transparent about build signing status. Every build emits `SIGNING_STATUS.txt` recording one of three states alongside `dmg_verified=true`:

| `signing_state` | Meaning | Gatekeeper Behaviour on macOS |
|---|---|---|
| `developer_id_signed_and_notarized` | Signed with an Apple Developer ID certificate (Hardened Runtime) and notarized + stapled via `xcrun notarytool` | Opens directly without Gatekeeper warnings |
| `developer_id_signed` | Signed with Developer ID (Hardened Runtime), but not submitted to Apple Notary Service | macOS may warn that Apple cannot check it for malicious software |
| `adhoc_signed_not_notarized` | Signed locally with an ad-hoc signature (`codesign --force --deep --sign -`) because Apple signing secrets were not configured | **Expected on community / open-source fork CI builds.** macOS Gatekeeper blocks double-click until explicitly approved |

### Configuring Automated Signing & Notarization in GitHub Actions

Repository maintainers with an Apple Developer account can enable signed + notarized CI releases by configuring these GitHub Actions Secrets:

| GitHub Secret | Purpose |
|---|---|
| `MACOS_CERTIFICATE_P12` | Base64-encoded `.p12` export of your `Developer ID Application` certificate + private key |
| `MACOS_CERTIFICATE_PASSWORD` | Password used when exporting the `.p12` bundle |
| `MACOS_SIGNING_IDENTITY` | Exact signing identity string (e.g., `Developer ID Application: Studio Ltd (ABC123XYZ0)`) |
| `MACOS_NOTARY_KEY` | Base64-encoded App Store Connect API `.p8` private key (optional; enables notarization) |
| `MACOS_NOTARY_KEY_ID` | App Store Connect API Key ID |
| `MACOS_NOTARY_ISSUER_ID` | App Store Connect Issuer UUID |

When these secrets are absent, `.github/workflows/build-macos-arm64.yml` **does not fail** — it transparently falls back to ad-hoc signing and posts the exact signing state in `SIGNING_STATUS.txt` and the GitHub Actions Step Summary.

### Opening a Developer ID Signed & Notarized Build
1. Verify `shasum -a 256 -c SHA256SUMS.txt` and confirm `notarized=true` in `SIGNING_STATUS.txt`.
2. Drag `Bang On Pro.app` into `/Applications` and double-click to launch normally.

### Opening an Ad-Hoc Signed Build Safely on macOS
After verifying `shasum -a 256 -c SHA256SUMS.txt` and copying `Bang On Pro.app` to `/Applications`:

- **Recommended GUI method (macOS Sequoia / Sonoma):**
  1. Attempt to open `/Applications/Bang On Pro.app` once (macOS displays the unverified developer alert — click **Done** / **OK**).
  2. Open **System Settings → Privacy & Security**, scroll to **Security**, and click **Open Anyway** next to `"Bang On Pro" was blocked`.
  3. Authenticate with Touch ID or your macOS password.
- **Terminal alternative (only after verifying `SHA256SUMS.txt`):**
  ```bash
  xattr -dr com.apple.quarantine "/Applications/Bang On Pro.app"
  open "/Applications/Bang On Pro.app"
  ```

### Hardened Runtime & Entitlements Posture

[`packaging/macos/entitlements.plist`](../packaging/macos/entitlements.plist) enforces a minimal, **JIT-free** Hardened Runtime posture:
- Requests only camera, microphone, user-selected file access, read-only Downloads access, outbound HTTPS client access, and library-validation relaxation required to load MLT/frei0r/FFmpeg/Python plugin `.dylib`/`.so` modules.
- **Explicitly omits** `com.apple.security.cs.allow-jit` and `com.apple.security.cs.allow-unsigned-executable-memory`.
- **Explicitly omits** AppleEvents / Accessibility UI scripting entitlements, because Bang On Pro's AI layer communicates exclusively through the native C++ `TimelineCommandEngine` API over a `0600` user-only local socket.

---

## 5. Honest Hardware Attribution & Environment Transparency

> **Important Engineering Transparency Notice**
>
> 1. **Bare-Metal vs Virtualized CI Apple Silicon:** GitHub Actions `macos-15` runners execute on **virtualized** Apple Silicon (`Apple M1 (Virtual)`, 3 vCPU, 7 GB RAM). Bang On Pro's benchmark harness (`bangonpro.macos.benchmark`) explicitly inspects `sysctl machdep.cpu.brand_string` and `kern.hv_vmm_present` and tags every report with its exact `host_tier`:
>    - `macos_arm64_bare_metal` — physical consumer/studio Apple Silicon Mac
>    - `macos_arm64_virtualized_ci` — virtualized Apple Silicon CI runner (never misrepresented as bare-metal hardware!)
>    - `linux_cpu_fallback` — Linux CPU-only CI/dev environment
> 2. **Linux-VM Packaging Limitations:** Native Apple Silicon (`arm64`) Mach-O binaries, Qt6/KF6 macOS frameworks, Apple VideoToolbox encoders, `codesign` signatures, `hdiutil` `.dmg` images, and `xcrun notarytool` notarization tickets **cannot be built, executed, or verified inside a Linux (`x86_64`) development VM or Linux container**.
>    - Linux CI (`.github/workflows/ci-linux.yml`) compiles and tests the C++/Qt5/KF5/MLT binary, CTest suites, `ruff` + `cppcheck` static analysis, Python AI/preset/integrity/E2E suites, shell syntax (`bash -n`), `requirements-bangonpro.lock` validation, `CraftConfig.ini` validation, plist XML validity, and workflow YAML syntax.
>    - Real Apple Silicon `.app` and `.dmg` deliverables **must** be built, DMG-verified, and benchmarked on native Apple Silicon hardware — either via GitHub Actions' `macos-15` (`arm64`) runner (`.github/workflows/build-macos-arm64.yml`) or locally on an M-series Mac using `packaging/macos/build_macos_arm64.sh`.
>    - No Linux-built artifact is ever represented as a verified macOS binary.

