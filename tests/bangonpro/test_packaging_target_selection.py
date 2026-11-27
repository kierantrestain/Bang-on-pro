# SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
# SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""Regression coverage for macOS packaging build-target selection.

CI run 46062145756 (job 140718711008) compiled only kdenlive, kdenlive_render
and mltpreview: target discovery piped a large `ninja -t targets all` listing
into `grep -q` under `set -o pipefail`, so SIGPIPE turned found targets into
"missing" ones and all seven C++ suites reported "Not Run".
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "packaging" / "macos" / "build_targets.sh"
BUILD_SCRIPT = REPO_ROOT / "packaging" / "macos" / "build_macos_arm64.sh"
TESTS_CMAKE = REPO_ROOT / "src" / "bangonpro" / "tests" / "CMakeLists.txt"

REQUIRED = [
    "bangonpro_test_timeline_command_engine",
    "bangonpro_test_ai_assistant_dock",
    "bangonpro_test_premiere_preset_browser",
    "bangonpro_test_pro_workspace",
    "bangonpro_test_roto_studio",
    "bangonpro_test_agent_control_centre",
    "bangonpro_test_effects_and_controls",
]

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")


def _run(snippet: str, *args: str) -> subprocess.CompletedProcess[str]:
    script = f'set -euo pipefail\nsource "{HELPER}"\n{snippet}\n'
    return subprocess.run(
        [BASH, "-c", script, "bash", *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _listing(*, with_tests: bool, padding: int = 60000) -> str:
    """A realistic, large ninja listing: thousands of object targets first."""
    lines = [f"src/CMakeFiles/obj{i}.cpp.o: CXX_COMPILER__kdenlive_RelWithDebInfo" for i in range(padding)]
    lines += ["kdenlive: phony", "kdenlive_render: phony", "mltpreview: phony", "pofiles: phony"]
    if with_tests:
        lines += [f"{t}: phony" for t in REQUIRED]
        lines += [f"bin/{t}: CXX_EXECUTABLE_LINKER__{t}_RelWithDebInfo" for t in REQUIRED]
        lines.append("bangonpro_all_tests: phony")
    lines += ["all: phony", "install: phony"]
    return "\n".join(lines) + "\n"


def _select(listing: str, skip_tests: str = "0", *, tmp: Path) -> subprocess.CompletedProcess[str]:
    # Large listings exceed the per-argument kernel limit, so pass them as a file.
    f = tmp / "ninja_targets.txt"
    f.write_text(listing, encoding="utf-8")
    return _run('bangonpro_select_build_targets "$(cat "$1")" "$2"', str(f), skip_tests)


def test_large_listing_selects_aggregate_test_target(tmp_path: Path) -> None:
    res = _select(_listing(with_tests=True), tmp=tmp_path)
    assert res.returncode == 0, res.stderr
    targets = res.stdout.split()
    assert targets[:3] == ["kdenlive", "kdenlive_render", "mltpreview"]
    assert "pofiles" in targets
    assert "tsfiles" not in targets
    assert targets[-1] == "bangonpro_all_tests"


def test_missing_aggregate_is_a_hard_error_when_testing(tmp_path: Path) -> None:
    res = _select(_listing(with_tests=False), tmp=tmp_path)
    assert res.returncode != 0
    assert "bangonpro_all_tests" in res.stderr


def test_skip_tests_omits_aggregate(tmp_path: Path) -> None:
    res = _select(_listing(with_tests=False), "1", tmp=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "bangonpro_all_tests" not in res.stdout.split()


def test_missing_application_target_fails(tmp_path: Path) -> None:
    listing = _listing(with_tests=True, padding=10).replace("mltpreview: phony\n", "")
    res = _select(listing, tmp=tmp_path)
    assert res.returncode != 0
    assert "mltpreview" in res.stderr


def test_target_match_is_exact_not_prefix() -> None:
    listing = "bangonpro_all_tests_extra: phony\nxbangonpro_all_tests: phony\n"
    res = _run('bangonpro_ninja_has_target "$1" bangonpro_all_tests', listing)
    assert res.returncode != 0
    res = _run('bangonpro_ninja_has_target "$1" bangonpro_all_tests', "a: phony\nbangonpro_all_tests: phony")
    assert res.returncode == 0


def _make_exe(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_verify_accepts_plain_and_bundle_layouts(tmp_path: Path) -> None:
    for i, name in enumerate(REQUIRED):
        if i % 2:
            _make_exe(tmp_path / "bin" / f"{name}.app" / "Contents" / "MacOS" / name)
        else:
            _make_exe(tmp_path / "bin" / name)
    res = _run('bangonpro_verify_test_executables "$1"', str(tmp_path))
    assert res.returncode == 0, res.stderr
    assert res.stdout.count("OK ") == len(REQUIRED)


def test_verify_names_every_missing_suite(tmp_path: Path) -> None:
    for name in REQUIRED[:5]:
        _make_exe(tmp_path / "bin" / name)
    # A non-executable file must not count as a built suite.
    (tmp_path / "bin" / REQUIRED[5]).write_text("", encoding="utf-8")
    res = _run('bangonpro_verify_test_executables "$1"', str(tmp_path))
    assert res.returncode != 0
    for name in REQUIRED[5:]:
        assert f"MISSING {name}" in res.stderr


def test_helper_required_list_matches_cmake_aggregate() -> None:
    res = _run('printf "%s\\n" "${BANGONPRO_REQUIRED_TEST_TARGETS[@]}"')
    assert res.stdout.split() == REQUIRED
    cmake = TESTS_CMAKE.read_text(encoding="utf-8")
    agg = re.search(r"add_custom_target\(bangonpro_all_tests\s+DEPENDS(.*?)\)", cmake, re.S)
    assert agg, "bangonpro_all_tests aggregate target missing"
    assert agg.group(1).split() == REQUIRED
    for name in REQUIRED:
        assert f"add_executable({name}" in cmake
        assert f"add_test(NAME {name} COMMAND {name})" in cmake
    assert cmake.count("MACOSX_BUNDLE OFF") == len(REQUIRED)


def test_build_script_uses_pipeline_free_selection() -> None:
    text = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert 'source "${SCRIPT_DIR}/build_targets.sh"' in text
    assert "bangonpro_select_build_targets" in text
    assert "bangonpro_verify_test_executables" in text
    assert "--no-tests=error" in text
    assert "| grep -q" not in text, "grep -q pipelines are SIGPIPE-prone under pipefail"
    assert 'die "One or more Bang On Pro C++ CTest suites failed."' in text


def test_scripts_parse() -> None:
    for script in (HELPER, BUILD_SCRIPT):
        res = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True, check=False)
        assert res.returncode == 0, res.stderr


@pytest.mark.skipif(os.name != "posix", reason="POSIX pipes required")
def test_root_cause_reproduces_with_legacy_pipeline(tmp_path: Path) -> None:
    """Documents the original defect: the legacy idiom reports a present target as absent."""
    f = tmp_path / "listing.txt"
    f.write_text("kdenlive: phony\n" + _listing(with_tests=True, padding=200000), encoding="utf-8")
    script = (
        "set -o pipefail\n"
        'avail="$(cat "$1")"\n'
        "fails=0\n"
        "for _ in 1 2 3 4 5; do\n"
        "  if ! printf '%s\\n' \"$avail\" | grep -qE '^kdenlive: '; then fails=$((fails+1)); fi\n"
        "done\n"
        'echo "$fails"\n'
    )
    res = subprocess.run([BASH, "-c", script, "bash", str(f)], capture_output=True, text=True, check=False)
    # The target is the first line of a ~10 MB listing, so grep exits long before
    # printf finishes and pipefail reports printf's SIGPIPE as a failed match.
    assert int(res.stdout.strip()) > 0
    # ...whereas the pipeline-free helper finds it every time.
    ok = _run('bangonpro_ninja_has_target "$(cat "$1")" kdenlive', str(f))
    assert ok.returncode == 0
