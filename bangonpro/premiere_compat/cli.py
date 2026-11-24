"""Command-line interface for Bang On Pro Premiere Preset Compatibility Engine.

Usage:
    python3 -m bangonpro.premiere_compat.cli import \\
        --input <zip_or_dir_or_prfpset> \\
        --output-dir <dir> \\
        [--audit-json <path>] \\
        [--verify-render] \\
        [--generate-previews]

    python3 -m bangonpro.premiere_compat.cli audit \\
        --input <zip_or_dir> \\
        [--audit-json <optional_audit.json>] \\
        --output <report.json> \\
        [--generate-previews]

    python3 -m bangonpro.premiere_compat.cli builtin-library \\
        --output <report.json> \\
        [--generate-previews]

SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .builtin_library import build_builtin_starter_report
from .mapper import (
    build_compatibility_report,
    build_missing_inputs_report,
    export_report_bundle,
    validate_report_schema,
)
from .models import MissingSourceInput
from .parser import parse_premiere_input
from .preview_generator import attach_previews_to_report
from .security import SecurityViolationError
from .verifier import verify_presets_batch


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bangonpro-premiere-compat",
        description="Bang On Pro Adobe Premiere Pro (.prfpset) Compatibility Engine",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # 1. import
    imp = sub.add_parser(
        "import",
        help="Safely inspect, convert, and report on a .prfpset file, directory, or .zip archive.",
    )
    imp.add_argument(
        "--input",
        required=True,
        help="Path to a .prfpset file, directory of presets, or .zip preset pack.",
    )
    imp.add_argument(
        "--output-dir",
        required=True,
        help="Directory to write compatibility_report.json, compatibility_report.md, and Kdenlive XMLs.",
    )
    imp.add_argument(
        "--audit-json",
        default=None,
        help="Optional path to Bang_on_pro_premiere_preset_audit.json for cross-checks & attribution.",
    )
    imp.add_argument(
        "--asset-dir",
        action="append",
        default=[],
        help="Optional directory to search for external LUTs/fonts/IRs (repeatable).",
    )
    imp.add_argument(
        "--fps",
        type=float,
        default=25.0,
        help="Target timeline frame rate for tick-to-frame keyframe conversion (default: 25.0).",
    )
    imp.add_argument(
        "--platform",
        default="linux",
        choices=("linux", "macos", "windows"),
        help="Target host platform for plugin availability checks (default: linux).",
    )
    imp.add_argument(
        "--verify-render",
        action="store_true",
        help="Run deterministic melt + ffmpeg before/after visual & audio verification renders.",
    )
    imp.add_argument(
        "--generate-previews",
        action="store_true",
        help="Render cached before/after/split PNG thumbnails for executable presets.",
    )
    imp.add_argument(
        "--preview-dir",
        default=None,
        help="Optional cache directory for rendered preview PNGs (defaults to <output-dir>/previews).",
    )

    # 2. audit
    aud = sub.add_parser(
        "audit",
        help=(
            "Produce an itemised Premiere preset compatibility audit report, or emit an explicit "
            "missing_source_inputs blocker report without fabricating entries when proprietary "
            "source files are absent."
        ),
    )
    aud.add_argument(
        "--input",
        required=True,
        help="Path to proprietary preset archive/dir (e.g. 'effects pr.zip' or preset directory).",
    )
    aud.add_argument(
        "--audit-json",
        default=None,
        help="Optional path to Bang_on_pro_premiere_preset_audit.json for reconciliation.",
    )
    aud.add_argument(
        "--output",
        required=True,
        help="Destination path for the schema-validated JSON audit report.",
    )
    aud.add_argument(
        "--asset-dir",
        action="append",
        default=[],
        help="Optional directory to search for external LUTs/fonts/IRs (repeatable).",
    )
    aud.add_argument(
        "--fps",
        type=float,
        default=25.0,
        help="Target timeline frame rate (default: 25.0).",
    )
    aud.add_argument(
        "--platform",
        default="linux",
        choices=("linux", "macos", "windows"),
        help="Target host platform for plugin availability checks (default: linux).",
    )
    aud.add_argument(
        "--generate-previews",
        action="store_true",
        help="Render cached before/after/split PNG thumbnails for executable presets.",
    )
    aud.add_argument(
        "--preview-dir",
        default=None,
        help="Optional directory for generated preview PNG thumbnails.",
    )
    aud.add_argument(
        "--strict",
        action="store_true",
        help="Exit with code 3 when required source inputs are missing.",
    )

    # 3. builtin-library
    bl = sub.add_parser(
        "builtin-library",
        help="Export the built-in 64-preset legal native GPL-3.0 starter effects library report.",
    )
    bl.add_argument(
        "--output",
        required=True,
        help="Destination path for the native starter library JSON report.",
    )
    bl.add_argument(
        "--generate-previews",
        action="store_true",
        help="Render cached before/after/split PNG thumbnails for starter presets.",
    )
    bl.add_argument(
        "--preview-dir",
        default=None,
        help="Optional cache directory for rendered preview PNGs.",
    )
    bl.add_argument(
        "--preview-limit",
        type=int,
        default=None,
        help="Optional maximum number of presets to render previews for.",
    )

    return parser


def _run_audit(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    audit_path = Path(args.audit_json) if args.audit_json else None
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    missing_inputs: list[MissingSourceInput] = []
    if not input_path.exists():
        missing_inputs.append(
            MissingSourceInput(
                input_name=input_path.name or "effects pr.zip",
                expected_path=str(input_path),
                reason=(
                    "Proprietary Premiere Pro preset pack is absent from the local environment "
                    "(never redistributed or committed to Git; zero entries fabricated)."
                ),
                remediation=(
                    f"Supply the user-owned preset archive at '{input_path}' and re-run "
                    "'python3 -m bangonpro.premiere_compat.cli audit', or load the built-in "
                    "legal Bang On Pro Native Starter Library via 'builtin-library'."
                ),
            )
        )

    if audit_path is not None and not audit_path.exists():
        missing_inputs.append(
            MissingSourceInput(
                input_name=audit_path.name or "Bang_on_pro_premiere_preset_audit.json",
                expected_path=str(audit_path),
                reason=(
                    "Optional preset audit reference JSON was requested but does not exist at the supplied path."
                ),
                remediation=(
                    f"Place '{audit_path.name}' at '{audit_path}' or omit '--audit-json' to run "
                    "audit directly against the preset archive."
                ),
            )
        )

    # If the primary input archive/directory is missing, emit clean blocker report (zero fabricated presets!)
    if not input_path.exists():
        blocker_report = build_missing_inputs_report(
            missing_inputs=missing_inputs,
            source_name=input_path.name or "missing_source_inputs",
        )
        report_dict = blocker_report.to_dict()
        validate_report_schema(report_dict)
        out_path.write_text(
            json.dumps(report_dict, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        out_payload: dict[str, Any] = {
            "status": "missing_source_inputs",
            "report_path": str(out_path),
            "summary": blocker_report.summary(),
            "missing_source_inputs": [m.to_dict() for m in missing_inputs],
        }
        print(json.dumps(out_payload, indent=2))
        return 3 if args.strict else 0

    valid_audit_arg = str(audit_path) if (audit_path is not None and audit_path.exists()) else None
    bundle = parse_premiere_input(
        input_path=input_path,
        audit_json_path=valid_audit_arg,
        fps=args.fps,
    )
    asset_dirs = [Path(d) for d in args.asset_dir]
    report = build_compatibility_report(
        bundle=bundle,
        fps=args.fps,
        asset_search_dirs=asset_dirs,
        platform_name=args.platform,
        missing_source_inputs=missing_inputs,
    )

    previews_generated = 0
    if args.generate_previews:
        pdir = Path(args.preview_dir) if args.preview_dir else (out_path.parent / "previews")
        previews = attach_previews_to_report(report, cache_dir=pdir, fps=max(1, round(args.fps)))
        previews_generated = len(previews)

    report_dict = report.to_dict()
    validate_report_schema(report_dict)
    out_path.write_text(
        json.dumps(report_dict, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    out_payload = {
        "status": "ok" if not missing_inputs else "partial_missing_inputs",
        "report_path": str(out_path),
        "summary": report.summary(),
        "previews_generated": previews_generated,
        "missing_source_inputs": [m.to_dict() for m in missing_inputs],
    }
    print(json.dumps(out_payload, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "import":
            bundle = parse_premiere_input(
                input_path=args.input,
                audit_json_path=args.audit_json,
                fps=args.fps,
            )
            asset_dirs = [Path(d) for d in args.asset_dir]
            report = build_compatibility_report(
                bundle=bundle,
                fps=args.fps,
                asset_search_dirs=asset_dirs,
                platform_name=args.platform,
            )

            if args.generate_previews:
                gen_preview_dir = (
                    Path(args.preview_dir)
                    if args.preview_dir
                    else (Path(args.output_dir) / "previews")
                )
                attach_previews_to_report(report, cache_dir=gen_preview_dir, fps=max(1, round(args.fps)))

            exported = export_report_bundle(report, output_dir=args.output_dir)

            summary = report.summary()
            out_payload: dict[str, Any] = {
                "status": "ok",
                "summary": summary,
                "json_report": str(exported["json_report"]),
                "markdown_report": str(exported["markdown_report"]),
                "kdenlive_effects_dir": str(exported["kdenlive_effects_dir"]),
            }

            if args.verify_render:
                verify_preview_dir: Path | None = (
                    Path(args.preview_dir)
                    if args.preview_dir
                    else (Path(args.output_dir) / "previews" if args.generate_previews else None)
                )
                ver_results = verify_presets_batch(
                    report.presets,
                    fps=max(1, round(args.fps)),
                    check_determinism=True,
                    preview_dir=verify_preview_dir,
                )
                ver_path = Path(args.output_dir) / "verification_results.json"
                ver_data = [r.to_dict() for r in ver_results]
                ver_path.write_text(
                    json.dumps(ver_data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                passed = sum(1 for r in ver_results if r.rendered_ok and r.deterministic)
                out_payload["verification"] = {
                    "verified_count": len(ver_results),
                    "passed_deterministic": passed,
                    "results_path": str(ver_path),
                }

            print(json.dumps(out_payload, indent=2))
            return 0

        if args.command == "audit":
            return _run_audit(args)

        if args.command == "builtin-library":
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            report = build_builtin_starter_report()
            previews_generated = 0
            if args.generate_previews:
                pdir = Path(args.preview_dir) if args.preview_dir else (out_path.parent / "previews")
                previews = attach_previews_to_report(
                    report,
                    cache_dir=pdir,
                    max_presets=args.preview_limit,
                )
                previews_generated = len(previews)

            report_dict = report.to_dict()
            validate_report_schema(report_dict)
            out_path.write_text(
                json.dumps(report_dict, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "report_path": str(out_path),
                        "summary": report.summary(),
                        "previews_generated": previews_generated,
                    },
                    indent=2,
                )
            )
            return 0

    except SecurityViolationError as exc:
        print(f"SECURITY REJECTION: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
