#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
# SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
#
# Reproducible Apple Silicon (arm64) macOS build, self-contained .app bundle,
# Mach-O dylib relocation, code signing, .dmg packaging, and smoke-test
# verification script for Bang On Pro.
#
# Must be executed natively on Apple Silicon macOS (arm64, macOS 13.0+ / CI macos-15).
# Cross-compiling Mach-O arm64 KDE/Qt6/MLT bundles from Linux is NOT supported.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Defaults
BUILD_MODE="auto"          # auto | craft | homebrew
BUILD_TYPE="RelWithDebInfo" # RelWithDebInfo | Release
SOURCE_DIR="${REPO_ROOT}"
BUILD_DIR="${REPO_ROOT}/build-macos-arm64"
STAGE_DIR="${REPO_ROOT}/stage-macos-arm64"
OUTPUT_DIR="${REPO_ROOT}/dist/macos-arm64"
CRAFT_ROOT="${CRAFT_ROOT:-${HOME}/CraftRoot}"
CRAFT_PREFIX=""
APP_NAME="Bang On Pro"
DMG_BASENAME="BangOnPro"
APP_VERSION="${BANGONPRO_VERSION:-1.5.1}"
MIN_MACOS="${MACOSX_DEPLOYMENT_TARGET:-13.0}"
SIGN_IDENTITY="${APPLE_SIGNING_IDENTITY:-${MACOS_SIGNING_IDENTITY:-}}"
NOTARY_PROFILE="${MACOS_NOTARY_PROFILE:-}"
NOTARY_KEY_PATH="${MACOS_NOTARY_KEY_PATH:-}"
NOTARY_KEY_ID="${MACOS_NOTARY_KEY_ID:-}"
NOTARY_ISSUER_ID="${MACOS_NOTARY_ISSUER_ID:-}"
SKIP_TESTS=0
SKIP_DMG=0
SKIP_PYTHON_VENV=0
JOBS="$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4)"

DMG_VERIFY_MOUNT_DIR=""
DMG_VERIFY_APPS_DIR=""
DMG_VERIFY_HOME_DIR=""

cleanup_verification_artifacts() {
    if [[ -n "${DMG_VERIFY_MOUNT_DIR}" && -d "${DMG_VERIFY_MOUNT_DIR}" ]]; then
        if mount | grep -Fq " on ${DMG_VERIFY_MOUNT_DIR} "; then
            hdiutil detach "${DMG_VERIFY_MOUNT_DIR}" >/dev/null 2>&1 || \
                hdiutil detach -force "${DMG_VERIFY_MOUNT_DIR}" >/dev/null 2>&1 || true
        fi
        rm -rf "${DMG_VERIFY_MOUNT_DIR}"
    fi
    if [[ -n "${DMG_VERIFY_APPS_DIR}" && -d "${DMG_VERIFY_APPS_DIR}" ]]; then
        rm -rf "${DMG_VERIFY_APPS_DIR}"
    fi
    if [[ -n "${DMG_VERIFY_HOME_DIR}" && -d "${DMG_VERIFY_HOME_DIR}" ]]; then
        rm -rf "${DMG_VERIFY_HOME_DIR}"
    fi
}
trap cleanup_verification_artifacts EXIT

usage() {
    cat <<'EOF'
Usage: packaging/macos/build_macos_arm64.sh [options]

Build and package a fully self-contained Bang On Pro.app and .dmg natively
for Apple Silicon (arm64) macOS.

Options:
  --mode <auto|craft|homebrew>  Dependency prefix discovery mode (default: auto)
  --build-type <type>           CMake build type: RelWithDebInfo|Release (default: RelWithDebInfo)
  --source-dir <path>           Repository source directory (default: repo root)
  --build-dir <path>            CMake build directory (default: ./build-macos-arm64)
  --output-dir <path>           Output directory for .app/.dmg/logs (default: ./dist/macos-arm64)
  --craft-root <path>           KDE Craft root or CraftMaster target prefix
                                (auto-detects <root>/macos-arm-clang if present)
  --version <ver>               Release version string (default: 1.5.1)
  --min-macos <ver>             MACOSX_DEPLOYMENT_TARGET (default: 13.0)
  --sign-identity <id>          Developer ID Application signing identity
                                (omit to fall back cleanly to ad-hoc signing)
  --notary-profile <profile>    Keychain profile name for `xcrun notarytool`
  --jobs <n>                    Parallel Ninja build jobs
  --skip-tests                  Skip CTest/pytest verification steps
  --skip-dmg                    Build & sign .app bundle only (skip .dmg creation)
  --skip-python-venv            Skip bundling self-contained Python 3.12 virtualenv
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

require_arg() {
    local opt="$1"
    local remaining="$2"
    [[ "${remaining}" -ge 2 ]] || die "Option ${opt} requires a value."
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)
            require_arg "$1" "$#"; BUILD_MODE="$2"; shift 2 ;;
        --build-type)
            require_arg "$1" "$#"; BUILD_TYPE="$2"; shift 2 ;;
        --source-dir)
            require_arg "$1" "$#"; SOURCE_DIR="$2"; shift 2 ;;
        --build-dir)
            require_arg "$1" "$#"; BUILD_DIR="$2"; shift 2 ;;
        --output-dir)
            require_arg "$1" "$#"; OUTPUT_DIR="$2"; shift 2 ;;
        --craft-root)
            require_arg "$1" "$#"; CRAFT_ROOT="$2"; shift 2 ;;
        --version)
            require_arg "$1" "$#"; APP_VERSION="$2"; shift 2 ;;
        --min-macos)
            require_arg "$1" "$#"; MIN_MACOS="$2"; shift 2 ;;
        --sign-identity)
            require_arg "$1" "$#"; SIGN_IDENTITY="$2"; shift 2 ;;
        --notary-profile)
            require_arg "$1" "$#"; NOTARY_PROFILE="$2"; shift 2 ;;
        --jobs)
            require_arg "$1" "$#"; JOBS="$2"; shift 2 ;;
        --skip-tests)
            SKIP_TESTS=1; shift ;;
        --skip-dmg)
            SKIP_DMG=1; shift ;;
        --skip-python-venv)
            SKIP_PYTHON_VENV=1; shift ;;
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

    # Auto-detect CraftMaster target subdirectory (e.g., $CRAFT_ROOT/macos-arm-clang)
    if [[ -d "${CRAFT_ROOT}/macos-arm-clang/lib/cmake/Qt6" || -d "${CRAFT_ROOT}/macos-arm-clang/lib" ]]; then
        CRAFT_PREFIX="${CRAFT_ROOT}/macos-arm-clang"
    elif [[ -d "${CRAFT_ROOT}" ]]; then
        CRAFT_PREFIX="${CRAFT_ROOT}"
    fi

    if [[ "${BUILD_MODE}" == "craft" || ( "${BUILD_MODE}" == "auto" && -n "${CRAFT_PREFIX}" && -d "${CRAFT_PREFIX}" ) ]]; then
        if [[ -n "${CRAFT_PREFIX}" && -d "${CRAFT_PREFIX}" ]]; then
            log "Using KDE Craft Qt6/KF6 prefix: ${CRAFT_PREFIX}"
            prefix_paths+=("${CRAFT_PREFIX}")
            [[ -d "${CRAFT_PREFIX}/dev-utils/bin" ]] && export PATH="${CRAFT_PREFIX}/dev-utils/bin:${PATH}"
            [[ -d "${CRAFT_PREFIX}/bin" ]] && export PATH="${CRAFT_PREFIX}/bin:${PATH}"
            export PKG_CONFIG_PATH="${CRAFT_PREFIX}/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
            export XDG_DATA_DIRS="${CRAFT_PREFIX}/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
        elif [[ "${BUILD_MODE}" == "craft" ]]; then
            die "KDE Craft prefix not found at ${CRAFT_ROOT} (or ${CRAFT_ROOT}/macos-arm-clang). See docs/MACOS_BUILD_AND_INSTALL.md."
        fi
    fi

    if [[ "${BUILD_MODE}" == "homebrew" || "${BUILD_MODE}" == "auto" ]]; then
        if command -v brew >/dev/null 2>&1; then
            local brew_prefix
            brew_prefix="$(brew --prefix)"
            log "Detected Homebrew prefix: ${brew_prefix}"
            prefix_paths+=("${brew_prefix}")
            local keg keg_path
            for keg in qt qt@6 mlt ffmpeg frei0r extra-cmake-modules; do
                keg_path="$(brew --prefix "${keg}" 2>/dev/null || true)"
                if [[ -n "${keg_path}" && -d "${keg_path}" ]]; then
                    prefix_paths+=("${keg_path}")
                    if [[ "${BUILD_MODE}" == "homebrew" ]]; then
                        [[ -d "${keg_path}/bin" ]] && export PATH="${keg_path}/bin:${PATH}"
                        [[ -d "${keg_path}/lib/pkgconfig" ]] && export PKG_CONFIG_PATH="${keg_path}/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
                    else
                        # Keep Craft ahead of Homebrew in auto mode
                        [[ -d "${keg_path}/bin" ]] && export PATH="${PATH}:${keg_path}/bin"
                        [[ -d "${keg_path}/lib/pkgconfig" ]] && export PKG_CONFIG_PATH="${PKG_CONFIG_PATH:-}:${keg_path}/lib/pkgconfig"
                    fi
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
    local required_tools=(cmake ninja pkg-config codesign hdiutil ditto lipo otool install_name_tool plutil shasum)
    local tool
    for tool in "${required_tools[@]}"; do
        command -v "${tool}" >/dev/null 2>&1 || die "Required tool missing: ${tool}"
    done

    # Check Qt6
    local qt6_config=""
    local p
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
    [[ -n "${kf6_config}" ]] || die "KF6CoreAddonsConfig.cmake not found. Bootstrap KDE Frameworks 6 via CraftMaster + packaging/macos/CraftConfig.ini."
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
    log "Configuring Bang On Pro with CMake + Ninja (Qt6/KF6, ${BUILD_TYPE}, arm64, macOS >= ${MIN_MACOS})..."
    mkdir -p "${BUILD_DIR}" "${STAGE_DIR}" "${OUTPUT_DIR}"

    local cmake_args=(
        -S "${SOURCE_DIR}"
        -B "${BUILD_DIR}"
        -G Ninja
        -DCMAKE_BUILD_TYPE="${BUILD_TYPE}"
        -DCMAKE_INSTALL_PREFIX="${STAGE_DIR}"
        -DCMAKE_PREFIX_PATH="${CMAKE_PREFIX_PATH}"
        -DCMAKE_OSX_ARCHITECTURES=arm64
        -DCMAKE_OSX_DEPLOYMENT_TARGET="${MIN_MACOS}"
        -DQT_MAJOR_VERSION=6
        -DBUILD_WITH_QT6=ON
        -DUSE_DBUS=OFF
        -DRELEASE_BUILD=ON
        -DKDE_INSTALL_USE_QT_SYS_PATHS=OFF
        -DCMAKE_DISABLE_FIND_PACKAGE_KF6DocTools=ON
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
    local build_targets=(kdenlive kdenlive_render mltpreview)
    local avail_targets
    avail_targets="$(ninja -C "${BUILD_DIR}" -t targets all 2>/dev/null || true)"
    for extra_t in pofiles tsfiles; do
        if printf '%s\n' "${avail_targets}" | grep -qE "^${extra_t}:"; then
            build_targets+=("${extra_t}")
        fi
    done
    if [[ "${SKIP_TESTS}" -eq 0 ]]; then
        build_targets+=(
            bangonpro_test_timeline_command_engine
            bangonpro_test_ai_assistant_dock
            bangonpro_test_premiere_preset_browser
            bangonpro_test_pro_workspace
            bangonpro_test_roto_studio
            bangonpro_test_agent_control_centre
        )
    fi
    log "Compiling ${build_targets[*]} with ${JOBS} parallel Ninja jobs..."
    cmake --build "${BUILD_DIR}" --target "${build_targets[@]}" --parallel "${JOBS}"

    if [[ "${SKIP_TESTS}" -eq 0 ]]; then
        log "Running Bang On Pro C++ CTest unit tests (offscreen Qt platform)..."
        QT_QPA_PLATFORM=offscreen SDL_AUDIODRIVER=dummy \
            ctest --test-dir "${BUILD_DIR}" -R "^bangonpro_" --output-on-failure --timeout 120 || \
            die "One or more Bang On Pro C++ CTest suites failed."
    fi

    log "Staging install tree into ${STAGE_DIR}..."
    cmake --install "${BUILD_DIR}"
}

# ---------------------------------------------------------------------------
# 4. Recursive Mach-O Dylib Relocator (Zero Leaked Build/Runner/Homebrew Paths)
# ---------------------------------------------------------------------------
resolve_external_lib_path() {
    local dep="$1"
    if [[ -f "${dep}" ]]; then
        printf '%s\n' "${dep}"
        return 0
    fi
    local base
    base="$(basename "${dep}")"
    local search_dirs=()
    [[ -n "${CRAFT_PREFIX}" && -d "${CRAFT_PREFIX}/lib" ]] && search_dirs+=("${CRAFT_PREFIX}/lib")
    [[ -d "${STAGE_DIR}/lib" ]] && search_dirs+=("${STAGE_DIR}/lib")
    if command -v brew >/dev/null 2>&1; then
        search_dirs+=("$(brew --prefix)/lib")
    fi
    local d
    for d in "${search_dirs[@]}"; do
        if [[ -f "${d}/${base}" ]]; then
            printf '%s\n' "${d}/${base}"
            return 0
        fi
    done
    return 1
}

ensure_rpath() {
    local macho="$1"
    local rpath="$2"
    if ! otool -l "${macho}" 2>/dev/null | grep -A 2 "cmd LC_RPATH" | grep -Fq "path ${rpath} "; then
        install_name_tool -add_rpath "${rpath}" "${macho}" 2>/dev/null || true
    fi
}

strip_external_rpaths() {
    local macho="$1"
    local bad_rpath
    while IFS= read -r bad_rpath; do
        [[ -n "${bad_rpath}" ]] || continue
        case "${bad_rpath}" in
            /Users/*|/opt/homebrew*|/usr/local/Cellar*|"${CRAFT_ROOT}"*|"${BUILD_DIR}"*|"${STAGE_DIR}"*|"${REPO_ROOT}"*)
                install_name_tool -delete_rpath "${bad_rpath}" "${macho}" 2>/dev/null || true
                ;;
        esac
    done < <(otool -l "${macho}" 2>/dev/null | awk '/cmd LC_RPATH/{f=1; next} f && /path /{print $2; f=0}')
}

relocate_macho_deps() {
    local app_bundle="$1"
    local frameworks_dir="${app_bundle}/Contents/Frameworks"
    local queue_file="${BUILD_DIR}/macho_reloc_queue.txt"
    local seen_file="${BUILD_DIR}/macho_reloc_seen.txt"

    log "Relocating all non-system Mach-O dylib dependencies into Contents/Frameworks/..."
    mkdir -p "${frameworks_dir}"
    : > "${queue_file}"
    : > "${seen_file}"

    # Seed queue with main executables, bundled tools, frameworks, plugins, and QML libraries
    local scan_roots=("${app_bundle}/Contents/MacOS" "${app_bundle}/Contents/Frameworks" "${app_bundle}/Contents/PlugIns")
    if [[ -d "${app_bundle}/Contents/Resources/qml" ]]; then
        scan_roots+=("${app_bundle}/Contents/Resources/qml")
    fi
    find "${scan_roots[@]}" \
        -type f \( -perm -111 -o -name "*.dylib" -o -name "*.so" \) 2>/dev/null | \
        while IFS= read -r candidate; do
            if file -b "${candidate}" 2>/dev/null | grep -q "Mach-O"; then
                printf '%s\n' "${candidate}" >> "${queue_file}"
            fi
        done

    local pass=0
    while [[ -s "${queue_file}" ]]; do
        pass=$((pass + 1))
        if [[ ${pass} -gt 20 ]]; then
            warn "Mach-O relocation reached maximum pass depth (20); stopping."
            break
        fi

        local next_queue="${BUILD_DIR}/macho_reloc_next.txt"
        : > "${next_queue}"

        while IFS= read -r target; do
            [[ -n "${target}" && -f "${target}" ]] || continue
            if grep -Fxq "${target}" "${seen_file}" 2>/dev/null; then
                continue
            fi
            printf '%s\n' "${target}" >> "${seen_file}"
            chmod u+w "${target}" 2>/dev/null || true

            # Strip any build-host, Craft, or Homebrew absolute LC_RPATH entries first
            strip_external_rpaths "${target}"

            # Ensure appropriate @rpath entries based on Mach-O location inside .app
            case "${target}" in
                "${app_bundle}/Contents/MacOS/"*)
                    ensure_rpath "${target}" "@executable_path/../Frameworks"
                    ;;
                "${app_bundle}/Contents/Frameworks/"*)
                    ensure_rpath "${target}" "@loader_path"
                    ensure_rpath "${target}" "@executable_path/../Frameworks"
                    ;;
                "${app_bundle}/Contents/PlugIns/"*|"${app_bundle}/Contents/Resources/qml/"*)
                    # Normalize plugin's own LC_ID_DYLIB so otool -L does not expose host paths
                    if [[ "${target}" == *.dylib || "${target}" == *.so ]]; then
                        install_name_tool -id "@rpath/$(basename "${target}")" "${target}" 2>/dev/null || true
                    fi
                    ensure_rpath "${target}" "@loader_path/../Frameworks"
                    ensure_rpath "${target}" "@loader_path/../../Frameworks"
                    ensure_rpath "${target}" "@loader_path/../../../Frameworks"
                    ensure_rpath "${target}" "@executable_path/../Frameworks"
                    ;;
            esac

            local dep
            while IFS= read -r dep; do
                [[ -n "${dep}" ]] || continue
                # Skip macOS system libraries and relative loader/executable paths
                case "${dep}" in
                    /System/Library/*|/usr/lib/*|@executable_path/*|@loader_path/*)
                        continue
                        ;;
                esac

                # Handle @rpath references (Craft Qt6/KF6/MLT dylibs & frameworks frequently use @rpath IDs)
                if [[ "${dep}" == @rpath/* ]]; then
                    local rpath_rel="${dep#@rpath/}"
                    if [[ "${rpath_rel}" == *".framework/"* ]]; then
                        local fw_bundle_name="${rpath_rel%%/*}"
                        if [[ -e "${frameworks_dir}/${rpath_rel}" ]]; then
                            continue
                        fi
                        local fw_cand=""
                        local fw_root
                        for fw_root in \
                            "${CRAFT_PREFIX}/lib" \
                            "${CRAFT_PREFIX}/Frameworks" \
                            "${STAGE_DIR}/lib" \
                            "/opt/homebrew/opt/qt@6/lib" \
                            "/opt/homebrew/lib"; do
                            if [[ -n "${fw_root}" && -d "${fw_root}/${fw_bundle_name}" ]]; then
                                fw_cand="${fw_root}/${fw_bundle_name}"
                                break
                            fi
                        done
                        if [[ -n "${fw_cand}" ]]; then
                            cp -R "${fw_cand}" "${frameworks_dir}/${fw_bundle_name}"
                            chmod -R u+w "${frameworks_dir}/${fw_bundle_name}" 2>/dev/null || true
                            if [[ -f "${frameworks_dir}/${rpath_rel}" ]]; then
                                install_name_tool -id "@rpath/${rpath_rel}" "${frameworks_dir}/${rpath_rel}" 2>/dev/null || true
                                printf '%s\n' "${frameworks_dir}/${rpath_rel}" >> "${next_queue}"
                            fi
                        fi
                        continue
                    else
                        local rpath_base
                        rpath_base="$(basename "${rpath_rel}")"
                        if [[ -f "${frameworks_dir}/${rpath_base}" ]]; then
                            if [[ "${rpath_rel}" != "${rpath_base}" ]]; then
                                install_name_tool -change "${dep}" "@rpath/${rpath_base}" "${target}" 2>/dev/null || true
                            fi
                            continue
                        fi
                        local resolved_rpath=""
                        resolved_rpath="$(resolve_external_lib_path "${rpath_base}" || true)"
                        if [[ -n "${resolved_rpath}" && -f "${resolved_rpath}" ]]; then
                            cp -L -f "${resolved_rpath}" "${frameworks_dir}/${rpath_base}"
                            chmod 755 "${frameworks_dir}/${rpath_base}"
                            install_name_tool -id "@rpath/${rpath_base}" "${frameworks_dir}/${rpath_base}" 2>/dev/null || true
                            printf '%s\n' "${frameworks_dir}/${rpath_base}" >> "${next_queue}"
                            if [[ "${rpath_rel}" != "${rpath_base}" ]]; then
                                install_name_tool -change "${dep}" "@rpath/${rpath_base}" "${target}" 2>/dev/null || true
                            fi
                        fi
                        continue
                    fi
                fi

                # Handle non-system Qt/KF6 frameworks referenced by absolute path
                if [[ "${dep}" == *".framework/"* ]]; then
                    local fw_subpath fw_bundle_dir fw_bundle_name
                    fw_subpath="$(printf '%s' "${dep}" | sed -E 's|.*/([^/]+\.framework/.*)|\1|')"
                    fw_bundle_name="${fw_subpath%%/*}"
                    fw_bundle_dir="$(printf '%s' "${dep}" | sed -E 's|(.*\.framework)/.*|\1|')"
                    if [[ ! -d "${frameworks_dir}/${fw_bundle_name}" && -d "${fw_bundle_dir}" ]]; then
                        cp -R "${fw_bundle_dir}" "${frameworks_dir}/${fw_bundle_name}"
                        if [[ -f "${frameworks_dir}/${fw_subpath}" ]]; then
                            chmod u+w "${frameworks_dir}/${fw_subpath}" 2>/dev/null || true
                            install_name_tool -id "@rpath/${fw_subpath}" "${frameworks_dir}/${fw_subpath}" 2>/dev/null || true
                            printf '%s\n' "${frameworks_dir}/${fw_subpath}" >> "${next_queue}"
                        fi
                    fi
                    if [[ -e "${frameworks_dir}/${fw_subpath}" ]]; then
                        install_name_tool -change "${dep}" "@rpath/${fw_subpath}" "${target}" 2>/dev/null || true
                        continue
                    fi
                fi

                local resolved_dep=""
                resolved_dep="$(resolve_external_lib_path "${dep}" || true)"
                if [[ -z "${resolved_dep}" || ! -f "${resolved_dep}" ]]; then
                    warn "Could not resolve external Mach-O dependency '${dep}' referenced by '${target}'"
                    continue
                fi

                local dep_base
                dep_base="$(basename "${dep}")"
                local bundled_lib="${frameworks_dir}/${dep_base}"

                if [[ ! -f "${bundled_lib}" ]]; then
                    cp -L -f "${resolved_dep}" "${bundled_lib}"
                    chmod 755 "${bundled_lib}"
                    install_name_tool -id "@rpath/${dep_base}" "${bundled_lib}" 2>/dev/null || true
                    printf '%s\n' "${bundled_lib}" >> "${next_queue}"
                fi

                install_name_tool -change "${dep}" "@rpath/${dep_base}" "${target}" 2>/dev/null || true
            done < <(otool -L "${target}" 2>/dev/null | tail -n +2 | awk '{print $1}')
        done < "${queue_file}"

        mv -f "${next_queue}" "${queue_file}"
    done

    rm -f "${queue_file}" "${seen_file}"
    log "Completed Mach-O dependency relocation in ${pass} pass(es)."
}

# ---------------------------------------------------------------------------
# 5. Assemble Self-Contained Native arm64 Bang On Pro.app Bundle
# ---------------------------------------------------------------------------
copy_plugin_dir_if_exists() {
    local src_dir="$1"
    local dst_dir="$2"
    if [[ -d "${src_dir}" ]]; then
        mkdir -p "${dst_dir}"
        cp -RL "${src_dir}/." "${dst_dir}/"
        return 0
    fi
    return 1
}

assemble_app_bundle() {
    local app_bundle="${OUTPUT_DIR}/${APP_NAME}.app"
    local contents_dir="${app_bundle}/Contents"
    local macos_dir="${contents_dir}/MacOS"
    local frameworks_dir="${contents_dir}/Frameworks"
    local plugins_dir="${contents_dir}/PlugIns"
    local resources_dir="${contents_dir}/Resources"
    local licenses_out="${resources_dir}/LICENSES"

    log "Assembling self-contained ${app_bundle}..."
    rm -rf "${app_bundle}"
    mkdir -p "${macos_dir}" "${frameworks_dir}" "${plugins_dir}" "${resources_dir}" "${licenses_out}"

    # 5.1 Locate and copy compiled Bang On Pro / Kdenlive Mach-O executable
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
    elif [[ -x "${BUILD_DIR}/bin/kdenlive_render" ]]; then
        cp -f "${BUILD_DIR}/bin/kdenlive_render" "${macos_dir}/kdenlive_render"
        chmod 755 "${macos_dir}/kdenlive_render"
    fi

    # 5.2 Bundle melt, ffmpeg, ffprobe for zero-setup self-contained rendering
    local media_bin
    for media_bin in melt ffmpeg ffprobe; do
        if command -v "${media_bin}" >/dev/null 2>&1; then
            cp -L -f "$(command -v "${media_bin}")" "${macos_dir}/${media_bin}"
            chmod 755 "${macos_dir}/${media_bin}"
        fi
    done

    # 5.3 Bundle staged Kdenlive/Bang On Pro share/ data, Breeze icons, MLT 7, and frei0r plugins
    if [[ -d "${STAGE_DIR}/share" ]]; then
        cp -RL "${STAGE_DIR}/share" "${resources_dir}/share"
        for sub in kdenlive kxmlgui6 kxmlgui5 knotifications6; do
            if [[ -d "${STAGE_DIR}/share/${sub}" ]]; then
                copy_plugin_dir_if_exists "${STAGE_DIR}/share/${sub}" "${resources_dir}/${sub}"
            fi
        done
    fi

    # Bundle Breeze icons (directory and/or compiled .rcc icon theme)
    local icon_src
    for icon_src in \
        "${CRAFT_PREFIX}/share/icons" \
        "${STAGE_DIR}/share/icons" \
        "/opt/homebrew/share/icons"; do
        if [[ -d "${icon_src}" ]]; then
            copy_plugin_dir_if_exists "${icon_src}" "${resources_dir}/icons" && break
        fi
    done
    local rcc_file
    for rcc_file in \
        "${CRAFT_PREFIX}/bin/data/icontheme.rcc" \
        "${CRAFT_PREFIX}/share/icons/breeze/breeze-icons.rcc" \
        "${CRAFT_PREFIX}/share/icons/breeze-dark/breeze-icons-dark.rcc"; do
        if [[ -f "${rcc_file}" ]]; then
            cp -f "${rcc_file}" "${resources_dir}/$(basename "${rcc_file}")"
        fi
    done

    # Bundle MLT 7 modules -> Contents/PlugIns/mlt/ (Craft relocatable macOS default) and data -> Contents/Resources/mlt/
    local mlt_mod_dir mlt_data_dir
    mlt_mod_dir="$(pkg-config --variable=moduledir mlt-framework-7 2>/dev/null || true)"
    mlt_data_dir="$(pkg-config --variable=mltdatadir mlt-framework-7 2>/dev/null || true)"
    if [[ -z "${mlt_mod_dir}" || ! -d "${mlt_mod_dir}" ]]; then
        for cand in \
            "${CRAFT_PREFIX}/lib/mlt" \
            "${CRAFT_PREFIX}/lib/mlt-7" \
            "${CRAFT_PREFIX}/plugins/mlt" \
            "${CRAFT_PREFIX}/PlugIns/mlt" \
            "/opt/homebrew/lib/mlt-7" \
            "/opt/homebrew/lib/mlt"; do
            if [[ -d "${cand}" ]] && ls "${cand}"/libmlt* >/dev/null 2>&1; then
                mlt_mod_dir="${cand}"
                break
            fi
        done
    fi
    if [[ -z "${mlt_mod_dir}" || ! -d "${mlt_mod_dir}" ]] && [[ -n "${CRAFT_PREFIX}" && -d "${CRAFT_PREFIX}" ]]; then
        local found_avfmt
        found_avfmt="$(find "${CRAFT_PREFIX}" -maxdepth 5 -type f -name 'libmltavformat*' 2>/dev/null | head -n 1 || true)"
        if [[ -n "${found_avfmt}" ]]; then
            mlt_mod_dir="$(dirname "${found_avfmt}")"
        fi
    fi
    if [[ -z "${mlt_data_dir}" || ! -d "${mlt_data_dir}" ]]; then
        for cand in \
            "${CRAFT_PREFIX}/share/mlt" \
            "${CRAFT_PREFIX}/share/mlt-7" \
            "${CRAFT_PREFIX}/Resources/mlt" \
            "/opt/homebrew/share/mlt-7" \
            "/opt/homebrew/share/mlt"; do
            [[ -d "${cand}" ]] && mlt_data_dir="${cand}" && break
        done
    fi
    if [[ -n "${mlt_mod_dir}" && -d "${mlt_mod_dir}" ]]; then
        log "Bundling MLT 7 modules from ${mlt_mod_dir} -> Contents/PlugIns/mlt/..."
        copy_plugin_dir_if_exists "${mlt_mod_dir}" "${plugins_dir}/mlt"
    else
        die "MLT 7 modules directory not found in ${CRAFT_PREFIX} or /opt/homebrew (libmltavformat missing)."
    fi
    if [[ -n "${mlt_data_dir}" && -d "${mlt_data_dir}" ]]; then
        log "Bundling MLT 7 profiles & presets from ${mlt_data_dir} -> Contents/Resources/mlt/..."
        copy_plugin_dir_if_exists "${mlt_data_dir}" "${resources_dir}/mlt"
    else
        warn "MLT 7 data directory not found; profiles may be incomplete."
    fi

    # Bundle frei0r plugins -> Contents/PlugIns/frei0r-1/
    local frei0r_dir=""
    local cand
    for cand in \
        "${CRAFT_PREFIX}/lib/frei0r-1" \
        "${CRAFT_PREFIX}/plugins/frei0r-1" \
        "${CRAFT_PREFIX}/PlugIns/frei0r-1" \
        "/opt/homebrew/lib/frei0r-1" \
        "/usr/local/lib/frei0r-1"; do
        if [[ -d "${cand}" ]]; then
            frei0r_dir="${cand}"
            break
        fi
    done
    if [[ -z "${frei0r_dir}" && -n "${CRAFT_PREFIX}" && -d "${CRAFT_PREFIX}" ]]; then
        frei0r_dir="$(find "${CRAFT_PREFIX}" -maxdepth 5 -type d -name 'frei0r-1' 2>/dev/null | head -n 1 || true)"
    fi
    if [[ -n "${frei0r_dir}" ]]; then
        log "Bundling frei0r plugins from ${frei0r_dir}..."
        copy_plugin_dir_if_exists "${frei0r_dir}" "${plugins_dir}/frei0r-1"
    fi

    # 5.4 Bundle Bang On Pro Python modules & self-contained Python 3.12 virtualenv
    if [[ -d "${SOURCE_DIR}/bangonpro" ]]; then
        cp -R "${SOURCE_DIR}/bangonpro" "${resources_dir}/bangonpro"
        find "${resources_dir}/bangonpro" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
    fi

    if [[ "${SKIP_PYTHON_VENV}" -eq 0 ]]; then
        [[ -x "${SCRIPT_DIR}/bootstrap_ai_runtime.sh" ]] || die "Required script missing or not executable: ${SCRIPT_DIR}/bootstrap_ai_runtime.sh"
        local py_req_file="${SOURCE_DIR}/requirements-bangonpro.lock"
        if [[ ! -f "${py_req_file}" ]]; then
            py_req_file="${SOURCE_DIR}/requirements-bangonpro.txt"
        fi
        log "Building self-contained Python 3.12 runtime inside Contents/Resources/python-env/ using $(basename "${py_req_file}")..."
        # V1.2 managed AI runtime: bundle the faster-whisper (CTranslate2) + onnxruntime packages so genuine
        # local Whisper / MobileSAM inference works out of the box; model WEIGHTS stay on-demand via the
        # in-app Model Manager (consent + SHA-256 verified). Override with BANGONPRO_BUNDLE_AI_TIER=none.
        local ai_tier="${BANGONPRO_BUNDLE_AI_TIER:-tier1}"
        local tier_args=()
        if [[ "${ai_tier}" != "none" ]]; then
            tier_args=(--tier "${ai_tier}")
        fi
        "${SCRIPT_DIR}/bootstrap_ai_runtime.sh" \
            --venv-dir "${resources_dir}/python-env" \
            --requirements "${py_req_file}" \
            ${tier_args[@]+"${tier_args[@]}"}
    else
        warn "Skipping bundled Python 3.12 virtualenv (--skip-python-venv specified)."
    fi

    # 5.5 Bundle GPL-3.0 licence files, component matrix, and ArtCraft attribution notice
    cp -f "${SOURCE_DIR}/COPYING" "${resources_dir}/COPYING"
    cp -f "${SOURCE_DIR}/COPYING" "${licenses_out}/COPYING"
    if [[ -d "${SOURCE_DIR}/LICENSES" ]]; then
        cp -RL "${SOURCE_DIR}/LICENSES/." "${licenses_out}/"
    fi
    if [[ -f "${SOURCE_DIR}/docs/OPEN_SOURCE_COMPONENTS.md" ]]; then
        cp -f "${SOURCE_DIR}/docs/OPEN_SOURCE_COMPONENTS.md" "${resources_dir}/OPEN_SOURCE_COMPONENTS.md"
        cp -f "${SOURCE_DIR}/docs/OPEN_SOURCE_COMPONENTS.md" "${licenses_out}/OPEN_SOURCE_COMPONENTS.md"
    fi
    cat > "${licenses_out}/ARTCRAFT_NOTICE.txt" <<'EOF'
Bang On Pro — ArtCraft Selective Integration Attribution Notice
===============================================================

Selected architecture patterns and rotoscoping/node-workflow concepts in
Bang On Pro draw inspiration from or selectively integrate open-source code
from ArtCraft (https://github.com/storytold/artcraft), dual-licensed under
the MIT License OR Apache License 2.0 (SPDX: MIT OR Apache-2.0).

In compliance with both licences and Bang On Pro's GPL-3.0 distribution:
- Full MIT and Apache-2.0 licence texts are preserved alongside this notice
  in Contents/Resources/LICENSES/MIT.txt and Apache-2.0.txt.
- Non-core third-party ArtCraft media assets (sound/, memes/, skybox/, pose/,
  characters_gif/) are strictly excluded and are NOT bundled in Bang On Pro.
EOF

    # 5.6 Build .icns icon from repo PNG icon set if iconutil is available
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

    # 5.7 Render Info.plist from template
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

    # 5.8 Deploy Qt6 frameworks, QML modules, and Qt/KF6 plugins via macdeployqt
    if command -v macdeployqt >/dev/null 2>&1; then
        log "Running macdeployqt to bundle Qt6 frameworks, plugins, and QML modules..."
        local deploy_args=()
        [[ -d "${SOURCE_DIR}/src/timeline2/view/qml" ]] && deploy_args+=("-qmldir=${SOURCE_DIR}/src/timeline2/view/qml")
        [[ -n "${CRAFT_PREFIX}" && -d "${CRAFT_PREFIX}/lib" ]] && deploy_args+=("-libpath=${CRAFT_PREFIX}/lib")
        macdeployqt "${app_bundle}" "${deploy_args[@]}" -verbose=1 || warn "macdeployqt reported warnings; continuing with recursive Mach-O relocation."
    else
        warn "macdeployqt not found; relying on recursive Mach-O relocation."
    fi

    # Ensure critical Qt6 & KF6 plugin subdirectories are present inside Contents/PlugIns/
    local qt_plug_root=""
    for cand in "${CRAFT_PREFIX}/plugins" "${CRAFT_PREFIX}/lib/qt6/plugins" "/opt/homebrew/opt/qt@6/share/qt/plugins"; do
        if [[ -d "${cand}" ]]; then
            qt_plug_root="${cand}"
            break
        fi
    done
    if [[ -n "${qt_plug_root}" ]]; then
        local psub
        for psub in platforms styles imageformats iconengines multimedia tls networkinformation kf6; do
            if [[ -d "${qt_plug_root}/${psub}" ]]; then
                copy_plugin_dir_if_exists "${qt_plug_root}/${psub}" "${plugins_dir}/${psub}"
            fi
        done
    fi

    # Copy QML modules from Craft prefix if present so QtQuick/KF6 QML imports work offline
    if [[ -n "${CRAFT_PREFIX}" && -d "${CRAFT_PREFIX}/qml" ]]; then
        mkdir -p "${resources_dir}/qml"
        cp -RL "${CRAFT_PREFIX}/qml/." "${resources_dir}/qml/" 2>/dev/null || true
    fi

    # Ensure qt.conf points Qt to internal PlugIns and Resources/qml
    cat > "${resources_dir}/qt.conf" <<'EOF'
[Paths]
Plugins = PlugIns
Imports = Resources/qml
Qml2Imports = Resources/qml
QmlImports = Resources/qml
EOF

    # 5.9 Recursively relocate ALL remaining external Mach-O dependencies
    relocate_macho_deps "${app_bundle}"

    # 5.10 Verify native arm64 Mach-O binary
    local archs
    archs="$(lipo -archs "${macos_dir}/${APP_NAME}")"
    log "Mach-O architectures for '${APP_NAME}': ${archs}"
    if [[ "${archs}" != *"arm64"* ]]; then
        die "Compiled binary does not contain arm64 slice (got: ${archs})."
    fi
}

# ---------------------------------------------------------------------------
# 6. Code Signing (Developer ID with Hardened Runtime, or Ad-Hoc Fallback)
# ---------------------------------------------------------------------------
SIGNING_STATE="unknown"

sign_nested_macho_objects() {
    local app_bundle="$1"
    local sign_identity="$2"
    local hardened_flags="$3"

    # Sign all nested .dylib and .so libraries inside-out
    find "${app_bundle}/Contents" -type f \( -name "*.dylib" -o -name "*.so" \) -print0 | \
        while IFS= read -r -d '' lib; do
            if [[ "${hardened_flags}" == "hardened" ]]; then
                codesign --force --options runtime --timestamp --sign "${sign_identity}" "${lib}"
            else
                codesign --force --sign - "${lib}"
            fi
        done

    # Sign any Mach-O binaries inside embedded python-env/bin
    if [[ -d "${app_bundle}/Contents/Resources/python-env/bin" ]]; then
        find "${app_bundle}/Contents/Resources/python-env/bin" -type f -print0 | \
            while IFS= read -r -d '' pybin; do
                if file -b "${pybin}" 2>/dev/null | grep -q "Mach-O"; then
                    if [[ "${hardened_flags}" == "hardened" ]]; then
                        codesign --force --options runtime --timestamp --sign "${sign_identity}" "${pybin}"
                    else
                        codesign --force --sign - "${pybin}"
                    fi
                fi
            done
    fi

    # Sign bundled frameworks
    if [[ -d "${app_bundle}/Contents/Frameworks" ]]; then
        find "${app_bundle}/Contents/Frameworks" -maxdepth 2 -name "*.framework" -print0 | \
            while IFS= read -r -d '' fw; do
                if [[ "${hardened_flags}" == "hardened" ]]; then
                    codesign --force --options runtime --timestamp --sign "${sign_identity}" "${fw}"
                else
                    codesign --force --sign - "${fw}"
                fi
            done
    fi
}

sign_app_bundle() {
    local app_bundle="${OUTPUT_DIR}/${APP_NAME}.app"
    local entitlements="${SCRIPT_DIR}/entitlements.plist"

    if [[ -n "${SIGN_IDENTITY}" ]] && security find-identity -v -p codesigning 2>/dev/null | grep -Fq "${SIGN_IDENTITY}"; then
        log "Developer ID signing identity found: ${SIGN_IDENTITY}"
        log "Signing nested binaries, frameworks, Python native extensions, and plugins with Hardened Runtime..."

        sign_nested_macho_objects "${app_bundle}" "${SIGN_IDENTITY}" "hardened"

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
        sign_nested_macho_objects "${app_bundle}" "-" "adhoc"
        local helper
        for helper in kdenlive_render melt ffmpeg ffprobe; do
            if [[ -x "${app_bundle}/Contents/MacOS/${helper}" ]]; then
                codesign --force --sign - --entitlements "${entitlements}" "${app_bundle}/Contents/MacOS/${helper}"
            fi
        done
        codesign --force --deep --sign - --entitlements "${entitlements}" "${app_bundle}"
        codesign --verify --deep --strict --verbose=2 "${app_bundle}"
        SIGNING_STATE="adhoc_signed_not_notarized"
        log "Applied ad-hoc signature (local/community build — Gatekeeper will require explicit user approval)."
    fi
}

# ---------------------------------------------------------------------------
# 7. Package .dmg & Optional Apple Notarization
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
# 8. Smoke-Test Launch Verification, Dylib/RPATH Audit, DMG Clean-Copy Test & Metadata
# ---------------------------------------------------------------------------
append_verify_log() {
    local log_file="$1"
    shift
    printf '%s\n' "$@" | tee -a "${log_file}"
}

audit_no_leaked_dylib_paths() {
    local app_bundle="$1"
    local leaked=0
    local checked=0
    local scan_roots=(
        "${app_bundle}/Contents/MacOS"
        "${app_bundle}/Contents/Frameworks"
        "${app_bundle}/Contents/PlugIns"
    )
    if [[ -d "${app_bundle}/Contents/Resources/qml" ]]; then
        scan_roots+=("${app_bundle}/Contents/Resources/qml")
    fi

    local leak_regex='^(/Users/|/opt/homebrew|/usr/local/Cellar'
    [[ -n "${CRAFT_ROOT}" ]] && leak_regex="${leak_regex}|${CRAFT_ROOT}"
    [[ -n "${BUILD_DIR}" ]] && leak_regex="${leak_regex}|${BUILD_DIR}"
    [[ -n "${STAGE_DIR}" ]] && leak_regex="${leak_regex}|${STAGE_DIR}"
    leak_regex="${leak_regex})"

    local bin
    while IFS= read -r bin; do
        [[ -n "${bin}" ]] || continue
        if ! file -b "${bin}" 2>/dev/null | grep -q "Mach-O"; then
            continue
        fi
        checked=$((checked + 1))

        local otool_l_out="" otool_l_rc=0
        otool_l_out="$(otool -L "${bin}" 2>&1)" || otool_l_rc=$?
        if [[ "${otool_l_rc}" -ne 0 ]]; then
            printf 'OTOOL_L_FAILED (rc=%d) for %s:\n%s\n' "${otool_l_rc}" "${bin}" "${otool_l_out}"
            leaked=1
            continue
        fi

        local bad_deps
        bad_deps="$(printf '%s\n' "${otool_l_out}" | tail -n +2 | awk '{print $1}' | grep -E "${leak_regex}" || true)"
        if [[ -n "${bad_deps}" ]]; then
            printf 'LEAKED DYLIB PATH in %s:\n%s\n' "${bin}" "${bad_deps}"
            leaked=1
        fi

        local otool_r_out="" otool_r_rc=0
        otool_r_out="$(otool -l "${bin}" 2>&1)" || otool_r_rc=$?
        if [[ "${otool_r_rc}" -ne 0 ]]; then
            printf 'OTOOL_RPATH_FAILED (rc=%d) for %s:\n%s\n' "${otool_r_rc}" "${bin}" "${otool_r_out}"
            leaked=1
            continue
        fi

        local bad_rpaths
        bad_rpaths="$(printf '%s\n' "${otool_r_out}" | awk '/cmd LC_RPATH/{f=1; next} f && /path /{print $2; f=0}' | grep -E "${leak_regex}" || true)"
        if [[ -n "${bad_rpaths}" ]]; then
            printf 'LEAKED LC_RPATH in %s:\n%s\n' "${bin}" "${bad_rpaths}"
            leaked=1
        fi
    done < <(find "${scan_roots[@]}" -type f \( -perm -111 -o -name "*.dylib" -o -name "*.so" \) 2>/dev/null)

    if [[ "${checked}" -eq 0 ]]; then
        printf 'LEAKED DYLIB PATH: no Mach-O binaries discovered inside %s\n' "${app_bundle}"
        return 1
    fi
    if [[ "${leaked}" -ne 0 ]]; then
        return 1
    fi
    printf 'PASS: Audited %d Mach-O object(s); zero leaked host/Homebrew/Craft dylib or LC_RPATH paths.\n' "${checked}"
    return 0
}

audit_bundle_security_hygiene() {
    local app_bundle="$1"
    local bad_files
    bad_files="$(find "${app_bundle}" -type f \( -name "*.p12" -o -name "*.p8" -o -name ".env" -o -name ".netrc" -o -name "*.prfpset" -o -name "effects pr.zip" \) 2>/dev/null || true)"
    if [[ -n "${bad_files}" ]]; then
        printf 'FORBIDDEN FILE(S) IN BUNDLE:\n%s\n' "${bad_files}"
        return 1
    fi

    if [[ -d "${app_bundle}/Contents/Resources/bangonpro" ]]; then
        local bad_artcraft
        bad_artcraft="$(find "${app_bundle}/Contents/Resources/bangonpro" -type d \( -name "sound" -o -name "memes" -o -name "skybox" -o -name "pose" -o -name "characters_gif" \) 2>/dev/null || true)"
        if [[ -n "${bad_artcraft}" ]]; then
            printf 'EXCLUDED ARTCRAFT ASSET DIR(S) IN BUNDLE:\n%s\n' "${bad_artcraft}"
            return 1
        fi
    fi

    printf 'PASS: Zero credentials, signing keys, proprietary presets, or excluded ArtCraft asset dirs in bundle.\n'
    return 0
}

verify_bundle_launch() {
    local app_bundle="$1"
    local context_label="$2"
    local log_file="$3"

    local abs_bundle
    abs_bundle="$(cd "$(dirname "${app_bundle}")" && pwd)/$(basename "${app_bundle}")"
    [[ -d "${abs_bundle}" ]] || die "[${context_label}] App bundle directory missing: ${abs_bundle}"

    append_verify_log "${log_file}" "" "=== ${context_label}: ${abs_bundle} ==="

    # 1. Info.plist validation
    append_verify_log "${log_file}" "--- Info.plist Validation ---"
    local out="" rc=0
    out="$(plutil -lint "${abs_bundle}/Contents/Info.plist" 2>&1)" || rc=$?
    append_verify_log "${log_file}" "${out}"
    [[ "${rc}" -eq 0 ]] || die "[${context_label}] plutil -lint failed (rc=${rc})"

    # 2. Mach-O architecture check (main app + bundled helper binaries)
    append_verify_log "${log_file}" "" "--- Mach-O Architecture Check ---"
    rc=0; out="$(file "${abs_bundle}/Contents/MacOS/${APP_NAME}" 2>&1)" || rc=$?
    append_verify_log "${log_file}" "${out}"
    [[ "${rc}" -eq 0 ]] || die "[${context_label}] file check failed for ${APP_NAME} (rc=${rc})"

    local archs=""
    rc=0; archs="$(lipo -archs "${abs_bundle}/Contents/MacOS/${APP_NAME}" 2>&1)" || rc=$?
    append_verify_log "${log_file}" "lipo -archs (${APP_NAME}): ${archs}"
    [[ "${rc}" -eq 0 && "${archs}" == "arm64" ]] || die "[${context_label}] Expected arm64 Mach-O slice for ${APP_NAME}, got '${archs}' (rc=${rc})"

    local helper
    for helper in kdenlive_render melt ffmpeg ffprobe; do
        if [[ -x "${abs_bundle}/Contents/MacOS/${helper}" ]]; then
            local h_archs="" h_rc=0
            h_archs="$(lipo -archs "${abs_bundle}/Contents/MacOS/${helper}" 2>&1)" || h_rc=$?
            append_verify_log "${log_file}" "lipo -archs (${helper}): ${h_archs}"
            [[ "${h_rc}" -eq 0 && "${h_archs}" == *"arm64"* ]] || die "[${context_label}] Helper '${helper}' missing arm64 slice (got '${h_archs}', rc=${h_rc})"
        fi
    done

    # 3. Code signature inspection & strict deep verification (never masked)
    append_verify_log "${log_file}" "" "--- Code Signature Verification ---"
    rc=0; out="$(codesign -dvv "${abs_bundle}" 2>&1)" || rc=$?
    append_verify_log "${log_file}" "${out}"
    [[ "${rc}" -eq 0 ]] || die "[${context_label}] codesign -dvv failed (rc=${rc})"

    rc=0; out="$(codesign --verify --deep --strict --verbose=2 "${abs_bundle}" 2>&1)" || rc=$?
    append_verify_log "${log_file}" "${out}"
    [[ "${rc}" -eq 0 ]] || die "[${context_label}] codesign --verify --deep --strict failed (rc=${rc})"

    # 4. Self-contained Mach-O dylib & LC_RPATH audit
    append_verify_log "${log_file}" "" "--- Self-Contained Mach-O Dylib & LC_RPATH Audit ---"
    rc=0; out="$(audit_no_leaked_dylib_paths "${abs_bundle}" 2>&1)" || rc=$?
    append_verify_log "${log_file}" "${out}"
    [[ "${rc}" -eq 0 ]] || die "[${context_label}] Mach-O dylib / LC_RPATH audit failed (rc=${rc})"

    # 5. Bundle credential & compliance hygiene audit
    append_verify_log "${log_file}" "" "--- Bundle Security & Asset Hygiene Audit ---"
    rc=0; out="$(audit_bundle_security_hygiene "${abs_bundle}" 2>&1)" || rc=$?
    append_verify_log "${log_file}" "${out}"
    [[ "${rc}" -eq 0 ]] || die "[${context_label}] Bundle security hygiene audit failed (rc=${rc})"

    # Prepare clean minimal environment with zero inherited Craft/Homebrew/CI secrets
    local clean_home
    clean_home="$(mktemp -d "${TMPDIR:-/tmp}/bangonpro-verify-home.XXXXXX")"
    chmod 700 "${clean_home}"
    DMG_VERIFY_HOME_DIR="${clean_home}"
    local clean_tmp="${clean_home}/tmp"
    mkdir -p "${clean_tmp}"
    chmod 700 "${clean_tmp}"
    local clean_user="${USER:-$(id -un)}"

    # 6. Headless launch smoke test (--version)
    append_verify_log "${log_file}" "" "--- Headless Launch Smoke Test (--version) ---"
    rc=0
    out="$(env -i \
        HOME="${clean_home}" \
        TMPDIR="${clean_tmp}" \
        USER="${clean_user}" \
        LANG="en_US.UTF-8" \
        PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
        MLT_REPOSITORY="${abs_bundle}/Contents/PlugIns/mlt" \
        MLT_DATA="${abs_bundle}/Contents/Resources/mlt" \
        FREI0R_PATH="${abs_bundle}/Contents/PlugIns/frei0r-1" \
        QT_QPA_PLATFORM=offscreen \
        SDL_AUDIODRIVER=dummy \
        "${abs_bundle}/Contents/MacOS/${APP_NAME}" --version 2>&1)" || rc=$?
    append_verify_log "${log_file}" "${out}"
    [[ "${rc}" -eq 0 ]] || die "[${context_label}] '${APP_NAME} --version' failed (rc=${rc})"
    [[ -n "${out}" ]] || die "[${context_label}] '${APP_NAME} --version' produced empty output"

    # 7. Bundled Media Stack (MLT 7 + FFmpeg VideoToolbox)
    append_verify_log "${log_file}" "" "--- Bundled Media Stack (MLT 7 + FFmpeg VideoToolbox) ---"
    [[ -x "${abs_bundle}/Contents/MacOS/melt" ]] || die "[${context_label}] Bundled melt binary missing from Contents/MacOS/melt"
    rc=0
    out="$(env -i \
        HOME="${clean_home}" \
        TMPDIR="${clean_tmp}" \
        USER="${clean_user}" \
        PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
        MLT_REPOSITORY="${abs_bundle}/Contents/PlugIns/mlt" \
        MLT_DATA="${abs_bundle}/Contents/Resources/mlt" \
        FREI0R_PATH="${abs_bundle}/Contents/PlugIns/frei0r-1" \
        "${abs_bundle}/Contents/MacOS/melt" --version 2>&1)" || rc=$?
    append_verify_log "${log_file}" "$(printf '%s\n' "${out}" | head -n 3)"
    [[ "${rc}" -eq 0 ]] || die "[${context_label}] Bundled melt --version failed (rc=${rc})"

    rc=0
    out="$(env -i \
        HOME="${clean_home}" \
        TMPDIR="${clean_tmp}" \
        USER="${clean_user}" \
        PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
        MLT_REPOSITORY="${abs_bundle}/Contents/PlugIns/mlt" \
        MLT_DATA="${abs_bundle}/Contents/Resources/mlt" \
        FREI0R_PATH="${abs_bundle}/Contents/PlugIns/frei0r-1" \
        "${abs_bundle}/Contents/MacOS/melt" -query producers 2>&1)" || rc=$?
    [[ "${rc}" -eq 0 ]] || {
        append_verify_log "${log_file}" "${out}"
        die "[${context_label}] Bundled melt -query producers failed (rc=${rc})"
    }
    if printf '%s\n' "${out}" | grep -q "avformat"; then
        append_verify_log "${log_file}" "PASS: Bundled melt loaded avformat producer from Contents/PlugIns/mlt"
    else
        append_verify_log "${log_file}" "${out}"
        die "[${context_label}] Bundled melt failed to load avformat producer from Contents/PlugIns/mlt"
    fi

    [[ -x "${abs_bundle}/Contents/MacOS/ffmpeg" ]] || die "[${context_label}] Bundled ffmpeg binary missing from Contents/MacOS/ffmpeg"
    rc=0
    out="$(env -i HOME="${clean_home}" TMPDIR="${clean_tmp}" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
        "${abs_bundle}/Contents/MacOS/ffmpeg" -version 2>&1)" || rc=$?
    append_verify_log "${log_file}" "$(printf '%s\n' "${out}" | head -n 2)"
    [[ "${rc}" -eq 0 ]] || die "[${context_label}] Bundled ffmpeg -version failed (rc=${rc})"

    rc=0
    out="$(env -i HOME="${clean_home}" TMPDIR="${clean_tmp}" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
        "${abs_bundle}/Contents/MacOS/ffmpeg" -hide_banner -hwaccels 2>&1)" || rc=$?
    append_verify_log "${log_file}" "${out}"
    [[ "${rc}" -eq 0 ]] || die "[${context_label}] Bundled ffmpeg -hwaccels failed (rc=${rc})"

    # 8. Bundled Self-Contained Python AI Engine CLI Smoke Test
    append_verify_log "${log_file}" "" "--- Bundled Self-Contained Python AI Engine CLI Smoke Test ---"
    if [[ "${SKIP_PYTHON_VENV}" -eq 0 ]]; then
        local bundled_py="${abs_bundle}/Contents/Resources/python-env/bin/python3"
        [[ -x "${bundled_py}" ]] || die "[${context_label}] Bundled Python interpreter missing: ${bundled_py}"

        rc=0
        out="$(env -i \
            HOME="${clean_home}" \
            TMPDIR="${clean_tmp}" \
            USER="${clean_user}" \
            LANG="en_US.UTF-8" \
            PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
            PYTHONNOUSERSITE=1 \
            PYTHONPATH="${abs_bundle}/Contents/Resources" \
            "${bundled_py}" -s -m bangonpro.ai_engine.cli --help 2>&1)" || rc=$?
        append_verify_log "${log_file}" "$(printf '%s\n' "${out}" | head -n 20)"
        [[ "${rc}" -eq 0 ]] || die "[${context_label}] Bundled Python AI Engine CLI --help failed (rc=${rc})"

        rc=0
        out="$(env -i \
            HOME="${clean_home}" \
            TMPDIR="${clean_tmp}" \
            USER="${clean_user}" \
            LANG="en_US.UTF-8" \
            PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
            PYTHONNOUSERSITE=1 \
            PYTHONPATH="${abs_bundle}/Contents/Resources" \
            "${bundled_py}" -s -c "import sqlite3, bangonpro.ai_engine, bangonpro.ai_engine.department.cli, bangonpro.ai_engine.department.intelligence, bangonpro.ai_engine.mcp.__main__, bangonpro.ai_engine.mcp.reasoning, bangonpro.show_profiles.loader, bangonpro.macos, bangonpro.premiere_compat, bangonpro.roto_studio; sqlite3.connect(':memory:').execute('PRAGMA journal_mode=WAL'); print('PASS: All Bang On Pro Python packages (department, MCP agents, shared reasoning router, learning store) importable offline')" 2>&1)" || rc=$?
        append_verify_log "${log_file}" "${out}"
        [[ "${rc}" -eq 0 ]] || die "[${context_label}] Bundled Python module import smoke check failed (rc=${rc})"
    else
        append_verify_log "${log_file}" "SKIPPED: bundled python-env disabled (--skip-python-venv)."
    fi

    # 9. Bundled Licence & Attribution Files
    append_verify_log "${log_file}" "" "--- Bundled Licence & Attribution Files ---"
    local req_lic
    for req_lic in \
        "${abs_bundle}/Contents/Resources/COPYING" \
        "${abs_bundle}/Contents/Resources/LICENSES/COPYING" \
        "${abs_bundle}/Contents/Resources/LICENSES/ARTCRAFT_NOTICE.txt" \
        "${abs_bundle}/Contents/Resources/LICENSES/MIT.txt" \
        "${abs_bundle}/Contents/Resources/LICENSES/Apache-2.0.txt" \
        "${abs_bundle}/Contents/Resources/LICENSES/OPEN_SOURCE_COMPONENTS.md"; do
        [[ -f "${req_lic}" ]] || die "[${context_label}] Missing required bundled licence file: ${req_lic}"
    done
    rc=0; out="$(ls -la "${abs_bundle}/Contents/Resources/LICENSES" 2>&1)" || rc=$?
    append_verify_log "${log_file}" "${out}"
    [[ "${rc}" -eq 0 ]] || die "[${context_label}] Failed to list bundled LICENSES directory (rc=${rc})"

    rm -rf "${clean_home}"
    DMG_VERIFY_HOME_DIR=""
    append_verify_log "${log_file}" "PASS: ${context_label} succeeded."
}

verify_packaged_dmg() {
    local dmg_path="$1"
    local log_file="$2"

    [[ -f "${dmg_path}" && -s "${dmg_path}" ]] || die "Packaged DMG missing or empty: ${dmg_path}"

    append_verify_log "${log_file}" "" "=== Packaged DMG Mount & Isolated Clean-Copy Launch Verification ==="

    # 1. Verify SHA-256 checksum matches SHA256SUMS.txt
    local out="" rc=0
    out="$(cd "${OUTPUT_DIR}" && shasum -a 256 -c SHA256SUMS.txt 2>&1)" || rc=$?
    append_verify_log "${log_file}" "${out}"
    [[ "${rc}" -eq 0 ]] || die "SHA-256 checksum verification failed for ${dmg_path} (rc=${rc})"

    # 2. Mount DMG read-only at private temp mountpoint and copy .app outside build tree
    DMG_VERIFY_MOUNT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/bangonpro-dmg-mount.XXXXXX")"
    DMG_VERIFY_APPS_DIR="$(mktemp -d "${TMPDIR:-/tmp}/bangonpro-dmg-apps.XXXXXX")"
    chmod 700 "${DMG_VERIFY_MOUNT_DIR}" "${DMG_VERIFY_APPS_DIR}"

    rc=0
    out="$(hdiutil attach "${dmg_path}" -mountpoint "${DMG_VERIFY_MOUNT_DIR}" -nobrowse -readonly 2>&1)" || rc=$?
    append_verify_log "${log_file}" "${out}"
    [[ "${rc}" -eq 0 ]] || die "hdiutil attach failed for ${dmg_path} (rc=${rc})"

    [[ -d "${DMG_VERIFY_MOUNT_DIR}/${APP_NAME}.app" ]] || die "Mounted DMG does not contain '${APP_NAME}.app'"
    [[ -L "${DMG_VERIFY_MOUNT_DIR}/Applications" ]] || die "Mounted DMG is missing /Applications drag-install symlink"

    local copied_app="${DMG_VERIFY_APPS_DIR}/${APP_NAME}.app"
    rc=0
    out="$(ditto "${DMG_VERIFY_MOUNT_DIR}/${APP_NAME}.app" "${copied_app}" 2>&1)" || rc=$?
    [[ -n "${out}" ]] && append_verify_log "${log_file}" "${out}"
    [[ "${rc}" -eq 0 && -d "${copied_app}" ]] || die "Failed to clean-copy '${APP_NAME}.app' from mounted DMG (rc=${rc})"

    # 3. Detach DMG BEFORE launching copied app to guarantee zero dependency on mounted image
    rc=0
    out="$(hdiutil detach "${DMG_VERIFY_MOUNT_DIR}" 2>&1)" || rc=$?
    if [[ "${rc}" -ne 0 ]]; then
        sleep 1
        rc=0
        out="$(hdiutil detach -force "${DMG_VERIFY_MOUNT_DIR}" 2>&1)" || rc=$?
    fi
    append_verify_log "${log_file}" "${out}"
    [[ "${rc}" -eq 0 ]] || die "hdiutil detach failed for ${DMG_VERIFY_MOUNT_DIR} (rc=${rc})"
    rm -rf "${DMG_VERIFY_MOUNT_DIR}"
    DMG_VERIFY_MOUNT_DIR=""

    # 4. Run full verification & launch tests against the copied app outside build/dist dirs
    verify_bundle_launch "${copied_app}" "DMG Clean-Copy Applications Verification" "${log_file}"

    rm -rf "${DMG_VERIFY_APPS_DIR}"
    DMG_VERIFY_APPS_DIR=""
    append_verify_log "${log_file}" "PASS: Packaged DMG mounted, clean-copied, detached, and verified end-to-end."
}

run_smoke_verification() {
    local app_bundle="${OUTPUT_DIR}/${APP_NAME}.app"
    local dmg_path="${OUTPUT_DIR}/${DMG_BASENAME}-${APP_VERSION}-arm64.dmg"
    local log_file="${OUTPUT_DIR}/BUILD_VERIFICATION.log"
    local status_file="${OUTPUT_DIR}/SIGNING_STATUS.txt"
    local dmg_verified_flag="false"

    : > "${log_file}"
    append_verify_log "${log_file}" \
        "=== Bang On Pro Apple Silicon (arm64) Self-Contained Build Verification ===" \
        "Timestamp (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "Version:         ${APP_VERSION}" \
        "Host macOS:      $(sw_vers -productVersion) ($(uname -m))" \
        "Host CPU:        $(sysctl -n machdep.cpu.brand_string 2>/dev/null || uname -m)" \
        "Signing State:   ${SIGNING_STATE}"

    verify_bundle_launch "${app_bundle}" "Staged App Bundle Verification" "${log_file}"

    if [[ "${SKIP_DMG}" -eq 0 ]]; then
        verify_packaged_dmg "${dmg_path}" "${log_file}"
        dmg_verified_flag="true"
    fi

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
self_contained_bundle=true
dmg_verified=${dmg_verified_flag}
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
    log "Done! Self-contained arm64 artifacts available in: ${OUTPUT_DIR}"
}

main

