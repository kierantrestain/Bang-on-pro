# SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
# SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
#
# Ninja build-target selection and C++ test-executable verification for the
# macOS arm64 packaging script. Sourced by build_macos_arm64.sh and exercised
# directly by tests/bangonpro/test_packaging_target_selection.py.
#
# Every function is pipeline-free on purpose. The previous implementation used
# `printf "$listing" | grep -q PATTERN` under `set -o pipefail`: grep -q exits
# on the first match, printf then receives SIGPIPE (exit 141) and pipefail turns
# a *successful* match into a failure. The result was a build that silently
# compiled only kdenlive/kdenlive_render/mltpreview and seven "Not Run" CTests.

# The seven mandatory Bang On Pro C++ QtTest suites (src/bangonpro/tests).
BANGONPRO_REQUIRED_TEST_TARGETS=(
    bangonpro_test_timeline_command_engine
    bangonpro_test_ai_assistant_dock
    bangonpro_test_premiere_preset_browser
    bangonpro_test_pro_workspace
    bangonpro_test_roto_studio
    bangonpro_test_agent_control_centre
    bangonpro_test_effects_and_controls
)
BANGONPRO_AGGREGATE_TEST_TARGET="bangonpro_all_tests"

# bangonpro_ninja_has_target LISTING TARGET
# LISTING is the verbatim output of `ninja -t targets all` ("name: rule" lines).
bangonpro_ninja_has_target() {
    local listing=$'\n'"$1"
    [[ "${listing}" == *$'\n'"$2: "* || "${listing}" == *$'\n'"$2:"$'\n'* ]]
}

# bangonpro_select_build_targets LISTING SKIP_TESTS
# Prints the targets to build, one per line. When tests are enabled the
# aggregate test target is mandatory: its absence (e.g. Qt Test not found, so
# src/bangonpro/tests returned early) is a hard error, never a silent skip.
bangonpro_select_build_targets() {
    local listing="$1"
    local skip_tests="${2:-0}"
    local t
    for t in kdenlive kdenlive_render mltpreview; do
        if ! bangonpro_ninja_has_target "${listing}" "${t}"; then
            printf 'ERROR: required build target "%s" is not defined by CMake.\n' "${t}" >&2
            return 1
        fi
        printf '%s\n' "${t}"
    done
    for t in pofiles tsfiles; do
        if bangonpro_ninja_has_target "${listing}" "${t}"; then
            printf '%s\n' "${t}"
        fi
    done
    if [[ "${skip_tests}" -eq 0 ]]; then
        if ! bangonpro_ninja_has_target "${listing}" "${BANGONPRO_AGGREGATE_TEST_TARGET}"; then
            printf 'ERROR: BUILD_TESTING is ON but "%s" is not defined (is Qt Test available?).\n' \
                "${BANGONPRO_AGGREGATE_TEST_TARGET}" >&2
            return 1
        fi
        printf '%s\n' "${BANGONPRO_AGGREGATE_TEST_TARGET}"
    fi
}

# bangonpro_find_test_executable BUILD_DIR NAME
# Accepts both a plain executable and a macOS bundle layout.
bangonpro_find_test_executable() {
    local build_dir="$1" name="$2" candidate
    for candidate in \
        "${build_dir}/bin/${name}" \
        "${build_dir}/bin/${name}.app/Contents/MacOS/${name}" \
        "${build_dir}/src/bangonpro/tests/${name}" \
        "${build_dir}/src/bangonpro/tests/${name}.app/Contents/MacOS/${name}"; do
        if [[ -f "${candidate}" && -x "${candidate}" ]]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done
    return 1
}

# bangonpro_verify_test_executables BUILD_DIR
# Fails, naming every missing suite, unless all seven executables exist.
bangonpro_verify_test_executables() {
    local build_dir="$1" name path missing=0
    for name in "${BANGONPRO_REQUIRED_TEST_TARGETS[@]}"; do
        if path="$(bangonpro_find_test_executable "${build_dir}" "${name}")"; then
            printf 'OK   %s -> %s\n' "${name}" "${path}"
        else
            printf 'MISSING %s (not built under %s)\n' "${name}" "${build_dir}" >&2
            missing=$((missing + 1))
        fi
    done
    [[ "${missing}" -eq 0 ]]
}
