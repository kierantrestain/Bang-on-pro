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
        help="List all built-in and custom validated Show Profiles and exit",
    )
    parser.add_argument(
        "--save-profile",
        default=None,
        help="Validate and save a custom Show Profile JSON file to the output path (--output) and exit",
    )
    parser.add_argument(
        "--list-providers",
        action="store_true",
        help="List local and optional external AI providers (and consent requirements) and exit",
    )
    parser.add_argument(
        "--provider-config",
        default=None,
        help="Optional JSON file configuring AI provider ID and explicit external transmission consent",
    )
    parser.add_argument(
        "--apply-transcript-edits",
        default=None,
        help="Path to an edited transcript.json to diff against --original-transcript into a Timeline Command batch",
    )
    parser.add_argument(
        "--original-transcript",
        default=None,
        help="Path to the original transcript.json used with --apply-transcript-edits",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Explicit output JSON file path for --apply-transcript-edits or --save-profile",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print final structured production summary as JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    from pathlib import Path

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

    if args.list_providers:
        from bangonpro.ai_engine.providers import AiProviderRegistry

        registry = AiProviderRegistry()
        providers = [
            {
                "provider_id": p.provider_id,
                "display_name": p.display_name,
                "is_local": p.is_local,
                "requires_consent": p.requires_consent,
                "capabilities": list(p.capabilities),
            }
            for p in registry.list_providers()
        ]
        if args.json:
            print(json.dumps(providers, indent=2))
        else:
            for p in providers:
                tier = "LOCAL" if p["is_local"] else "EXTERNAL (consent required)"
                print(f"{p['provider_id']:24s} | {tier:28s} | {p['display_name']}")
        return 0

    if args.save_profile:
        from bangonpro.show_profiles.loader import save_custom_profile

        src_path = Path(args.save_profile)
        raw_profile = json.loads(src_path.read_text(encoding="utf-8"))
        dest_path = Path(args.output) if args.output else src_path
        saved = save_custom_profile(raw_profile, dest_path)
        payload = {"saved": True, "path": str(saved), "profile_id": raw_profile["profile_id"]}
        print(json.dumps(payload, indent=2) if args.json else f"Saved profile to {saved}")
        return 0

    if args.apply_transcript_edits:
        from bangonpro.ai_engine.transcript import (
            build_transcript_edit_command_batch,
            load_transcript,
        )

        edited = load_transcript(args.apply_transcript_edits)
        original = (
            load_transcript(args.original_transcript)
            if args.original_transcript
            else edited
        )
        batch = build_transcript_edit_command_batch(
            original,
            edited,
            sequence_id=args.sequence_id,
        )
        if args.output:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(batch, indent=2), encoding="utf-8")
        print(json.dumps(batch, indent=2))
        return 0

    if not args.inputs:
        parser.error("--inputs is required unless a utility subcommand flag is specified")

    provider_config = None
    if args.provider_config:
        provider_config = json.loads(Path(args.provider_config).read_text(encoding="utf-8"))

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
        provider_config=provider_config,
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
        "missing_media_count": len(state.missing_media),
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
