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
import sys
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


# ----------------------------------------------------------------------------- framework bundling
FW_HELPER = REPO_ROOT / "packaging" / "macos" / "framework_bundle.sh"


def _homebrew_like_python_framework(root: Path) -> Path:
    """A Homebrew-shaped Python.framework: library + a whole second Python installation + host symlinks."""
    fw = root / "Python.framework"
    v = fw / "Versions" / "3.12"
    (v / "Resources" / "Python.app" / "Contents" / "MacOS").mkdir(parents=True)
    (v / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python").write_bytes(b"\xcf\xfa\xed\xfe interpreter")
    (v / "Resources" / "Python.app" / "Contents" / "Info.plist").write_text("<plist/>", encoding="utf-8")
    (v / "lib" / "python3.12").mkdir(parents=True)
    (v / "bin").mkdir()
    (v / "Python").write_bytes(b"\xcf\xfa\xed\xfe fake mach-o")
    (v / "Resources" / "Info.plist").write_text("<plist><dict><key>CFBundleExecutable</key><string>Python</string></dict></plist>", encoding="utf-8")
    (v / "bin" / "python3.12").write_text("#!/bin/sh\n", encoding="utf-8")
    (v / "lib" / "python3.12" / "site-packages").symlink_to("/opt/homebrew/lib/python3.12/site-packages")
    (fw / "Versions" / "Current").symlink_to("3.12")
    (fw / "Python").symlink_to("Versions/Current/Python")
    (fw / "Resources").symlink_to("Versions/Current/Resources")
    return fw


def _run_fw(snippet: str, *args: str) -> subprocess.CompletedProcess[str]:
    script = f'set -euo pipefail\nsource "{FW_HELPER}"\n{snippet}\n'
    return subprocess.run([BASH, "-c", script, "bash", *args], capture_output=True, text=True, timeout=60, check=False)


def test_python_framework_is_bundled_minimal_and_self_contained(tmp_path: Path) -> None:
    src = _homebrew_like_python_framework(tmp_path / "homebrew")
    dest = tmp_path / "Bang On Pro.app" / "Contents" / "Frameworks" / "Python.framework"
    dest.parent.mkdir(parents=True)
    res = _run_fw('bangonpro_framework_needs_minimal_copy Python.framework && bangonpro_bundle_minimal_framework "$1" "$2" "$3"',
                  str(src), str(dest), "Python.framework/Versions/3.12/Python")
    assert res.returncode == 0, res.stderr
    files = sorted(str(p.relative_to(dest)) for p in dest.rglob("*"))
    # The library, its Info.plist and the Python.app interpreter the launcher stub re-execs
    # (macOS run 46078752183) - but none of Homebrew's bin/, lib/ or host symlinks.
    assert files == ["Python", "Resources", "Versions", "Versions/3.12", "Versions/3.12/Python", "Versions/3.12/Resources",
                     "Versions/3.12/Resources/Info.plist", "Versions/3.12/Resources/Python.app", "Versions/3.12/Resources/Python.app/Contents",
                     "Versions/3.12/Resources/Python.app/Contents/Info.plist", "Versions/3.12/Resources/Python.app/Contents/MacOS",
                     "Versions/3.12/Resources/Python.app/Contents/MacOS/Python", "Versions/Current"]
    assert os.readlink(dest / "Versions" / "Current") == "3.12"
    assert os.readlink(dest / "Python") == "Versions/Current/Python"
    assert os.readlink(dest / "Resources") == "Versions/Current/Resources"
    assert (dest / "Python").read_bytes() == (src / "Versions" / "3.12" / "Python").read_bytes()
    for p in dest.rglob("*"):  # no link may leave the bundle (codesign and the leak audit both reject that)
        if p.is_symlink():
            assert not os.path.isabs(os.readlink(p)) and p.resolve().is_relative_to(dest.resolve()), p


def test_minimal_framework_refuses_missing_or_flat_binaries(tmp_path: Path) -> None:
    src = _homebrew_like_python_framework(tmp_path / "homebrew")
    res = _run_fw('bangonpro_bundle_minimal_framework "$1" "$2" "$3"', str(src), str(tmp_path / "out.framework"), "Python.framework/Versions/3.13/Python")
    assert res.returncode != 0
    res = _run_fw('bangonpro_bundle_minimal_framework "$1" "$2" "$3"', str(src), str(tmp_path / "out.framework"), "Python.framework/Python")
    assert res.returncode != 0


def test_qt_frameworks_are_still_copied_whole() -> None:
    for name in ("QtCore.framework", "KF6CoreAddons.framework", "QtQuick.framework"):
        assert _run_fw('bangonpro_framework_needs_minimal_copy "$1"', name).returncode != 0
    text = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert 'source "${SCRIPT_DIR}/framework_bundle.sh"' in text and "bangonpro_bundle_minimal_framework" in text


# ----------------------------------------------------------------------------- otool parsing / relative deps
OTOOL_SAMPLE = """/Users/runner/w/dist/macos-arm64/Bang On Pro.app/Contents/Resources/python-env/lib/python3.12/site-packages/opentimelineio/_otio.cpython-312-darwin.so (architecture arm64):
\t/Users/runner/w/dist/macos-arm64/Bang On Pro.app/Contents/Resources/python-env/lib/python3.12/site-packages/opentimelineio/libopentimelineio.dylib (compatibility version 0.0.0, current version 0.18.1)
\t@rpath/libopentime.dylib (compatibility version 0.0.0, current version 0.18.1)
\t/usr/lib/libc++.1.dylib (compatibility version 1.0.0, current version 1800.101.0)
"""


def test_otool_deps_keep_paths_with_spaces_whole() -> None:
    res = _run_fw('bangonpro_otool_deps_from_text <<< "$1"', OTOOL_SAMPLE)
    assert res.returncode == 0, res.stderr
    assert res.stdout.splitlines() == [
        "/Users/runner/w/dist/macos-arm64/Bang On Pro.app/Contents/Resources/python-env/lib/python3.12/site-packages/opentimelineio/libopentimelineio.dylib",
        "@rpath/libopentime.dylib",
        "/usr/lib/libc++.1.dylib",
    ]


def test_in_bundle_absolute_deps_become_loader_path_relative() -> None:
    app = "/Users/runner/w/dist/macos-arm64/Bang On Pro.app"
    cases = {
        (f"{app}/Contents/Resources/python-env/lib/python3.12/site-packages/opentimelineio",
         f"{app}/Contents/Resources/python-env/lib/python3.12/site-packages/opentimelineio/libopentime.dylib"): "libopentime.dylib",
        (f"{app}/Contents/Resources/python-env/lib/python3.12/site-packages/google/_upb",
         f"{app}/Contents/Frameworks/libprotobuf.dylib"): "../../../../../../../Frameworks/libprotobuf.dylib",
        (f"{app}/Contents/MacOS", f"{app}/Contents/Frameworks/QtCore.framework/Versions/A/QtCore"): "../Frameworks/QtCore.framework/Versions/A/QtCore",
    }
    for (frm, to), want in cases.items():
        res = _run_fw('bangonpro_relpath "$1" "$2"', frm, to)
        assert res.returncode == 0 and res.stdout.strip() == want, (frm, to, res.stdout, res.stderr)
        assert os.path.normpath(os.path.join(frm, want)) == to


def test_relocator_and_audit_use_space_safe_otool_parsing() -> None:
    text = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "awk '{print $1}'" not in text.split("audit_bundle_security_hygiene()")[0].split("relocate_macho_deps()")[1]
    assert text.count("bangonpro_otool_deps_from_text") >= 2
    assert '@loader_path/$(bangonpro_relpath' in text


def test_nested_framework_apps_are_signed_before_their_framework() -> None:
    text = BUILD_SCRIPT.read_text(encoding="utf-8")
    sign = text[text.index("sign_nested_macho_objects() {"):text.index("sign_app_bundle() {")]
    assert sign.index('-name "*.app"') < sign.index("# Sign bundled frameworks")


def test_packaging_preflight_reuses_the_release_functions_and_publish_requires_linux() -> None:
    pre = (REPO_ROOT / "packaging" / "macos" / "preflight_python_runtime.sh").read_text(encoding="utf-8")
    for fn in ("bootstrap_ai_runtime.sh", "relocate_macho_deps", "sign_app_bundle", "audit_no_leaked_dylib_paths", "DYLD_PRINT_LIBRARIES", "Relocated Install"):
        assert fn in pre, fn
    assert BUILD_SCRIPT.read_text(encoding="utf-8").rstrip().endswith('if [[ "${BANGONPRO_PACKAGING_LIB_ONLY:-0}" != "1" ]]; then\n    main\nfi')
    wf = (REPO_ROOT / ".github" / "workflows" / "build-macos-arm64.yml").read_text(encoding="utf-8")
    publish = wf[wf.index("publish-release:"):]
    assert publish.index("Require a successful CI Linux run on this exact commit") < publish.index("softprops/action-gh-release")
    res = subprocess.run([BASH, "-n", str(REPO_ROOT / "packaging" / "macos" / "preflight_python_runtime.sh")], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr


def test_macdeployqt_never_touches_the_bundled_python_runtime() -> None:
    """Release run 46091501746: macdeployqt rewrote python-env extensions to @executable_path/../Frameworks."""
    text = BUILD_SCRIPT.read_text(encoding="utf-8")
    fn = text[text.index("deploy_qt_frameworks() {"):text.index("assemble_app_bundle() {")]
    park, deploy, unpark = fn.index('mv "${py_env}" "${parked}/python-env"'), fn.index('macdeployqt "${app_bundle}"'), fn.index('mv "${parked}/python-env" "${py_env}"')
    assert park < deploy < unpark
    assert '@executable_path/../Frameworks/*' in text  # relocator safeguard for code loaded by the bundled Python


def test_shared_frameworks_libraries_never_resolve_via_executable_path() -> None:
    """Release run 46097185166: macdeployqt's Frameworks/libssl.3.dylib referenced libcrypto via
    @executable_path, which resolves inside Python.app when the bundled Python loads it."""
    text = BUILD_SCRIPT.read_text(encoding="utf-8")
    block = text[text.index('if [[ "${dep}" == @executable_path/../Frameworks/* ]]; then'):]
    case_line = block.splitlines()[2]
    assert '"${frameworks_dir}/"*' in case_line and "python-env" in case_line, case_line
    pre = (REPO_ROOT / "packaging" / "macos" / "preflight_python_runtime.sh").read_text(encoding="utf-8")
    assert "emulate_macdeployqt_shared_libs" in pre and "@executable_path/../Frameworks/libcrypto.3.dylib" in pre


def test_bundled_runtime_ships_its_own_ca_trust_store(tmp_path: Path) -> None:
    """Release run 46107603951: Model Manager downloads failed with CERTIFICATE_VERIFY_FAILED because
    the bundled OpenSSL looked for CA certificates under its build-host OPENSSLDIR."""
    boot = (REPO_ROOT / "packaging" / "macos" / "bootstrap_ai_runtime.sh").read_text(encoding="utf-8")
    main_body = boot[boot.index("\nmain() {"):]
    assert main_body.index('make_venv_relocatable "${VENV_DIR}"') < main_body.index('install_ca_trust_store "${VENV_DIR}"')
    fn = boot[boot.index("install_ca_trust_store() {"):boot.index("\nmain() {")]
    # Run the hook line that install_ca_trust_store writes, against a fake prefix holding a CA bundle.
    hook = re.search(r"printf '%s\\n' '([^']+)'", fn).group(1)
    (tmp_path / "etc" / "ssl").mkdir(parents=True)
    (tmp_path / "etc" / "ssl" / "cert.pem").write_text("x", encoding="utf-8")
    probe = f"import os, sys; sys.prefix = {str(tmp_path)!r}; os.environ.pop('SSL_CERT_FILE', None)\nexec({hook!r})\nprint(os.environ['SSL_CERT_FILE'])"
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True).stdout.strip()
    assert out == str(tmp_path / "etc" / "ssl" / "cert.pem")
    keep = f"import os, sys; sys.prefix = {str(tmp_path)!r}; os.environ['SSL_CERT_FILE'] = '/user/choice.pem'\nexec({hook!r})\nprint(os.environ['SSL_CERT_FILE'])"
    assert subprocess.run([sys.executable, "-c", keep], capture_output=True, text=True, check=True).stdout.strip() == "/user/choice.pem"
    pre = (REPO_ROOT / "packaging" / "macos" / "preflight_python_runtime.sh").read_text(encoding="utf-8")
    assert "CA bundle outside the app" in pre and "verified HTTPS download path" in pre
