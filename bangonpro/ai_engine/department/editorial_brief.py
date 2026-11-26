"""Executive brief interpretation, conflict resolution, plan confidence and final verification (V1.2.2).

* ``interpret_brief`` turns the editor's brief into flags, pacing directives, requested
  deliverables and explicit negations, and detects **conflicts** (e.g. "keep every
  word" with "tight cut", social clips without an export) with a stated resolution.
* ``build_executive_plan`` gives every agent an explicit prior confidence, its
  dependencies and the reason it depends on them, and escalates to human review when
  confidence is low or a conflict cannot be resolved automatically.
* ``verify_draft_against_brief`` is the Executive final verifier: it checks the
  finished draft against the brief (requested deliverables present, QC errors, captions
  withheld, target duration, "keep every word" honoured, protected pauses retained).

Priors are planning estimates from what is known before the run (inputs, installed
models, profile); they are labelled as such and never reported as measurements.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import re
from typing import Any

LOW_CONFIDENCE = 0.6

_DIRECTIVE_PATTERNS: dict[str, str] = {
    "keep_every_word": (
        r"\b(keep|don'?t\s+(?:cut|remove|lose|drop)|preserve|leave\s+in)\s+(?:every|all(?:\s+the)?|each)\s+"
        r"(?:single\s+)?(?:words?|lines?|dialogue|sentences?)\b|\bverbatim\b|\bunedited\s+dialogue\b"
    ),
    "tight": r"\b(tight(?:en|er|ly)?|snappy|punchy|fast[\s-]paced|pacey|brisk|quick[\s-]fire|lose\s+the\s+(?:dead\s*air|gaps))\b",
    "loose": (
        r"\b(natural\s+pac\w*|relaxed\s+pac\w*|breathing\s+room|don'?t\s+rush|leave\s+(?:the\s+)?(?:pauses|breaths?)|"
        r"keep\s+(?:the\s+|all\s+(?:the\s+)?)?(?:pauses|silences?|breaths?)|slow(?:er)?\s+pac\w*|looser)\b"
    ),
    "explicit_silence_removal": r"\b(remove|cut|trim|lose)\s+(?:the\s+|all\s+(?:the\s+)?)?(silence|silences|dead\s*air|pauses?|gaps?)\b",
    "title_card": r"\b(title\s+card|episode\s+title|opener|intro\s+card)\b",
    "topic_cards": r"\b(topic|story|segment)\s+(cards?|titles?|straps?|headers?)\b",
}
_MINUTES_RE = re.compile(r"\b(?:under|less\s+than|no\s+(?:more|longer)\s+than|max(?:imum)?|around|about|roughly|exactly)?\s*(\d+(?:\.\d+)?)\s*(?:-\s*)?min(?:ute)?s?\b")
_CLIP_SECONDS_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:-\s*)?(?:sec(?:ond)?s?|s)\b(?:\s+\w+){0,2}?\s+(?:clips?|shorts?|reels?|teasers?|versions?)")


def interpret_brief(brief: str, *, export: bool = False, n_video: int | None = None, neural_asr: bool | None = None) -> dict[str, Any]:
    """Deterministic brief reading: flags, directives, deliverables, negations and conflicts."""
    from bangonpro.ai_engine.department.agents import parse_brief_flags
    from bangonpro.ai_engine.intent import explicit_negations

    low = (brief or "").lower()
    flags = parse_brief_flags(brief)
    neg = explicit_negations(brief)
    d = {k: bool(re.search(p, low)) for k, p in _DIRECTIVE_PATTERNS.items()}
    pacing = "natural"
    if d["tight"] and not (d["loose"] or neg["silence"]):
        pacing = "tight"
    elif d["loose"] or neg["silence"]:
        pacing = "loose" if d["loose"] else "natural"
    target_min = None
    m = _MINUTES_RE.search(low)
    if m:
        target_min = float(m.group(1))
    clip_sec = None
    m2 = _CLIP_SECONDS_RE.search(low)
    if m2:
        clip_sec = float(m2.group(1))
    deliverables = {
        "editable_project": True,
        "captions": flags.get("captions", False) and not neg["captions"],
        "lower_thirds": bool(re.search(r"\b(lower[\s-]*thirds?|name\s+(?:titles?|straps?)|chyrons?|standard\s+graphics)\b", low)) and not neg["graphics"],
        "title_card": d["title_card"],
        "topic_cards": d["topic_cards"],
        "social_clips": flags.get("social", False),
        "highlights": flags.get("highlights", False),
        "master_export": flags.get("export", False) or bool(export),
    }
    conflicts: list[dict[str, Any]] = []

    def conflict(code: str, message: str, resolution: str, severity: str = "info", escalate: bool = False) -> None:
        conflicts.append({"code": code, "message": message, "resolution": resolution, "severity": severity, "needs_human_review": escalate})

    if d["keep_every_word"] and (d["tight"] or d["explicit_silence_removal"]):
        conflict(
            "KEEP_EVERY_WORD_VS_TIGHT_CUT",
            "The brief asks to keep every word and also for a tight cut",
            "Every word is kept (no false-start, stutter, repeat or filler removal); the tight pacing applies to pauses only, which are compressed to the tight target",
        )
    if d["tight"] and (d["loose"] or neg["silence"]):
        conflict(
            "PACING_CONTRADICTION",
            "The brief asks for a tight cut but also to keep the pauses",
            "The explicit instruction to keep pauses wins over the general pacing adjective; tightening is limited to setup delay and disfluencies",
        )
    if (deliverables["social_clips"] or deliverables["highlights"]) and not deliverables["master_export"]:
        conflict(
            "SOCIAL_WITHOUT_EXPORT",
            "Social clips / highlights are requested but no master export",
            "Vertical clips are built as editable projects with checked review proxies; final-quality renders follow the approved master export",
            "info",
            False,
        )
    if flags.get("captions") and neg["captions"]:
        conflict("CAPTIONS_CONTRADICTION", "The brief both asks for and excludes captions", "The explicit exclusion wins: no captions")
    if deliverables["captions"] and neural_asr is False:
        conflict(
            "CAPTIONS_NEED_SPOKEN_TRANSCRIPT",
            "Captions are requested but no local Whisper model is installed",
            "Captions are withheld in autonomous modes instead of being faked from a speech-activity transcript; install a Whisper model and rerun Captions",
            "warning",
            True,
        )
    if n_video is not None and n_video <= 1 and (flags.get("colour") or re.search(r"\b(multicam|switch\s+cameras?|cut\s+between)\b", low)):
        conflict("SINGLE_CAMERA", "Camera matching or switching is requested with a single video angle", "Single-angle edit; colour work is limited to exposure")
    if target_min is not None:
        conflict("TARGET_DURATION", f"Target duration {target_min:g} min", "The Executive final check compares the draft duration with the target", "info")
    return {
        "flags": flags,
        "negations": neg,
        "directives": {**d, "pacing": pacing, "target_duration_min": target_min, "clip_length_sec": clip_sec},
        "deliverables": deliverables,
        "conflicts": conflicts,
    }


# ---------------------------------------------------------------------------
# Executive plan: per-agent prior confidence and dependency reasoning
# ---------------------------------------------------------------------------
DEPENDENCY_REASONS: dict[str, str] = {
    "executive": "Plans first: nothing runs before the brief is interpreted",
    "learning": "Applies approved series preferences before any editorial decision is made",
    "media_ingest": "Needs the plan (which inputs, how much storage the requested renders need)",
    "synchronisation": "Needs verified, probed media (roles, audio streams) from Media Ingest",
    "editorial_intelligence": "Needs every microphone on one synchronised master clock",
    "multicam_director": "Needs speaker turns, talk time and reaction moments from Editorial Intelligence",
    "rough_cut": "Needs pause/speech analysis (Editorial Intelligence) and the camera plan (Director)",
    "audio_engineer": "Processes the dialogue clips the Rough Cut placed on A1",
    "colourist": "Grades the shots the Rough Cut selected",
    "captions": "Times captions to the edited timeline, so it needs the final Rough Cut",
    "graphics": "Places lower thirds where the named speaker is on screen in the Rough Cut",
    "vfx": "Masks the shots the Rough Cut selected",
    "quality_control": "Verifies the finished draft, so it waits for every finishing agent",
    "distribution": "Emits and renders only what Quality Control has verified",
}
_GENERIC_ROLES = {"host", "guest", "panel", "contestant", "interviewer", "subject", "presenter", "anchor", "speaker", "studio", "reporter", "caller"}


def _named_speakers(profile: dict[str, Any]) -> bool:
    names = (profile.get("graphics_rules", {}) or {}).get("speaker_names") or {}
    if names:
        return True
    labels = list((profile.get("speaker_camera_map") or {}).keys())
    return bool(labels) and all(lbl.strip().lower().split(" ")[0] not in _GENERIC_ROLES for lbl in labels)


def build_executive_plan(
    *,
    brief: str,
    mode: str,
    activation: dict[str, dict[str, Any]],
    profile: dict[str, Any],
    inputs: dict[str, str],
    neural_asr_model: str | None,
    export: bool,
    intent_confidence: float,
) -> dict[str, Any]:
    from bangonpro.ai_engine.department.registry import AGENT_ORDER, AGENTS_BY_ID

    n_video = sum(1 for p in inputs.values() if not str(p).lower().endswith((".wav", ".mp3", ".flac", ".aac", ".m4a")))
    neural = bool(neural_asr_model)
    brief_doc = interpret_brief(brief, export=export, n_video=n_video, neural_asr=neural)
    spk_map = profile.get("speaker_camera_map") or {}
    mapped = [c for c in spk_map.values() if c in inputs]
    priors: dict[str, tuple[float, list[str]]] = {
        "executive": (max(0.0, min(1.0, float(intent_confidence or 0.0))), []),
        "learning": (0.98, []),
        "media_ingest": (0.95, []),
        "synchronisation": (0.9 if len(inputs) > 1 else 1.0, [] if len(inputs) > 1 else ["Single source: nothing to align"]),
        "editorial_intelligence": (0.9, []) if neural else (0.6, ["No Whisper model installed: speech regions only, no words (pauses can't be typed as questions, no false-start removal)"]),
        "multicam_director": (0.85, []) if (len(mapped) >= len(spk_map) and spk_map) else (0.65, ["Profile speaker map does not cover the supplied angles: speakers are auto-mapped by microphone dominance"]),
        "rough_cut": (0.85 if neural else 0.75, []),
        "audio_engineer": (0.9, []),
        "colourist": (0.8 if n_video > 1 else 0.7, [] if n_video > 1 else ["Single angle: exposure only"]),
        "captions": (0.9, []) if neural else (0.1, ["No spoken transcript: captions will be withheld in autonomous modes"]),
        "graphics": (0.85, []) if _named_speakers(profile) else (0.65, ["Only role labels (Host/Guest) known: add speaker names in graphics_rules.speaker_names"]),
        "vfx": (0.6, ["Static subject matte from one frame; check moving subjects"]),
        "quality_control": (0.95, []),
        "distribution": (0.95, []) if (export or not (brief_doc["deliverables"]["social_clips"] or brief_doc["deliverables"]["highlights"])) else (0.7, ["Social/highlight renders need a master export"]),
    }
    agents: list[dict[str, Any]] = []
    escalations: list[str] = []
    for a in AGENT_ORDER:
        act = activation.get(a, {"active": False, "reason": ""})
        conf, risks = priors.get(a, (0.8, []))
        deps = list(AGENTS_BY_ID[a].depends_on)
        inactive_deps = [x for x in deps if not activation.get(x, {}).get("active") and x not in ("executive",)]
        if act.get("active") and inactive_deps:
            risks = risks + [f"Dependency not active in this job: {', '.join(inactive_deps)} (uses whatever state is already available)"]
            conf = min(conf, 0.7)
        row = {
            "agent_id": a,
            "active": bool(act.get("active")),
            "activation_reason": act.get("reason", ""),
            "depends_on": deps,
            "dependency_reason": DEPENDENCY_REASONS.get(a, ""),
            "prior_confidence": round(conf, 3),
            "confidence_basis": "planning prior (inputs, installed models, profile), not a measurement",
            "risks": risks,
        }
        if row["active"] and conf < LOW_CONFIDENCE and a != "executive":
            row["needs_human_review"] = True
            escalations.append(f"{a}: low planning confidence {conf:.2f} ({'; '.join(risks) or 'see plan'})")
        agents.append(row)
    for c in brief_doc["conflicts"]:
        if c["needs_human_review"]:
            escalations.append(f"{c['code']}: {c['resolution']}")
    active_conf = [r["prior_confidence"] for r in agents if r["active"] and r["agent_id"] != "executive"]
    overall = round(min([float(intent_confidence or 0.0)] + ([sum(active_conf) / len(active_conf)] if active_conf else [])), 4)
    return {
        "mode": mode,
        "brief": brief,
        "interpretation": brief_doc,
        "agents": agents,
        "overall_confidence": overall,
        "escalations": escalations,
        "needs_human_review": bool(escalations) or overall < LOW_CONFIDENCE,
    }


# ---------------------------------------------------------------------------
# Final verification of the draft against the brief
# ---------------------------------------------------------------------------
def verify_draft_against_brief(st: Any, job_doc: dict[str, Any]) -> dict[str, Any]:
    from pathlib import Path

    plan = st.metadata.get("executive_plan") or {}
    interp = plan.get("interpretation") or interpret_brief(st.instruction or "", export=bool(st.intent.export_video))
    want = interp["deliverables"]
    checks: list[dict[str, Any]] = []

    def check(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    agents = job_doc.get("agents", {}) or {}
    arts = job_doc.get("artifacts", {}) or {}
    kd = arts.get("kdenlive_project") or st.artifacts.get("kdenlive_project")
    check("editable_project", "pass" if kd and Path(kd).is_file() else "fail", kd or "no editable Kdenlive draft was emitted")
    failed = [a for a, r in agents.items() if r.get("status") in ("failed", "blocked")]
    check("agents_completed", "fail" if failed else "pass", ("failed/blocked: " + ", ".join(failed)) if failed else "all active agents completed")
    if st.intent.add_captions or want.get("captions"):
        cap = agents.get("captions", {})
        if st.captions and st.artifacts.get("captions_srt"):
            check("captions", "pass", f"{len(st.captions)} cues")
        elif cap.get("skipped_reason") == "no_spoken_transcript" or (not st.asr_backend_report.is_neural_inference):
            check("captions", "needs_review", "Captions withheld: no spoken transcript (install a Whisper model and rerun Captions)")
        else:
            check("captions", "fail", "Captions requested but none were produced")
    if st.intent.add_lower_thirds or want.get("lower_thirds"):
        check("lower_thirds", "pass" if st.lower_thirds else "needs_review", f"{len(st.lower_thirds)} lower thirds" if st.lower_thirds else "No lower thirds placed (no named speaker on screen long enough)")
    manifest: dict[str, Any] = {}
    mpath = st.artifacts.get("delivery_manifest_json")
    if mpath and Path(mpath).is_file():
        import json

        try:
            manifest = json.loads(Path(mpath).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
    kinds = {d.get("kind"): d for d in manifest.get("deliverables", []) or []}
    if want.get("master_export") or st.intent.export_video:
        m = kinds.get("master")
        if not m:
            check("master_export", "fail", "Export requested but no master was rendered")
        else:
            check("master_export", "pass" if m.get("loudness_ok") else "needs_review", f"integrated {m.get('integrated_lufs')} LUFS (target {m.get('target_lufs')})")
    if want.get("social_clips"):
        clips = st.metadata.get("social_clips") or []
        rendered = kinds.get("social_9x16", {}).get("status") == "rendered"
        if not clips:
            check("social_clips", "needs_review", "No social clip could be selected")
        elif want.get("master_export") or st.intent.export_video:
            check("social_clips", "pass" if rendered else "needs_review", f"{len(clips)} clip(s) selected; render {'done' if rendered else 'missing'}")
        else:
            check("social_clips", "needs_review", f"{len(clips)} clip(s) selected and timed, not rendered (no export requested)")
    if want.get("highlights"):
        hl = st.metadata.get("highlights") or []
        check("highlights", "pass" if hl else "needs_review", f"{len(hl)} highlight(s) selected")
    errors = [i for i in st.qc_report.issues if i.severity == "error"]
    check("qc_errors", "needs_review" if errors else "pass", f"{len(errors)} QC error(s): " + "; ".join(f"{i.code}" for i in errors[:5]) if errors else "no QC errors")
    target = interp["directives"].get("target_duration_min")
    if target and st.rough_cut_shots:
        dur_min = st.rough_cut_shots[-1].timeline_out_sec / 60.0
        check("target_duration", "pass" if dur_min <= target * 1.1 else "needs_review", f"draft {dur_min:.1f} min vs target {target:g} min")
    decisions = st.metadata.get("rough_cut_decisions") or {}
    if interp["directives"].get("keep_every_word") and decisions:
        n = len(decisions.get("disfluencies", []) or [])
        check("keep_every_word", "fail" if n else "pass", f"{n} word-level cut(s)" if n else "no words removed")
    pm = st.metadata.get("pacing_metrics") or {}
    if pm.get("protected_pauses"):
        ret = float(pm.get("protected_retention", 1.0))
        check("protected_pauses", "pass" if ret >= 0.9 else "needs_review", f"{pm.get('protected_retained')}/{pm.get('protected_pauses')} meaningful pauses kept")
    for c in interp.get("conflicts", []):
        if c.get("needs_human_review"):
            check(f"brief_conflict:{c['code']}", "needs_review", c["resolution"])
    verdict = "fail" if any(c["status"] == "fail" for c in checks) else ("needs_review" if any(c["status"] == "needs_review" for c in checks) else "pass")
    unmet = [f"{c['check']}: {c['detail']}" for c in checks if c["status"] != "pass"]
    return {"verdict": verdict, "checks": checks, "unmet": unmet, "summary": f"{verdict.upper()}: {sum(c['status'] == 'pass' for c in checks)}/{len(checks)} checks passed"}


__all__ = [
    "DEPENDENCY_REASONS",
    "LOW_CONFIDENCE",
    "build_executive_plan",
    "interpret_brief",
    "verify_draft_against_brief",
]
