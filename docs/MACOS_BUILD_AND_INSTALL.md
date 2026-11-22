# Bang On Pro — Apple Silicon (macOS `arm64`) Build & Installation Guide

This guide covers installing **Bang On Pro** on Apple Silicon Macs (M1, M2, M3, M4) from GitHub Actions builds or compiling a reproducible native `arm64` `.app` bundle and `.dmg` installer locally.

---

## 1. System Requirements

- **Hardware:** Apple Silicon Mac (`arm64` — M1 / M2 / M3 / M4 family). Intel `x86_64` Macs are not the primary target of the `arm64` packaging scripts.
- **Operating System:** macOS **13.0 Ventura** or newer (CI builds and tests natively on **macOS 14 Sonoma `macos-14`**).
- **Disk Space:**
  - Installing the prebuilt `.dmg`: ~500 MB free.
  - Building locally from source with KDE Craft Qt6/KF6: ~10–15 GB free (can be placed on an external APFS SSD via `CRAFT_ROOT`).
  - Optional AI models (WhisperX, SAM2): see [Model Storage Guidance](OPEN_SOURCE_COMPONENTS.md#3-optional-ml-backends--low-storage-mac-model-management).
- **Hardware Video Acceleration:** Bundled/system FFmpeg utilizes Apple **VideoToolbox** (`h264_videotoolbox`, `hevc_videotoolbox`, `prores_videotoolbox`) for hardware-accelerated decoding and export.

---

## 2. Option A: Install Prebuilt `.dmg` from GitHub Actions

Every push to `main`, `bangonpro/**`, release tags (`v*`), or manual dispatch triggers [`.github/workflows/build-macos-arm64.yml`](../.github/workflows/build-macos-arm64.yml) on GitHub's native Apple Silicon **`macos-14`** runner.

1. Open the repository's **Actions** tab → select **Build macOS Apple Silicon (arm64)** → open the latest green run.
2. Download the artifact **`BangOnPro-macos-arm64-<version>`**, which contains:
   - `BangOnPro-<version>-arm64.dmg` — Native `arm64` macOS disk image
   - `SHA256SUMS.txt` — Cryptographic checksum of the `.dmg`
   - `SIGNING_STATUS.txt` — Explicit record of whether the artifact is Developer ID signed & notarized or ad-hoc signed
   - `BUILD_VERIFICATION.log` — Mach-O `lipo`, `plutil`, `codesign --verify`, VideoToolbox, and headless `--version` smoke-test transcript
3. Verify the checksum before opening the disk image:
   ```bash
   cd ~/Downloads/BangOnPro-macos-arm64-*
   shasum -a 256 -c SHA256SUMS.txt
   cat SIGNING_STATUS.txt
   ```
4. Mount `BangOnPro-<version>-arm64.dmg` and drag **`Bang On Pro.app`** into **`/Applications`**.
5. If `SIGNING_STATUS.txt` reports `signing_state=adhoc_signed_not_notarized`, follow [Section 4 (Gatekeeper Instructions)](#4-code-signing-notarization--gatekeeper-instructions) on first launch.

---

## 3. Option B: Local Reproducible Build on an Apple Silicon Mac

### 3.1 Install Prerequisites

1. **Xcode Command Line Tools:**
   ```bash
   xcode-select --install
   ```
2. **Homebrew Base Tooling (`arm64` in `/opt/homebrew`):**
   ```bash
   brew install cmake ninja ccache pkg-config create-dmg ffmpeg mlt frei0r qt@6 python@3.12
   ```
3. **KDE Frameworks 6 (KF6) + Qt6 Prefix via KDE Craft:**
   Homebrew Core does not maintain all 18 KDE Frameworks 6 libraries required by Kdenlive/Bang On Pro. Use KDE Craft's official Apple Silicon Qt6 cache together with our blueprint override [`packaging/macos/craft_blueprint.py`](../packaging/macos/craft_blueprint.py):
   ```bash
   export CRAFT_ROOT="$HOME/CraftRoot"
   curl -fsSL https://raw.githubusercontent.com/KDE/craft/master/setup/CraftBootstrap.py -o /tmp/CraftBootstrap.py
   python3 /tmp/CraftBootstrap.py --prefix "$CRAFT_ROOT" --branch qt6 --use-defaults

   mkdir -p "$CRAFT_ROOT/etc/blueprints/locations/craft-blueprints-kde/kde/kdemultimedia/bangonpro"
   cp packaging/macos/craft_blueprint.py \
      "$CRAFT_ROOT/etc/blueprints/locations/craft-blueprints-kde/kde/kdemultimedia/bangonpro/bangonpro.py"

   source "$CRAFT_ROOT/craft/craftenv.sh"
   craft --install-deps bangonpro
   ```

### 3.2 Run the Reproducible Packaging Script

```bash
chmod +x packaging/macos/build_macos_arm64.sh
./packaging/macos/build_macos_arm64.sh \
  --mode craft \
  --craft-root "$HOME/CraftRoot" \
  --output-dir dist/macos-arm64
```

What `build_macos_arm64.sh` performs step-by-step:
1. Confirms the host is native `Darwin` + `arm64`.
2. Verifies `Qt6`, `KF6CoreAddons`, `ECM`, `MLT >= 7.22.0`, `frei0r`, and `FFmpeg` (checking Apple **VideoToolbox** availability).
3. Configures and compiles via CMake + Ninja (`-DCMAKE_OSX_ARCHITECTURES=arm64`, `-DQT_MAJOR_VERSION=6`, `-DBUILD_WITH_QT6=ON`, `-DUSE_DBUS=OFF`).
4. Assembles `dist/macos-arm64/Bang On Pro.app` using [`packaging/macos/Info.plist.in`](../packaging/macos/Info.plist.in) (`com.bangonpro.editor`), embeds `bangonpro/` Python modules, `COPYING`, and `LICENSES/`, and deploys Qt6 frameworks via `macdeployqt`.
5. Applies **Developer ID** signing (if configured) or falls back cleanly to **ad-hoc signing** (`codesign --force --deep --sign -`) with [`packaging/macos/entitlements.plist`](../packaging/macos/entitlements.plist).
6. Packages `dist/macos-arm64/BangOnPro-<version>-arm64.dmg` via `create-dmg` (with automatic `hdiutil` fallback), writes `SHA256SUMS.txt`, and executes a launch & Mach-O smoke test saved to `BUILD_VERIFICATION.log`.

### 3.3 Optional: Local Developer ID Signing & Notarization

If you hold an Apple Developer Program membership (`Developer ID Application` certificate installed in your login keychain and an `xcrun notarytool` keychain profile):

```bash
./packaging/macos/build_macos_arm64.sh \
  --mode craft \
  --sign-identity "Developer ID Application: Your Name (TEAMID1234)" \
  --notary-profile "bangonpro-notary" \
  --output-dir dist/macos-arm64
```

---

## 4. Code Signing, Notarization & Gatekeeper Instructions

Bang On Pro is completely transparent about build signing status. Every build emits `SIGNING_STATUS.txt` recording one of three states:

| `signing_state` | Meaning | Gatekeeper Behaviour on macOS |
|---|---|---|
| `developer_id_signed_and_notarized` | Signed with an Apple Developer ID certificate (Hardened Runtime) and notarized + stapled via `xcrun notarytool` | Opens directly without Gatekeeper warnings |
| `developer_id_signed` | Signed with Developer ID (Hardened Runtime), but not submitted to Apple Notary Service | macOS may warn that Apple cannot check it for malicious software |
| `adhoc_signed_not_notarized` | Signed locally with an ad-hoc signature (`codesign --force --deep --sign -`) because Apple signing secrets were not configured | **Expected on open-source fork/PR CI builds.** macOS Gatekeeper blocks double-click until explicitly approved |

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

### Opening an Ad-Hoc Signed Build Safely on macOS

After verifying `shasum -a 256 -c SHA256SUMS.txt` and copying `Bang On Pro.app` to `/Applications`:

- **Recommended GUI method (macOS Sequoia / Sonoma):**
  1. Attempt to open `/Applications/Bang On Pro.app` once (macOS displays the unverified developer alert — click **Done** / **OK**).
  2. Open **System Settings → Privacy & Security**, scroll to **Security**, and click **Open Anyway** next to `"Bang On Pro" was blocked`.
  3. Authenticate with Touch ID or your macOS password.
- **Terminal alternative (after verifying SHA-256 checksum):**
  ```bash
  xattr -dr com.apple.quarantine "/Applications/Bang On Pro.app"
  open "/Applications/Bang On Pro.app"
  ```

### Hardened Runtime & Entitlements Posture

[`packaging/macos/entitlements.plist`](../packaging/macos/entitlements.plist) enforces a minimal, **JIT-free** Hardened Runtime posture:
- Requests only camera, microphone, user-selected file access, read-only Downloads access, outbound HTTPS client access, and library-validation relaxation required to load MLT/frei0r/FFmpeg plugin `.dylib`s.
- **Explicitly omits** `com.apple.security.cs.allow-jit` and `com.apple.security.cs.allow-unsigned-executable-memory`.
- **Explicitly omits** AppleEvents / Accessibility UI scripting entitlements, because Bang On Pro's AI layer communicates exclusively through the native C++ `TimelineTransactionWriter` API.

---

## 5. Honest Note on Linux-VM Packaging Limitations

> **Important Engineering Transparency Notice**
>
> Native Apple Silicon (`arm64`) Mach-O binaries, Qt6/KF6 macOS frameworks, Apple VideoToolbox encoders, `codesign` signatures, `hdiutil` `.dmg` images, and `xcrun notarytool` notarization tickets **cannot be built, executed, or verified inside a Linux (`x86_64`) development VM or Linux container**.
>
> Accordingly:
> - Linux CI (`.github/workflows/ci-linux.yml`) compiles and tests the C++/Qt5/KF5/MLT binary, CTest suite, Python AI/preset/integrity suites, shell syntax (`bash -n packaging/macos/build_macos_arm64.sh`), plist XML validity, and workflow YAML syntax.
> - Real Apple Silicon `.app` and `.dmg` deliverables **must** be built and smoke-tested on native Apple Silicon hardware — either via GitHub Actions' `macos-14` (`arm64`) runner (`.github/workflows/build-macos-arm64.yml`) or locally on an M-series Mac using `packaging/macos/build_macos_arm64.sh`.
> - No Linux-built artifact is ever represented as a verified macOS binary.
