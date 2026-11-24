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

    # Strip cached bytecode & transient caches to minimize .app/.dmg footprint
    find "${VENV_DIR}" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
    find "${VENV_DIR}" -type f \( -name "*.pyc" -o -name ".env" -o -name "*.p12" -o -name "*.p8" \) -delete 2>/dev/null || true

    # Verify core imports inside the self-contained environment (isolated from host site-packages)
    "${venv_py}" -s -c "import numpy, scipy, defusedxml, jsonschema, yaml, PIL, opentimelineio, scenedetect; print('Core Python runtime OK')"
    log "Self-contained Python runtime ready at ${VENV_DIR}"
}

main

