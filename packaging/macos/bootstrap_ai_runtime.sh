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
REQUIREMENTS_FILE="${REPO_ROOT}/requirements-bangonpro.txt"
EXTRA_PACKAGES=("PyYAML>=6.0,<7.0" "Pillow>=10.0.0,<14.0.0")
INSTALL_OPTIONAL_TIER=""
MODEL_DIR="${BANGONPRO_MODEL_DIR:-${HOME}/.cache/bangonpro/models}"
SKIP_UPGRADE_PIP=0

usage() {
    cat <<'EOF'
Usage: packaging/macos/bootstrap_ai_runtime.sh [options]

Create a self-contained Python 3.12 virtual environment for Bang On Pro's
core AI/editing runtime (Tier 0) and optional local ML tiers.

Options:
  --venv-dir <path>         Target virtual environment directory (required)
  --python <path>           Python 3.12+ interpreter path (auto-detected if omitted)
  --requirements <path>     Core requirements file (default: requirements-bangonpro.txt)
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

while [[ $# -gt 0 ]]; do
    case "$1" in
        --venv-dir)
            VENV_DIR="$2"; shift 2 ;;
        --python)
            PYTHON_BIN="$2"; shift 2 ;;
        --requirements)
            REQUIREMENTS_FILE="$2"; shift 2 ;;
        --tier)
            INSTALL_OPTIONAL_TIER="$2"; shift 2 ;;
        --model-dir)
            MODEL_DIR="$2"; shift 2 ;;
        --skip-upgrade-pip)
            SKIP_UPGRADE_PIP=1; shift ;;
        -h|--help)
            usage; exit 0 ;;
        *)
            die "Unknown option: $1 (use --help for usage)" ;;
    esac
done

[[ -n "${VENV_DIR}" ]] || die "--venv-dir <path> is required."
[[ -f "${REQUIREMENTS_FILE}" ]] || die "Requirements file not found: ${REQUIREMENTS_FILE}"

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
        "${venv_py}" -m pip install --no-cache-dir --upgrade pip wheel setuptools
    fi

    log "Installing Tier 0 core runtime requirements from ${REQUIREMENTS_FILE}..."
    "${venv_py}" -m pip install --no-cache-dir -r "${REQUIREMENTS_FILE}" "${EXTRA_PACKAGES[@]}"

    if [[ -n "${INSTALL_OPTIONAL_TIER}" ]]; then
        mkdir -p "${MODEL_DIR}"
        log "Installing optional local AI tier '${INSTALL_OPTIONAL_TIER}' (model dir: ${MODEL_DIR})..."
        case "${INSTALL_OPTIONAL_TIER}" in
            tier1)
                "${venv_py}" -m pip install --no-cache-dir "faster-whisper>=1.0.3,<2.0.0"
                ;;
            tier2)
                "${venv_py}" -m pip install --no-cache-dir "faster-whisper>=1.0.3,<2.0.0"
                ;;
            tier3)
                "${venv_py}" -m pip install --no-cache-dir "opencv-python-headless>=4.9.0,<6.0.0" "torch>=2.3.0"
                ;;
            *)
                die "Unknown --tier '${INSTALL_OPTIONAL_TIER}'. Expected tier1, tier2, or tier3."
                ;;
        esac
    fi

    # Strip cached bytecode & pip caches to minimize .app/.dmg footprint
    find "${VENV_DIR}" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
    find "${VENV_DIR}" -type f -name "*.pyc" -delete 2>/dev/null || true

    # Verify core imports inside the self-contained environment
    "${venv_py}" -c "import numpy, scipy, defusedxml, jsonschema, yaml, PIL; print('Core Python runtime OK')"
    log "Self-contained Python runtime ready at ${VENV_DIR}"
}

main
