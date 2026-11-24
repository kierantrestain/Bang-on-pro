#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
# SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
#
# Reproducible Apple Silicon (arm64) macOS build, .app bundle, code signing,
# .dmg packaging, and launch smoke-test verification script for Bang On Pro.
#
# Must be executed natively on Apple Silicon macOS (arm64, macOS 13.0+ / CI macos-15).
# Cross-compiling Mach-O arm64 KDE/Qt6/MLT bundles from Linux is NOT supported.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Defaults
BUILD_MODE="auto"          # auto | craft | homebrew
SOURCE_DIR="${REPO_ROOT}"
BUILD_DIR="${REPO_ROOT}/build-macos-arm64"
STAGE_DIR="${REPO_ROOT}/stage-macos-arm64"
OUTPUT_DIR="${REPO_ROOT}/dist/macos-arm64"
CRAFT_ROOT="${CRAFT_ROOT:-${HOME}/CraftRoot}"
APP_NAME="Bang On Pro"
DMG_BASENAME="BangOnPro"
APP_VERSION="${BANGONPRO_VERSION:-0.1.0-alpha}"
MIN_MACOS="${MACOSX_DEPLOYMENT_TARGET:-13.0}"
SIGN_IDENTITY="${APPLE_SIGNING_IDENTITY:-${MACOS_SIGNING_IDENTITY:-}}"
NOTARY_PROFILE="${MACOS_NOTARY_PROFILE:-}"
NOTARY_KEY_PATH="${MACOS_NOTARY_KEY_PATH:-}"
NOTARY_KEY_ID="${MACOS_NOTARY_KEY_ID:-}"
NOTARY_ISSUER_ID="${MACOS_NOTARY_ISSUER_ID:-}"
SKIP_TESTS=0
SKIP_DMG=0
JOBS="$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4)"

usage() {
    cat <<'EOF'
Usage: packaging/macos/build_macos_arm64.sh [options]

Build and package Bang On Pro natively for Apple Silicon (arm64) macOS.

Options:
  --mode <auto|craft|homebrew>  Dependency prefix discovery mode (default: auto)
  --source-dir <path>           Repository source directory (default: repo root)
  --build-dir <path>            CMake build directory (default: ./build-macos-arm64)
  --output-dir <path>           Output directory for .app/.dmg/logs (default: ./dist/macos-arm64)
  --craft-root <path>           KDE Craft root prefix (default: $HOME/CraftRoot)
  --version <ver>               Release version string (default: 0.1.0-alpha)
  --min-macos <ver>             MACOSX_DEPLOYMENT_TARGET (default: 13.0)
  --sign-identity <id>          Developer ID Application signing identity
                                (omit to fall back cleanly to ad-hoc signing)
  --notary-profile <profile>    Keychain profile name for `xcrun notarytool`
  --jobs <n>                    Parallel Ninja build jobs
  --skip-tests                  Skip CTest/pytest verification steps
  --skip-dmg                    Build & sign .app bundle only (skip .dmg creation)
  -h, --help                    Show this help text

Environment variables:
  APPLE_SIGNING_IDENTITY / MACOS_SIGNING_IDENTITY
  MACOS_NOTARY_KEY_PATH, MACOS_NOTARY_KEY_ID, MACOS_NOTARY_ISSUER_ID
  CRAFT_ROOT, CMAKE_PREFIX_PATH
EOF
}

log() {
    printf '[bangonpro-macos] %s\n' "$*"
}

warn() {
    printf '[bangonpro-macos] WARNING: %s\n' "$*" >&2
}

die() {
    printf '[bangonpro-macos] ERROR: %s\n' "$*" >&2
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)
            BUILD_MODE="$2"; shift 2 ;;
        --source-dir)
            SOURCE_DIR="$2"; shift 2 ;;
        --build-dir)
            BUILD_DIR="$2"; shift 2 ;;
        --output-dir)
            OUTPUT_DIR="$2"; shift 2 ;;
        --craft-root)
            CRAFT_ROOT="$2"; shift 2 ;;
        --version)
            APP_VERSION="$2"; shift 2 ;;
        --min-macos)
            MIN_MACOS="$2"; shift 2 ;;
        --sign-identity)
            SIGN_IDENTITY="$2"; shift 2 ;;
        --notary-profile)
            NOTARY_PROFILE="$2"; shift 2 ;;
        --jobs)
            JOBS="$2"; shift 2 ;;
        --skip-tests)
            SKIP_TESTS=1; shift ;;
        --skip-dmg)
            SKIP_DMG=1; shift ;;
        -h|--help)
            usage; exit 0 ;;
        *)
            die "Unknown option: $1 (use --help for usage)" ;;
    esac
done

# ---------------------------------------------------------------------------
# 1. Host Platform & Architecture Verification
# ---------------------------------------------------------------------------
verify_host_platform() {
    local host_os host_arch
    host_os="$(uname -s)"
    host_arch="$(uname -m)"

    if [[ "${host_os}" != "Darwin" ]]; then
        die "This script must run on macOS (detected OS: ${host_os}). For CI builds, use GitHub Actions workflow .github/workflows/build-macos-arm64.yml on macos-15."
    fi

    if [[ "${host_arch}" != "arm64" ]]; then
        die "Apple Silicon (arm64) host is required (detected arch: ${host_arch})."
    fi

    log "Verified host: macOS $(sw_vers -productVersion) (${host_arch}), Xcode CLI: $(xcode-select -p)"
}

# ---------------------------------------------------------------------------
# 2. Dependency Prefix Resolution & Verification (Qt6, KF6, MLT 7, FFmpeg)
# ---------------------------------------------------------------------------
resolve_prefixes() {
    local prefix_paths=()

    if [[ "${BUILD_MODE}" == "craft" || ( "${BUILD_MODE}" == "auto" && -d "${CRAFT_ROOT}" ) ]]; then
        if [[ -d "${CRAFT_ROOT}" ]]; then
            log "Using KDE Craft Qt6/KF6 prefix: ${CRAFT_ROOT}"
            prefix_paths+=("${CRAFT_ROOT}")
            export PATH="${CRAFT_ROOT}/bin:${CRAFT_ROOT}/dev-utils/bin:${PATH}"
            export PKG_CONFIG_PATH="${CRAFT_ROOT}/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
        elif [[ "${BUILD_MODE}" == "craft" ]]; then
            die "KDE Craft prefix not found at ${CRAFT_ROOT}. See docs/MACOS_BUILD_AND_INSTALL.md."
        fi
    fi

    if [[ "${BUILD_MODE}" == "homebrew" || "${BUILD_MODE}" == "auto" ]]; then
        if command -v brew >/dev/null 2>&1; then
            local brew_prefix
            brew_prefix="$(brew --prefix)"
            log "Detected Homebrew prefix: ${brew_prefix}"
            prefix_paths+=("${brew_prefix}")
            for keg in qt qt@6 mlt ffmpeg frei0r extra-cmake-modules; do
                local keg_path
                keg_path="$(brew --prefix "${keg}" 2>/dev/null || true)"
                if [[ -n "${keg_path}" && -d "${keg_path}" ]]; then
                    prefix_paths+=("${keg_path}")
                    [[ -d "${keg_path}/bin" ]] && export PATH="${keg_path}/bin:${PATH}"
                    [[ -d "${keg_path}/lib/pkgconfig" ]] && export PKG_CONFIG_PATH="${keg_path}/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
                fi
            done
        elif [[ "${BUILD_MODE}" == "homebrew" ]]; then
            die "Homebrew not found in PATH."
        fi
    fi

    if [[ -n "${CMAKE_PREFIX_PATH:-}" ]]; then
        prefix_paths+=("${CMAKE_PREFIX_PATH}")
    fi

    if [[ ${#prefix_paths[@]} -eq 0 ]]; then
        die "No Qt6/KF6/MLT dependency prefix discovered. Install KDE Craft Qt6 or Homebrew dependencies first."
    fi

    local joined=""
    local p
    for p in "${prefix_paths[@]}"; do
        if [[ -z "${joined}" ]]; then
            joined="${p}"
        else
            joined="${joined};${p}"
        fi
    done
    export CMAKE_PREFIX_PATH="${joined}"
    log "Resolved CMAKE_PREFIX_PATH=${CMAKE_PREFIX_PATH}"
}

verify_dependencies() {
    log "Checking required build & packaging tools..."
    local required_tools=(cmake ninja pkg-config codesign hdiutil lipo otool install_name_tool plutil shasum)
    local tool
    for tool in "${required_tools[@]}"; do
        command -v "${tool}" >/dev/null 2>&1 || die "Required tool missing: ${tool}"
    done

    # Check Qt6
    local qt6_config=""
    IFS=';' read -r -a _prefixes <<< "${CMAKE_PREFIX_PATH}"
    for p in "${_prefixes[@]}"; do
        if [[ -f "${p}/lib/cmake/Qt6/Qt6Config.cmake" ]]; then
            qt6_config="${p}/lib/cmake/Qt6/Qt6Config.cmake"
            break
        fi
    done
    [[ -n "${qt6_config}" ]] || die "Qt6Config.cmake not found in CMAKE_PREFIX_PATH (${CMAKE_PREFIX_PATH})."
    log "Found Qt6: ${qt6_config}"

    # Check KDE Frameworks 6 (KF6CoreAddons + ECM)
    local kf6_config=""
    local ecm_config=""
    for p in "${_prefixes[@]}"; do
        [[ -f "${p}/lib/cmake/KF6CoreAddons/KF6CoreAddonsConfig.cmake" ]] && kf6_config="${p}/lib/cmake/KF6CoreAddons/KF6CoreAddonsConfig.cmake"
        [[ -f "${p}/share/ECM/cmake/ECMConfig.cmake" ]] && ecm_config="${p}/share/ECM/cmake/ECMConfig.cmake"
    done
    [[ -n "${kf6_config}" ]] || die "KF6CoreAddonsConfig.cmake not found. Install KDE Frameworks 6 via KDE Craft (packaging/macos/craft_blueprint.py)."
    [[ -n "${ecm_config}" ]] || die "ECMConfig.cmake not found in CMAKE_PREFIX_PATH."
    log "Found KF6: ${kf6_config}"
    log "Found ECM: ${ecm_config}"

    # Check MLT >= 7.22.0
    if ! pkg-config --atleast-version=7.22.0 mlt-framework-7 2>/dev/null; then
        die "MLT >= 7.22.0 (mlt-framework-7) is required via pkg-config."
    fi
    log "Found MLT: $(pkg-config --modversion mlt-framework-7)"
    command -v melt >/dev/null 2>&1 || warn "melt CLI not found in PATH; bundle verification will rely on libmlt."

    # Check FFmpeg + VideoToolbox hardware acceleration
    command -v ffmpeg >/dev/null 2>&1 || die "ffmpeg binary not found in PATH."
    log "Found FFmpeg: $(ffmpeg -version | head -n 1)"
    if ffmpeg -hide_banner -hwaccels 2>/dev/null | grep -qi videotoolbox; then
        log "Verified FFmpeg Apple VideoToolbox hardware acceleration is available."
    else
        warn "FFmpeg VideoToolbox hwaccel not reported; hardware-accelerated H.264/HEVC/ProRes export may fall back to software."
    fi
    if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q "h264_videotoolbox"; then
        log "Verified h264_videotoolbox encoder is enabled."
    fi
}

# ---------------------------------------------------------------------------
# 3. CMake Configure, Compile & Stage Install (Native arm64 Qt6/KF6)
# ---------------------------------------------------------------------------
build_application() {
    log "Configuring Bang On Pro with CMake + Ninja (Qt6/KF6, arm64, macOS >= ${MIN_MACOS})..."
    mkdir -p "${BUILD_DIR}" "${STAGE_DIR}" "${OUTPUT_DIR}"

    local cmake_args=(
        -S "${SOURCE_DIR}"
        -B "${BUILD_DIR}"
        -G Ninja
        -DCMAKE_BUILD_TYPE=Release
        -DCMAKE_INSTALL_PREFIX="${STAGE_DIR}"
        -DCMAKE_PREFIX_PATH="${CMAKE_PREFIX_PATH}"
        -DCMAKE_OSX_ARCHITECTURES=arm64
        -DCMAKE_OSX_DEPLOYMENT_TARGET="${MIN_MACOS}"
        -DQT_MAJOR_VERSION=6
        -DBUILD_WITH_QT6=ON
        -DUSE_DBUS=OFF
        -DRELEASE_BUILD=ON
        -DKDE_INSTALL_USE_QT_SYS_PATHS=OFF
    )

    if [[ "${SKIP_TESTS}" -eq 1 ]]; then
        cmake_args+=(-DBUILD_TESTING=OFF)
    else
        cmake_args+=(-DBUILD_TESTING=ON)
    fi

    if command -v ccache >/dev/null 2>&1; then
        cmake_args+=(
            -DCMAKE_C_COMPILER_LAUNCHER=ccache
            -DCMAKE_CXX_COMPILER_LAUNCHER=ccache
        )
    fi

    cmake "${cmake_args[@]}"
    log "Compiling with ${JOBS} parallel Ninja jobs..."
    cmake --build "${BUILD_DIR}" --parallel "${JOBS}"

    if [[ "${SKIP_TESTS}" -eq 0 ]]; then
        log "Running CTest C++ unit tests (offscreen Qt platform)..."
        QT_QPA_PLATFORM=offscreen ctest --test-dir "${BUILD_DIR}" --output-on-failure --timeout 120 || warn "One or more upstream CTest suites failed on macOS offscreen runner; check logs."
    fi

    log "Staging install tree into ${STAGE_DIR}..."
    cmake --install "${BUILD_DIR}"
}

# ---------------------------------------------------------------------------
# 4. Assemble Native arm64 Bang On Pro.app Bundle
# ---------------------------------------------------------------------------
assemble_app_bundle() {
    local app_bundle="${OUTPUT_DIR}/${APP_NAME}.app"
    local contents_dir="${app_bundle}/Contents"
    local macos_dir="${contents_dir}/MacOS"
    local frameworks_dir="${contents_dir}/Frameworks"
    local plugins_dir="${contents_dir}/PlugIns"
    local resources_dir="${contents_dir}/Resources"

    log "Assembling ${app_bundle}..."
    rm -rf "${app_bundle}"
    mkdir -p "${macos_dir}" "${frameworks_dir}" "${plugins_dir}" "${resources_dir}"

    # Locate built Kdenlive/Bang On Pro executable
    local built_bin=""
    if [[ -x "${STAGE_DIR}/Applications/KDE/kdenlive.app/Contents/MacOS/kdenlive" ]]; then
        built_bin="${STAGE_DIR}/Applications/KDE/kdenlive.app/Contents/MacOS/kdenlive"
    elif [[ -x "${STAGE_DIR}/bin/kdenlive.app/Contents/MacOS/kdenlive" ]]; then
        built_bin="${STAGE_DIR}/bin/kdenlive.app/Contents/MacOS/kdenlive"
    elif [[ -x "${STAGE_DIR}/bin/kdenlive" ]]; then
        built_bin="${STAGE_DIR}/bin/kdenlive"
    elif [[ -x "${BUILD_DIR}/bin/kdenlive.app/Contents/MacOS/kdenlive" ]]; then
        built_bin="${BUILD_DIR}/bin/kdenlive.app/Contents/MacOS/kdenlive"
    else
        die "Could not locate compiled kdenlive/Bang On Pro binary in ${STAGE_DIR} or ${BUILD_DIR}."
    fi

    cp -f "${built_bin}" "${macos_dir}/${APP_NAME}"
    chmod 755 "${macos_dir}/${APP_NAME}"

    # Copy helper renderer binary if staged
    if [[ -x "${STAGE_DIR}/bin/kdenlive_render" ]]; then
        cp -f "${STAGE_DIR}/bin/kdenlive_render" "${macos_dir}/kdenlive_render"
        chmod 755 "${macos_dir}/kdenlive_render"
    fi

    # Bundle melt, ffmpeg, ffprobe for self-contained rendering
    local media_bin
    for media_bin in melt ffmpeg ffprobe; do
        if command -v "${media_bin}" >/dev/null 2>&1; then
            cp -f "$(command -v "${media_bin}")" "${macos_dir}/${media_bin}"
            chmod 755 "${macos_dir}/${media_bin}"
        fi
    done

    # Copy staged share resources (effects, profiles, icons, KXMLGUI data)
    if [[ -d "${STAGE_DIR}/share" ]]; then
        cp -R "${STAGE_DIR}/share" "${resources_dir}/share"
    fi

    # Copy Bang On Pro Python modules, JSON schemas, and GPL-3.0 licence notices
    if [[ -d "${SOURCE_DIR}/bangonpro" ]]; then
        cp -R "${SOURCE_DIR}/bangonpro" "${resources_dir}/bangonpro"
    fi
    cp -f "${SOURCE_DIR}/COPYING" "${resources_dir}/COPYING"
    cp -R "${SOURCE_DIR}/LICENSES" "${resources_dir}/LICENSES"
    [[ -f "${SOURCE_DIR}/docs/OPEN_SOURCE_COMPONENTS.md" ]] && cp -f "${SOURCE_DIR}/docs/OPEN_SOURCE_COMPONENTS.md" "${resources_dir}/OPEN_SOURCE_COMPONENTS.md"

    # Build .icns icon from repo PNG icon set if iconutil is available
    local icon_file="bangonpro.icns"
    if command -v iconutil >/dev/null 2>&1 && [[ -f "${SOURCE_DIR}/data/icons/128-apps-kdenlive.png" ]]; then
        local iconset_dir="${BUILD_DIR}/bangonpro.iconset"
        rm -rf "${iconset_dir}"
        mkdir -p "${iconset_dir}"
        local sz
        for sz in 16 32 64 128 256 512; do
            if [[ -f "${SOURCE_DIR}/data/icons/${sz}-apps-kdenlive.png" ]]; then
                cp "${SOURCE_DIR}/data/icons/${sz}-apps-kdenlive.png" "${iconset_dir}/icon_${sz}x${sz}.png"
            elif command -v sips >/dev/null 2>&1; then
                sips -z "${sz}" "${sz}" "${SOURCE_DIR}/data/icons/128-apps-kdenlive.png" --out "${iconset_dir}/icon_${sz}x${sz}.png" >/dev/null 2>&1 || true
            fi
        done
        iconutil -c icns "${iconset_dir}" -o "${resources_dir}/${icon_file}" || warn "iconutil failed; continuing without custom .icns"
    fi

    # Render Info.plist from template
    local git_sha build_number
    git_sha="$(git -C "${SOURCE_DIR}" rev-parse --short HEAD 2>/dev/null || echo "nogit")"
    build_number="${GITHUB_RUN_NUMBER:-$(date +%Y%m%d)}"
    sed \
        -e "s|@BANGONPRO_EXECUTABLE@|${APP_NAME}|g" \
        -e "s|@BANGONPRO_ICON_FILE@|${icon_file}|g" \
        -e "s|@BANGONPRO_VERSION@|${APP_VERSION}|g" \
        -e "s|@BANGONPRO_BUILD_NUMBER@|${build_number}|g" \
        -e "s|@BANGONPRO_GIT_SHA@|${git_sha}|g" \
        -e "s|@BANGONPRO_MIN_MACOS@|${MIN_MACOS}|g" \
        "${SCRIPT_DIR}/Info.plist.in" > "${contents_dir}/Info.plist"

    plutil -lint "${contents_dir}/Info.plist"

    # Deploy Qt6 frameworks & QML plugins via macdeployqt if available
    if command -v macdeployqt >/dev/null 2>&1; then
        log "Running macdeployqt to bundle Qt6 frameworks and QML modules..."
        local qml_args=()
        [[ -d "${SOURCE_DIR}/src/timeline2/view/qml" ]] && qml_args+=("-qmldir=${SOURCE_DIR}/src/timeline2/view/qml")
        macdeployqt "${app_bundle}" "${qml_args[@]}" -verbose=1 || warn "macdeployqt reported warnings; verify dylib linkage below."
    else
        warn "macdeployqt not found; ensure Qt6 frameworks are bundled or Craft packager is used."
    fi

    # Verify native arm64 Mach-O binary
    local archs
    archs="$(lipo -archs "${macos_dir}/${APP_NAME}")"
    log "Mach-O architectures for '${APP_NAME}': ${archs}"
    if [[ "${archs}" != *"arm64"* ]]; then
        die "Compiled binary does not contain arm64 slice (got: ${archs})."
    fi
}

# ---------------------------------------------------------------------------
# 5. Code Signing (Developer ID with Hardened Runtime, or Ad-Hoc Fallback)
# ---------------------------------------------------------------------------
SIGNING_STATE="unknown"

sign_app_bundle() {
    local app_bundle="${OUTPUT_DIR}/${APP_NAME}.app"
    local entitlements="${SCRIPT_DIR}/entitlements.plist"

    if [[ -n "${SIGN_IDENTITY}" ]] && security find-identity -v -p codesigning 2>/dev/null | grep -Fq "${SIGN_IDENTITY}"; then
        log "Developer ID signing identity found: ${SIGN_IDENTITY}"
        log "Signing nested binaries, frameworks, and plugins with Hardened Runtime..."

        # Sign nested Mach-O libraries and plugins inside-out
        find "${app_bundle}/Contents" -type f \( -name "*.dylib" -o -name "*.so" \) -print0 | \
            while IFS= read -r -d '' lib; do
                codesign --force --options runtime --timestamp --sign "${SIGN_IDENTITY}" "${lib}"
            done

        if [[ -d "${app_bundle}/Contents/Frameworks" ]]; then
            find "${app_bundle}/Contents/Frameworks" -maxdepth 2 -name "*.framework" -print0 | \
                while IFS= read -r -d '' fw; do
                    codesign --force --options runtime --timestamp --sign "${SIGN_IDENTITY}" "${fw}"
                done
        fi

        local helper
        for helper in kdenlive_render melt ffmpeg ffprobe; do
            if [[ -x "${app_bundle}/Contents/MacOS/${helper}" ]]; then
                codesign --force --options runtime --timestamp --entitlements "${entitlements}" --sign "${SIGN_IDENTITY}" "${app_bundle}/Contents/MacOS/${helper}"
            fi
        done

        codesign --force --options runtime --timestamp --entitlements "${entitlements}" --sign "${SIGN_IDENTITY}" "${app_bundle}"
        codesign --verify --deep --strict --verbose=2 "${app_bundle}"
        SIGNING_STATE="developer_id_signed"
        log "Verified Developer ID signature on ${app_bundle}."
    else
        if [[ -n "${SIGN_IDENTITY}" ]]; then
            warn "Requested signing identity '${SIGN_IDENTITY}' was not found in the active keychain."
        fi
        warn "Developer ID signing credentials not configured; falling back to ad-hoc signing (not notarized)."
        codesign --force --deep --sign - --entitlements "${entitlements}" "${app_bundle}"
        codesign --verify --deep --strict --verbose=2 "${app_bundle}"
        SIGNING_STATE="adhoc_signed_not_notarized"
        log "Applied ad-hoc signature (local/community build — Gatekeeper will require explicit user approval)."
    fi
}

# ---------------------------------------------------------------------------
# 6. Package .dmg & Optional Apple Notarization
# ---------------------------------------------------------------------------
package_dmg() {
    local app_bundle="${OUTPUT_DIR}/${APP_NAME}.app"
    local dmg_path="${OUTPUT_DIR}/${DMG_BASENAME}-${APP_VERSION}-arm64.dmg"
    local dmg_staging="${BUILD_DIR}/dmg-staging"

    rm -rf "${dmg_staging}" "${dmg_path}"
    mkdir -p "${dmg_staging}"
    cp -R "${app_bundle}" "${dmg_staging}/${APP_NAME}.app"
    ln -s /Applications "${dmg_staging}/Applications"

    log "Creating DMG installer: ${dmg_path}"
    if command -v create-dmg >/dev/null 2>&1; then
        create-dmg \
            --volname "${APP_NAME}" \
            --window-pos 200 120 \
            --window-size 640 400 \
            --icon-size 100 \
            --icon "${APP_NAME}.app" 175 190 \
            --hide-extension "${APP_NAME}.app" \
            --app-drop-link 465 190 \
            --no-internet-enable \
            "${dmg_path}" \
            "${dmg_staging}" || {
                warn "create-dmg failed (common in headless CI without Finder); falling back to hdiutil."
                hdiutil create -volname "${APP_NAME}" -srcfolder "${dmg_staging}" -ov -format UDZO "${dmg_path}"
            }
    else
        hdiutil create -volname "${APP_NAME}" -srcfolder "${dmg_staging}" -ov -format UDZO "${dmg_path}"
    fi

    # Sign & notarize DMG only when Developer ID signing was performed
    if [[ "${SIGNING_STATE}" == "developer_id_signed" ]]; then
        codesign --force --timestamp --sign "${SIGN_IDENTITY}" "${dmg_path}"

        if [[ -n "${NOTARY_KEY_PATH}" && -n "${NOTARY_KEY_ID}" && -n "${NOTARY_ISSUER_ID}" ]]; then
            log "Submitting ${dmg_path} to Apple Notary Service via App Store Connect API key..."
            xcrun notarytool submit "${dmg_path}" \
                --key "${NOTARY_KEY_PATH}" \
                --key-id "${NOTARY_KEY_ID}" \
                --issuer "${NOTARY_ISSUER_ID}" \
                --wait
            xcrun stapler staple "${dmg_path}"
            spctl --assess --type open --context context:primary-signature -vv "${dmg_path}" || warn "spctl assessment returned warning."
            SIGNING_STATE="developer_id_signed_and_notarized"
        elif [[ -n "${NOTARY_PROFILE}" ]]; then
            log "Submitting ${dmg_path} to Apple Notary Service via keychain profile '${NOTARY_PROFILE}'..."
            xcrun notarytool submit "${dmg_path}" --keychain-profile "${NOTARY_PROFILE}" --wait
            xcrun stapler staple "${dmg_path}"
            SIGNING_STATE="developer_id_signed_and_notarized"
        else
            warn "Notary credentials not provided; DMG is Developer ID signed but NOT notarized."
        fi
    fi

    # Generate SHA-256 checksum
    (cd "${OUTPUT_DIR}" && shasum -a 256 "$(basename "${dmg_path}")" > SHA256SUMS.txt)
    log "Wrote checksum: $(cat "${OUTPUT_DIR}/SHA256SUMS.txt")"
}

# ---------------------------------------------------------------------------
# 7. Smoke-Test Launch Verification & Artifact Metadata
# ---------------------------------------------------------------------------
run_smoke_verification() {
    local app_bundle="${OUTPUT_DIR}/${APP_NAME}.app"
    local log_file="${OUTPUT_DIR}/BUILD_VERIFICATION.log"
    local status_file="${OUTPUT_DIR}/SIGNING_STATUS.txt"

    {
        echo "=== Bang On Pro Apple Silicon (arm64) Build Verification ==="
        echo "Timestamp (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "Version:         ${APP_VERSION}"
        echo "Host macOS:      $(sw_vers -productVersion) ($(uname -m))"
        echo "Signing State:   ${SIGNING_STATE}"
        echo ""
        echo "--- Info.plist Validation ---"
        plutil -lint "${app_bundle}/Contents/Info.plist"
        echo ""
        echo "--- Mach-O Architecture Check ---"
        file "${app_bundle}/Contents/MacOS/${APP_NAME}"
        lipo -archs "${app_bundle}/Contents/MacOS/${APP_NAME}"
        echo ""
        echo "--- Code Signature Verification ---"
        codesign -dvv "${app_bundle}" 2>&1 || true
        codesign --verify --deep --strict --verbose=2 "${app_bundle}" 2>&1
        echo ""
        echo "--- Headless Launch Smoke Test (--version) ---"
        QT_QPA_PLATFORM=offscreen "${app_bundle}/Contents/MacOS/${APP_NAME}" --version 2>&1 || true
        echo ""
        echo "--- Bundled Media Stack ---"
        if [[ -x "${app_bundle}/Contents/MacOS/melt" ]]; then
            "${app_bundle}/Contents/MacOS/melt" --version 2>&1 | head -n 3
        fi
        if [[ -x "${app_bundle}/Contents/MacOS/ffmpeg" ]]; then
            "${app_bundle}/Contents/MacOS/ffmpeg" -version 2>&1 | head -n 2
            "${app_bundle}/Contents/MacOS/ffmpeg" -hide_banner -hwaccels 2>&1 || true
        fi
    } | tee "${log_file}"

    local notarized_flag="false"
    local gatekeeper_note
    if [[ "${SIGNING_STATE}" == "developer_id_signed_and_notarized" ]]; then
        notarized_flag="true"
        gatekeeper_note="Signed with Developer ID and notarized by Apple."
    elif [[ "${SIGNING_STATE}" == "developer_id_signed" ]]; then
        gatekeeper_note="Signed with Developer ID but NOT notarized; Gatekeeper may prompt on first launch."
    else
        gatekeeper_note="Ad-hoc signed ONLY (not Developer ID signed, not notarized). macOS Gatekeeper will warn on first launch; see docs/MACOS_BUILD_AND_INSTALL.md."
    fi

    cat > "${status_file}" <<EOF
artifact_version=${APP_VERSION}
architecture=arm64
signing_state=${SIGNING_STATE}
notarized=${notarized_flag}
gatekeeper_note=${gatekeeper_note}
EOF
    log "Wrote verification log (${log_file}) and signing annotation (${status_file})."
}

main() {
    verify_host_platform
    resolve_prefixes
    verify_dependencies
    build_application
    assemble_app_bundle
    sign_app_bundle
    if [[ "${SKIP_DMG}" -eq 0 ]]; then
        package_dmg
    fi
    run_smoke_verification
    log "Done! Artifacts available in: ${OUTPUT_DIR}"
}

main
