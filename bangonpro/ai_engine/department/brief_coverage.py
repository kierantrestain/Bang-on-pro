"""Instruction coverage and per-job reasoning reporting (V1.6).

Two promises to the editor:

1. **No instruction is silently dropped.** The brief is split into clauses. A clause
   a local rule understands is *handled*. A clause no rule understands is sent - with
   the full brief for context - to the editor's authorised subscription through the
   shared provider router (same priority, failover, budget and ledger as every other
   reasoning call). The router maps it onto operations the department really performs,
   or says plainly that it is unsupported. Mapped clauses are applied by appending the
   canonical phrase the local rules read; everything else is listed in the Production
   panel as "Not done from your instructions", with the reason.
2. **Usage is reported, never invented.** ``job_reasoning_summary`` reads the router's
   own ledger rows for this job (``task_id`` = job id): which provider answered, how
   many calls succeeded, failover events and the usage the client itself reported.
   Zero calls is reported as local processing, with why no call was needed.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import re
from typing import Any

# Clause boundaries: sentences, commas, and "and"/"then" joining a new instruction verb.
_VERBS = (
    "add|make|cut|remove|keep|use|put|open|start|end|finish|tighten|trim|include|show|zoom|blur|match|colou?r|grade|normali[sz]e|mix|level|"
    "clean|caption|subtitle|insert|create|give|do|try|avoid|lose|drop|speed|slow|punch|highlight|emphasi[sz]e|feature|place|move|delete|reduce|boost"
)
_SPLIT = re.compile(rf"[.;!?\n]+|,\s*(?:and\s+|then\s+)?|\s+(?:and then|then|and also|also|plus)\s+|\s+and\s+(?=(?:{_VERBS})\b)", re.IGNORECASE)
_VAGUE = re.compile(
    r"^(please|thanks?(\s+you)?|cheers|for\s+(youtube|the\s+channel|our\s+channel|social(\s+media)?)|(make\s+it\s+)?(look\s+)?(good|great|nice|better|awesome|professional|polished|clean)"
    r"|do\s+your\s+best|as\s+usual|like\s+(always|usual)|thank\s+you)$",
    re.IGNORECASE,
)

# What a reasoning answer may map a clause onto: each is a phrase the local rules already act on.
CANONICAL: dict[str, str] = {
    "tight_pacing": "Tight pacing, lose the dead air.",
    "natural_pacing": "Keep natural pauses.",
    "remove_fillers": "Remove filler words.",
    "captions": "Add captions.",
    "lower_thirds": "Add lower thirds.",
    "title_card": "Add a title card.",
    "topic_cards": "Add topic cards.",
    "motion_graphics": "Add motion graphics and callouts.",
    "colour": "Match the colour and grade.",
    "blur_background": "Blur the background.",
    "audio_cleanup": "Clean up the audio levels.",
    "social_clips": "Make vertical social clips.",
    "highlights": "Make highlights of the best moments.",
    "export_master": "Render the master.",
}


def split_clauses(brief: str) -> list[str]:
    return [c.strip(" -–—:\"'") for c in _SPLIT.split(brief or "") if c and c.strip(" -–—:\"'")]


def _locally_handled(clause: str) -> bool:
    from bangonpro.ai_engine.department.agents import parse_brief_flags
    from bangonpro.ai_engine.department.editorial_brief import interpret_brief
    from bangonpro.ai_engine.intent import explicit_negations, parse_production_instruction

    if any(parse_brief_flags(clause).values()) or any(explicit_negations(clause).values()):
        return True
    d = interpret_brief(clause)["directives"]
    if any(v is True for v in d.values()) or d.get("target_duration_min") or d.get("clip_length_sec"):
        return True
    intent = parse_production_instruction(clause, allow_default_empty=True)
    actions = ("sync_cameras", "transcribe", "remove_setup_delay", "remove_silence", "build_rough_cut", "multicam_switch", "restore_audio",
               "add_captions", "add_lower_thirds", "export_video")
    return any(bool(getattr(intent, a, False)) for a in actions) and not intent.needs_clarification


def brief_coverage(brief: str) -> dict[str, Any]:
    """``{handled: [...], vague: [...], not_handled: [{text}]}`` for the editor's own words."""
    out: dict[str, Any] = {"handled": [], "vague": [], "not_handled": [], "interpreted": []}
    for clause in split_clauses(brief):
        if _locally_handled(clause):
            out["handled"].append(clause)
        elif _VAGUE.match(clause.strip().lower()) or len(re.findall(r"[A-Za-z']+", clause)) < 2:
            out["vague"].append(clause)
        else:
            out["not_handled"].append({"text": clause, "reason": ""})
    return out


def interpret_unhandled(brief: str, coverage: dict[str, Any], *, job_id: str) -> tuple[str, dict[str, Any]]:
    """Ask the authorised subscription about clauses no local rule understood.

    Returns the brief with canonical phrases appended for clauses that map onto real operations,
    and the updated coverage. Without an authorised, consented provider nothing is sent and each
    clause keeps an explicit "not applied" reason.
    """
    pending = [c["text"] for c in coverage.get("not_handled", [])]
    if not pending:
        return brief, coverage
    try:
        from bangonpro.ai_engine.mcp.reasoning import CliReasoner, ReasonerError, enabled, load_config

        if not enabled(load_config()):
            raise ReasonerError("Connected subscriptions are switched off (Local Only)")
        router = CliReasoner()
        if not router.authorised():
            raise ReasonerError("No subscription is connected")
        router.begin_task(job_id)
    except Exception as exc:  # noqa: BLE001 - unavailable reasoning keeps the local plan, and says so
        for c in coverage["not_handled"]:
            c["reason"] = f"Not applied: no local rule covers this, and subscription reasoning is unavailable ({str(exc)[:120]}). Rephrase it, or connect a subscription in AI Providers."
        coverage["reasoning_unavailable"] = str(exc)[:200]
        return brief, coverage

    keys = ", ".join(sorted(CANONICAL))

    def prompt(_policy: dict[str, Any]) -> str:
        return (
            "You help Bang On Pro, a video editor's production assistant, understand parts of an editing brief its local rules did not recognise. "
            "Reply with ONE JSON object and nothing else: {\"clauses\": [{\"text\": <clause>, \"maps_to\": [<operation keys>], \"target_minutes\": <number or null>, "
            "\"unsupported_reason\": <short sentence or null>}]}. Use only these operation keys: " + keys + ". "
            "Map a clause only when one of these operations genuinely achieves it; otherwise leave maps_to empty and give a short, honest unsupported_reason "
            "(for example footage or assets that do not exist, or an effect the editor must do by hand).\n"
            f"Full brief: {brief[:1500]}\nClauses to interpret: {json.dumps(pending)[:1500]}"
        )

    try:
        answer = router.plan(prompt, purpose="interpret_brief", agent="executive")
    except Exception as exc:  # noqa: BLE001 - provider failure: keep the local plan, explain it
        for c in coverage["not_handled"]:
            c["reason"] = f"Not applied: subscription reasoning failed ({str(exc)[:120]})."
        coverage["reasoning_error"] = str(exc)[:200]
        return brief, coverage
    by_text = {str(r.get("text", "")).strip().lower(): r for r in (answer.get("clauses") or []) if isinstance(r, dict)}
    additions: list[str] = []
    still: list[dict[str, Any]] = []
    for c in coverage["not_handled"]:
        r = by_text.get(c["text"].strip().lower()) or {}
        maps = [k for k in (r.get("maps_to") or []) if k in CANONICAL]
        minutes = r.get("target_minutes")
        if isinstance(minutes, (int, float)) and 0 < float(minutes) < 600:
            additions.append(f"Target {float(minutes):g} minutes.")
            maps.append(f"target_{float(minutes):g}_minutes")
        additions.extend(CANONICAL[k] for k in maps if k in CANONICAL)
        if maps:
            coverage["interpreted"].append({"text": c["text"], "maps_to": maps, "provider": router.provider})
        else:
            reason = str(r.get("unsupported_reason") or "The subscription could not map this to an operation Bang On Pro performs.")
            still.append({"text": c["text"], "reason": f"Not applied: {reason[:240]}", "provider": router.provider})
    coverage["not_handled"] = still
    effective = brief.rstrip()
    if additions:
        effective = f"{effective}{'' if effective.endswith('.') else '.'} {' '.join(dict.fromkeys(additions))}".strip()
    return effective, coverage


def job_reasoning_summary(job_id: str, coverage: dict[str, Any] | None = None) -> dict[str, Any]:
    """What reasoning this job actually used, from the router's ledger (nothing estimated)."""
    try:
        from bangonpro.ai_engine.mcp import reasoning as r

        rows = [x for x in r._ledger_rows(0) if x.get("task_id") == job_id and x.get("kind") != "started"]
        state = r._load_state()
        status = r.reasoning_status()
    except Exception as exc:  # noqa: BLE001 - reporting must never break a production
        return {"calls": 0, "mode": "unknown", "message": "", "detail": f"Reasoning status unavailable: {exc}"}
    ok = [x for x in rows if x.get("ok")]
    titles = {p: (v or {}).get("title", p) for p, v in (status.get("providers") or {}).items()}
    providers = list(dict.fromkeys(titles.get(x.get("provider"), x.get("provider")) for x in ok))
    failovers = [e for e in (state.get("events") or []) if e.get("task_id") == job_id]
    tokens = 0
    usage_reported = False
    for x in ok:
        u = x.get("usage")
        if isinstance(u, dict):
            inner = u.get("usage") if isinstance(u.get("usage"), dict) else u
            for k in ("input_tokens", "output_tokens", "total_tokens"):
                if isinstance(inner.get(k), (int, float)) and k != "total_tokens":
                    tokens += int(inner[k])
                    usage_reported = True
    connected = [titles.get(p, p) for p in (status.get("priority") or []) if (status.get("providers") or {}).get(p, {}).get("authorised")]
    cov = coverage or {}
    out: dict[str, Any] = {"calls": len(rows), "successful_calls": len(ok), "providers": providers, "failovers": len(failovers),
                           "tokens_reported": tokens if usage_reported else None, "connected": connected,
                           "last_used_utc": ok[-1].get("at") if ok else None, "purposes": sorted({str(x.get("purpose")) for x in rows})}
    if ok:
        out["mode"] = "subscription"
        msg = f"Subscription reasoning: {', '.join(providers)} · {len(ok)} successful call{'s' if len(ok) != 1 else ''}"
        msg += f" · {tokens:,} tokens reported by the client" if usage_reported else " · usage not reported by the client"
        if failovers:
            last = failovers[-1]
            msg += f" · switched from {titles.get(last.get('from'), last.get('from'))} ({last.get('kind')})"
        out["message"] = msg
    elif rows:
        out["mode"] = "subscription_failed"
        out["message"] = f"Subscription reasoning was attempted {len(rows)} time(s) but did not answer; everything else ran on this Mac."
    else:
        out["mode"] = "local"
        if cov.get("not_handled") and cov.get("reasoning_unavailable"):
            out["message"] = "Processed on this Mac. Some instructions needed reasoning, but no connected subscription was available (AI Providers…)."
        elif connected:
            out["message"] = f"Processed on this Mac: your instructions were understood locally, so {connected[0]} was not needed (0 subscription calls)."
        else:
            out["message"] = "Processed on this Mac (no subscription connected, none was needed)."
    out["detail"] = json.dumps({k: out[k] for k in ("calls", "successful_calls", "providers", "failovers", "tokens_reported", "connected", "purposes")})
    return out


__all__ = ["CANONICAL", "brief_coverage", "interpret_unhandled", "job_reasoning_summary", "split_clauses"]
