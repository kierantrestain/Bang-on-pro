#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
# SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
#
# Native Apple Silicon packaging preflight for the bundled Python AI runtime (~10 min).
#
# Builds a skeleton "Bang On Pro.app" (no C++ compilation) with the release build's own functions -
# bootstrap_ai_runtime.sh, relocate_macho_deps, sign_app_bundle and audit_no_leaked_dylib_paths from
# build_macos_arm64.sh - then verifies, from a relocated install directory containing spaces with
# Homebrew's Python hidden and a clean environment:
#   1. Python.framework / Python.app structure (minimal, versioned, no host payload or symlinks)
#   2. Mach-O relocation: no absolute host dependency or LC_RPATH anywhere in the bundle
#   3. signatures of the app, the framework and the nested Python.app (codesign --verify --strict)
#   4. the bundled interpreter runs: prefix inside the app; NumPy, SciPy, PyAV decode, faster-whisper
#      + CTranslate2, ONNX Runtime, OpenCV, Pillow, OpenTimelineIO, the Bang On Pro packages and CLI
#   5. every image dyld loads comes from the relocated app or the OS (DYLD_PRINT_LIBRARIES)
#
# Usage: preflight_python_runtime.sh [--work-dir DIR] [--hide-homebrew-python]
set -euo pipefail

PREFLIGHT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFLIGHT_REPO="$(cd "${PREFLIGHT_DIR}/../.." && pwd)"
WORK="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/bop packaging preflight"
HIDE_BREW_PY=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --work-dir) WORK="$2"; shift 2 ;;
        --hide-homebrew-python) HIDE_BREW_PY=1; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
[[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]] || { echo "preflight requires native Apple Silicon macOS" >&2; exit 2; }

set --
export BANGONPRO_PACKAGING_LIB_ONLY=1
# shellcheck source=packaging/macos/build_macos_arm64.sh
source "${PREFLIGHT_DIR}/build_macos_arm64.sh"
set -euo pipefail

FAILS=0
pass() { printf '[preflight] PASS: %s\n' "$*"; }
fail() { printf '[preflight] FAIL: %s\n' "$*" >&2; FAILS=$((FAILS + 1)); }

rm -rf "${WORK}"
OUTPUT_DIR="${WORK}/stage"
BUILD_DIR="${WORK}/build"
STAGE_DIR="${WORK}/install-prefix"
SOURCE_DIR="${PREFLIGHT_REPO}"
SIGN_IDENTITY=""
CRAFT_PREFIX="${CRAFT_PREFIX:-}"
# Resolve tools exactly as the release build does (build_macos_arm64.sh --mode craft --craft-root <prefix>)
if [[ -n "${CRAFT_PREFIX}" ]]; then
    BUILD_MODE="craft"
    CRAFT_ROOT="${CRAFT_PREFIX}"
    resolve_prefixes
fi
printf '[preflight] macdeployqt: %s\n' "$(command -v macdeployqt || echo 'not on PATH (as in the release build)')"
[[ -n "${CRAFT_PREFIX}" ]] && find "${CRAFT_PREFIX}" -maxdepth 5 -name 'macdeployqt*' 2>/dev/null | head -n 5 | sed 's/^/[preflight] found: /'
mkdir -p "${OUTPUT_DIR}" "${BUILD_DIR}" "${STAGE_DIR}/lib"
APP="${OUTPUT_DIR}/${APP_NAME}.app"

# ---------------------------------------------------------------- skeleton bundle, release runtime
mkdir -p "${APP}/Contents/MacOS" "${APP}/Contents/Frameworks" "${APP}/Contents/Resources" "${APP}/Contents/PlugIns"
cp /usr/bin/true "${APP}/Contents/MacOS/kdenlive"
cat > "${APP}/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>kdenlive</string>
<key>CFBundleIdentifier</key><string>org.bangonpro.preflight</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>${APP_VERSION}</string>
</dict></plist>
EOF
cp -R "${PREFLIGHT_REPO}/bangonpro" "${APP}/Contents/Resources/bangonpro"
find "${APP}/Contents/Resources/bangonpro" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true

"${PREFLIGHT_DIR}/bootstrap_ai_runtime.sh" --venv-dir "${APP}/Contents/Resources/python-env" \
    --requirements "${PREFLIGHT_REPO}/requirements-bangonpro.lock" --tier "${BANGONPRO_BUNDLE_AI_TIER:-tier1}"
# Same Qt deployment step as the release (it parks the Python runtime outside the bundle while
# macdeployqt runs, so the runtime sees the same processing whether or not macdeployqt is found).
show_sqlite_deps() {
    local so
    so="$(find "${APP}/Contents/Resources/python-env/lib" -name '_sqlite3*.so' | head -n 1)"
    [[ -n "${so}" ]] && { printf '[preflight] _sqlite3 deps %s:\n' "$1"; otool -L "${so}" | sed 's/^/    /'; }
}
show_sqlite_deps "after bootstrap"
deploy_qt_frameworks "${APP}"
show_sqlite_deps "after macdeployqt"

# The skeleton has no Qt binaries, so macdeployqt deploys nothing into Contents/Frameworks here. In the
# full app it deploys Qt's dependencies there - OpenSSL for Qt's TLS backend among them - with their
# references rewritten to @executable_path/../Frameworks/..., and the bundled Python then reuses those
# shared copies (release run 46097185166: _ssl -> Frameworks/libssl.3.dylib -> @executable_path/../
# Frameworks/libcrypto.3.dylib, resolved inside Python.app). Reproduce that layout exactly.
emulate_macdeployqt_shared_libs() {
    local fw="${APP}/Contents/Frameworks" ossl lib dep
    ossl="$(brew --prefix openssl@3)/lib"
    for lib in libcrypto.3.dylib libssl.3.dylib; do
        cp -L "${ossl}/${lib}" "${fw}/${lib}"
        chmod u+w "${fw}/${lib}"
        install_name_tool -id "@executable_path/../Frameworks/${lib}" "${fw}/${lib}"
    done
    while IFS= read -r dep; do
        if [[ "${dep}" == */libcrypto.3.dylib && "${dep}" != @executable_path/* ]]; then
            install_name_tool -change "${dep}" "@executable_path/../Frameworks/libcrypto.3.dylib" "${fw}/libssl.3.dylib"
        fi
    done < <(otool -L "${fw}/libssl.3.dylib" | bangonpro_otool_deps_from_text)
    printf '[preflight] emulated macdeployqt OpenSSL deployment:\n'
    otool -L "${fw}/libssl.3.dylib" | sed 's/^/    /'
}
emulate_macdeployqt_shared_libs
relocate_macho_deps "${APP}"
show_sqlite_deps "after relocation"
sign_app_bundle

# ---------------------------------------------------------------- 1. framework structure
FW="${APP}/Contents/Frameworks/Python.framework"
if [[ -d "${FW}" ]]; then
    ver="$(readlink "${FW}/Versions/Current" || true)"
    [[ -n "${ver}" && -f "${FW}/Versions/${ver}/Python" ]] && pass "Python.framework Versions/Current -> ${ver} with library" || fail "Python.framework has no Versions/Current library"
    [[ -f "${FW}/Versions/${ver}/Resources/Info.plist" ]] && pass "Python.framework Info.plist" || fail "Python.framework Info.plist missing"
    [[ -x "${FW}/Versions/${ver}/Resources/Python.app/Contents/MacOS/Python" && -f "${FW}/Versions/${ver}/Resources/Python.app/Contents/Info.plist" ]] \
        && pass "nested Python.app interpreter present" || fail "nested Python.app interpreter missing"
    for host_payload in bin lib include share; do
        [[ ! -e "${FW}/Versions/${ver}/${host_payload}" ]] || fail "Python.framework carries host payload ${host_payload}/"
    done
    while IFS= read -r -d '' link; do
        tgt="$(readlink "${link}")"
        [[ "${tgt}" != /* ]] || fail "absolute symlink in Python.framework: ${link} -> ${tgt}"
    done < <(find "${FW}" -type l -print0)
else
    fail "Contents/Frameworks/Python.framework missing"
fi

# ---------------------------------------------------------------- 2. relocation / LC_RPATH
audit_no_leaked_dylib_paths "${APP}" && pass "Mach-O dependency and LC_RPATH audit" || fail "Mach-O dependency / LC_RPATH audit"

# ---------------------------------------------------------------- 3. signatures
for target in "${APP}" "${FW}" "${FW}/Versions/Current/Resources/Python.app"; do
    if codesign --verify --strict --verbose=2 "${target}" 2>&1; then pass "signature: ${target#"${OUTPUT_DIR}/"}"; else fail "signature: ${target}"; fi
done
codesign --verify --deep --strict "${APP}" && pass "deep signature verification" || fail "deep signature verification"

# ---------------------------------------------------------------- 4-5. relocated, Homebrew-free execution
RELOC_ROOT="${WORK}/Relocated Install/Applications Folder"
mkdir -p "${RELOC_ROOT}"
ditto "${APP}" "${RELOC_ROOT}/${APP_NAME}.app"
rm -rf "${APP}"
RAPP="${RELOC_ROOT}/${APP_NAME}.app"
PY="${RAPP}/Contents/Resources/python-env/bin/python3"

restore_brew() { :; }
if [[ "${HIDE_BREW_PY}" -eq 1 ]]; then
    for d in /opt/homebrew/Cellar/python@3.12 /opt/homebrew/Frameworks/Python.framework; do
        if [[ -e "${d}" || -L "${d}" ]]; then sudo mv "${d}" "${d}.preflight-hidden"; fi
    done
    restore_brew() {
        local d
        for d in /opt/homebrew/Cellar/python@3.12 /opt/homebrew/Frameworks/Python.framework; do
            if [[ -e "${d}.preflight-hidden" || -L "${d}.preflight-hidden" ]]; then sudo mv "${d}.preflight-hidden" "${d}"; fi
        done
    }
    trap restore_brew EXIT
    [[ ! -e /opt/homebrew/opt/python@3.12/Frameworks/Python.framework ]] && pass "Homebrew Python hidden for the runtime checks" || fail "could not hide Homebrew Python"
fi

CLEAN_HOME="${WORK}/clean home"
mkdir -p "${CLEAN_HOME}/tmp"
run_clean() {
    env -i HOME="${CLEAN_HOME}" TMPDIR="${CLEAN_HOME}/tmp" USER="${USER:-runner}" LANG="en_US.UTF-8" \
        PATH="/usr/bin:/bin:/usr/sbin:/sbin" PYTHONNOUSERSITE=1 PYTHONPATH="${RAPP}/Contents/Resources" "$@"
}

CHECK_PY='
import importlib, json, math, os, sqlite3, sys, sysconfig, tempfile, wave
# Every compiled standard-library extension must load (release run 46091501746: _sqlite3 did not)
dyn = os.path.join(sysconfig.get_path("platstdlib"), "lib-dynload")
bad = []
for f in sorted(os.listdir(dyn)):
    if f.endswith(".so"):
        try:
            importlib.import_module(f.split(".")[0])
        except Exception as exc:
            bad.append(f"{f}: {exc}")
assert not bad, bad
sqlite3.connect(":memory:").execute("PRAGMA journal_mode=WAL")
import numpy, scipy.signal, av, ctranslate2, faster_whisper, onnxruntime, cv2, PIL.Image, opentimelineio
import bangonpro.ai_engine, bangonpro.ai_engine.department.cli, bangonpro.ai_engine.mcp.reasoning, bangonpro.macos
app = os.environ["BOP_APP"]
assert sys.prefix.startswith(app), ("prefix outside the app", sys.prefix)
assert os.path.realpath(sys.executable).startswith(os.path.realpath(app)), ("executable outside the app", sys.executable)
assert float(numpy.linalg.norm(numpy.ones(4))) == 2.0
sos = scipy.signal.butter(2, 0.25, output="sos")
p = os.path.join(tempfile.mkdtemp(), "tone with spaces.wav")
with wave.open(p, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(b"".join(int(8000 * math.sin(2 * math.pi * 440 * i / 16000)).to_bytes(2, "little", signed=True) for i in range(16000)))
with av.open(p) as c:
    samples = sum(f.samples for f in c.decode(audio=0))
assert samples == 16000, samples
assert "float32" in ctranslate2.get_supported_compute_types("cpu") or ctranslate2.get_supported_compute_types("cpu")
assert "CPUExecutionProvider" in onnxruntime.get_available_providers()
assert cv2.GaussianBlur(numpy.zeros((8, 8), numpy.uint8), (3, 3), 0).shape == (8, 8)
v = lambda m: getattr(m, "__version__", "?")
print(json.dumps({"prefix": sys.prefix, "python": sys.version.split()[0], "numpy": v(numpy), "av": v(av),
                  "faster_whisper": v(faster_whisper), "ctranslate2": v(ctranslate2), "onnxruntime": v(onnxruntime)}))
'
if out="$(run_clean BOP_APP="${RAPP}" DYLD_PRINT_LIBRARIES=1 "${PY}" -s -c "${CHECK_PY}" 2> "${WORK}/dyld.log")"; then
    pass "bundled interpreter + native packages from the relocated app: ${out}"
else
    fail "bundled interpreter / native package check (see below)"; tail -n 40 "${WORK}/dyld.log" >&2
fi
foreign="$(grep -E '^dyld\[[0-9]+\]: <' "${WORK}/dyld.log" | sed -E 's/^dyld\[[0-9]+\]: <[^>]*> //' \
    | grep -vE "^(/usr/lib/|/System/)" | grep -vF "${RAPP}/" || true)"
loaded="$(grep -cE '^dyld\[[0-9]+\]: <' "${WORK}/dyld.log" || true)"
if [[ "${loaded}" -gt 0 && -z "${foreign}" ]]; then
    pass "all ${loaded} loaded images come from the relocated app or the OS"
else
    fail "images loaded from outside the app (${loaded} seen):"; printf '%s\n' "${foreign}" | head -n 20 >&2
fi
if run_clean "${PY}" -s -m bangonpro.ai_engine.cli --help > /dev/null; then pass "AI engine CLI --help"; else fail "AI engine CLI --help"; fi

restore_brew
trap - EXIT
if [[ "${FAILS}" -ne 0 ]]; then
    printf '[preflight] %d check(s) FAILED\n' "${FAILS}" >&2
    exit 1
fi
printf '[preflight] ALL CHECKS PASSED\n'
