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
APP_VERSION="${BANGONPRO_VERSION:-1.0.0}"
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
  --version <ver>               Release version string (default: 1.0.0)
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

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)
            BUILD_MODE="$2"; shift 2 ;;
        --build-type)
            BUILD_TYPE="$2"; shift 2 ;;
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
    local required_tools=(cmake ninja pkg-config codesign hdiutil lipo otool install_name_tool plutil shasum)
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

    # Bundle MLT 7 modules -> Contents/PlugIns/mlt-7/ and data/profiles/presets -> Contents/Resources/mlt-7/
    local mlt_mod_dir mlt_data_dir
    mlt_mod_dir="$(pkg-config --variable=moduledir mlt-framework-7 2>/dev/null || true)"
    mlt_data_dir="$(pkg-config --variable=mltdatadir mlt-framework-7 2>/dev/null || true)"
    if [[ -z "${mlt_mod_dir}" || ! -d "${mlt_mod_dir}" ]]; then
        for cand in "${CRAFT_PREFIX}/lib/mlt-7" "/opt/homebrew/lib/mlt-7"; do
            [[ -d "${cand}" ]] && mlt_mod_dir="${cand}" && break
        done
    fi
    if [[ -z "${mlt_data_dir}" || ! -d "${mlt_data_dir}" ]]; then
        for cand in "${CRAFT_PREFIX}/share/mlt-7" "/opt/homebrew/share/mlt-7"; do
            [[ -d "${cand}" ]] && mlt_data_dir="${cand}" && break
        done
    fi
    if [[ -n "${mlt_mod_dir}" && -d "${mlt_mod_dir}" ]]; then
        log "Bundling MLT 7 modules from ${mlt_mod_dir}..."
        copy_plugin_dir_if_exists "${mlt_mod_dir}" "${plugins_dir}/mlt-7"
    else
        warn "MLT 7 modules directory not found; bundled melt may have limited plugins."
    fi
    if [[ -n "${mlt_data_dir}" && -d "${mlt_data_dir}" ]]; then
        log "Bundling MLT 7 profiles & presets from ${mlt_data_dir}..."
        copy_plugin_dir_if_exists "${mlt_data_dir}" "${resources_dir}/mlt-7"
    fi

    # Bundle frei0r plugins -> Contents/PlugIns/frei0r-1/
    local frei0r_dir=""
    local cand
    for cand in "${CRAFT_PREFIX}/lib/frei0r-1" "/opt/homebrew/lib/frei0r-1" "/usr/local/lib/frei0r-1"; do
        if [[ -d "${cand}" ]]; then
            frei0r_dir="${cand}"
            break
        fi
    done
    if [[ -n "${frei0r_dir}" ]]; then
        log "Bundling frei0r plugins from ${frei0r_dir}..."
        copy_plugin_dir_if_exists "${frei0r_dir}" "${plugins_dir}/frei0r-1"
    fi

    # 5.4 Bundle Bang On Pro Python modules & self-contained Python 3.12 virtualenv
    if [[ -d "${SOURCE_DIR}/bangonpro" ]]; then
        cp -R "${SOURCE_DIR}/bangonpro" "${resources_dir}/bangonpro"
        find "${resources_dir}/bangonpro" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
    fi

    if [[ "${SKIP_PYTHON_VENV}" -eq 0 && -x "${SCRIPT_DIR}/bootstrap_ai_runtime.sh" ]]; then
        log "Building self-contained Python 3.12 runtime inside Contents/Resources/python-env/..."
        "${SCRIPT_DIR}/bootstrap_ai_runtime.sh" \
            --venv-dir "${resources_dir}/python-env" \
            --requirements "${SOURCE_DIR}/requirements-bangonpro.txt"
    elif [[ "${SKIP_PYTHON_VENV}" -eq 1 ]]; then
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
# 8. Smoke-Test Launch Verification, Dylib Audit & Artifact Metadata
# ---------------------------------------------------------------------------
audit_no_leaked_dylib_paths() {
    local app_bundle="$1"
    local leaked=0
    local bin
    while IFS= read -r bin; do
        [[ -n "${bin}" ]] || continue
        local bad_deps
        bad_deps="$(otool -L "${bin}" 2>/dev/null | tail -n +2 | awk '{print $1}' | \
            grep -E '^(/Users/runner|/opt/homebrew|/usr/local/Cellar|'"${CRAFT_ROOT}"')' || true)"
        if [[ -n "${bad_deps}" ]]; then
            printf 'LEAKED DYLIB PATH in %s:\n%s\n' "${bin}" "${bad_deps}"
            leaked=1
        fi
    done < <(find "${app_bundle}/Contents/MacOS" "${app_bundle}/Contents/Frameworks" "${app_bundle}/Contents/PlugIns" \
        -type f \( -perm -111 -o -name "*.dylib" -o -name "*.so" \) 2>/dev/null)

    if [[ "${leaked}" -ne 0 ]]; then
        return 1
    fi
    echo "PASS: Zero leaked /Users/runner, /opt/homebrew, or CraftRoot paths detected by otool -L."
    return 0
}

run_smoke_verification() {
    local app_bundle="${OUTPUT_DIR}/${APP_NAME}.app"
    local log_file="${OUTPUT_DIR}/BUILD_VERIFICATION.log"
    local status_file="${OUTPUT_DIR}/SIGNING_STATUS.txt"
    local dylib_audit_ok=0

    {
        echo "=== Bang On Pro Apple Silicon (arm64) Self-Contained Build Verification ==="
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
        test "$(lipo -archs "${app_bundle}/Contents/MacOS/${APP_NAME}")" = "arm64"
        echo ""
        echo "--- Code Signature Verification ---"
        codesign -dvv "${app_bundle}" 2>&1 || true
        codesign --verify --deep --strict --verbose=2 "${app_bundle}" 2>&1
        echo ""
        echo "--- Self-Contained Mach-O Dylib Linkage Audit (otool -L) ---"
        if audit_no_leaked_dylib_paths "${app_bundle}"; then
            dylib_audit_ok=1
        fi
        echo ""
        echo "--- Headless Launch Smoke Test (--version) ---"
        MLT_REPOSITORY="${app_bundle}/Contents/PlugIns/mlt-7" \
        MLT_DATA="${app_bundle}/Contents/Resources/mlt-7" \
        FREI0R_PATH="${app_bundle}/Contents/PlugIns/frei0r-1" \
        QT_QPA_PLATFORM=offscreen \
            "${app_bundle}/Contents/MacOS/${APP_NAME}" --version 2>&1
        echo ""
        echo "--- Bundled Media Stack (MLT 7 + FFmpeg VideoToolbox) ---"
        if [[ -x "${app_bundle}/Contents/MacOS/melt" ]]; then
            MLT_REPOSITORY="${app_bundle}/Contents/PlugIns/mlt-7" \
            MLT_DATA="${app_bundle}/Contents/Resources/mlt-7" \
            FREI0R_PATH="${app_bundle}/Contents/PlugIns/frei0r-1" \
                "${app_bundle}/Contents/MacOS/melt" --version 2>&1 | head -n 3
        fi
        if [[ -x "${app_bundle}/Contents/MacOS/ffmpeg" ]]; then
            "${app_bundle}/Contents/MacOS/ffmpeg" -version 2>&1 | head -n 2
            "${app_bundle}/Contents/MacOS/ffmpeg" -hide_banner -hwaccels 2>&1
        fi
        echo ""
        echo "--- Bundled Self-Contained Python AI Engine CLI Smoke Test ---"
        if [[ -x "${app_bundle}/Contents/Resources/python-env/bin/python3" ]]; then
            PYTHONPATH="${app_bundle}/Contents/Resources" \
                "${app_bundle}/Contents/Resources/python-env/bin/python3" -m bangonpro.ai_engine.cli --help 2>&1 | head -n 20
        else
            echo "SKIPPED: bundled python-env not present (--skip-python-venv)."
        fi
        echo ""
        echo "--- Bundled Licence & Attribution Files ---"
        ls -la "${app_bundle}/Contents/Resources/LICENSES"
    } | tee "${log_file}"

    if grep -q "LEAKED DYLIB PATH" "${log_file}"; then
        die "Bundle dylib audit failed: leaked non-relocated library path(s) detected in ${log_file}."
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
