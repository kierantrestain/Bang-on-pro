"""Specialist production-agent handlers for the Bang On Pro AI Production Department.

Each handler receives an ``AgentContext`` (shared project state, cancellation
token, progress reporter, shared analysis cache and job options) and returns an
``AgentOutcome``. Handlers reuse the proven V1.1 engine stages wherever possible
and add the missing specialist capabilities (colour matching, VFX masking,
highlights, extended QC, deliverables). All timeline changes are emitted as
validated Timeline Command API v1 batches so they remain editable in the native
Kdenlive timeline.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import copy
import dataclasses
import re
import shutil
import subprocess
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from bangonpro.ai_engine.agents import audio_graphics_qc as agq
from bangonpro.ai_engine.agents.ingest_sync import (
    _SHA256_MEMO,
    compute_file_sha256,
    media_identity_key,
    probe_media_file,
    run_ingest_sync_agent,
)
from bangonpro.ai_engine.agents.rough_cut_multicam import run_rough_cut_multicam_agent
from bangonpro.ai_engine.agents.transcribe_diarise import run_transcribe_diarise_agent
from bangonpro.ai_engine.department.cache import AnalysisCache
from bangonpro.ai_engine.department.checkpoint import write_json_atomic
from bangonpro.ai_engine.department.learning import ProfilePreferenceStore
from bangonpro.ai_engine.department.registry import (
    AUTONOMOUS_MODES,
    MODE_ASSISTED,
    MODE_AUTONOMOUS_FIRST_CUT,
    MODE_FULL_PRODUCTION_DRAFT,
    MODE_MANUAL,
)
from bangonpro.ai_engine.orchestrator import CancellationToken
from bangonpro.ai_engine.project_builder import run_project_emit_export_stage
from bangonpro.ai_engine.state import (
    AsrBackendReport,
    ProductionIntent,
    ProjectAnalysisState,
    QCIssue,
    StorySection,
    TimeInterval,
    TranscriptSegment,
)
from bangonpro.show_profiles.loader import get_effective_profile_settings

ProgressFn = Callable[[float, str], None]


class InsufficientStorageError(RuntimeError):
    """Raised by the Media Ingest Agent before any processing when disk space is too low."""


@dataclass
class JobOptions:
    mode: str = MODE_ASSISTED
    export_video: bool = False
    budget_usd: float = 0.0
    series_id: str = "default"
    episode_title: str = ""
    model_dir: str | None = None
    prefs_root: str | None = None
    min_free_bytes: int = 512 * 1024 * 1024
    enabled_overrides: dict[str, bool] = field(default_factory=dict)
    provider_config: dict[str, Any] | None = None
    asr_model_id: str | None = None


@dataclass
class AgentContext:
    agent_id: str
    state: ProjectAnalysisState
    token: CancellationToken
    progress: ProgressFn
    cache: AnalysisCache
    options: JobOptions
    plan: dict[str, Any]


@dataclass
class AgentOutcome:
    batches: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 1.0
    summary: str = ""
    rationale: str = ""
    outputs: dict[str, str] = field(default_factory=dict)
    neural_inference: bool | None = None
    fallback_reason: str | None = None
    review_items: list[str] = field(default_factory=list)
    est_external_cost_usd: float = 0.0
    cache_hits: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    skipped_reason: str | None = None


def _norm_batches(emitted: Any) -> list[dict[str, Any]]:
    if isinstance(emitted, dict):
        return [emitted]
    if isinstance(emitted, list):
        return [b for b in emitted if b]
    return []


def _batch_confidence(batches: list[dict[str, Any]], default: float = 1.0) -> float:
    vals = [float(op.get("confidence", default)) for b in batches for op in b.get("operations", [])]
    return round(sum(vals) / len(vals), 4) if vals else default


def _video_assets(state: ProjectAnalysisState) -> dict[str, Any]:
    return {
        cid: a for cid, a in state.media_assets.items() if a.has_video and a.camera_role != "audio_recorder"
    }


# ---------------------------------------------------------------------------
# Brief parsing & activation (Executive Editing Agent)
# ---------------------------------------------------------------------------
_BRIEF_PATTERNS: dict[str, str] = {
    "colour": r"\b(colou?rs?|grade|grading|match (the )?cameras|white balance|exposure|luts?)\b",
    "vfx": r"\b(mask|rotoscop\w*|blur (the )?background|background (blur|replace\w*|removal)|remove (the )?background|green ?screen|track(ing)? (the )?(face|subject|person))\b",
    "social": r"\b(social|vertical|9:16|shorts|reels?|tiktok|instagram)\b",
    "highlights": r"\b(highlights?|best (bits|moments)|clips?|teaser|promo)\b",
    "graphics": r"\b(graphics?|lower thirds?|brand\w*|title card|intro|outro|logo)\b",
    "captions": r"\b(captions?|subtitles?|srt)\b",
    "audio": r"\b(audio|sound|loudness|lufs|denoise|noise|levels?|mix)\b",
    "export": r"\b(export|render|master|deliver\w*)\b",
}


_ASR_PREFERENCE = ("faster-whisper-small", "faster-whisper-medium", "faster-whisper-tiny", "faster-whisper-large-v3", "openai-whisper-small", "openai-whisper-tiny")


def select_local_asr_model(model_dir: str | None, requested: str | None = None) -> str | None:
    """Pick an installed, runnable local Whisper model (neural when sufficient; never downloads)."""
    from bangonpro.ai_engine.model_manager import ModelManager

    mgr = ModelManager(model_dir=model_dir)
    ready = {m.model_id for m in mgr.list_models(family="asr_whisper") if m.model_available_on_disk and m.backend_package_installed}
    if requested:
        return requested if requested in ready else None
    for mid in _ASR_PREFERENCE:
        if mid in ready:
            return mid
    return next(iter(sorted(ready)), None)


def parse_brief_flags(brief: str) -> dict[str, bool]:
    low = (brief or "").lower()
    return {k: bool(re.search(p, low)) for k, p in _BRIEF_PATTERNS.items()}


def shape_intent_for_mode(intent: ProductionIntent, mode: str, flags: dict[str, bool], n_video: int, export: bool) -> ProductionIntent:
    """Widen the parsed intent according to the automation mode (never narrower than the brief)."""
    shaped = dataclasses.replace(intent)
    if mode == MODE_AUTONOMOUS_FIRST_CUT:
        for f in ("sync_cameras", "transcribe", "remove_setup_delay", "remove_silence", "build_rough_cut", "restore_audio", "run_qc"):
            setattr(shaped, f, True)
        shaped.multicam_switch = shaped.multicam_switch or n_video > 1
        shaped.add_captions = shaped.add_captions or flags.get("captions", False)
        shaped.add_lower_thirds = shaped.add_lower_thirds or flags.get("graphics", False)
    elif mode == MODE_FULL_PRODUCTION_DRAFT:
        full = ProductionIntent.full_production(intent.raw_instruction, profile_hint=intent.profile_hint)
        for f in (
            "sync_cameras", "transcribe", "remove_setup_delay", "remove_silence", "build_rough_cut",
            "restore_audio", "add_captions", "add_lower_thirds", "run_qc",
        ):
            setattr(shaped, f, True)
        shaped.multicam_switch = n_video > 1 or full.multicam_switch
    if mode in AUTONOMOUS_MODES:
        shaped.needs_clarification = False
        shaped.clarification_question = ""
        shaped.scope = mode
        # V1.2.2: mode widening never overrides an explicit instruction ("keep the pauses",
        # "no captions"); the Executive plan records the resolution.
        from bangonpro.ai_engine.intent import explicit_negations

        neg = explicit_negations(intent.raw_instruction)
        for key, attr in (("silence", "remove_silence"), ("setup", "remove_setup_delay"), ("captions", "add_captions"), ("graphics", "add_lower_thirds"), ("audio", "restore_audio"), ("multicam", "multicam_switch")):
            if neg.get(key):
                setattr(shaped, attr, False)
    shaped.export_video = bool(export or shaped.export_video)
    return shaped


def compute_activation(
    intent: ProductionIntent,
    mode: str,
    flags: dict[str, bool],
    profile: dict[str, Any],
    *,
    n_inputs: int,
    n_video: int,
    overrides: dict[str, bool] | None = None,
) -> dict[str, dict[str, Any]]:
    """Decide which specialist agents a job needs, with a human-readable reason for each."""
    agent_cfg = profile.get("agent_config", {}) or {}
    disabled_by_profile = set(agent_cfg.get("disabled_agents", []) or [])
    colour_rules = profile.get("colour_rules", {}) or {}
    vfx_rules = profile.get("vfx_rules", {}) or {}
    dist_rules = profile.get("distribution_rules", {}) or {}
    full = mode == MODE_FULL_PRODUCTION_DRAFT
    has_media = n_inputs > 0
    act: dict[str, tuple[bool, str]] = {
        "executive": (True, "Always active: plans, delegates and verifies the job"),
        "learning": (True, "Applies approved series preferences (cheap, deterministic)"),
        "media_ingest": (has_media, "Source media supplied" if has_media else "No source media supplied"),
        "synchronisation": (has_media, f"Import and align {n_inputs} source file(s)" if has_media else "No source media"),
        "editorial_intelligence": (
            has_media and any((intent.transcribe, intent.remove_silence, intent.build_rough_cut, intent.add_captions, intent.multicam_switch)),
            "Transcript/speech analysis needed by requested edits",
        ),
        "multicam_director": (has_media and intent.multicam_switch and n_video > 1, f"{n_video} video angles require coverage decisions"),
        "rough_cut": (
            has_media and any((intent.build_rough_cut, intent.remove_silence, intent.remove_setup_delay, intent.multicam_switch)),
            "Assembly / pacing requested",
        ),
        "audio_engineer": (has_media and intent.restore_audio, "Dialogue cleanup and loudness normalisation requested"),
        "colourist": (
            has_media and n_video > 0 and (flags.get("colour", False) or (full and colour_rules.get("enabled", True) is not False)),
            "Colour treatment requested" if flags.get("colour") else "Full production draft includes camera matching",
        ),
        "captions": (has_media and intent.add_captions, "Captions requested"),
        "graphics": (has_media and (intent.add_lower_thirds or flags.get("graphics", False)), "Graphics/branding requested"),
        "vfx": (
            has_media and n_video > 0 and (flags.get("vfx", False) or (full and bool(vfx_rules.get("tasks")))),
            "Masking/VFX requested" if flags.get("vfx") else "Profile defines VFX tasks",
        ),
        "quality_control": (has_media and (intent.run_qc or mode in AUTONOMOUS_MODES), "Verify the draft before review"),
        "distribution": (
            has_media,
            "Emit editable project"
            + ("; render master" if intent.export_video else "")
            + ("; social/highlight versions" if (flags.get("social") or flags.get("highlights") or (full and dist_rules.get("social_versions"))) else ""),
        ),
    }
    result: dict[str, dict[str, Any]] = {}
    for agent_id, (active, reason) in act.items():
        if mode == MODE_MANUAL and agent_id not in ("executive",):
            active, reason = False, "Manual editing mode: agents run only on explicit request"
        if agent_id in disabled_by_profile and agent_id != "executive":
            active, reason = False, "Disabled by production profile agent_config"
        if overrides and agent_id in overrides and agent_id != "executive":
            active = bool(overrides[agent_id])
            reason = ("Enabled" if active else "Disabled") + " by user for this job"
        result[agent_id] = {"active": bool(active), "reason": reason}
    # V1.2.2: explicit dependency reasoning in the plan
    from bangonpro.ai_engine.department.editorial_brief import DEPENDENCY_REASONS
    from bangonpro.ai_engine.department.registry import AGENTS_BY_ID

    for agent_id, row in result.items():
        spec = AGENTS_BY_ID.get(agent_id)
        row["depends_on"] = list(spec.depends_on) if spec else []
        row["dependency_reason"] = DEPENDENCY_REASONS.get(agent_id, "")
    return result


def run_executive_plan(ctx: AgentContext) -> AgentOutcome:
    from bangonpro.ai_engine.department.editorial_brief import build_executive_plan

    st = ctx.state
    flags = ctx.plan.get("brief_flags", {})
    active = [a for a, v in ctx.plan["activation"].items() if v["active"]]
    eplan = build_executive_plan(
        brief=st.instruction or ctx.plan.get("brief", ""),
        mode=ctx.options.mode,
        activation=ctx.plan["activation"],
        profile=st.profile,
        inputs=dict(st.input_camera_files),
        neural_asr_model=st.metadata.get("asr_model_id"),
        export=bool(st.intent.export_video),
        intent_confidence=float(st.intent.intent_confidence if st.intent.intent_confidence is not None else 1.0),
    )
    st.metadata["executive_plan"] = eplan
    path = write_json_atomic(Path(st.output_dir) / "executive_plan.json", eplan)
    st.artifacts["executive_plan_json"] = str(path)
    conflicts = eplan["interpretation"]["conflicts"]
    ctx.progress(1.0, f"Plan: {len(active)} agents active in {ctx.options.mode} mode; {len(conflicts)} brief conflict(s) resolved")
    review = [f"Needs human review: {e}" for e in eplan["escalations"]]
    return AgentOutcome(
        confidence=float(eplan["overall_confidence"]),
        summary=f"Planned {len(active)} active agents ({', '.join(active)})" + (f"; resolved {len(conflicts)} brief conflict(s)" if conflicts else ""),
        rationale=f"Brief flags: {', '.join(k for k, v in flags.items() if v) or 'none'}; mode={ctx.options.mode}; pacing={eplan['interpretation']['directives']['pacing']}"
        + "".join(f"; {c['code']}: {c['resolution']}" for c in conflicts),
        outputs={"production_plan": ctx.plan.get("plan_path", ""), "executive_plan_json": str(path)},
        review_items=review,
        est_external_cost_usd=float(ctx.plan.get("executive_llm_cost_usd", 0.0)),
        metrics={"brief_conflicts": [c["code"] for c in conflicts], "overall_plan_confidence": eplan["overall_confidence"], "needs_human_review": eplan["needs_human_review"]},
    )


# ---------------------------------------------------------------------------
# Learning Agent (pre-production application of approved preferences)
# ---------------------------------------------------------------------------
def run_learning_apply(ctx: AgentContext) -> AgentOutcome:
    st = ctx.state
    store = ProfilePreferenceStore(str(st.profile.get("profile_id", "custom")), ctx.options.series_id, ctx.options.prefs_root)
    merged, overlay = store.apply_to_profile(st.profile)
    st.profile = merged
    version = store.active_version()
    st.metadata["preference_version"] = version
    st.metadata["preference_overlay_keys"] = sorted(overlay.keys())
    pending = [s for s in store.suggestions() if s.get("status") == "pending"]
    ctx.progress(1.0, f"Applied preference v{version} ({len(overlay)} keys); {len(pending)} suggestions pending review")
    return AgentOutcome(
        summary=f"Series '{ctx.options.series_id}' preferences v{version} applied" if overlay else "No approved preferences yet (base profile)",
        rationale="Only human-approved, versioned preferences are applied",
        metrics={"preference_version": version, "pending_suggestions": len(pending)},
        review_items=[f"{len(pending)} profile improvement suggestion(s) awaiting approval"] if pending else [],
    )


# ---------------------------------------------------------------------------
# Media Ingest Agent
# ---------------------------------------------------------------------------
_ROLE_HINTS = (
    ("wide", r"wide|master|ws\b|establish"),
    ("host", r"host|presenter|anchor"),
    ("guest", r"guest|interviewee"),
    ("reaction", r"react|audience|crowd"),
    ("audio_recorder", r"\b(mic|lav|zoom|recorder|audio)\b"),
)
_KIND_ROLES = {"screen_capture": "screen_capture", "graphics_or_still": "graphics"}


def _guess_role(path: str, has_video: bool, source_kind: str | None = None) -> str:
    name = Path(path).stem.lower().replace("_", " ").replace("-", " ")
    if not has_video or (source_kind or "").startswith("audio_recorder"):
        return "audio_recorder"
    if source_kind in _KIND_ROLES:
        return _KIND_ROLES[source_kind]
    for role, pat in _ROLE_HINTS:
        if re.search(pat, name):
            return role
    return "unlabelled"


def run_media_ingest(ctx: AgentContext) -> AgentOutcome:
    from bangonpro.ai_engine.department import media_ingest as mi

    st = ctx.state
    profile_fps = st.fps
    cams = dict(st.input_camera_files)
    inventory: list[dict[str, Any]] = []
    missing: list[str] = []
    hits: list[str] = []
    total_bytes = 0
    total_duration = 0.0
    fps_seen: Counter[str] = Counter()
    roles = (st.profile.get("camera_roles") or {})
    issues: list[dict[str, Any]] = []
    fps_map: dict[str, Any] = {}
    for idx, (cam_id, path) in enumerate(sorted(cams.items())):
        ctx.token.check(f"ingest {cam_id}")
        ctx.progress(0.6 * idx / max(1, len(cams)), f"Inspecting {cam_id}")
        p = Path(path)
        if not p.is_file():
            missing.append(cam_id)
            inventory.append({"camera_id": cam_id, "path": str(p), "status": "missing"})
            continue
        ident = media_identity_key(p)
        cached_sha = ctx.cache.get("sha256", "1", list(ident))
        if cached_sha:
            _SHA256_MEMO[ident] = str(cached_sha)
            hits.append(f"sha256:{cam_id}")
        sha = compute_file_sha256(p)
        if not cached_sha:
            ctx.cache.put("sha256", "1", list(ident), sha)
        insp = mi.inspect_source(p, sha, ctx.cache)
        if insp.get("cache_hit"):
            hits.append(f"ingest:{cam_id}")
        for iss in insp.get("issues", []):
            issues.append({**iss, "camera_id": cam_id})
        try:
            probe = probe_media_file(p)
        except (OSError, RuntimeError, ValueError) as exc:
            missing.append(cam_id)
            inventory.append({"camera_id": cam_id, "path": str(p), "status": "unreadable", "sha256": sha, "error": str(exc), "action": insp.get("action", "")})
            continue
        has_video = bool(probe.get("video_codec")) and int(probe.get("width", 0) or 0) > 0
        fps_num = int(probe.get("fps_num", 0) or 0) if has_video else 0
        fps_den = int(probe.get("fps_den", 1) or 1)
        fps = round(fps_num / fps_den, 3) if fps_num else 0.0
        if has_video and fps:
            fps_seen[f"{fps:g}"] += 1
        size = p.stat().st_size
        total_bytes += size
        dur = float(probe.get("duration_sec", 0.0) or 0.0)
        total_duration = max(total_duration, dur)
        kind = insp.get("source_kind")
        role = roles.get(cam_id) or _guess_role(str(p), has_video, kind)
        ident_info = insp.get("identity") or {}
        conform = mi.conform_plan(fps, profile_fps) if has_video and fps else None
        if conform:
            fps_map[cam_id] = conform
        inventory.append(
            {
                "camera_id": cam_id,
                "path": str(p),
                "status": "ok",
                "sha256": sha,
                "size_bytes": size,
                "duration_sec": round(dur, 3),
                "has_video": has_video,
                "has_audio": bool(probe.get("audio_codec")),
                "fps": fps,
                "width": int(probe.get("width", 0) or 0),
                "height": int(probe.get("height", 0) or 0),
                "role_label": role,
                "role_source": "profile" if cam_id in roles else ("media_classification" if kind in _KIND_ROLES or not has_video else "filename_heuristic"),
                "conform_required": bool(has_video and fps and abs(fps - profile_fps) > 0.01),
                # V1.2.2 identification / validation
                "source_kind": kind,
                "classification_reasons": insp.get("classification_reasons", []),
                "device": {k: ident_info.get(k) for k in ("make", "model", "software", "originator", "serial", "handler_names", "creation_time", "timecode", "timecode_sec", "bwf_time_reference_sec")},
                "streams": {k: ident_info.get(k) for k in ("container", "video_codec", "pix_fmt", "fps_avg", "fps_r", "video_duration_sec", "audio_codec", "audio_sample_rate", "audio_channels", "audio_channel_layout", "audio_bit_depth", "audio_duration_sec", "container_duration_sec")},
                "vfr": insp.get("vfr", {}),
                "decode_probes": insp.get("decode_probes", []),
                "video_dhash": insp.get("video_dhash", []),
                "audio_channels_check": {k: v for k, v in (insp.get("audio") or {}).items() if k != "level_env_db"},
                "_lev": (insp.get("audio") or {}).get("level_env_db"),
                "frame_rate_conform": conform,
                "validation_issue_codes": [i["code"] for i in insp.get("issues", [])],
                "_fp": np.frombuffer(bytes.fromhex(insp.get("fingerprint_hex", "")), dtype=np.uint16) if insp.get("fingerprint_hex") else None,
            }
        )
    duplicates = mi.find_duplicates(inventory)
    continuations = mi.continuation_groups(inventory)
    for e in inventory:
        e.pop("_fp", None)
        e.pop("_lev", None)
    mixed = len(fps_seen) > 1 or any(i.get("conform_required") for i in inventory)
    out_dir = Path(st.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    visual = mi.run_visual_sampling(ctx, inventory)
    free = shutil.disk_usage(out_dir).free
    # Analysis artefacts are small; a render needs ~ bitrate x duration (+ derivatives)
    need = int(ctx.options.min_free_bytes)
    if st.intent.export_video:
        need += int(total_duration * (12_000_000 / 8) * 2.5)
    storage = {"free_bytes": int(free), "required_bytes_estimate": need, "sufficient": free >= need}
    unreadable = [i["camera_id"] for i in inventory if i.get("status") == "unreadable"]
    report = {
        "inputs": inventory,
        "missing_camera_ids": missing,
        "unreadable_camera_ids": unreadable,
        "mixed_frame_rates": mixed,
        "frame_rates_seen": dict(fps_seen),
        "project_fps": profile_fps,
        "storage": storage,
        "duplicates": duplicates,
        "continuation_files": continuations,
        "validation_issues": issues,
        "frame_rate_conform": fps_map,
        "visual_analysis": visual.get("summary", {}),
    }
    inv_path = write_json_atomic(out_dir / "media_inventory.json", report)
    st.metadata["media_inventory"] = report
    st.artifacts["media_inventory_json"] = str(inv_path)
    if not storage["sufficient"]:
        raise InsufficientStorageError(
            f"Insufficient storage in {out_dir}: {free / 1e9:.2f} GB free, ~{need / 1e9:.2f} GB required. "
            "Free disk space or choose another output location, then resume the job."
        )
    if missing and len(missing) == len(cams):
        raise FileNotFoundError(f"All source media are missing: {', '.join(missing)}")
    absent = [c for c in missing if c not in unreadable]
    review = [f"Missing media: {', '.join(absent)}. The job continued with the remaining sources"] if absent else []
    for i in inventory:
        if i.get("status") == "unreadable":
            review.append(f"Unreadable media {i['camera_id']} ({Path(i['path']).name}): {i.get('action') or i.get('error')}. The job continued without it")
    if mixed:
        plans = "; ".join(f"{c}: {v['method']}" for c, v in sorted(fps_map.items()) if v.get("method") != "native")
        review.append(f"Mixed frame rates ({', '.join(f'{k} fps ×{v}' for k, v in sorted(fps_seen.items()))}) conformed to {profile_fps:g} fps" + (f" ({plans})" if plans else ""))
    unl = [i["camera_id"] for i in inventory if i.get("role_label") == "unlabelled"]
    if unl:
        review.append(f"Unlabelled angles {', '.join(unl)}: assign camera roles in the profile for better coverage")
    for dup in duplicates:
        a, b = dup["camera_ids"]
        why = {"identical_sha256": "identical file contents (SHA-256)", "same_audio_fingerprint": "the same audio and duration under a different name (re-wrap or transcode)", "same_video_hash": "the same pictures and duration"}[dup["kind"]]
        review.append(f"Duplicate source: {b} and {a} have {why}. Remove one from the job, or the same angle is synced and cut twice")
    for c in continuations:
        review.append(f"{c['camera_ids'][1]} continues {c['camera_ids'][0]} from the same {c['device']} after a {c['gap_sec']:.0f}s stop; both are synced independently")
    review += [i["message"] for i in issues if i.get("severity") in ("warning", "error") and i.get("code") != "UNREADABLE_MEDIA"]
    ctx.progress(1.0, f"Inventoried {len(inventory)} sources")
    return AgentOutcome(
        confidence=(0.99 if not missing else 0.8) * (0.9 if any(i.get("severity") == "error" for i in issues) else 1.0),
        summary=f"{len(inventory) - len(missing)} sources verified, {len(missing)} missing, mixed_fps={mixed}" + (f", {len(duplicates)} duplicate(s)" if duplicates else "") + (f", {len(issues)} validation finding(s)" if issues else ""),
        outputs={"media_inventory_json": str(inv_path)},
        review_items=review,
        cache_hits=hits + visual.get("cache_hits", []),
        metrics={"total_bytes": total_bytes, "max_duration_sec": round(total_duration, 3), "duplicates_found": len(duplicates), "validation_issues": dict(Counter(i["code"] for i in issues)), "visual_analysis_sec": visual.get("elapsed_sec"), "visual_cameras": visual.get("cameras", [])},
        est_external_cost_usd=0.0,
    )


# ---------------------------------------------------------------------------
# Synchronisation Agent (engine: GCC-PHAT)
# ---------------------------------------------------------------------------
def run_synchronisation(ctx: AgentContext) -> AgentOutcome:
    from bangonpro.ai_engine.department.media_intel import finish_synchronisation

    st = ctx.state
    batches = _norm_batches(run_ingest_sync_agent(st, ctx.token, ctx.progress))
    extra = finish_synchronisation(ctx, batches)  # V1.2.2: drift, segments, visual analysis on the master clock
    sr = st.sync_result
    conf = dict(sr.confidences) if sr else {}
    low = [c for c, v in conf.items() if v < 0.5 and sr and c != sr.reference_camera_id]
    mean_conf = round(float(np.mean(list(conf.values()))), 4) if conf else 1.0
    return AgentOutcome(
        batches=batches,
        confidence=mean_conf,
        summary=f"Synchronised {len(conf)} sources to {sr.reference_camera_id if sr else 'n/a'} via {sr.method if sr else 'n/a'}" + extra.get("summary_suffix", ""),
        rationale="Audio cross-correlation (GCC-PHAT) against the reference recording, measured in windows across the whole recording for clock drift and restarts",
        review_items=([f"Low sync confidence for {', '.join(low)}: verify alignment"] if low else []) + extra.get("review", []),
        metrics={"offsets_sec": dict(sr.offsets_sec) if sr else {}, "confidences": conf, **extra.get("metrics", {})},
        cache_hits=extra.get("cache_hits", []),
        est_external_cost_usd=0.0,
    )


# ---------------------------------------------------------------------------
# Editorial Intelligence Agent (engine: ASR/VAD/diarisation) + highlights
# ---------------------------------------------------------------------------
_EI_CACHE_VERSION = "1.2.2"  # V1.2.2 diarisation adds non-speech activity and word assignment
# Only the editing rules the transcription/VAD stage reads invalidate the (expensive) transcript;
# pause, pacing and highlight rules are applied on top of the cached analysis every run.
_EI_TRANSCRIPT_RULE_KEYS = ("min_silence_sec", "interruption_gap_sec", "review_confidence_threshold", "silence_threshold_db", "min_topic_sec")


def _editorial_cache_key(st: ProjectAnalysisState) -> dict[str, Any]:
    eff = get_effective_profile_settings(st.profile)
    sr = st.sync_result
    rules = st.profile.get("editing_rules", {}) or {}
    return {
        "analysis": "1.2.2",
        "media": sorted((cid, a.sha256) for cid, a in st.media_assets.items()),
        "offsets": {k: round(v, 3) for k, v in (sr.offsets_sec.items() if sr else [])},
        "asr_model": st.metadata.get("asr_model_id"),
        "model_dir": st.metadata.get("model_dir"),
        "silence_db": eff.get("silence_threshold_db"),
        "editing_rules": {k: rules.get(k) for k in _EI_TRANSCRIPT_RULE_KEYS if k in rules},
        "format_type": st.profile.get("format_type"),
        "speaker_map": st.profile.get("speaker_camera_map", {}),
        "fps": [st.fps_num, st.fps_den],
        "flags": [st.intent.transcribe, st.intent.remove_silence, st.intent.remove_setup_delay],
        "time_range": list(st.intent.time_range_sec) if st.intent.time_range_sec else None,
        "speakers": list(st.intent.target_speakers),
    }


_EI_FIELDS = ("transcript_segments", "story_sections", "silence_intervals", "interruption_intervals")


def _restore_editorial(st: ProjectAnalysisState, cached: dict[str, Any]) -> list[dict[str, Any]]:
    st.transcript_segments = [TranscriptSegment(**s) for s in cached["transcript_segments"]]
    st.story_sections = [StorySection(**s) for s in cached["story_sections"]]
    st.silence_intervals = [TimeInterval(**i) for i in cached["silence_intervals"]]
    st.interruption_intervals = [TimeInterval(**i) for i in cached["interruption_intervals"]]
    st.setup_delay_sec = float(cached["setup_delay_sec"])
    st.transcript_backend = cached["transcript_backend"]
    st.asr_backend_report = AsrBackendReport(**cached["asr_backend_report"])
    st.transcript_document = cached.get("transcript_document")
    st.metadata.update(cached.get("metadata", {}))
    batches = []
    for b in cached.get("batches", []):
        nb = copy.deepcopy(b)
        nb["request_id"] = f"req_{uuid.uuid4().hex[:12]}"
        nb["expected_revision"] = st.revision
        batches.append(nb)
    return batches


def score_highlights(st: ProjectAnalysisState, *, max_items: int = 8, min_len: float = 4.0, max_len: float = 30.0) -> list[dict[str, Any]]:
    """Rank highlight windows (V1.2.2: complete thoughts with setup and payoff on sentence/turn boundaries).

    Delegates to ``editorial_analysis.score_highlights``; the V1.2.1 energy z-score ranking is
    kept below as ``_score_highlights_v121`` for comparison only.
    """
    from bangonpro.ai_engine.department.editorial_analysis import score_highlights as _context_highlights

    return _context_highlights(st, max_items=max_items, min_len=min_len, max_len=max_len)


def _score_highlights_v121(st: ProjectAnalysisState, *, max_items: int = 8, min_len: float = 4.0, max_len: float = 30.0) -> list[dict[str, Any]]:
    """V1.2.1 ranking (energy z-score, turn density, overlap, keyword list); comparison only."""
    segs = [s for s in st.transcript_segments if not s.deleted and not s.is_filler_or_silence and (s.end_sec - s.start_sec) >= 1.0]
    if not segs:
        return []
    energies = np.array([s.energy_db for s in segs], dtype=float)
    mu, sd = float(energies.mean()), float(energies.std() or 1.0)
    neural = any(s.is_neural_inference for s in segs)
    hl: list[dict[str, Any]] = []
    for i, s in enumerate(segs):
        z = (s.energy_db - mu) / sd
        turn_density = sum(1 for o in segs[max(0, i - 3): i + 4] if o.speaker_id != s.speaker_id) / 6.0
        text = s.text if neural else ""
        emph = (text.count("!") + text.count("?")) * 0.15 + (0.3 if re.search(r"\b(amazing|incredible|unbelievable|goal|winner|breaking|wow)\b", text.lower()) else 0.0)
        score = 0.5 * z + 0.4 * turn_density + (0.4 if s.is_overlap else 0.0) + emph
        start = max(0.0, s.start_sec - 1.0)
        end = s.end_sec + 1.0
        if end - start < min_len:
            end = start + min_len
        end = min(end, start + max_len)
        reasons = []
        if z > 0.8:
            reasons.append("high vocal energy")
        if turn_density >= 0.5:
            reasons.append("rapid speaker exchange")
        if s.is_overlap:
            reasons.append("overlapping reactions")
        if emph:
            reasons.append("emphatic language")
        hl.append({"start_sec": round(start, 3), "end_sec": round(end, 3), "score": round(score, 4), "speaker_id": s.speaker_id, "segment_id": s.segment_id, "reasons": reasons or ["above-average engagement"], "text_available": neural})
    hl.sort(key=lambda h: h["score"], reverse=True)
    chosen: list[dict[str, Any]] = []
    for h in hl:
        if all(h["end_sec"] <= c["start_sec"] or h["start_sec"] >= c["end_sec"] for c in chosen):
            chosen.append(h)
        if len(chosen) >= max_items:
            break
    return sorted(chosen, key=lambda h: h["start_sec"])


def run_editorial_intelligence(ctx: AgentContext) -> AgentOutcome:
    st = ctx.state
    key = _editorial_cache_key(st)
    cached = ctx.cache.get("editorial", _EI_CACHE_VERSION, key)
    hits: list[str] = []
    if cached:
        batches = _restore_editorial(st, cached)
        hits.append("editorial:transcript")
        ctx.progress(0.7, "Reused cached transcript/diarisation (no re-inference)")
    else:
        batches = _norm_batches(run_transcribe_diarise_agent(st, ctx.token, ctx.progress))
        payload: dict[str, Any] = {f: [dataclasses.asdict(x) for x in getattr(st, f)] for f in _EI_FIELDS}
        payload.update(
            {
                "setup_delay_sec": st.setup_delay_sec,
                "transcript_backend": st.transcript_backend,
                "asr_backend_report": dataclasses.asdict(st.asr_backend_report),
                "transcript_document": st.transcript_document,
                "metadata": {
                    k: st.metadata[k]
                    for k in ("asr_backends_available", "asr_backend_report", "vad_threshold_db", "overlap_segment_count", "nonspeech_activity", "noise_floor_db", "topic_segmentation")
                    if k in st.metadata
                },
                "batches": batches,
            }
        )
        ctx.cache.put("editorial", _EI_CACHE_VERSION, key, payload)
    # V1.2.2: cheap editorial analysis recomputed on top of the (cached) transcript every run
    from bangonpro.ai_engine.department import editorial_analysis as ea

    ctx.progress(0.85, "Typing moments, identifying speakers, scoring highlights")
    econtext = ea.build_editorial_context(st)
    st.metadata["editorial_context"] = econtext
    for seg in st.transcript_segments:
        info = econtext["speakers"].get(seg.speaker_id)
        if info and info.get("name"):
            seg.speaker_name = info["name"]
    rules = st.profile.get("distribution_rules", {}) or {}
    highlights = ea.score_highlights(st, max_items=int(rules.get("highlight_count", 6)), moments=econtext["moments"])
    social = ea.select_social_clips(st, moments=econtext["moments"], speakers=econtext["speakers"])
    st.metadata["highlights"] = highlights
    st.metadata["social_clips"] = social
    out_dir = Path(st.output_dir)
    method = "context-aware: complete thoughts, setup + payoff, sentence/turn boundaries" + ("" if st.asr_backend_report.is_neural_inference else " (speech activity only: no text)")
    hl_path = write_json_atomic(out_dir / "highlights.json", {"highlights": highlights, "social_clips": social, "method": method})
    ctx_path = write_json_atomic(out_dir / "editorial_context.json", econtext)
    st.artifacts["highlights_json"] = str(hl_path)
    st.artifacts["editorial_context_json"] = str(ctx_path)
    rep = st.asr_backend_report
    review = []
    unnamed = [s for s, v in econtext["speakers"].items() if v.get("name_source") == "role label only"]
    if unnamed:
        review.append(f"No names for {', '.join(unnamed)}: add graphics_rules.speaker_names in the profile for lower thirds")
    if not rep.is_neural_inference:
        review.append("Speech regions only, no words. Install a Whisper model in Model Manager for word-level editing and captions")
    low = sum(1 for s in st.transcript_segments if s.is_low_confidence)
    if low:
        review.append(f"{low} low-confidence transcript segment{'s' if low != 1 else ''} to check")
    return AgentOutcome(
        batches=batches,
        confidence=round(float(np.mean([s.confidence for s in st.transcript_segments])), 4) if st.transcript_segments else 0.5,
        summary=f"{len(st.transcript_segments)} segments, {len(st.story_sections)} topics, {len(econtext['moments'])} moments, {len(highlights)} highlights, {len(social)} social clips ({st.transcript_backend})",
        rationale="Speech, speaker turns, energy and overlaps analysed once (cached); topics, typed moments, speaker names, protected pauses and highlights recomputed on top",
        outputs={"highlights_json": str(hl_path), "editorial_context_json": str(ctx_path), **({"transcript_json": st.artifacts["transcript_json"]} if "transcript_json" in st.artifacts else {})},
        neural_inference=bool(rep.is_neural_inference),
        fallback_reason=rep.fallback_reason,
        review_items=review,
        cache_hits=hits,
        metrics={
            "segments": len(st.transcript_segments),
            "topics": len(st.story_sections),
            "moments": econtext["moment_counts"],
            "protected_pauses": len(econtext["protected_pauses"]),
            "speakers_named": sum(1 for v in econtext["speakers"].values() if v.get("name_source") != "role label only"),
            "highlights": len(highlights),
            "social_clips": len(social),
            "setup_delay_sec": round(st.setup_delay_sec, 3),
        },
        est_external_cost_usd=0.0,
    )


# ---------------------------------------------------------------------------
# Multicamera Director
# ---------------------------------------------------------------------------
def run_multicam_director(ctx: AgentContext) -> AgentOutcome:
    st = ctx.state
    video = _video_assets(st)
    spk_map = dict(st.profile.get("speaker_camera_map") or {})
    roles = dict(st.profile.get("camera_roles") or {})
    inv = {i["camera_id"]: i for i in (st.metadata.get("media_inventory", {}) or {}).get("inputs", [])}
    for cid in video:
        if cid not in roles and inv.get(cid, {}).get("role_label") not in (None, "unlabelled"):
            roles[cid] = inv[cid]["role_label"]
    # Map diarised speakers to the angle where their microphone dominates
    votes: dict[str, Counter[str]] = {}
    talk: dict[str, dict[str, float]] = {}
    for s in st.transcript_segments:
        if s.camera_id in video:
            spk_talk = talk.setdefault(s.speaker_id, {})
            spk_talk[s.camera_id] = spk_talk.get(s.camera_id, 0.0) + max(0.0, s.end_sec - s.start_sec)
    for spk_id, per_cam in talk.items():
        votes[spk_id] = Counter({cam: round(sec * 1000) for cam, sec in per_cam.items()})
    repaired: list[str] = []
    for spk, cnt in votes.items():
        if spk_map.get(spk) in video:
            continue
        cam, _ = cnt.most_common(1)[0]
        spk_map[spk] = cam
        repaired.append(f"{spk}->{cam}")
    if not any(r == "wide" for r in roles.values()) and video:
        widest = max(video, key=lambda c: (video[c].width * video[c].height, -video[c].priority))
        roles.setdefault(widest, "wide")
    st.profile["speaker_camera_map"] = spk_map
    st.profile["camera_roles"] = {**(st.profile.get("camera_roles") or {}), **roles}
    eff = get_effective_profile_settings(st.profile)
    plan = {
        "speaker_camera_map": spk_map,
        "camera_roles": st.profile["camera_roles"],
        "reaction_shot_frequency": eff.get("reaction_shot_frequency"),
        "wide_shot_interval_sec": eff.get("wide_shot_interval_sec"),
        "min_shot_duration_sec": eff.get("min_shot_duration_sec"),
        "max_shot_duration_sec": eff.get("max_shot_duration_sec"),
        "camera_preferences": st.profile.get("camera_preferences", {}),
        "repaired_mappings": repaired,
        "speaker_talk_time_sec": {k: round(sum(v.values()), 2) for k, v in talk.items()},
    }
    # V1.2.2: listener shots, camera availability and a provisional shot plan with objective metrics
    from bangonpro.ai_engine.department import editorial_director as edir

    sw = edir.build_switch_context(st)
    plan["listener_camera_map"] = {spk: edir.listener_camera(sw, spk, spk_map, list(video), spk_map.get(spk, "")) for spk in talk}
    plan["camera_unavailable_intervals"] = {c: [list(iv) for iv in ivs] for c, ivs in sw["availability"].items()}
    plan["switching_policy"] = {
        "interjection_max_sec": float((st.profile.get("camera_preferences") or {}).get("interjection_max_sec", min(1.2, float(eff.get("min_shot_duration_sec", 2.0))))),
        "listener_shots": bool((st.profile.get("camera_preferences") or {}).get("listener_shots", True)),
        "wide_on_rapid_exchange": bool((st.profile.get("camera_preferences") or {}).get("wide_on_rapid_exchange", True)),
        "wide_on_topic_change": bool((st.profile.get("camera_preferences") or {}).get("wide_on_topic_change", True)),
        "cutaways": "timed to reactions and sentence boundaries, to the listener or the person reacting",
    }
    st.metadata["camera_plan"] = plan
    provisional = edir.provisional_shot_plan(st)
    if provisional is not None:
        plan["provisional_metrics"] = provisional["metrics"]
    path = write_json_atomic(Path(st.output_dir) / "camera_plan.json", plan)
    st.metadata["camera_plan"] = plan
    unmapped = [s for s in votes if s not in spk_map]
    ctx.progress(1.0, f"Camera plan for {len(video)} angles, {len(spk_map)} speakers")
    review = [f"Auto-mapped speakers {', '.join(repaired)}: confirm camera assignments"] if repaired else []
    if sw["availability"]:
        review.append("Unavailable video on " + ", ".join(f"{c} ({sum(b - a for a, b in ivs):.1f}s)" for c, ivs in sw["availability"].items()) + ": covered by other angles")
    return AgentOutcome(
        confidence=0.9 if not unmapped else 0.7,
        summary=f"{len(video)} angles; speaker map {spk_map}; repaired {len(repaired)} mapping(s)",
        rationale="Speakers mapped to the angle whose microphone dominates their speech; listener/reaction shots timed to reactions and sentence boundaries; wide on crosstalk, rapid exchanges and topic changes",
        outputs={"camera_plan_json": str(path)},
        review_items=review,
        metrics=plan,
        est_external_cost_usd=0.0,
    )



# ---------------------------------------------------------------------------
# Rough Cut Agent (engine)
# ---------------------------------------------------------------------------
def run_rough_cut(ctx: AgentContext) -> AgentOutcome:
    from bangonpro.ai_engine.department.editorial_pacing import pacing_metrics

    st = ctx.state
    # Scene detection is the only expensive step here: serve it from the shared analysis cache so
    # a quality-loop correction (or a rerun) never repeats it.
    hits: list[str] = []
    ref = st.sync_result.reference_camera_id if st.sync_result else None
    ref_asset = st.media_assets.get(ref) if ref else None
    scene_key = {"camera": ref, "sha256": ref_asset.sha256 if ref_asset else None, "threshold": 27.0}
    if ref_asset is not None and ref_asset.has_video:
        cached = ctx.cache.get("scene_cuts", "1", scene_key)
        if cached:
            st.metadata["scene_cuts_precomputed"] = {"camera_id": ref, "sha256": ref_asset.sha256, "cuts": cached["cuts"], "backend": cached["backend"]}
            hits.append("scene_cuts")
    batches = _norm_batches(run_rough_cut_multicam_agent(st, ctx.token, ctx.progress))
    if ref_asset is not None and ref_asset.has_video and not hits and st.metadata.get("scene_cut_camera") == ref:
        ctx.cache.put("scene_cuts", "1", scene_key, {"cuts": list(st.scene_cuts_sec), "backend": st.metadata.get("scene_detect_backend")})
    st.metadata.pop("scene_cuts_precomputed", None)
    shots = st.rough_cut_shots
    edited = shots[-1].timeline_out_sec if shots else 0.0
    sources = max((a.duration_sec for a in st.media_assets.values()), default=0.0)
    review = sum(1 for s in shots if s.needs_review)
    decisions = st.metadata.get("rough_cut_decisions") or {}
    pm = pacing_metrics(st, st.kept_master_intervals, declared_disfluencies=decisions.get("disfluencies")) if st.kept_master_intervals else {}
    st.metadata["pacing_metrics"] = {k: v for k, v in pm.items() if k != "protected_detail"}
    items = [f"{review} shot{'s' if review != 1 else ''} to check for pacing or angle"] if review else []
    n_dis = len(decisions.get("disfluencies", []) or [])
    if n_dis:
        items.append(f"{n_dis} false start(s)/stutter(s)/repeat(s) removed on word boundaries: check the 'Removed ...' markers")
    stats = decisions.get("stats", {})
    return AgentOutcome(
        batches=batches,
        confidence=round(float(np.mean([s.confidence for s in shots])), 4) if shots else 0.5,
        summary=f"{len(shots)} shots, edited {edited:.1f}s from {sources:.1f}s source ({(1 - edited / sources) * 100 if sources else 0:.0f}% removed); "
        f"{stats.get('protected_pauses', 0)} meaningful pause(s) protected, {n_dis} disfluenc{'y' if n_dis == 1 else 'ies'} removed",
        rationale="Setup delay trimmed; pauses compressed to a natural show-specific length (pauses after questions, before punchlines and around reactions protected); no cut inside speech; speaker-aware switching per camera plan",
        review_items=items,
        cache_hits=hits,
        metrics={
            "shots": len(shots),
            "edited_duration_sec": round(edited, 3),
            "source_duration_sec": round(sources, 3),
            "pause_policy": (decisions.get("rules") or {}).get("pause_policy"),
            "protected_pauses": pm.get("protected_pauses"),
            "protected_retention": pm.get("protected_retention"),
            "dead_air_removed_ratio": pm.get("dead_air_removed_ratio"),
            "cuts_inside_words": len(pm.get("cuts_inside_words", []) or []),
            "words_lost": len(pm.get("words_lost", []) or []),
            "disfluencies_removed": n_dis,
        },
        est_external_cost_usd=0.0,
    )


# ---------------------------------------------------------------------------
# Audio Engineer / Captions / Graphics (split V1.1 finishing engine)
# ---------------------------------------------------------------------------
def run_audio_engineer(ctx: AgentContext) -> AgentOutcome:
    from bangonpro.ai_engine.department.media_intel import engineer_audio

    st = ctx.state
    plan = engineer_audio(ctx) if st.intent.restore_audio else {}
    agq.prepare_audio_restoration(st)
    b = agq.new_agent_builder(st, agent_id="audio_engineer", agent_role="audio_engineer", transaction_label="Audio Engineer: dialogue cleanup, mic balance & loudness")
    n = agq.emit_audio_ops(st, b)
    for cam, dc in (plan.get("declip") or {}).items():
        if dc.get("created"):
            src = st.media_assets[cam]
            b.add_operation(
                "import_media",
                media_ref=f"media_{cam}_declipped",
                params={"camera_id": f"{cam}_declipped", "name": f"{cam} (declipped)", "path": dc["path"], "source_path": dc["path"], "file_path": dc["path"], "duration_sec": round(src.duration_sec, 4), "is_audio_only": True, "audio_codec": "pcm_f32le", "derived_from": cam, "process": "adeclip", "read_only_verified": False},
                confidence=0.85,
                rationale=f"Declipped copy of {cam} ({dc.get('clipped_share_of_speech', 0):.2%} of speech clipped); swap it in for the affected shots after listening",
            )
            n += 1
    m = plan.get("matching") or {}
    trims = m.get("trims_db", st.metadata.get("per_camera_gain_trim_db", {}))
    ctx.progress(1.0, f"{n} audio operations")
    denoise = [f.filter_id for f in st.audio_filters if "noise" in f.filter_id or "rnn" in f.filter_id or "afftdn" in f.ffmpeg_filter]
    review = []
    for cam, dc in (plan.get("declip") or {}).items():
        review.append(f"{cam} clips on {dc.get('clipped_share_of_speech', 0):.2%} of speech: " + ("a declipped copy is in the bin; listen and swap it in" if dc.get("created") else f"not repaired ({dc.get('reason', 'declip disabled')})"))
    return AgentOutcome(
        batches=[b.build()] if n else [],
        confidence=0.95,
        summary=f"{len(st.audio_filters)} filters, {len([t for t in trims.values() if abs(t) >= 1.0])} mic trims, target {agq._loudness_target(st)} LUFS"
        + (f"; mic spread {m.get('level_spread_before_db', 0):.1f} -> {m.get('level_spread_after_db', 0):.1f} dB" if m else ""),
        rationale="Measured per-mic levels, tone, clipping, bleed and noise; high-pass, denoise, natural compression, mic matching and EBU R128 loudness",
        neural_inference=bool(st.rnnoise_model_path),
        fallback_reason=None if st.rnnoise_model_path else "RNNoise model not configured; non-neural noise-floor expander (avfilter.compand) used",
        review_items=review,
        cache_hits=list(plan.get("cache_hits", [])),
        metrics={"filters": [f.filter_id for f in st.audio_filters], "mic_trims_db": trims, "denoise": denoise, "audio_analysis_sec": plan.get("elapsed_sec")},
        est_external_cost_usd=0.0,
    )


def run_captions(ctx: AgentContext) -> AgentOutcome:
    st = ctx.state
    neural = bool(st.asr_backend_report.is_neural_inference)
    if not neural and ctx.options.mode in AUTONOMOUS_MODES:
        st.captions = []
        return AgentOutcome(
            confidence=0.0,
            summary="Captions withheld: only a VAD (non-neural) transcript is available",
            neural_inference=False,
            fallback_reason=st.asr_backend_report.fallback_reason or "No local Whisper model installed",
            review_items=["Captions need a spoken transcript. Install a Whisper model in Model Manager, then rerun Captions"],
            skipped_reason="no_spoken_transcript",
        )
    agq.prepare_captions(st)
    b = agq.new_agent_builder(st, agent_id="captions", agent_role="captions", transaction_label="Captions Agent: subtitles")
    n = agq.emit_caption_ops(st, b)
    style = get_effective_profile_settings(st.profile)["caption_style"]
    max_cps = float(style.get("max_chars_per_sec", 20.0))
    fast = [c.index for c in st.captions if len(c.text) / max(0.1, c.end_sec - c.start_sec) > max_cps]
    return AgentOutcome(
        batches=[b.build()] if n else [],
        confidence=round(float(np.mean([c.confidence for c in st.captions])), 4) if st.captions else 0.5,
        summary=f"{len(st.captions)} caption cues ({'neural ASR' if neural else 'VAD placeholders - review required'})",
        outputs={k: st.artifacts[k] for k in ("captions_srt", "captions_ass") if k in st.artifacts},
        neural_inference=neural,
        fallback_reason=None if neural else st.asr_backend_report.fallback_reason,
        review_items=([f"{len(fast)} cues exceed {max_cps:g} chars/sec"] if fast else []) + ([] if neural else ["Captions are VAD placeholders"]),
    )


def run_graphics(ctx: AgentContext) -> AgentOutcome:
    from bangonpro.ai_engine.department import editorial_graphics as eg

    st = ctx.state
    rules = st.profile.get("graphics_rules", {}) or {}
    templates = st.profile.get("graphics_templates", {}) or {}
    title = ctx.options.episode_title or rules.get("title_card_text") or ""
    want_title = bool(title and rules.get("enable_title_card", True))
    title_sec = float(min(6.0, max(2.0, float(rules.get("title_card_duration_sec", 3.0)))))
    spec = st.profile["video_spec"]
    width, height = int(spec["width"]), int(spec["height"])
    accent = str(rules.get("brand_accent_hex", templates.get("accent_hex", "#FF5A1F")))
    font = str(templates.get("font_family", "DejaVu Sans"))
    # V1.2.2: V2 occupancy is planned up front: title card, then topic cards, then lower thirds
    reserved: list[tuple[float, float]] = [(0.0, title_sec)] if want_title else []
    topic_cards = eg.plan_topic_cards(st, reserved) if st.rough_cut_shots or st.transcript_segments else []
    st.metadata["graphics_reserved"] = reserved
    agq.prepare_lower_thirds(st)
    b = agq.new_agent_builder(st, agent_id="graphics", agent_role="graphics", transaction_label="Graphics Agent: lower thirds & title card")
    n = agq.emit_lower_third_ops(st, b)
    report: list[dict[str, Any]] = []
    lt_name, lt_spec, lt_known = eg.get_template(str(templates.get("lower_third_template", "studio_clean_bar")), "lower_third")
    lt_min, lt_max = float(rules.get("lower_third_min_sec", 3.0)), float(rules.get("lower_third_max_sec", 8.0))
    for lt in st.lower_thirds:
        report.append({"graphic_id": lt.graphic_id, "kind": "lower_third", "template": lt.template, "start_sec": lt.start_sec, **eg.validate_graphic(spec=lt_spec, position=lt.position, width=width, height=height, title=lt.title, subtitle=lt.subtitle, duration_sec=lt.duration_sec, min_sec=lt_min, max_sec=lt_max)})
    tc_name, tc_spec, _ = eg.get_template("topic_card", "topic_card")
    for k, card in enumerate(topic_cards, start=1):
        dur_f = max(1, st.sec_to_frame(card["duration_sec"]))
        xml = agq.generate_kdenlive_lower_third_xml(
            title=card["title"], subtitle=card["subtitle"], width=width, height=height, accent_hex=accent, duration_frames=dur_f,
            position=str(tc_spec["position"]), font_family=font, box_w_frac=float(tc_spec["box_w"]), box_h_frac=float(tc_spec["box_h"]),
            title_px_frac=float(tc_spec["title_px"]), sub_px_frac=float(tc_spec["sub_px"]),
        )
        start_f = st.sec_to_frame(card["start_sec"])
        b.add_operation(
            "place_clip",
            clip_id=f"topic_card_{k:02d}",
            track_id="V2",
            in_frame=0,
            out_frame=max(0, dur_f - 1),
            timeline_start=start_f,
            params={"clip_kind": "kdenlivetitle", "title": card["title"], "subtitle": card["subtitle"], "template": tc_name, "position": str(tc_spec["position"]), "kdenlive_title_xml": xml},
            confidence=0.85,
            rationale=f"Topic card for {card['section_id']} at the start of the story",
        )
        report.append({"graphic_id": f"topic_card_{k:02d}", "kind": "topic_card", "template": tc_name, "start_sec": card["start_sec"], **eg.validate_graphic(spec=tc_spec, position=str(tc_spec["position"]), width=width, height=height, title=card["title"], subtitle=card["subtitle"], duration_sec=card["duration_sec"], min_sec=2.5, max_sec=6.0)})
    title_added = False
    if want_title:
        t_name, t_spec, _ = eg.get_template(str(templates.get("title_card_template", "episode_opener")), "title_card")
        dur_f = st.sec_to_frame(title_sec)
        xml = agq.generate_kdenlive_lower_third_xml(
            title=title,
            subtitle=str(st.profile.get("display_name", "")),
            width=width,
            height=height,
            accent_hex=accent,
            duration_frames=dur_f,
            position="center",
            font_family=font,
            box_w_frac=float(t_spec["box_w"]),
            box_h_frac=float(t_spec["box_h"]),
            title_px_frac=float(t_spec["title_px"]),
            sub_px_frac=float(t_spec["sub_px"]),
        )
        report.append({"graphic_id": "title_card_001", "kind": "title_card", "template": t_name, "start_sec": 0.0, **eg.validate_graphic(spec=t_spec, position="center", width=width, height=height, title=title, subtitle=str(st.profile.get("display_name", "")), duration_sec=title_sec, min_sec=2.0, max_sec=6.0)})
        b.add_operation(
            "place_clip",
            clip_id="title_card_001",
            track_id="V2",
            in_frame=0,
            out_frame=max(0, dur_f - 1),
            timeline_start=0,
            params={"clip_kind": "kdenlivetitle", "title": title, "subtitle": str(st.profile.get("display_name", "")), "template": str(templates.get("title_card_template", "episode_opener")), "position": "center", "kdenlive_title_xml": xml},
            confidence=0.97,
            rationale=f"Episode title card '{title}' ({templates.get('title_card_template', 'episode_opener')})",
        )
        title_added = True
    intro = (st.profile.get("intro_outro_rules", {}) or {})
    st.metadata["graphics_report"] = {"graphics": report, "lower_third_plan": st.metadata.get("lower_third_plan", []), "template_known": lt_known, "lower_third_template": lt_name}
    write_json_atomic(Path(st.output_dir) / "graphics_report.json", st.metadata["graphics_report"])
    review = [] if st.lower_thirds or title_added else ["No speakers identified for lower thirds; add names in the profile speaker map"]
    for r in report:
        if not r["inside_title_safe"]:
            review.append(f"{r['graphic_id']} extends outside the title-safe area")
        if not r["text_fits"]:
            review.append(f"{r['graphic_id']}: text may not fit the {r['template']} template")
    for row in st.metadata.get("lower_third_plan", []):
        if row.get("tier") == 3:
            review.append(f"Lower third for {row['title']} placed without the speaker on screen: check timing")
    if not lt_known:
        review.append(f"Unknown lower-third template '{templates.get('lower_third_template')}': used {lt_name}")
    return AgentOutcome(
        batches=[b.build()] if b.operation_count else [],
        confidence=0.95 if all(r.get("tier", 1) < 3 for r in st.metadata.get("lower_third_plan", [])) else 0.8,
        summary=f"{n} lower thirds" + (f", {len(topic_cards)} topic cards" if topic_cards else "") + (", title card" if title_added else "") + (f"; intro={intro.get('intro_asset')}" if intro.get("intro_asset") else ""),
        rationale="Lower thirds at each speaker's first substantive appearance on their own angle (never during crosstalk or under another graphic); branded templates; title/action-safe validated",
        review_items=review,
        metrics={"lower_thirds": n, "topic_cards": len(topic_cards), "safe_area_failures": sum(1 for r in report if not r["inside_title_safe"])},
        est_external_cost_usd=0.0,
    )


# ---------------------------------------------------------------------------
# Colourist
# ---------------------------------------------------------------------------
def sample_frame_rgb(path: str, t_sec: float, width: int = 96) -> np.ndarray | None:
    cmd = [
        "ffmpeg", "-v", "error", "-ss", f"{max(0.0, t_sec):.3f}", "-i", str(path), "-frames:v", "1",
        "-vf", f"scale={width}:-2:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0 or not res.stdout:
        return None
    raw = np.frombuffer(res.stdout, dtype=np.uint8)
    if raw.size % (width * 3):
        return None
    return raw.reshape(-1, width, 3)


def camera_colour_stats(path: str, duration: float, samples: int = 5) -> dict[str, Any] | None:
    frames = []
    for k in range(samples):
        t = duration * (k + 0.5) / samples if duration > 0 else 0.0
        fr = sample_frame_rgb(path, t)
        if fr is not None:
            frames.append(fr.astype(np.float64) / 255.0)
    if not frames:
        return None
    stack = np.stack([f.reshape(-1, 3) for f in frames])
    mean = stack.reshape(-1, 3).mean(axis=0)
    per_frame = stack.mean(axis=1)
    luma = float(0.2126 * mean[0] + 0.7152 * mean[1] + 0.0722 * mean[2])
    return {
        "mean_rgb": [round(float(x), 5) for x in mean],
        "luma": round(luma, 5),
        "temporal_std": round(float(per_frame.std(axis=0).mean()), 5),
        "frames_sampled": len(frames),
    }


def run_colourist(ctx: AgentContext) -> AgentOutcome:
    from bangonpro.ai_engine.department.media_intel import colour_from_visual_analysis

    st = ctx.state
    rules = st.profile.get("colour_rules", {}) or {}
    video = _video_assets(st)
    v122 = colour_from_visual_analysis(ctx, video)  # V1.2.2: neutral WB, skin, shot-level (sampled stats)
    if v122 is not None:
        return v122
    stats: dict[str, Any] = {}
    for i, (cid, a) in enumerate(sorted(video.items())):
        ctx.token.check(f"colour analysis {cid}")
        ctx.progress(0.6 * i / max(1, len(video)), f"Measuring {cid}")
        s = camera_colour_stats(a.file_path, a.duration_sec, int(rules.get("samples_per_camera", 5)))
        if s:
            stats[cid] = s
    if not stats:
        return AgentOutcome(confidence=0.0, summary="No decodable video frames for colour analysis", skipped_reason="no_frames")
    roles = st.profile.get("camera_roles", {}) or {}
    ref = rules.get("reference_camera")
    if ref not in stats:
        ref = next((c for c in stats if roles.get(c) == "wide"), None) or min(stats, key=lambda c: video[c].priority)
    target_luma = float(rules.get("target_luma", 0.45))
    ref_luma = stats[ref]["luma"]
    exposure = 1.0
    if rules.get("correct_exposure", True) and ref_luma > 0:
        if ref_luma < float(rules.get("min_luma", 0.30)):
            exposure = min(1.5, target_luma / ref_luma)
        elif ref_luma > float(rules.get("max_luma", 0.68)):
            exposure = max(0.7, target_luma / ref_luma)
    max_gain = float(rules.get("max_channel_gain", 1.33))
    corrections: dict[str, dict[str, float]] = {}
    for cid, s in stats.items():
        gains = []
        for c in range(3):
            g = (stats[ref]["mean_rgb"][c] / s["mean_rgb"][c]) if (cid != ref and s["mean_rgb"][c] > 1e-3) else 1.0
            gains.append(float(min(max_gain, max(1.0 / max_gain, g)) * exposure))
        corrections[cid] = {"gain_r": round(gains[0], 4), "gain_g": round(gains[1], 4), "gain_b": round(gains[2], 4)}
    lut = rules.get("lut_path")
    lut_ok = bool(lut and Path(str(lut)).expanduser().is_file())
    b = agq.new_agent_builder(st, agent_id="colourist", agent_role="colourist", transaction_label="Colourist: camera match, exposure & treatment")
    applied = 0
    for shot in st.rough_cut_shots:
        corr = corrections.get(shot.camera_id)
        if not corr:
            continue
        clip_id = f"{shot.shot_id}_v"
        if any(abs(v - 1.0) > 0.02 for v in corr.values()):
            props = {"lift_r": 0.0, "lift_g": 0.0, "lift_b": 0.0, "gamma_r": 1.0, "gamma_g": 1.0, "gamma_b": 1.0, **corr}
            b.add_operation(
                "add_effect",
                clip_id=clip_id,
                track_id="V1",
                params={"effect_id": "lift_gamma_gain", "mlt_service": "lift_gamma_gain", "kdenlive_id": "lift_gamma_gain", "parameters": props, "properties": props, "agent_id": "colourist"},
                confidence=0.85 if shot.camera_id != ref else 0.9,
                rationale=(f"Match {shot.camera_id} to reference {ref}" if shot.camera_id != ref else "Reference camera") + (f"; exposure x{exposure:.2f}" if exposure != 1.0 else ""),
            )
            applied += 1
        if lut_ok:
            b.add_operation(
                "add_effect",
                clip_id=clip_id,
                track_id="V1",
                params={"effect_id": "avfilter.lut3d", "mlt_service": "avfilter.lut3d", "kdenlive_id": "avfilter.lut3d", "parameters": {"av.file": str(Path(str(lut)).expanduser())}, "properties": {"av.file": str(Path(str(lut)).expanduser())}, "agent_id": "colourist"},
                confidence=0.95,
                rationale=f"Approved series colour treatment LUT {Path(str(lut)).name}",
            )
    report = {"reference_camera": ref, "exposure_gain": round(exposure, 4), "camera_stats": stats, "corrections": corrections, "lut": str(lut) if lut else None, "lut_found": lut_ok}
    path = write_json_atomic(Path(st.output_dir) / "colour_report.json", report)
    st.metadata["colour_report"] = report
    ctx.progress(1.0, f"{b.operation_count} colour operations")
    review = []
    if lut and not lut_ok:
        review.append(f"Profile LUT not found: {lut}")
    noisy = [c for c, s in stats.items() if s["temporal_std"] > 0.08]
    if noisy:
        review.append(f"Lighting varies strongly over time on {', '.join(noisy)}: per-shot grading recommended")
    return AgentOutcome(
        batches=[b.build()] if b.operation_count else [],
        confidence=0.85 if not noisy else 0.65,
        summary=f"Reference {ref}; {applied} shot corrections; exposure x{exposure:.2f}" + ("; LUT applied" if lut_ok else ""),
        rationale="Per-camera RGB means from sampled frames matched to the reference angle with native lift/gamma/gain",
        outputs={"colour_report_json": str(path)},
        review_items=review,
        metrics={"corrections": corrections},
    )


# ---------------------------------------------------------------------------
# VFX Agent
# ---------------------------------------------------------------------------
def _vfx_tasks(st: ProjectAnalysisState, brief: str) -> list[dict[str, Any]]:
    tasks = list((st.profile.get("vfx_rules", {}) or {}).get("tasks", []) or [])
    low = brief.lower()
    if re.search(r"blur (the )?background|background blur", low):
        tasks.append({"effect": "blur_background", "camera_roles": ["host", "guest", "presenter", "anchor"]})
    elif re.search(r"(replace|remove) (the )?background|background (replace\w*|removal)|green ?screen", low):
        tasks.append({"effect": "replace_background", "camera_roles": ["host", "guest", "presenter", "anchor"]})
    return tasks


def run_vfx(ctx: AgentContext) -> AgentOutcome:
    from PIL import Image

    from bangonpro.roto_studio.mask_engine import segment_frame_detailed

    st = ctx.state
    tasks = _vfx_tasks(st, st.instruction)
    if not tasks:
        return AgentOutcome(summary="No VFX tasks requested", skipped_reason="no_tasks")
    roles = st.profile.get("camera_roles", {}) or {}
    video = _video_assets(st)
    vfx_dir = Path(st.output_dir) / "vfx"
    vfx_dir.mkdir(parents=True, exist_ok=True)
    b = agq.new_agent_builder(st, agent_id="vfx", agent_role="vfx", transaction_label="VFX Agent: segmentation masks & selective effects")
    seg_meta: dict[str, Any] = {}
    neural_any = False
    fallback_reasons: list[str] = []
    for task in tasks:
        target_cams = [c for c in video if roles.get(c, video[c].camera_role) in task.get("camera_roles", [])] or list(task.get("camera_ids", []))
        for cid in target_cams:
            ctx.token.check(f"vfx {cid}")
            shots = [s for s in st.rough_cut_shots if s.camera_id == cid]
            if not shots:
                continue
            t = (shots[0].source_in_sec + shots[0].source_out_sec) / 2.0
            a = video[cid]
            fr = sample_frame_rgb(a.file_path, t, width=min(640, max(64, a.width or 640)))
            if fr is None:
                continue
            h, w = fr.shape[:2]
            res = segment_frame_detailed(fr, positive_points=[(w * 0.5, h * 0.42)], backend="auto", model_dir=ctx.options.model_dir)
            neural_any = neural_any or res.is_neural_inference
            if res.fallback_reason:
                fallback_reasons.append(res.fallback_reason)
            alpha = np.asarray(res.alpha_mask, dtype=np.float32)
            alpha = np.clip(alpha if alpha.max() <= 1.0 else alpha / 255.0, 0, 1)
            matte_path = vfx_dir / f"{cid}_subject_matte.png"
            Image.fromarray((alpha * 255).astype(np.uint8)).resize((a.width or w, a.height or h)).save(matte_path)
            seg_meta[cid] = res.to_metadata_dict()
            if task.get("effect") == "replace_background":
                # Background removed to black through the native mask; composite a plate in Roto Studio
                masked = {"mlt_service": "brightness", "kdenlive_id": "brightness", "properties": {"level": 0.0}}
            else:
                strength = int((st.profile.get("vfx_rules", {}) or {}).get("blur_strength", 12))
                masked = {"mlt_service": "boxblur", "kdenlive_id": "boxblur", "properties": {"hori": strength, "vert": strength}}
            conf = float(res.confidence) * (1.0 if res.is_neural_inference else 0.6)
            for s in shots:
                b.add_operation(
                    "apply_mask",
                    clip_id=f"{s.shot_id}_v",
                    track_id="V1",
                    in_frame=0,
                    out_frame=max(1, s.source_out_frame - s.source_in_frame),
                    timeline_start=s.timeline_in_frame,
                    params={
                        "mask_id": f"vfx_{cid}_subject",
                        "mask_type": "ai_sam" if res.is_neural_inference else "matte_file",
                        "matte_path": str(matte_path),
                        "feather": 8.0,
                        "expand": 0.0,
                        "invert": True,
                        "operation": "write_on_clear",
                        "masked_effect": masked,
                        "vfx_task": task.get("effect"),
                        "segmentation": res.to_metadata_dict(),
                        "agent_id": "vfx",
                    },
                    confidence=round(min(1.0, max(0.0, conf)), 4),
                    rationale=f"{task.get('effect')} on {cid} using {res.backend_id} ({'neural' if res.is_neural_inference else 'labelled fallback'}) static subject matte",
                )
    write_json_atomic(vfx_dir / "vfx_report.json", {"tasks": tasks, "segmentation": seg_meta})
    return AgentOutcome(
        batches=[b.build()] if b.operation_count else [],
        confidence=_batch_confidence([b.build()]) if b.operation_count else 0.0,
        summary=f"{b.operation_count} masked effect operations on {len(seg_meta)} camera(s)",
        rationale="Subject segmented on a representative frame; background effect applied through native Kdenlive masks",
        outputs={"vfx_report_json": str(vfx_dir / "vfx_report.json")},
        neural_inference=neural_any,
        fallback_reason="; ".join(sorted(set(fallback_reasons))) or None,
        review_items=["Check the mattes in Roto and 2D and refine them where the subject moves"] + ([] if neural_any else ["Mattes used the non-neural fallback. Install MobileSAM in Model Manager for neural segmentation"]),
        skipped_reason=None if b.operation_count else "no_target_cameras",
    )


# ---------------------------------------------------------------------------
# Quality Control Agent
# ---------------------------------------------------------------------------
def department_qc_checks(st: ProjectAnalysisState) -> list[QCIssue]:
    eff = get_effective_profile_settings(st.profile)
    issues: list[QCIssue] = []
    min_shot = float(eff.get("min_shot_duration_sec", 2.0))
    max_shot = float(eff.get("max_shot_duration_sec", 12.0))
    shots = st.rough_cut_shots
    for i, s in enumerate(shots):
        dur = s.timeline_out_sec - s.timeline_in_sec
        last = i == len(shots) - 1
        if dur < min_shot - 0.5 and not last:
            issues.append(QCIssue("SHOT_TOO_SHORT", "warning", f"Shot {s.shot_id} lasts {dur:.2f}s (< {min_shot:g}s)", s.timeline_in_sec, {"shot_id": s.shot_id}))
        if dur > max_shot * 1.5:
            issues.append(QCIssue("SHOT_TOO_LONG", "info", f"Shot {s.shot_id} holds {dur:.1f}s (> {max_shot:g}s)", s.timeline_in_sec, {"shot_id": s.shot_id}))
        if i and shots[i - 1].camera_id == s.camera_id and abs(shots[i - 1].source_out_sec - s.source_in_sec) > 0.25:
            issues.append(QCIssue("JUMP_CUT", "warning", f"Jump cut on {s.camera_id} between {shots[i - 1].shot_id} and {s.shot_id}", s.timeline_in_sec, {"camera_id": s.camera_id}))
    sr = st.sync_result
    if sr:
        for cid, c in sr.confidences.items():
            if cid != sr.reference_camera_id and c < 0.35:
                issues.append(QCIssue("SYNC_LOW_CONFIDENCE", "error" if c < 0.2 else "warning", f"Sync confidence for {cid} is {c:.2f}", 0.0, {"camera_id": cid}))
    style = eff.get("caption_style", {}) or {}
    max_cps = float(style.get("max_chars_per_sec", 20.0))
    prev_end = -1.0
    for cue in st.captions:
        cps = len(cue.text) / max(0.1, cue.end_sec - cue.start_sec)
        if cps > max_cps:
            issues.append(QCIssue("CAPTION_READING_SPEED", "warning", f"Caption #{cue.index} at {cps:.1f} chars/sec (> {max_cps:g})", cue.start_sec, {"index": cue.index}))
        if cue.start_sec < prev_end - 0.01:
            issues.append(QCIssue("CAPTION_OVERLAP", "warning", f"Caption #{cue.index} overlaps previous cue", cue.start_sec, {"index": cue.index}))
        prev_end = cue.end_sec
    if st.captions and not st.asr_backend_report.is_neural_inference:
        issues.append(QCIssue("CAPTIONS_NOT_SPOKEN_TEXT", "error", "Captions were generated from a VAD-only transcript", 0.0))
    inv = st.metadata.get("media_inventory", {}) or {}
    for cid in inv.get("missing_camera_ids", []):
        issues.append(QCIssue("MISSING_MEDIA", "error", f"Source for {cid} is missing", 0.0, {"camera_id": cid}))
    if inv.get("mixed_frame_rates"):
        issues.append(QCIssue("MIXED_FRAME_RATES", "info", f"Sources at {inv.get('frame_rates_seen')} conformed to {inv.get('project_fps')} fps", 0.0))
    # Speaker coverage: share of speech time on the speaker's own angle or the wide
    plan = st.metadata.get("camera_plan", {}) or {}
    spk_map = plan.get("speaker_camera_map") or st.profile.get("speaker_camera_map", {}) or {}
    roles = st.profile.get("camera_roles", {}) or {}
    if shots and spk_map and st.transcript_segments:
        on, total = 0.0, 0.0
        for seg in st.transcript_segments:
            want = spk_map.get(seg.speaker_id)
            if not want:
                continue
            for s in shots:
                ov = min(seg.end_sec, s.master_sync_out_sec) - max(seg.start_sec, s.master_sync_in_sec)
                if ov > 0:
                    total += ov
                    if s.camera_id == want or roles.get(s.camera_id) == "wide":
                        on += ov
        if total > 0:
            cov = on / total
            st.metadata["speaker_coverage"] = round(cov, 4)
            if cov < 0.5:
                issues.append(QCIssue("SPEAKER_COVERAGE_LOW", "warning", f"Only {cov:.0%} of speech is on the speaker's angle or the wide", 0.0))
    return issues


def run_quality_control(ctx: AgentContext) -> AgentOutcome:
    st = ctx.state
    from bangonpro.ai_engine.department.quality_loop import run_qc_checks

    base = agq.prepare_qc(st) if st.intent.run_qc else st.qc_report
    extra = department_qc_checks(st)
    hooked, hook_errors = run_qc_checks(st)
    st.qc_report.issues = list(base.issues) + extra + hooked
    if hook_errors:
        st.metadata["qc_check_errors"] = hook_errors
    st.qc_report.passed = not any(i.severity == "error" for i in st.qc_report.issues)
    path = write_json_atomic(Path(st.output_dir) / "qc_report.json", dataclasses.asdict(st.qc_report))
    st.artifacts["qc_report_json"] = str(path)
    b = agq.new_agent_builder(st, agent_id="quality_control", agent_role="quality_control", transaction_label="QC Agent: review markers")
    agq.emit_qc_ops(st, b)
    counts = Counter(i.severity for i in st.qc_report.issues)
    return AgentOutcome(
        batches=[b.build()] if b.operation_count else [],
        confidence=0.95,
        summary=f"QC {'PASSED' if st.qc_report.passed else 'FAILED'}: {counts.get('error', 0)} errors, {counts.get('warning', 0)} warnings, {counts.get('info', 0)} info",
        rationale="Technical (loudness/levels/black/sync) plus editorial continuity, coverage and caption checks",
        outputs={"qc_report_json": str(path)},
        review_items=[f"{i.code}: {i.message}" for i in st.qc_report.issues if i.severity == "error"][:10],
        metrics={"issues": dict(counts), "speaker_coverage": st.metadata.get("speaker_coverage")},
    )


# ---------------------------------------------------------------------------
# Distribution Agent
# ---------------------------------------------------------------------------
def measure_loudness_lufs(path: str) -> float | None:
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.findall(r"I:\s+(-?\d+(?:\.\d+)?) LUFS", res.stderr)
    return float(m[-1]) if m else None


def _master_window_to_timeline(st: ProjectAnalysisState, start: float, end: float) -> tuple[float, float] | None:
    """Map a master-clock window onto the edited timeline, clamped to the kept shots."""
    kept = [s for s in st.rough_cut_shots if s.master_sync_out_sec > start and s.master_sync_in_sec < end]
    if not kept:
        return None
    first, last = kept[0], kept[-1]
    a = first.timeline_in_sec + max(0.0, start - first.master_sync_in_sec)
    z = last.timeline_in_sec + (min(end, last.master_sync_out_sec) - last.master_sync_in_sec)
    return (round(a, 3), round(z, 3)) if z > a else None


def _ffmpeg(args: list[str], timeout: int = 3600) -> bool:
    try:
        res = subprocess.run(["ffmpeg", "-y", "-v", "error", *args], capture_output=True, timeout=timeout, check=False)
        return res.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def run_distribution(ctx: AgentContext) -> AgentOutcome:
    st = ctx.state
    flags = ctx.plan.get("brief_flags", {})
    rules = st.profile.get("distribution_rules", {}) or {}
    full = ctx.options.mode == MODE_FULL_PRODUCTION_DRAFT
    batches = _norm_batches(run_project_emit_export_stage(st, ctx.token, lambda f, m: ctx.progress(0.6 * f, m)))
    deliverables: list[dict[str, Any]] = []
    review: list[str] = []
    for key in ("kdenlive_project", "otio_timeline"):
        if key in st.artifacts:
            deliverables.append({"kind": key, "path": st.artifacts[key], "status": "draft"})
    master = st.artifacts.get("rendered_video")
    if master and Path(master).is_file():
        target = float(get_effective_profile_settings(st.profile)["loudness_target_lufs"])
        tol = float(rules.get("loudness_tolerance_lu", 2.0))
        lufs = measure_loudness_lufs(master)
        measured_first_pass = lufs
        corrected = False
        if lufs is not None and abs(lufs - target) > tol and abs(lufs - target) <= 12.0:
            # Second pass: measured linear gain + true-peak limiter on the master we rendered
            ctx.progress(0.65, f"Correcting master loudness {lufs:.1f} -> {target:.1f} LUFS")
            tp = float((st.profile.get("audio_rules", {}) or {}).get("true_peak_dbtp", -1.5))
            tmp = Path(master).with_name(Path(master).stem + "_loudfix" + Path(master).suffix)
            limit = 10 ** (tp / 20.0)
            if _ffmpeg(["-i", master, "-c:v", "copy", "-af", f"volume={target - lufs:.2f}dB,alimiter=limit={limit:.4f}:level=disabled", "-c:a", "aac", "-b:a", "192k", str(tmp)]):
                tmp.replace(master)
                lufs = measure_loudness_lufs(master)
                corrected = True
        ok = lufs is not None and abs(lufs - target) <= tol
        deliverables.append({"kind": "master", "path": master, "integrated_lufs": lufs, "first_pass_lufs": measured_first_pass, "loudness_corrected": corrected, "target_lufs": target, "loudness_ok": ok, "sha256": compute_file_sha256(master, use_cache=False)})
        if not ok:
            review.append(f"Master loudness {lufs} LUFS vs target {target} LUFS")
    want_social = flags.get("social") or (full and rules.get("social_versions"))
    want_hl = flags.get("highlights") or (full and rules.get("highlight_reel", False))
    highlights = st.metadata.get("highlights", [])
    if (want_social or want_hl) and not (master and Path(master).is_file()):
        review.append("Social/highlight renders planned but not rendered (master export not requested/approved)")
    if master and Path(master).is_file():
        out = Path(st.output_dir)
        if want_social:
            ctx.token.check("social render")
            ctx.progress(0.75, "Rendering 9:16 social version")
            social = out / f"{st.sequence_id}_social_9x16.mp4"
            ok = _ffmpeg(["-i", master, "-vf", "crop=ih*9/16:ih,scale=1080:1920:flags=lanczos,setsar=1", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k", str(social)])
            deliverables.append({"kind": "social_9x16", "path": str(social), "status": "rendered" if ok else "failed"})
        if want_hl and highlights:
            ctx.token.check("highlight render")
            ctx.progress(0.88, "Rendering highlight reel")
            windows = []
            for h in highlights:
                win = _master_window_to_timeline(st, h["start_sec"], h["end_sec"])
                if win and win[1] - win[0] >= 1.0:
                    windows.append(win)
            if windows:
                sel = "+".join(f"between(t,{a:.3f},{z:.3f})" for a, z in windows)
                reel = out / f"{st.sequence_id}_highlights.mp4"
                ok = _ffmpeg(["-i", master, "-vf", f"select='{sel}',setpts=N/FRAME_RATE/TB", "-af", f"aselect='{sel}',asetpts=N/SR/TB", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", str(reel)])
                deliverables.append({"kind": "highlight_reel", "path": str(reel), "windows_timeline_sec": windows, "status": "rendered" if ok else "failed"})
    manifest = {
        "sequence_id": st.sequence_id,
        "deliverables": deliverables,
        "publish_status": "not_published (requires explicit editor approval)",
        "export_presets": st.profile.get("export_presets", []),
    }
    mpath = write_json_atomic(Path(st.output_dir) / "delivery_manifest.json", manifest)
    st.artifacts["delivery_manifest_json"] = str(mpath)
    failed = [d for d in deliverables if d.get("status") == "failed"]
    return AgentOutcome(
        batches=batches,
        confidence=1.0 if not failed else 0.6,
        summary=f"{len(deliverables)} deliverables" + ("" if master else " (no master render)"),
        rationale="Editable project always emitted; renders verified for loudness and checksummed; nothing published",
        outputs={"delivery_manifest_json": str(mpath), **{k: st.artifacts[k] for k in ("kdenlive_project", "otio_timeline", "rendered_video") if k in st.artifacts}},
        review_items=review + [f"Render failed: {d['kind']}" for d in failed],
    )


HANDLERS: dict[str, Callable[[AgentContext], AgentOutcome]] = {
    "executive": run_executive_plan,
    "learning": run_learning_apply,
    "media_ingest": run_media_ingest,
    "synchronisation": run_synchronisation,
    "editorial_intelligence": run_editorial_intelligence,
    "multicam_director": run_multicam_director,
    "rough_cut": run_rough_cut,
    "audio_engineer": run_audio_engineer,
    "colourist": run_colourist,
    "captions": run_captions,
    "graphics": run_graphics,
    "vfx": run_vfx,
    "quality_control": run_quality_control,
    "distribution": run_distribution,
}

__all__ = [
    "HANDLERS",
    "MODE_ASSISTED",
    "MODE_AUTONOMOUS_FIRST_CUT",
    "MODE_FULL_PRODUCTION_DRAFT",
    "MODE_MANUAL",
    "AgentContext",
    "AgentOutcome",
    "InsufficientStorageError",
    "JobOptions",
    "compute_activation",
    "department_qc_checks",
    "parse_brief_flags",
    "score_highlights",
    "shape_intent_for_mode",
]
