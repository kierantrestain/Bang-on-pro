"""Command-line interface for Bang On Pro Premiere Preset Compatibility Engine.

Usage:
    python3 -m bangonpro.premiere_compat.cli import \\
        --input <zip_or_dir_or_prfpset> \\
        --output-dir <dir> \\
        [--audit-json <path>] \\
        [--verify-render]

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .mapper import build_compatibility_report, export_report_bundle
from .parser import parse_premiere_input
from .security import SecurityViolationError
from .verifier import verify_presets_batch


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bangonpro-premiere-compat",
        description="Bang On Pro Adobe Premiere Pro (.prfpset) Compatibility Engine",
    )
    sub = parser.add_subparsers(dest="command", required=True)

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
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "import":
        try:
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
            exported = export_report_bundle(report, output_dir=args.output_dir)

            summary = report.summary()
            out_payload = {
                "status": "ok",
                "summary": summary,
                "json_report": str(exported["json_report"]),
                "markdown_report": str(exported["markdown_report"]),
                "kdenlive_effects_dir": str(exported["kdenlive_effects_dir"]),
            }

            if args.verify_render:
                ver_results = verify_presets_batch(
                    report.presets,
                    fps=max(1, int(round(args.fps))),
                    check_determinism=True,
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
        except SecurityViolationError as exc:
            print(f"SECURITY REJECTION: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
