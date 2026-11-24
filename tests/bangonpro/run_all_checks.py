#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
# SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""Unified CLI QA, Security, Compliance & Project Integrity Verification Runner.

Discovers and runs all Bang On Pro pytest suites under `tests/bangonpro/`,
executes static packaging/workflow syntax checks, prints a formatted summary
table, and emits structured JSON + JUnit XML reports for CI artifacts.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import defusedxml.ElementTree as DefusedET

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = Path(__file__).resolve().parent


def run_static_checks(*, require_ruff: bool = False) -> list[dict[str, Any]]:
    """Run static syntax validation on macOS packaging scripts and GitHub workflows."""
    checks: list[dict[str, Any]] = []

    # 1. bash -n on packaging/macos/build_macos_arm64.sh
    macos_script = REPO_ROOT / "packaging" / "macos" / "build_macos_arm64.sh"
    t0 = time.monotonic()
    if macos_script.is_file():
        proc = subprocess.run(
            ["bash", "-n", str(macos_script)],
            shell=False,
            capture_output=True,
            text=True,
        )
        checks.append(
            {
                "name": "bash_syntax:build_macos_arm64.sh",
                "status": "PASS" if proc.returncode == 0 else "FAIL",
                "duration_sec": round(time.monotonic() - t0, 3),
                "detail": proc.stderr.strip() or "ok",
            }
        )
    else:
        checks.append(
            {
                "name": "bash_syntax:build_macos_arm64.sh",
                "status": "FAIL",
                "duration_sec": 0.0,
                "detail": "File missing",
            }
        )

    # 2. py_compile on packaging/macos/craft_blueprint.py
    craft_bp = REPO_ROOT / "packaging" / "macos" / "craft_blueprint.py"
    t0 = time.monotonic()
    if craft_bp.is_file():
        proc = subprocess.run(
            [sys.executable, "-m", "py_compile", str(craft_bp)],
            shell=False,
            capture_output=True,
            text=True,
        )
        checks.append(
            {
                "name": "python_syntax:craft_blueprint.py",
                "status": "PASS" if proc.returncode == 0 else "FAIL",
                "duration_sec": round(time.monotonic() - t0, 3),
                "detail": proc.stderr.strip() or "ok",
            }
        )
    else:
        checks.append(
            {
                "name": "python_syntax:craft_blueprint.py",
                "status": "FAIL",
                "duration_sec": 0.0,
                "detail": "File missing",
            }
        )

    # 3. YAML validation on .github/workflows/*.yml
    workflows = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    t0 = time.monotonic()
    if not workflows:
        checks.append(
            {
                "name": "yaml_syntax:github_workflows",
                "status": "FAIL",
                "duration_sec": 0.0,
                "detail": "No workflow YAML files found",
            }
        )
    else:
        yaml_code = (
            "import sys, yaml\n"
            "for p in sys.argv[1:]:\n"
            "    with open(p, 'r', encoding='utf-8') as f:\n"
            "        doc = yaml.safe_load(f)\n"
            "    assert isinstance(doc, dict) and 'jobs' in doc, p\n"
        )
        status = "FAIL"
        detail = "PyYAML not installed in current or system python3"
        for py_bin in (sys.executable, "/usr/bin/python3"):
            if not Path(py_bin).exists():
                continue
            proc = subprocess.run(
                [py_bin, "-c", yaml_code, *[str(w) for w in workflows]],
                shell=False,
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0:
                status = "PASS"
                detail = f"Validated {len(workflows)} workflow(s)"
                break
            if "ModuleNotFoundError" not in proc.stderr:
                status = "FAIL"
                detail = proc.stderr.strip()
                break
        checks.append(
            {
                "name": "yaml_syntax:github_workflows",
                "status": status,
                "duration_sec": round(time.monotonic() - t0, 3),
                "detail": detail,
            }
        )

    # 4. bash -n on packaging/macos/bootstrap_ai_runtime.sh
    bootstrap_script = REPO_ROOT / "packaging" / "macos" / "bootstrap_ai_runtime.sh"
    t0 = time.monotonic()
    if bootstrap_script.is_file():
        proc = subprocess.run(
            ["bash", "-n", str(bootstrap_script)],
            shell=False,
            capture_output=True,
            text=True,
        )
        checks.append(
            {
                "name": "bash_syntax:bootstrap_ai_runtime.sh",
                "status": "PASS" if proc.returncode == 0 else "FAIL",
                "duration_sec": round(time.monotonic() - t0, 3),
                "detail": proc.stderr.strip() or "ok",
            }
        )

    # 5. configparser check on packaging/macos/CraftConfig.ini
    craft_ini = REPO_ROOT / "packaging" / "macos" / "CraftConfig.ini"
    t0 = time.monotonic()
    if craft_ini.is_file():
        import configparser

        cp = configparser.ConfigParser()
        cp.optionxform = str
        ok = bool(cp.read(craft_ini, encoding="utf-8")) and "macos-arm-clang" in cp
        checks.append(
            {
                "name": "ini_syntax:CraftConfig.ini",
                "status": "PASS" if ok else "FAIL",
                "duration_sec": round(time.monotonic() - t0, 3),
                "detail": "ok" if ok else "Missing [macos-arm-clang] section",
            }
        )

    # 6. Optional/required ruff critical error check (E9, F63, F7, F82)
    if require_ruff:
        import shutil

        t0 = time.monotonic()
        ruff_bin = shutil.which("ruff")
        if ruff_bin is None:
            checks.append(
                {
                    "name": "ruff_static:bangonpro_python",
                    "status": "FAIL",
                    "duration_sec": 0.0,
                    "detail": "ruff executable not found on PATH (--require-ruff specified)",
                }
            )
        else:
            proc = subprocess.run(
                [ruff_bin, "check", "--select=E9,F63,F7,F82", "bangonpro/", "tests/bangonpro/"],
                cwd=REPO_ROOT,
                shell=False,
                capture_output=True,
                text=True,
            )
            checks.append(
                {
                    "name": "ruff_static:bangonpro_python",
                    "status": "PASS" if proc.returncode == 0 else "FAIL",
                    "duration_sec": round(time.monotonic() - t0, 3),
                    "detail": (proc.stdout + proc.stderr).strip() or "ok",
                }
            )

    return checks


def parse_junit_xml(junit_path: Path) -> list[dict[str, Any]]:
    """Parse pytest JUnit XML safely with defusedxml into per-file suite summaries."""
    if not junit_path.is_file():
        return []

    root = DefusedET.parse(junit_path).getroot()
    suites_by_file: dict[str, dict[str, Any]] = {}

    for testcase in root.findall(".//testcase"):
        classname = testcase.attrib.get("classname", "unknown")
        # Group by test module name (last segment before test class/function)
        parts = classname.split(".")
        suite_name = parts[-1] if parts else classname
        for p in parts:
            if p.startswith("test_"):
                suite_name = p
                break

        rec = suites_by_file.setdefault(
            suite_name,
            {
                "suite": suite_name,
                "tests": 0,
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "duration_sec": 0.0,
                "failures": [],
            },
        )
        rec["tests"] += 1
        rec["duration_sec"] = round(
            rec["duration_sec"] + float(testcase.attrib.get("time", "0") or 0.0), 3
        )

        failure = testcase.find("failure")
        error = testcase.find("error")
        skipped = testcase.find("skipped")
        if failure is not None:
            rec["failed"] += 1
            rec["failures"].append(
                {
                    "test": testcase.attrib.get("name", ""),
                    "message": failure.attrib.get("message", ""),
                }
            )
        elif error is not None:
            rec["errors"] += 1
            rec["failures"].append(
                {
                    "test": testcase.attrib.get("name", ""),
                    "message": error.attrib.get("message", ""),
                }
            )
        elif skipped is not None:
            rec["skipped"] += 1
        else:
            rec["passed"] += 1

    return sorted(suites_by_file.values(), key=lambda r: r["suite"])


def print_summary_table(report: dict[str, Any]) -> None:
    """Render a clean summary table of static checks and pytest suites."""
    print("\n" + "=" * 78)
    print("BANG ON PRO — QA, SECURITY, LICENCE & PROJECT INTEGRITY SUMMARY")
    print("=" * 78)
    print(
        f"{'Check / Suite':<40} {'Status':<8} {'Pass':>5} {'Fail':>5} {'Skip':>5} {'Time(s)':>9}"
    )
    print("-" * 78)

    for chk in report["static_checks"]:
        p = 1 if chk["status"] == "PASS" else 0
        f = 0 if chk["status"] == "PASS" else 1
        print(
            f"{chk['name']:<40} {chk['status']:<8} {p:>5} {f:>5} {0:>5} {chk['duration_sec']:>9.3f}"
        )

    for suite in report["pytest_suites"]:
        st = "PASS" if (suite["failed"] == 0 and suite["errors"] == 0) else "FAIL"
        print(
            f"{suite['suite']:<40} {st:<8} {suite['passed']:>5} "
            f"{suite['failed'] + suite['errors']:>5} {suite['skipped']:>5} "
            f"{suite['duration_sec']:>9.3f}"
        )

    totals = report["totals"]
    print("-" * 78)
    print(
        f"{'TOTAL':<40} {report['overall_status']:<8} {totals['passed']:>5} "
        f"{totals['failed']:>5} {totals['skipped']:>5} {totals['duration_sec']:>9.3f}"
    )
    print("=" * 78 + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run all Bang On Pro pytest suites and static QA/security checks."
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Directory where bangonpro-qa-report.json and pytest-junit.xml will be saved.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Explicit output path for structured JSON report.",
    )
    parser.add_argument(
        "--junit-xml",
        type=Path,
        default=None,
        help="Explicit output path for pytest JUnit XML report.",
    )
    parser.add_argument(
        "--suite",
        action="append",
        default=[],
        help="Optional test file name(s) under tests/bangonpro/ to run (repeatable).",
    )
    parser.add_argument(
        "--skip-static",
        action="store_true",
        help="Skip shell/YAML/blueprint static checks and run pytest only.",
    )
    parser.add_argument(
        "--require-ruff",
        action="store_true",
        help="Fail static checks if ruff is not installed on PATH instead of reporting a warning.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Stream verbose pytest output.",
    )
    args = parser.parse_args(argv)

    # Resolve output locations
    tmp_dir: tempfile.TemporaryDirectory[str] | None = None
    if args.report_dir is not None:
        args.report_dir.mkdir(parents=True, exist_ok=True)
        junit_path = args.junit_xml or (args.report_dir / "pytest-junit.xml")
        json_path = args.json_out or (args.report_dir / "bangonpro-qa-report.json")
    else:
        tmp_dir = tempfile.TemporaryDirectory(prefix="bangonpro_qa_")
        junit_path = args.junit_xml or (Path(tmp_dir.name) / "pytest-junit.xml")
        json_path = args.json_out

    try:
        static_checks = (
            [] if args.skip_static else run_static_checks(require_ruff=args.require_ruff)
        )

        # Discover pytest suites under tests/bangonpro/
        if args.suite:
            targets = [str(TESTS_DIR / s) for s in args.suite]
        else:
            targets = [str(p) for p in sorted(TESTS_DIR.glob("test_*.py"))]

        if not targets:
            print("ERROR: No test_*.py suites found in tests/bangonpro/", file=sys.stderr)
            return 2

        pytest_cmd = [
            sys.executable,
            "-m",
            "pytest",
            f"--junitxml={junit_path}",
            "-v" if args.verbose else "-q",
            *targets,
        ]
        t0 = time.monotonic()
        pytest_proc = subprocess.run(
            pytest_cmd,
            cwd=REPO_ROOT,
            shell=False,
        )
        pytest_duration = round(time.monotonic() - t0, 3)

        pytest_suites = parse_junit_xml(junit_path)

        static_pass = sum(1 for c in static_checks if c["status"] == "PASS")
        static_fail = sum(1 for c in static_checks if c["status"] != "PASS")
        pt_pass = sum(s["passed"] for s in pytest_suites)
        pt_fail = sum(s["failed"] + s["errors"] for s in pytest_suites)
        pt_skip = sum(s["skipped"] for s in pytest_suites)

        overall_ok = (pytest_proc.returncode == 0) and (static_fail == 0) and (pt_fail == 0)
        report: dict[str, Any] = {
            "schema_version": "1.0",
            "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "platform": {
                "system": platform.system(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            },
            "overall_status": "PASS" if overall_ok else "FAIL",
            "pytest_exit_code": pytest_proc.returncode,
            "static_checks": static_checks,
            "pytest_suites": pytest_suites,
            "totals": {
                "passed": static_pass + pt_pass,
                "failed": static_fail + pt_fail,
                "skipped": pt_skip,
                "duration_sec": round(
                    sum(c["duration_sec"] for c in static_checks) + pytest_duration, 3
                ),
            },
        }

        print_summary_table(report)

        if json_path is not None:
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(f"Wrote structured JSON QA report: {json_path}")

        return 0 if overall_ok else 1
    finally:
        if tmp_dir is not None:
            tmp_dir.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
