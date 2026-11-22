"""Command-Line Interface for Bang On Pro AI Production Engine.

Example:
    python3 -m bangonpro.ai_engine.cli \\
        "Sync all four cameras, remove setup delays, build a first cut and add our standard graphics" \\
        --profile podcast_multicam \\
        --inputs cam_wide=wide.mp4 cam_host=host.mp4 cam_guest=guest.mp4 cam_reaction=react.mp4 \\
        --output-dir ./out \\
        --export

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from bangonpro.ai_engine.pipeline import run_production_pipeline
from bangonpro.show_profiles.loader import list_show_profiles, load_show_profile


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bangonpro-ai",
        description="Bang On Pro Autonomous Multi-Agent AI Video Production CLI",
    )
    parser.add_argument(
        "instruction",
        nargs="?",
        default="",
        help=(
            "Natural-language editing instruction, e.g. "
            "'Sync all four cameras, remove setup delays, build a first cut and add our standard graphics'"
        ),
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Show Profile ID or JSON path (default: inferred from instruction or podcast_multicam)",
    )
    parser.add_argument(
        "--inputs",
        nargs="+",
        default=[],
        help="Input camera media files (either plain paths or camera_id=path pairs)",
    )
    parser.add_argument(
        "--output-dir",
        default="./bangonpro_output",
        help="Output directory for .kdenlive, .otio, captions, reports, and exported video",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Render final edited MP4 via melt and verify with ffprobe",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run analysis and emit dry-run Timeline Command API v1 batches without rendering video",
    )
    parser.add_argument(
        "--sequence-id",
        default="seq_main",
        help="Target sequence identifier (default: seq_main)",
    )
    parser.add_argument(
        "--rnnoise-model",
        default=None,
        help="Optional path to an RNNoise .rnnn model file for avfilter.arnndn",
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="List all built-in validated Show Profiles and exit",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print final structured production summary as JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.list_profiles:
        profiles = []
        for pid in list_show_profiles():
            prof = load_show_profile(pid)
            profiles.append(
                {
                    "profile_id": prof["profile_id"],
                    "display_name": prof["display_name"],
                    "format_type": prof["format_type"],
                    "cameras": [c["camera_id"] for c in prof["cameras"]],
                }
            )
        if args.json:
            print(json.dumps(profiles, indent=2))
        else:
            for p in profiles:
                print(
                    f"{p['profile_id']:20s} | {p['display_name']} | "
                    f"cameras={', '.join(p['cameras'])}"
                )
        return 0

    if not args.inputs:
        parser.error("--inputs is required unless --list-profiles is specified")

    def _on_progress(stage: str, pct: float, msg: str) -> None:
        if not args.json:
            print(f"[{int(pct * 100):3d}%] ({stage}) {msg}", file=sys.stderr)

    export_override = True if args.export else None
    state, _ = run_production_pipeline(
        inputs=args.inputs,
        output_dir=args.output_dir,
        profile_ref=args.profile,
        instruction=args.instruction,
        export_video=export_override,
        dry_run=args.dry_run,
        sequence_id=args.sequence_id,
        rnnoise_model_path=args.rnnoise_model,
        progress_callback=_on_progress,
    )

    summary = {
        "project_id": state.project_id,
        "sequence_id": state.sequence_id,
        "profile_id": state.profile["profile_id"],
        "dry_run": state.dry_run,
        "revision": state.revision,
        "parsed_actions": state.intent.parsed_actions,
        "offsets_sec": state.sync_result.offsets_sec if state.sync_result else {},
        "setup_delay_sec": state.setup_delay_sec,
        "shot_count": len(state.rough_cut_shots),
        "caption_count": len(state.captions),
        "lower_third_count": len(state.lower_thirds),
        "qc_passed": state.qc_report.passed,
        "qc_issue_count": len(state.qc_report.issues),
        "artifacts": state.artifacts,
        "stage_timings_sec": state.stage_timings_sec,
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"Bang On Pro Production Complete — Profile: {summary['profile_id']}")
        print(f"  Shots: {summary['shot_count']} | Captions: {summary['caption_count']} | Lower Thirds: {summary['lower_third_count']}")
        print(f"  QC Passed: {summary['qc_passed']} | Total Time: {summary['stage_timings_sec'].get('total_pipeline', 0.0):.2f}s")
        for key, val in sorted(summary["artifacts"].items()):
            print(f"  {key}: {val}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
