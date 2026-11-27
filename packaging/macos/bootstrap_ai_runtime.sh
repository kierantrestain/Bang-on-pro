#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
# SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
#
# Bootstrap a self-contained Python 3.12 runtime virtual environment for Bang On Pro
# (used during macOS .app bundling into Contents/Resources/python-env/ and optionally
# by end users to install Tier 1/2/3 local AI models into $BANGONPRO_MODEL_DIR).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

VENV_DIR=""
PYTHON_BIN=""
REQUIREMENTS_FILE=""
DEFAULT_LOCKFILE="${REPO_ROOT}/requirements-bangonpro.lock"
DEFAULT_REQUIREMENTS="${REPO_ROOT}/requirements-bangonpro.txt"
INSTALL_OPTIONAL_TIER=""
MODEL_DIR="${BANGONPRO_MODEL_DIR:-${HOME}/.cache/bangonpro/models}"
SKIP_UPGRADE_PIP=0
SCRATCH_TMPDIR=""

cleanup_tmp() {
    if [[ -n "${SCRATCH_TMPDIR}" && -d "${SCRATCH_TMPDIR}" ]]; then
        rm -rf "${SCRATCH_TMPDIR}"
    fi
}
trap cleanup_tmp EXIT

usage() {
    cat <<'EOF'
Usage: packaging/macos/bootstrap_ai_runtime.sh [options]

Create a self-contained Python 3.12 virtual environment for Bang On Pro's
core AI/editing runtime (Tier 0) and optional local ML tiers.

Note: End users on macOS do NOT need to run this script manually; Bang On Pro
bundles the Tier 0 Python runtime inside Bang On Pro.app and provides an in-app
GUI Model Manager for optional local AI models.

Options:
  --venv-dir <path>         Target virtual environment directory (required)
  --python <path>           Python 3.12+ interpreter path (auto-detected if omitted)
  --requirements <path>     Requirements/lock file (default: requirements-bangonpro.lock if present,
                            else requirements-bangonpro.txt)
  --lockfile <path>         Alias for --requirements pointing to an exact == lockfile
  --tier <tier1|tier2|tier3> Optional local AI tier to install (opt-in only):
                              tier1: faster-whisper (MIT, CPU/INT8 fast transcription)
                              tier2: faster-whisper + open-source speech tooling
                              tier3: torch + sam2 / opencv-python-headless (roto/masking)
  --model-dir <path>        Model storage directory (default: $BANGONPRO_MODEL_DIR)
  --skip-upgrade-pip        Do not upgrade pip before installing wheels
  -h, --help                Show this help text
EOF
}

log() {
    printf '[bangonpro-python-env] %s\n' "$*"
}

warn() {
    printf '[bangonpro-python-env] WARNING: %s\n' "$*" >&2
}

die() {
    printf '[bangonpro-python-env] ERROR: %s\n' "$*" >&2
    exit 1
}

require_arg() {
    local opt="$1"
    local remaining="$2"
    [[ "${remaining}" -ge 2 ]] || die "Option ${opt} requires a value."
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --venv-dir)
            require_arg "$1" "$#"; VENV_DIR="$2"; shift 2 ;;
        --python)
            require_arg "$1" "$#"; PYTHON_BIN="$2"; shift 2 ;;
        --requirements|--lockfile)
            require_arg "$1" "$#"; REQUIREMENTS_FILE="$2"; shift 2 ;;
        --tier)
            require_arg "$1" "$#"; INSTALL_OPTIONAL_TIER="$2"; shift 2 ;;
        --model-dir)
            require_arg "$1" "$#"; MODEL_DIR="$2"; shift 2 ;;
        --skip-upgrade-pip)
            SKIP_UPGRADE_PIP=1; shift ;;
        -h|--help)
            usage; exit 0 ;;
        *)
            die "Unknown option: $1 (use --help for usage)" ;;
    esac
done

if [[ -z "${REQUIREMENTS_FILE}" ]]; then
    if [[ -f "${DEFAULT_LOCKFILE}" ]]; then
        REQUIREMENTS_FILE="${DEFAULT_LOCKFILE}"
    else
        REQUIREMENTS_FILE="${DEFAULT_REQUIREMENTS}"
    fi
fi

[[ -n "${VENV_DIR}" ]] || die "--venv-dir <path> is required."
[[ -f "${REQUIREMENTS_FILE}" ]] || die "Requirements file not found: ${REQUIREMENTS_FILE}"

sanitize_runtime_env() {
    # Prevent host virtualenv/Python overrides, custom pip registries, or API/signing
    # secrets from leaking into the bundled Python environment or pip subprocesses.
    unset PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV
    unset PIP_CONFIG_FILE PIP_EXTRA_INDEX_URL PIP_FIND_LINKS PIP_INDEX_URL PIP_TRUSTED_HOST
    unset OPENAI_API_KEY ANTHROPIC_API_KEY DEEPGRAM_API_KEY BANGONPRO_AI_API_KEY
    unset HF_TOKEN HUGGING_FACE_HUB_TOKEN GITHUB_TOKEN GH_TOKEN
    unset MACOS_CERTIFICATE_P12 MACOS_CERTIFICATE_PASSWORD MACOS_NOTARY_KEY
    export PYTHONNOUSERSITE=1
    export PIP_DISABLE_PIP_VERSION_CHECK=1
    export PIP_NO_CACHE_DIR=1
}

resolve_python() {
    if [[ -n "${PYTHON_BIN}" ]]; then
        [[ -x "${PYTHON_BIN}" ]] || die "Specified Python interpreter is not executable: ${PYTHON_BIN}"
        return 0
    fi

    local candidates=(
        "/opt/homebrew/opt/python@3.12/bin/python3.12"
        "/opt/homebrew/bin/python3.12"
        "$(command -v python3.12 2>/dev/null || true)"
        "$(command -v python3 2>/dev/null || true)"
    )
    local cand
    for cand in "${candidates[@]}"; do
        if [[ -n "${cand}" && -x "${cand}" ]]; then
            PYTHON_BIN="${cand}"
            return 0
        fi
    done
    die "No Python 3 interpreter found in PATH or /opt/homebrew."
}

make_venv_relocatable() {
    local venv_dir="$1"
    local base_py="$2"
    local venv_py="${venv_dir}/bin/python3"

    # 1. Ensure bin/python3 is a real Mach-O binary copy (not a symlink to /opt/homebrew or host Python)
    #    so build_macos_arm64.sh can relocate its Mach-O links into Contents/Frameworks/.
    local bin_cand resolved_bin
    for bin_cand in "${venv_dir}/bin/python3" "${venv_dir}/bin/python3.12"; do
        if [[ -L "${bin_cand}" ]]; then
            resolved_bin="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "${bin_cand}" 2>/dev/null || true)"
            if [[ -n "${resolved_bin}" && -f "${resolved_bin}" ]]; then
                rm -f "${bin_cand}"
                cp -L -f "${resolved_bin}" "${bin_cand}"
                chmod 755 "${bin_cand}"
            fi
        fi
    done

    # 2. On macOS, bundle the Python standard library + lib-dynload directly inside python-env/lib/pythonX.Y
    #    so the .app runs on Macs without Homebrew Python 3.12 installed.
    if [[ "$(uname -s)" == "Darwin" ]]; then
        local py_ver stdlib_dir platstdlib_dir target_lib_dir
        py_ver="$("${base_py}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
        stdlib_dir="$("${base_py}" -c 'import sysconfig; print(sysconfig.get_path("stdlib") or "")')"
        platstdlib_dir="$("${base_py}" -c 'import sysconfig; print(sysconfig.get_path("platstdlib") or "")')"
        target_lib_dir="${venv_dir}/lib/python${py_ver}"
        mkdir -p "${target_lib_dir}"

        if [[ -n "${stdlib_dir}" && -d "${stdlib_dir}" ]]; then
            log "Bundling Python ${py_ver} stdlib from ${stdlib_dir} -> ${target_lib_dir}..."
            local entry name
            for entry in "${stdlib_dir}"/*; do
                [[ -e "${entry}" ]] || continue
                name="$(basename "${entry}")"
                case "${name}" in
                    site-packages|test|tests|idlelib|tkinter|turtledemo|ensurepip|__pycache__|config-*)
                        continue
                        ;;
                esac
                if [[ ! -e "${target_lib_dir}/${name}" ]]; then
                    cp -RL "${entry}" "${target_lib_dir}/${name}"
                fi
            done
        fi

        if [[ -n "${platstdlib_dir}" && -d "${platstdlib_dir}/lib-dynload" && ! -d "${target_lib_dir}/lib-dynload" ]]; then
            cp -RL "${platstdlib_dir}/lib-dynload" "${target_lib_dir}/lib-dynload"
        fi

        # Remove hardcoded host/Homebrew pyvenv.cfg so CPython resolves sys.prefix via
        # the bundled lib/pythonX.Y landmark relative to Contents/Resources/python-env/bin/python3.
        if [[ -f "${target_lib_dir}/os.py" && -f "${venv_dir}/pyvenv.cfg" ]]; then
            rm -f "${venv_dir}/pyvenv.cfg"
        fi
    elif [[ -f "${venv_dir}/pyvenv.cfg" ]]; then
        # Scrub build-host absolute command lines from pyvenv.cfg where safe
        grep -vE '^(command|executable) *= *(/opt/homebrew|/usr/local|/Users/)' "${venv_dir}/pyvenv.cfg" > "${venv_dir}/pyvenv.cfg.tmp" || true
        mv -f "${venv_dir}/pyvenv.cfg.tmp" "${venv_dir}/pyvenv.cfg"
    fi

    # 3. Rewrite absolute build-host shebangs in bin/ scripts to portable #!/usr/bin/env python3
    local script_file first_line
    for script_file in "${venv_dir}/bin"/*; do
        [[ -f "${script_file}" && ! -L "${script_file}" ]] || continue
        if file -b "${script_file}" 2>/dev/null | grep -qi "text"; then
            first_line="$(head -n 1 "${script_file}" 2>/dev/null || true)"
            if [[ "${first_line}" == "#!"*python* && "${first_line}" != "#!/usr/bin/env"* ]]; then
                {
                    printf '#!/usr/bin/env python3\n'
                    tail -n +2 "${script_file}"
                } > "${script_file}.reloc"
                chmod --reference="${script_file}" "${script_file}.reloc" 2>/dev/null || chmod 755 "${script_file}.reloc"
                mv -f "${script_file}.reloc" "${script_file}"
            fi
        fi
    done

    [[ -x "${venv_py}" ]] || die "Relocated Python binary missing or not executable: ${venv_py}"
}

main() {
    sanitize_runtime_env
    SCRATCH_TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/bangonpro-pyvenv.XXXXXX")"
    chmod 700 "${SCRATCH_TMPDIR}"
    export TMPDIR="${SCRATCH_TMPDIR}"

    resolve_python
    log "Using Python interpreter: ${PYTHON_BIN} ($("${PYTHON_BIN}" --version 2>&1))"
    log "Creating self-contained virtual environment at: ${VENV_DIR}"

    rm -rf "${VENV_DIR}"
    mkdir -p "$(dirname "${VENV_DIR}")"
    # Use --copies so the venv is relocatable and doesn't hold broken host symlinks
    "${PYTHON_BIN}" -m venv --copies "${VENV_DIR}"

    local venv_py="${VENV_DIR}/bin/python3"
    [[ -x "${venv_py}" ]] || die "Failed to initialize Python virtual environment at ${VENV_DIR}"

    if [[ "${SKIP_UPGRADE_PIP}" -eq 0 ]]; then
        "${venv_py}" -m pip install --no-cache-dir --disable-pip-version-check --upgrade pip wheel setuptools
    fi

    log "Installing Tier 0 core runtime requirements from ${REQUIREMENTS_FILE}..."
    "${venv_py}" -m pip install --no-cache-dir --disable-pip-version-check -r "${REQUIREMENTS_FILE}"

    if [[ -n "${INSTALL_OPTIONAL_TIER}" ]]; then
        mkdir -p "${MODEL_DIR}"
        log "Installing optional local AI tier '${INSTALL_OPTIONAL_TIER}' (model dir: ${MODEL_DIR})..."
        case "${INSTALL_OPTIONAL_TIER}" in
            tier1)
                "${venv_py}" -m pip install --no-cache-dir --disable-pip-version-check "faster-whisper>=1.0.3,<2.0.0"
                ;;
            tier2)
                "${venv_py}" -m pip install --no-cache-dir --disable-pip-version-check "faster-whisper>=1.0.3,<2.0.0"
                ;;
            tier3)
                "${venv_py}" -m pip install --no-cache-dir --disable-pip-version-check "opencv-python-headless>=4.9.0,<6.0.0" "torch>=2.3.0"
                ;;
            *)
                die "Unknown --tier '${INSTALL_OPTIONAL_TIER}'. Expected tier1, tier2, or tier3."
                ;;
        esac
    fi

    make_venv_relocatable "${VENV_DIR}" "${PYTHON_BIN}"

    # Strip cached bytecode & transient caches to minimize .app/.dmg footprint
    find "${VENV_DIR}" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
    find "${VENV_DIR}" -type f \( -name "*.pyc" -o -name ".env" -o -name "*.p12" -o -name "*.p8" \) -delete 2>/dev/null || true

    # Verify core imports inside the self-contained environment (isolated from host site-packages)
    "${venv_py}" -s -c "import sys, numpy, scipy, defusedxml, jsonschema, yaml, PIL, opentimelineio, scenedetect; print('Core Python runtime OK:', sys.prefix)"
    log "Self-contained Python runtime ready at ${VENV_DIR}"
}

main

