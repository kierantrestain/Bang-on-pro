# SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
# SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""Security, Static AST, Licence Compliance & Contract Schema Tests for Bang On Pro."""

from __future__ import annotations

import ast
import configparser
import importlib.util
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import defusedxml.ElementTree as DefusedET
from defusedxml.common import DTDForbidden, EntitiesForbidden
import jsonschema
from jsonschema import Draft202012Validator
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BANGONPRO_DIR = REPO_ROOT / "bangonpro"
SCHEMAS_DIR = BANGONPRO_DIR / "schemas"
PACKAGING_MACOS_DIR = REPO_ROOT / "packaging" / "macos"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


# ---------------------------------------------------------------------------
# Static Security AST Analyzer (used across bangonpro/ & self-tested below)
# ---------------------------------------------------------------------------

UNSAFE_XML_MODULES = {
    "xml.etree.ElementTree",
    "xml.etree.cElementTree",
    "xml.dom.minidom",
    "xml.dom.pulldom",
    "xml.sax",
    "xml.parsers.expat",
    "lxml.etree",
}

UNSAFE_XML_PARSE_FUNCS = {
    "parse",
    "fromstring",
    "fromstringlist",
    "iterparse",
    "XML",
    "XMLID",
    "parseString",
    "ParserCreate",
}

SUBPROCESS_EXEC_FUNCS = {"run", "Popen", "call", "check_call", "check_output"}


def scan_python_source_for_vulnerabilities(
    source: str, filename: str = "<memory>"
) -> list[dict[str, Any]]:
    """Static AST security scanner checking for command injection, XXE, and unsafe eval."""
    tree = ast.parse(source, filename=filename)
    findings: list[dict[str, Any]] = []

    # Track imported module/symbol aliases
    unsafe_xml_module_aliases: set[str] = set()
    unsafe_xml_func_aliases: dict[str, str] = {}
    subprocess_aliases: set[str] = set()
    subprocess_func_aliases: dict[str, str] = {}
    os_aliases: set[str] = set()
    os_exec_aliases: dict[str, str] = {}
    pickle_aliases: set[str] = set()
    yaml_aliases: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name in UNSAFE_XML_MODULES:
                    unsafe_xml_module_aliases.add(bound)
                elif alias.name == "subprocess":
                    subprocess_aliases.add(bound)
                elif alias.name == "os":
                    os_aliases.add(bound)
                elif alias.name == "pickle":
                    pickle_aliases.add(bound)
                elif alias.name == "yaml":
                    yaml_aliases.add(bound)

        elif isinstance(node, ast.ImportFrom) and node.module:
            mod = node.module
            for alias in node.names:
                bound = alias.asname or alias.name
                full = f"{mod}.{alias.name}"
                if full in UNSAFE_XML_MODULES:
                    unsafe_xml_module_aliases.add(bound)
                elif mod in UNSAFE_XML_MODULES and alias.name in UNSAFE_XML_PARSE_FUNCS:
                    unsafe_xml_func_aliases[bound] = full
                elif mod == "subprocess" and alias.name in SUBPROCESS_EXEC_FUNCS:
                    subprocess_func_aliases[bound] = full
                elif mod == "os" and alias.name in {"system", "popen"}:
                    os_exec_aliases[bound] = full

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        lineno = getattr(node, "lineno", 0)
        func = node.func

        # 1. Check subprocess.*(..., shell=True)
        is_subprocess_call = False
        call_name = ""
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in subprocess_aliases
            and func.attr in SUBPROCESS_EXEC_FUNCS
        ):
            is_subprocess_call = True
            call_name = f"{func.value.id}.{func.attr}"
        elif isinstance(func, ast.Name) and func.id in subprocess_func_aliases:
            is_subprocess_call = True
            call_name = subprocess_func_aliases[func.id]

        if is_subprocess_call:
            for kw in node.keywords:
                if kw.arg == "shell":
                    # Only literal False is safe
                    if not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
                        findings.append(
                            {
                                "rule": "command_injection_shell_true",
                                "file": filename,
                                "line": lineno,
                                "detail": f"{call_name} invoked with shell!=False",
                            }
                        )

        # 2. Check os.system / os.popen
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in os_aliases
            and func.attr in {"system", "popen"}
        ):
            findings.append(
                {
                    "rule": "command_injection_os_shell",
                    "file": filename,
                    "line": lineno,
                    "detail": f"os.{func.attr} is forbidden; use subprocess with shell=False",
                }
            )
        elif isinstance(func, ast.Name) and func.id in os_exec_aliases:
            findings.append(
                {
                    "rule": "command_injection_os_shell",
                    "file": filename,
                    "line": lineno,
                    "detail": f"{os_exec_aliases[func.id]} is forbidden",
                }
            )

        # 3. Check raw stdlib XML parsing (XXE risk) — defusedxml is allowed, XML writing is allowed
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in unsafe_xml_module_aliases
            and func.attr in UNSAFE_XML_PARSE_FUNCS
        ):
            findings.append(
                {
                    "rule": "xxe_unsafe_xml_parse",
                    "file": filename,
                    "line": lineno,
                    "detail": f"Unsafe XML parser {func.value.id}.{func.attr}(); use defusedxml",
                }
            )
        elif isinstance(func, ast.Name) and func.id in unsafe_xml_func_aliases:
            findings.append(
                {
                    "rule": "xxe_unsafe_xml_parse",
                    "file": filename,
                    "line": lineno,
                    "detail": f"Unsafe XML parser {unsafe_xml_func_aliases[func.id]}(); use defusedxml",
                }
            )

        # 4. Check eval / exec / pickle.load(s) / unsafe yaml.load
        if isinstance(func, ast.Name) and func.id in {"eval", "exec"}:
            findings.append(
                {
                    "rule": "unsafe_code_execution",
                    "file": filename,
                    "line": lineno,
                    "detail": f"Direct call to {func.id}() is prohibited",
                }
            )
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in pickle_aliases
            and func.attr in {"load", "loads"}
        ):
            findings.append(
                {
                    "rule": "unsafe_deserialization",
                    "file": filename,
                    "line": lineno,
                    "detail": f"pickle.{func.attr}() is prohibited on untrusted data",
                }
            )
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in yaml_aliases
            and func.attr == "load"
        ):
            loader_kw = next((kw for kw in node.keywords if kw.arg == "Loader"), None)
            safe_loader = (
                loader_kw is not None
                and isinstance(loader_kw.value, ast.Attribute)
                and loader_kw.value.attr in {"SafeLoader", "CSafeLoader"}
            )
            if not safe_loader:
                findings.append(
                    {
                        "rule": "unsafe_yaml_load",
                        "file": filename,
                        "line": lineno,
                        "detail": "yaml.load() without SafeLoader is prohibited; use yaml.safe_load()",
                    }
                )

    return findings


def safe_extract_zip(
    archive_path: Path,
    dest_dir: Path,
    *,
    max_total_bytes: int = 50 * 1024 * 1024,
    max_single_file_bytes: int = 20 * 1024 * 1024,
    max_entries: int = 500,
) -> list[Path]:
    """Reference hardened ZIP extractor enforcing zip-slip, symlink, and zip-bomb limits."""
    dest_root = dest_dir.resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    total_written = 0

    with zipfile.ZipFile(archive_path, "r") as zf:
        infos = zf.infolist()
        if len(infos) > max_entries:
            raise ValueError(f"Zip archive exceeds max_entries={max_entries}")

        for info in infos:
            raw_name = info.filename
            # Reject absolute paths, drive prefixes, NUL bytes, and parent directory traversal
            if (
                "\x00" in raw_name
                or raw_name.startswith(("/", "\\"))
                or re.match(r"^[A-Za-z]:", raw_name)
                or ".." in Path(raw_name.replace("\\", "/")).parts
            ):
                raise ValueError(f"Zip-slip / path traversal entry blocked: {raw_name!r}")

            # Reject symlinks encoded in Unix external_attr mode bits (0o120000)
            unix_mode = (info.external_attr >> 16) & 0o170000
            if unix_mode == 0o120000:
                raise ValueError(f"Symlink entry blocked in zip archive: {raw_name!r}")

            target_path = (dest_root / raw_name).resolve()
            if not target_path.is_relative_to(dest_root):
                raise ValueError(f"Resolved path escapes target sandbox: {raw_name!r}")

            if info.is_dir():
                target_path.mkdir(parents=True, exist_ok=True)
                continue

            if info.file_size > max_single_file_bytes:
                raise ValueError(
                    f"Zip-bomb guard: entry {raw_name!r} declares {info.file_size} bytes"
                )

            target_path.parent.mkdir(parents=True, exist_ok=True)
            written_for_entry = 0
            with zf.open(info, "r") as src, open(target_path, "wb") as dst:
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    written_for_entry += len(chunk)
                    total_written += len(chunk)
                    if (
                        written_for_entry > max_single_file_bytes
                        or total_written > max_total_bytes
                    ):
                        dst.close()
                        target_path.unlink(missing_ok=True)
                        raise ValueError(
                            f"Zip-bomb guard: decompressed size exceeded limit on {raw_name!r}"
                        )
                    dst.write(chunk)
            extracted.append(target_path)

    return extracted


# ---------------------------------------------------------------------------
# 1. GPL-3.0 & Upstream Kdenlive Licence Compliance Audits
# ---------------------------------------------------------------------------


def test_gpl3_copying_and_upstream_licenses_intact() -> None:
    """Verify upstream Kdenlive GPL-3.0 COPYING and LICENSES/ directory remain intact."""
    copying = REPO_ROOT / "COPYING"
    assert copying.is_file(), "Top-level GPL-3.0 COPYING file is missing"
    copying_text = copying.read_text(encoding="utf-8")
    assert "GNU GENERAL PUBLIC LICENSE" in copying_text
    assert "Version 3, 29 June 2007" in copying_text
    assert len(copying_text) > 30000, "COPYING appears truncated"

    licenses_dir = REPO_ROOT / "LICENSES"
    assert licenses_dir.is_dir(), "Upstream LICENSES/ directory is missing"

    required_license_files = [
        "GPL-3.0-only.txt",
        "GPL-3.0-or-later.txt",
        "LicenseRef-KDE-Accepted-GPL.txt",
        "LGPL-3.0-only.txt",
        "Apache-2.0.txt",
        "BSD-3-Clause.txt",
        "MIT.txt",
    ]
    for fname in required_license_files:
        fpath = licenses_dir / fname
        assert fpath.is_file() and fpath.stat().st_size > 200, (
            f"Required upstream licence file missing or empty: LICENSES/{fname}"
        )


def test_open_source_components_matrix_and_requirements_audit() -> None:
    """Verify docs/OPEN_SOURCE_COMPONENTS.md and requirements-bangonpro.txt compliance."""
    doc_path = REPO_ROOT / "docs" / "OPEN_SOURCE_COMPONENTS.md"
    assert doc_path.is_file(), "docs/OPEN_SOURCE_COMPONENTS.md is missing"
    doc_text = doc_path.read_text(encoding="utf-8")

    required_components = [
        "Kdenlive",
        "Qt",
        "KDE Frameworks",
        "MLT",
        "FFmpeg",
        "frei0r",
        "OpenTimelineIO",
        "PySceneDetect",
        "RNNoise",
        "Auto-Editor",
        "WhisperX",
        "SAM2",
    ]
    for comp in required_components:
        assert comp in doc_text, f"Component missing from OPEN_SOURCE_COMPONENTS.md: {comp}"

    assert "GPL-3.0" in doc_text
    assert "BANGONPRO_MODEL_DIR" in doc_text, "Model storage guidance missing"
    assert "--enable-nonfree" in doc_text, "FFmpeg nonfree exclusion policy missing"

    # Verify requirements-bangonpro.txt uses pinned/bounded version specifiers
    req_path = REPO_ROOT / "requirements-bangonpro.txt"
    assert req_path.is_file(), "requirements-bangonpro.txt is missing"
    allowed_core_packages = {
        "numpy",
        "scipy",
        "defusedxml",
        "jsonschema",
        "opentimelineio",
        "scenedetect",
        "pytest",
        "pillow",
        "pyyaml",
    }
    found_packages: set[str] = set()
    for raw_line in req_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*([<>=!~].+)$", line)
        assert match is not None, f"Unpinned requirement in requirements-bangonpro.txt: {line!r}"
        pkg_name = match.group(1).lower()
        found_packages.add(pkg_name)
        assert pkg_name in allowed_core_packages, (
            f"Unvetted dependency in requirements-bangonpro.txt: {pkg_name!r}"
        )

    assert {"defusedxml", "jsonschema", "pytest"}.issubset(found_packages)


# ---------------------------------------------------------------------------
# 2. Proprietary Preset Archive Protection & Repository Hygiene
# ---------------------------------------------------------------------------


def test_gitignore_blocks_proprietary_preset_archives() -> None:
    """Verify .gitignore explicitly blocks proprietary preset archives and binaries."""
    gitignore_path = REPO_ROOT / ".gitignore"
    assert gitignore_path.is_file(), ".gitignore missing"
    content = gitignore_path.read_text(encoding="utf-8")

    for pattern in ("effects pr.zip", "*pr.zip", "*.prfpset", "*.dmg", "*.app/"):
        assert pattern in content, f"Missing required .gitignore pattern: {pattern!r}"

    # Confirm Git itself enforces these ignore rules
    if shutil.which("git"):
        for sample_path in (
            "effects pr.zip",
            "presets/effects pr.zip",
            "thirdparty_pr.zip",
            "presets/my_transition.prfpset",
        ):
            proc = subprocess.run(
                ["git", "check-ignore", "--no-index", sample_path],
                cwd=REPO_ROOT,
                shell=False,
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 0, (
                f"Git does not ignore proprietary preset path {sample_path!r}: "
                f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
            )


def test_repository_contains_no_committed_prfpset_or_preset_zips() -> None:
    """Guarantee ZERO .prfpset or third-party preset .zip binaries are committed or staged."""
    forbidden_tracked: list[str] = []

    if shutil.which("git"):
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO_ROOT,
            shell=False,
            check=True,
            capture_output=True,
            text=True,
        )
        tracked_files = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    else:
        tracked_files = [
            str(p.relative_to(REPO_ROOT))
            for p in REPO_ROOT.rglob("*")
            if p.is_file() and ".git" not in p.parts
        ]

    for rel in tracked_files:
        low = rel.lower()
        name = Path(low).name
        if low.endswith(".prfpset"):
            forbidden_tracked.append(f"prfpset committed: {rel}")
        elif name == "effects pr.zip" or name.endswith("pr.zip"):
            forbidden_tracked.append(f"proprietary preset zip committed: {rel}")
        elif low.endswith(".zip"):
            full_zip = REPO_ROOT / rel
            if full_zip.is_file():
                try:
                    with zipfile.ZipFile(full_zip, "r") as zf:
                        if any(entry.lower().endswith(".prfpset") for entry in zf.namelist()):
                            forbidden_tracked.append(
                                f"zip archive containing .prfpset committed: {rel}"
                            )
                except zipfile.BadZipFile:
                    pass

    assert not forbidden_tracked, (
        "Proprietary Premiere preset files MUST NEVER be committed to Git:\n"
        + "\n".join(forbidden_tracked)
    )


# ---------------------------------------------------------------------------
# 3. Static Security AST Scan over bangonpro/
# ---------------------------------------------------------------------------


def test_ast_security_scanner_detects_synthetic_vulnerabilities() -> None:
    """Self-test the AST security scanner to ensure real vulnerabilities are flagged."""
    vulnerable_sample = """
import os
import subprocess
import xml.etree.ElementTree as ET

def bad_shell(user_file):
    subprocess.run(f"melt {user_file}", shell=True)
    os.system("rm -rf /tmp/x")

def bad_xml(prfpset_path):
    return ET.parse(prfpset_path)

def bad_eval(expr):
    return eval(expr)
"""
    findings = scan_python_source_for_vulnerabilities(vulnerable_sample, "synthetic_vuln.py")
    rules = {f["rule"] for f in findings}
    assert "command_injection_shell_true" in rules
    assert "command_injection_os_shell" in rules
    assert "xxe_unsafe_xml_parse" in rules
    assert "unsafe_code_execution" in rules

    safe_sample = """
import subprocess
import defusedxml.ElementTree as SafeET
import xml.etree.ElementTree as OutgoingET

def safe_run(path):
    return subprocess.run(["ffprobe", str(path)], shell=False, check=True)

def safe_parse(xml_bytes):
    return SafeET.fromstring(xml_bytes)

def safe_serialize():
    el = OutgoingET.Element("mlt")
    return OutgoingET.tostring(el, encoding="utf-8")
"""
    clean_findings = scan_python_source_for_vulnerabilities(safe_sample, "synthetic_clean.py")
    assert clean_findings == [], f"Unexpected false positives: {clean_findings}"


def test_bangonpro_codebase_passes_static_security_ast_scan() -> None:
    """Scan all Python source files under bangonpro/ for command-injection, XXE, and unsafe eval."""
    py_files = sorted(BANGONPRO_DIR.rglob("*.py"))
    assert py_files, "Expected Python files inside bangonpro/"

    all_findings: list[dict[str, Any]] = []
    for py_file in py_files:
        source = py_file.read_text(encoding="utf-8")
        rel = str(py_file.relative_to(REPO_ROOT))
        all_findings.extend(scan_python_source_for_vulnerabilities(source, rel))

    assert not all_findings, (
        "Static AST security violations detected in bangonpro/:\n"
        + json.dumps(all_findings, indent=2)
    )


# ---------------------------------------------------------------------------
# 4. Runtime Security Regression Tests (XXE, Zip-Slip, Zip-Bomb, Injection)
# ---------------------------------------------------------------------------


def test_xxe_and_billion_laughs_prfpset_payloads_rejected() -> None:
    """Verify XXE and XML entity expansion (Billion Laughs) payloads are blocked."""
    xxe_payload = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<!DOCTYPE PremiereData [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b'<PremiereData Version="3"><Preset Name="&xxe;"/></PremiereData>'
    )
    billion_laughs_payload = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<!DOCTYPE lolz ['
        b'<!ENTITY lol "lol">'
        b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
        b'<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">'
        b']>'
        b'<PremiereData Version="3"><Preset Name="&lol3;"/></PremiereData>'
    )

    with pytest.raises((DTDForbidden, EntitiesForbidden)):
        DefusedET.fromstring(xxe_payload)

    with pytest.raises((DTDForbidden, EntitiesForbidden)):
        DefusedET.fromstring(billion_laughs_payload)


def test_zip_slip_path_traversal_and_zip_bomb_blocked(tmp_path: Path) -> None:
    """Verify adversarial zip-slip, absolute-path, symlink, and zip-bomb archives are blocked."""
    sandbox = tmp_path / "extract_sandbox"
    outside_marker = tmp_path / "escaped_file.txt"

    # Case A: Relative parent directory traversal (zip-slip)
    slip_zip = tmp_path / "zip_slip.zip"
    with zipfile.ZipFile(slip_zip, "w") as zf:
        zf.writestr("../escaped_file.txt", "pwned")
    with pytest.raises(ValueError, match="traversal|escapes"):
        safe_extract_zip(slip_zip, sandbox)
    assert not outside_marker.exists(), "Zip-slip wrote outside extraction sandbox!"

    # Case B: Absolute path entry
    abs_zip = tmp_path / "abs_path.zip"
    with zipfile.ZipFile(abs_zip, "w") as zf:
        zf.writestr("/etc/cron.d/bangonpro_pwn", "pwned")
    with pytest.raises(ValueError, match="traversal|blocked"):
        safe_extract_zip(abs_zip, sandbox)

    # Case C: Unix symlink entry inside zip
    symlink_zip = tmp_path / "symlink.zip"
    with zipfile.ZipFile(symlink_zip, "w") as zf:
        zi = zipfile.ZipInfo("sneaky_link.prfpset")
        zi.create_system = 3  # Unix
        zi.external_attr = (0o120777 << 16)
        zf.writestr(zi, "/etc/passwd")
    with pytest.raises(ValueError, match="Symlink"):
        safe_extract_zip(symlink_zip, sandbox)

    # Case D: Zip-bomb exceeding decompressed byte cap
    bomb_zip = tmp_path / "zip_bomb.zip"
    with zipfile.ZipFile(bomb_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("huge.prfpset", b"A" * (2 * 1024 * 1024))
    with pytest.raises(ValueError, match="Zip-bomb"):
        safe_extract_zip(bomb_zip, sandbox, max_single_file_bytes=256 * 1024)

    # Case E: Benign archive succeeds cleanly inside sandbox
    good_zip = tmp_path / "benign.zip"
    with zipfile.ZipFile(good_zip, "w") as zf:
        zf.writestr("Pack/Zoom In.prfpset", "<PremiereData Version='3'/>")
    extracted = safe_extract_zip(good_zip, sandbox)
    assert len(extracted) == 1
    assert extracted[0].resolve().is_relative_to(sandbox.resolve())


def test_command_injection_metacharacters_neutralized_with_argv_list(tmp_path: Path) -> None:
    """Verify shell metacharacters in untrusted filenames never execute commands when shell=False."""
    canary_a = tmp_path / "injected_a.txt"
    canary_b = tmp_path / "injected_b.txt"
    canary_c = tmp_path / "injected_c.txt"
    adversarial_names = [
        f"clip; touch {canary_a.name};.mp4",
        f"$(touch {canary_b.name}).mp4",
        f"`touch {canary_c.name}`.mp4",
    ]
    for name in adversarial_names:
        media_file = tmp_path / name
        media_file.write_text("harmless-media-marker", encoding="utf-8")
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import pathlib, sys; print(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))",
                name,
            ],
            cwd=tmp_path,
            shell=False,
            check=True,
            capture_output=True,
            text=True,
        )
        assert proc.stdout.strip() == "harmless-media-marker"

    assert not canary_a.exists(), "Shell injection executed via semicolon metacharacter!"
    assert not canary_b.exists(), "Shell injection executed via command substitution!"
    assert not canary_c.exists(), "Shell injection executed via backtick substitution!"


# ---------------------------------------------------------------------------
# 5. JSON Schema Draft 2020-12 Validation
# ---------------------------------------------------------------------------


def test_all_bangonpro_json_schemas_are_valid_draft_2020_12() -> None:
    """Verify every JSON schema in bangonpro/schemas/ is a valid Draft 2020-12 schema."""
    schema_files = sorted(SCHEMAS_DIR.glob("*.schema.json"))
    expected = {
        "timeline_command_v1.schema.json",
        "show_profile_v1.schema.json",
        "premiere_preset_report_v1.schema.json",
    }
    found_names = {p.name for p in schema_files}
    assert expected.issubset(found_names), (
        f"Missing required Bang On Pro schemas: {expected - found_names}"
    )

    for schema_path in schema_files:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        assert schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema", (
            f"{schema_path.name} must declare Draft 2020-12 $schema URI"
        )
        Draft202012Validator.check_schema(schema)


def test_json_schemas_enforce_contracts_on_sample_payloads() -> None:
    """Verify the three v1 schemas accept valid payloads and reject malformed payloads."""
    # 1. Timeline Command v1
    tl_schema = json.loads(
        (SCHEMAS_DIR / "timeline_command_v1.schema.json").read_text(encoding="utf-8")
    )
    tl_validator = Draft202012Validator(tl_schema)
    valid_tl = {
        "api_version": "1.0",
        "request_id": "req-001",
        "project_id": "proj-demo",
        "sequence_id": "seq-main",
        "expected_revision": 0,
        "actor": {"agent_id": "ai-editor", "role": "rough_cut", "requires_approval": True},
        "operations": [
            {
                "op_id": "op-1",
                "type": "place_clip",
                "media_ref": "clip_a.mp4",
                "track_id": "V1",
                "frame_range": {"in_frame": 0, "out_frame": 50, "timeline_start": 0},
            }
        ],
    }
    tl_validator.validate(valid_tl)

    invalid_tl = dict(valid_tl)
    invalid_tl["operations"] = [{"op_id": "op-bad", "type": "run_arbitrary_shell"}]
    with pytest.raises(jsonschema.ValidationError):
        tl_validator.validate(invalid_tl)

    # 2. Show Profile v1
    sp_schema = json.loads(
        (SCHEMAS_DIR / "show_profile_v1.schema.json").read_text(encoding="utf-8")
    )
    sp_validator = Draft202012Validator(sp_schema)
    valid_sp = {
        "schema_version": "1.0",
        "profile_id": "studio-podcast",
        "display_name": "Studio Podcast 2-Cam",
        "format_type": "podcast",
        "video_spec": {"width": 1920, "height": 1080, "fps_num": 25, "fps_den": 1},
        "cameras": [{"camera_id": "camA", "role": "host", "speaker_label": "Host"}],
        "editing_rules": {
            "min_shot_sec": 1.5,
            "max_shot_sec": 12.0,
            "silence_threshold_db": -38.0,
            "min_silence_sec": 0.6,
        },
        "audio_rules": {"target_lufs": -16.0},
        "graphics_rules": {"enable_captions": True},
        "export_presets": [
            {
                "name": "YouTube 1080p",
                "container": "mp4",
                "video_codec": "libx264",
                "audio_codec": "aac",
            }
        ],
    }
    sp_validator.validate(valid_sp)

    invalid_sp = dict(valid_sp)
    invalid_sp["cameras"] = []
    with pytest.raises(jsonschema.ValidationError):
        sp_validator.validate(invalid_sp)

    # 3. Premiere Preset Report v1
    pr_schema = json.loads(
        (SCHEMAS_DIR / "premiere_preset_report_v1.schema.json").read_text(encoding="utf-8")
    )
    pr_validator = Draft202012Validator(pr_schema)
    valid_pr = {
        "schema_version": "1.0",
        "generated_at": "2026-11-22T00:00:00Z",
        "source_archive": {"name": "user_pack.zip", "sha256": "0" * 64, "file_count": 1},
        "summary": {
            "total_presets": 1,
            "distinct_effects": 1,
            "exact_validated": 1,
            "approximated": 0,
            "unsupported": 0,
            "missing_dependency": 0,
        },
        "presets": [
            {
                "preset_id": "p-001",
                "name": "Clean Fade",
                "pack": "User Pack",
                "folder_path": ["Transitions"],
                "source_file": "Clean Fade.prfpset",
                "compatibility_status": "exact_validated",
                "effect_chain": [{"effect": "Opacity"}],
                "missing_dependencies": [],
                "mlt_filters": [{"mlt_service": "brightness"}],
            }
        ],
    }
    pr_validator.validate(valid_pr)

    invalid_pr = json.loads(json.dumps(valid_pr))
    invalid_pr["presets"][0]["compatibility_status"] = "magic_auto_converted"
    with pytest.raises(jsonschema.ValidationError):
        pr_validator.validate(invalid_pr)


# ---------------------------------------------------------------------------
# 6. macOS Packaging Metadata, Hardened Entitlements & CI Config Checks
# ---------------------------------------------------------------------------


def test_macos_plist_and_entitlements_are_valid_and_jit_free() -> None:
    """Verify macOS Info.plist.in and entitlements.plist are valid and enforce JIT-free posture."""
    info_plist_in = PACKAGING_MACOS_DIR / "Info.plist.in"
    entitlements_plist = PACKAGING_MACOS_DIR / "entitlements.plist"
    assert info_plist_in.is_file(), "packaging/macos/Info.plist.in missing"
    assert entitlements_plist.is_file(), "packaging/macos/entitlements.plist missing"

    # Substitute template tokens and parse via plistlib
    rendered_info = (
        info_plist_in.read_text(encoding="utf-8")
        .replace("@BANGONPRO_EXECUTABLE@", "Bang On Pro")
        .replace("@BANGONPRO_ICON_FILE@", "bangonpro.icns")
        .replace("@BANGONPRO_VERSION@", "0.1.0-alpha")
        .replace("@BANGONPRO_BUILD_NUMBER@", "1")
        .replace("@BANGONPRO_GIT_SHA@", "deadbeef")
        .replace("@BANGONPRO_MIN_MACOS@", "13.0")
    )
    info_dict = plistlib.loads(rendered_info.encode("utf-8"))
    assert info_dict["CFBundleIdentifier"] == "com.bangonpro.editor"
    assert info_dict["CFBundleName"] == "Bang On Pro"
    assert "arm64" in info_dict.get("LSArchitecturePriority", [])
    assert info_dict.get("NSHighResolutionCapable") is True

    ent_dict = plistlib.loads(entitlements_plist.read_bytes())
    assert ent_dict.get("com.apple.security.device.audio-input") is True
    assert ent_dict.get("com.apple.security.device.camera") is True
    assert ent_dict.get("com.apple.security.files.user-selected.read-write") is True
    # JIT-free & no AppleEvents UI automation enforcement
    assert not ent_dict.get("com.apple.security.cs.allow-jit", False)
    assert not ent_dict.get("com.apple.security.cs.allow-unsigned-executable-memory", False)
    assert not ent_dict.get("com.apple.security.automation.apple-events", False)


def test_macos_packaging_script_and_ci_workflows_syntax() -> None:
    """Verify build_macos_arm64.sh passes bash -n and GitHub workflows exist and are valid."""
    build_script = PACKAGING_MACOS_DIR / "build_macos_arm64.sh"
    assert build_script.is_file(), "packaging/macos/build_macos_arm64.sh missing"
    assert os.access(build_script, os.X_OK), "build_macos_arm64.sh must be executable"

    proc = subprocess.run(
        ["bash", "-n", str(build_script)],
        shell=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"bash -n failed: {proc.stderr}"

    ci_linux = WORKFLOWS_DIR / "ci-linux.yml"
    ci_macos = WORKFLOWS_DIR / "build-macos-arm64.yml"
    assert ci_linux.is_file(), ".github/workflows/ci-linux.yml missing"
    assert ci_macos.is_file(), ".github/workflows/build-macos-arm64.yml missing"

    # Validate YAML syntax using PyYAML in current Python or system Python if installed
    yaml_validator = (
        "import sys, yaml\n"
        "for p in sys.argv[1:]:\n"
        "    with open(p, 'r', encoding='utf-8') as fh:\n"
        "        data = yaml.safe_load(fh)\n"
        "    assert isinstance(data, dict) and 'jobs' in data, f'Invalid workflow: {p}'\n"
    )
    validated = False
    for py_bin in (sys.executable, "/usr/bin/python3"):
        if not Path(py_bin).exists():
            continue
        check = subprocess.run(
            [py_bin, "-c", yaml_validator, str(ci_linux), str(ci_macos)],
            shell=False,
            capture_output=True,
            text=True,
        )
        if check.returncode == 0:
            validated = True
            break
        if "ModuleNotFoundError" not in check.stderr:
            pytest.fail(f"Workflow YAML validation failed: {check.stderr}")

    assert validated, "Could not validate workflow YAML syntax (PyYAML unavailable)"


def test_artcraft_attribution_and_excluded_third_party_assets_absent() -> None:
    """Verify ArtCraft (storytold/artcraft, MIT OR Apache-2.0) attribution and ZERO excluded asset dirs."""
    doc_text = (REPO_ROOT / "docs" / "OPEN_SOURCE_COMPONENTS.md").read_text(encoding="utf-8")
    assert "storytold/artcraft" in doc_text, "ArtCraft upstream repository reference missing"
    assert "MIT OR Apache-2.0" in doc_text, "ArtCraft dual MIT OR Apache-2.0 licence missing"

    excluded_artcraft_dirs = ("sound", "memes", "skybox", "pose", "characters_gif")
    for dirname in excluded_artcraft_dirs:
        assert f"{dirname}/" in doc_text, (
            f"Excluded ArtCraft asset directory '{dirname}/' must be documented in OPEN_SOURCE_COMPONENTS.md"
        )

    # Ensure build_macos_arm64.sh embeds ArtCraft attribution notice inside .app/Contents/Resources/LICENSES/
    build_sh_text = (PACKAGING_MACOS_DIR / "build_macos_arm64.sh").read_text(encoding="utf-8")
    assert "ARTCRAFT_NOTICE.txt" in build_sh_text, "macOS bundle must include ARTCRAFT_NOTICE.txt"

    # Scan Git tracked files to guarantee ZERO excluded ArtCraft third-party media directories exist
    if shutil.which("git"):
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO_ROOT,
            shell=False,
            check=True,
            capture_output=True,
            text=True,
        )
        tracked_files = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    else:
        tracked_files = [
            str(p.relative_to(REPO_ROOT))
            for p in REPO_ROOT.rglob("*")
            if p.is_file() and ".git" not in p.parts
        ]

    forbidden_asset_hits: list[str] = []
    excluded_set = set(excluded_artcraft_dirs)
    for rel in tracked_files:
        parts_lower = [part.lower() for part in Path(rel).parts[:-1]]
        if any(part in excluded_set for part in parts_lower):
            forbidden_asset_hits.append(rel)

    assert not forbidden_asset_hits, (
        "Excluded ArtCraft third-party asset directories MUST NOT exist in Bang On Pro:\n"
        + "\n".join(forbidden_asset_hits)
    )


def test_macos_craft_config_blueprint_and_ci_self_contained_packaging() -> None:
    """Validate CraftConfig.ini, craft_blueprint.py (Craft master libs/qt/*), bootstrap_ai_runtime.sh, and macOS CI."""
    craft_ini = PACKAGING_MACOS_DIR / "CraftConfig.ini"
    assert craft_ini.is_file(), "packaging/macos/CraftConfig.ini missing"

    cp = configparser.ConfigParser()
    cp.optionxform = str  # preserve case sensitivity required by CraftMaster
    cp.read(craft_ini, encoding="utf-8")

    for section in ("General", "Variables", "GeneralSettings", "BlueprintSettings", "macos-arm-clang"):
        assert section in cp, f"Missing required section [{section}] in CraftConfig.ini"

    assert cp["General"].get("Branch") == "master"
    assert cp["General"].get("ShallowClone") == "True"
    assert cp["Variables"].get("UseCache") == "True"
    assert cp["Variables"].get("CreateCache") == "False"
    assert cp["GeneralSettings"].get("General/KFHostToolingVersion") == "6"
    assert cp["GeneralSettings"].get("Packager/RepositoryUrl") == "https://files.kde.org/craft/Qt6/"
    assert cp["GeneralSettings"].get("CodeSigning/Enabled") == "False"
    assert cp["BlueprintSettings"].get("libs/qt.qtMajorVersion") == "6"
    assert cp["BlueprintSettings"].get("libs/qt5.ignored") == "True"
    assert cp["BlueprintSettings"].get("libs/qt6/qtdoc.ignored") == "True"
    assert cp["BlueprintSettings"].get("/.buildTests") == "False"
    assert cp["macos-arm-clang"].get("General/ABI") == "macos-clang-arm64"
    assert cp["macos-arm-clang"].get("Compile/BuildType") == "RelWithDebInfo"
    assert cp["macos-arm-clang"].get("Packager/PackageType") == "MacDMGPackager"

    # Verify CraftMaster key syntax invariants: '/' in GeneralSettings/target, '.' in BlueprintSettings
    for sec in ("GeneralSettings", "macos-arm-clang"):
        for key in cp[sec]:
            assert "/" in key, f"CraftMaster requires Category/Key in [{sec}], got {key!r}"
    for key in cp["BlueprintSettings"]:
        assert "." in key, f"CraftMaster requires blueprint.option in [BlueprintSettings], got {key!r}"

    # Verify craft_blueprint.py imports offline, uses version 1.0.0 & Craft master libs/qt/* names
    bp_path = PACKAGING_MACOS_DIR / "craft_blueprint.py"
    spec = importlib.util.spec_from_file_location("bangonpro_craft_blueprint", bp_path)
    assert spec is not None and spec.loader is not None
    bp_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bp_mod)
    assert bp_mod.BANGONPRO_VERSION == "1.0.0"

    si = bp_mod.subinfo()
    si.setTargets()
    si.setDependencies()
    deps = set(si.runtimeDependencies.keys())
    for required_dep in (
        "libs/qt/qtbase",
        "libs/qt/qtdeclarative",
        "libs/qt/qtmultimedia",
        "libs/qt/qtnetworkauth",
        "libs/qt/qtsvg",
        "libs/qt/qtimageformats",
        "kde/frameworks/tier1/breeze-icons",
        "kde/frameworks/tier1/kcolorscheme",
        "kde/frameworks/tier1/kplotting",
        "kde/frameworks/tier1/kimageformats",
        "kde/frameworks/tier3/kdeclarative",
        "kde/frameworks/tier4/frameworkintegration",
        "kde/plasma/breeze",
        "libs/ffmpeg",
        "libs/mlt",
        "libs/frei0r-plugins",
        "libs/rubberband",
    ):
        assert required_dep in deps, f"craft_blueprint.py missing Craft master dependency: {required_dep}"
    assert not any(d.startswith("libs/qt6/") for d in deps), (
        "craft_blueprint.py must use Craft master libs/qt/* paths, not deprecated libs/qt6/*"
    )

    # Verify bootstrap_ai_runtime.sh exists, is executable, and passes bash -n
    bootstrap_sh = PACKAGING_MACOS_DIR / "bootstrap_ai_runtime.sh"
    assert bootstrap_sh.is_file(), "packaging/macos/bootstrap_ai_runtime.sh missing"
    assert os.access(bootstrap_sh, os.X_OK), "bootstrap_ai_runtime.sh must be executable"
    proc = subprocess.run(["bash", "-n", str(bootstrap_sh)], shell=False, capture_output=True, text=True)
    assert proc.returncode == 0, f"bash -n failed on bootstrap_ai_runtime.sh: {proc.stderr}"

    # Verify build_macos_arm64.sh performs recursive dylib relocation & otool -L audit
    build_sh_text = (PACKAGING_MACOS_DIR / "build_macos_arm64.sh").read_text(encoding="utf-8")
    assert "relocate_macho_deps" in build_sh_text
    assert "audit_no_leaked_dylib_paths" in build_sh_text
    assert "python-env" in build_sh_text
    assert "mlt-7" in build_sh_text
    assert "frei0r-1" in build_sh_text

    # Verify build-macos-arm64.yml uses CraftMaster.py (never broken --branch qt6) & non-draft releases
    wf_text = (WORKFLOWS_DIR / "build-macos-arm64.yml").read_text(encoding="utf-8")
    assert "CraftMaster.py" in wf_text
    assert "packaging/macos/CraftConfig.ini" in wf_text
    assert "--branch qt6" not in wf_text, "Broken CraftBootstrap --branch qt6 must not be used"
    assert "draft: false" in wf_text, "Versioned tag releases must publish non-draft releases"
    assert 'default: "1.1.0"' in wf_text
    assert "verify_packaged_dmg" in build_sh_text
    assert "benchmark_results.json" in wf_text

