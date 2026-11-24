"""Command-Line Interface for Bang On Pro AI Production Engine.

Supports:
- Full multi-agent production pipeline from raw media ``--inputs``
- Active project context editing via ``--kdenlive-project`` / ``--project-state``
  (without forcing media re-import) and ``--plan-only`` structured planning
- Managed local AI model lifecycle (``--model-status``, ``--install-model``,
  ``--remove-model``) with honest installed / neural / fallback reporting
- Show Profile management, AI provider listing, and text-based transcript edits

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from bangonpro.ai_engine.model_manager import ModelManager, ModelManagerError, ModelNotFoundError
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
        "--kdenlive-project",
        default=None,
        help="Optional active .kdenlive XML project file for context-aware editing without media re-import",
    )
    parser.add_argument(
        "--project-state",
        default=None,
        help="Optional read-only Timeline Command Engine JSON state snapshot for context-aware editing",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Run the 4-stage AI planner and emit validated timeline_command_v1 proposals without rendering",
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
        "--project-id",
        default=None,
        help="Optional active project identifier to preserve across planning and approval",
    )
    parser.add_argument(
        "--sequence-id",
        default=None,
        help="Optional target sequence identifier (defaults to active project sequence or seq_main)",
    )
    parser.add_argument(
        "--rnnoise-model",
        default=None,
        help="Optional path to an RNNoise .rnnn model file for avfilter.arnndn",
    )
    parser.add_argument(
        "--model-dir",
        default=None,
        help="Managed local AI model storage directory (overrides BANGONPRO_MODEL_DIR)",
    )
    parser.add_argument(
        "--asr-model",
        default=None,
        help="Optional Whisper-family catalog model ID to prefer for local neural transcription",
    )
    parser.add_argument(
        "--model-status",
        action="store_true",
        help="Report hardware capabilities, installed AI models, backend packages, and disk usage",
    )
    parser.add_argument(
        "--install-model",
        default=None,
        help="Download/stage and SHA-256 verify a managed local AI model by catalog model_id",
    )
    parser.add_argument(
        "--model-source-path",
        default=None,
        help="Optional local weights file to stage into BANGONPRO_MODEL_DIR when using --install-model",
    )
    parser.add_argument(
        "--model-sha256",
        default=None,
        help="Optional expected SHA-256 hex digest to verify when using --install-model",
    )
    parser.add_argument(
        "--remove-model",
        default=None,
        help="Safely remove a managed local AI model by catalog model_id",
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
        help="Print final structured output as JSON",
    )
    return parser


def _handle_model_commands(args: argparse.Namespace) -> int | None:
    if not (args.model_status or args.install_model or args.remove_model):
        return None

    manager = ModelManager(model_dir=args.model_dir)

    if args.model_status:
        report = manager.get_storage_report()
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            hw = report["hardware"]
            print(
                f"Model Dir: {report['model_dir']} | "
                f"Hardware: {hw['os_name']}/{hw['arch']} ({hw['total_ram_gb']} GB, tier={hw['ram_tier']})"
            )
            for m in report["models"]:
                rec = " [RECOMMENDED]" if m["recommended"] else ""
                print(
                    f"  {m['model_id']:26s} | {m['status']:16s} | "
                    f"pkg={m['backend_package_installed']} disk={m['model_available_on_disk']} | "
                    f"{m['display_name']}{rec}"
                )
        return 0

    if args.install_model:
        def _on_dl_progress(frac: float, done: int, total: int, msg: str) -> None:
            progress_evt = {
                "event": "progress",
                "model_id": args.install_model,
                "fraction": frac,
                "bytes_done": done,
                "bytes_total": total,
                "message": msg,
            }
            print(json.dumps(progress_evt), file=sys.stderr, flush=True)

        try:
            result = manager.install_model(
                args.install_model,
                source_path=args.model_source_path,
                expected_sha256=args.model_sha256,
                progress_callback=_on_dl_progress,
            )
        except (ModelNotFoundError, ModelManagerError, FileNotFoundError, OSError) as exc:
            err_payload = {"installed": False, "model_id": args.install_model, "error": str(exc)}
            if args.json:
                print(json.dumps(err_payload, indent=2))
            else:
                print(f"Model install failed: {exc}", file=sys.stderr)
            return 2

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Installed {result['model_id']} -> {result['install_path']} (sha256={result['sha256'][:12]}...)")
        return 0

    if args.remove_model:
        try:
            result = manager.remove_model(args.remove_model)
        except (ModelNotFoundError, ModelManagerError, OSError) as exc:
            err_payload = {"removed": False, "model_id": args.remove_model, "error": str(exc)}
            if args.json:
                print(json.dumps(err_payload, indent=2))
            else:
                print(f"Model remove failed: {exc}", file=sys.stderr)
            return 2

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(result["message"])
        return 0

    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    model_rc = _handle_model_commands(args)
    if model_rc is not None:
        return model_rc

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
        providers: list[dict[str, Any]] = [
            {
                "provider_id": p.provider_id,
                "display_name": p.display_name,
                "kind": p.kind,
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
            sequence_id=args.sequence_id or "seq_main",
        )
        if args.output:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(batch, indent=2), encoding="utf-8")
        print(json.dumps(batch, indent=2))
        return 0

    if not args.inputs and not args.kdenlive_project and not args.project_state:
        parser.error(
            "--inputs, --kdenlive-project, or --project-state is required unless a utility subcommand flag is specified"
        )

    provider_config = None
    if args.provider_config:
        provider_config = json.loads(Path(args.provider_config).read_text(encoding="utf-8"))

    def _on_progress(stage: str, pct: float, msg: str) -> None:
        if not args.json:
            print(f"[{int(pct * 100):3d}%] ({stage}) {msg}", file=sys.stderr)

    export_override = True if args.export else None
    state, _ = run_production_pipeline(
        inputs=args.inputs or None,
        output_dir=args.output_dir,
        profile_ref=args.profile,
        instruction=args.instruction,
        export_video=export_override,
        dry_run=args.dry_run,
        project_id=args.project_id,
        sequence_id=args.sequence_id,
        rnnoise_model_path=args.rnnoise_model,
        provider_config=provider_config,
        progress_callback=_on_progress,
        kdenlive_project=args.kdenlive_project,
        project_state=args.project_state,
        plan_only=args.plan_only,
        model_dir=args.model_dir,
        asr_model_id=args.asr_model,
    )

    summary = {
        "project_id": state.project_id,
        "sequence_id": state.sequence_id,
        "profile_id": state.profile["profile_id"],
        "dry_run": state.dry_run,
        "revision": state.revision,
        "execution_status": state.metadata.get("execution_status", "executed"),
        "needs_clarification": state.intent.needs_clarification,
        "clarification_question": state.intent.clarification_question,
        "intent_scope": state.intent.scope,
        "parsed_actions": state.intent.parsed_actions,
        "command_batch_count": len(state.command_batches),
        "offsets_sec": state.sync_result.offsets_sec if state.sync_result else {},
        "setup_delay_sec": state.setup_delay_sec,
        "shot_count": len(state.rough_cut_shots),
        "caption_count": len(state.captions),
        "lower_third_count": len(state.lower_thirds),
        "asr_backend_used": state.metadata.get("asr_backend_used"),
        "is_neural_inference": state.metadata.get("is_neural_inference", False),
        "qc_passed": state.qc_report.passed,
        "qc_issue_count": len(state.qc_report.issues),
        "missing_media_count": len(state.missing_media),
        "artifacts": state.artifacts,
        "stage_timings_sec": state.stage_timings_sec,
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        if summary["needs_clarification"]:
            print(f"Clarification required: {summary['clarification_question']}")
        else:
            print(f"Bang On Pro Production Complete — Profile: {summary['profile_id']}")
            print(f"  Status:  {summary['execution_status']} (scope={summary['intent_scope']})")
            print(f"  Actions: {', '.join(summary['parsed_actions']) or 'none'}")
            print(f"  Shots:   {summary['shot_count']} | Captions: {summary['caption_count']} | Lower thirds: {summary['lower_third_count']}")
            print(f"  QC Pass: {summary['qc_passed']} (issues={summary['qc_issue_count']})")
            for name, path in summary["artifacts"].items():
                print(f"  Artifact [{name}]: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
