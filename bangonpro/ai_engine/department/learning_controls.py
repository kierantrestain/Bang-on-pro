"""The editor's controls over department learning (``bop_learn`` and ``python -m bangonpro.ai_engine.mcp learn``).

Inspect lessons, skills, strategies and measured performance; approve, reject, disable, enable and roll
back; change learning scope, retention and switches; run one bounded optimisation round; export or
remove what was learned.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bangonpro.ai_engine.department import skills as sk
from bangonpro.ai_engine.department import strategies as strat
from bangonpro.ai_engine.department.intelligence import Intelligence, job_context


def performance(intel: Intelligence, agent: str | None = None) -> list[dict[str, Any]]:
    out = []
    for k in intel.knowledge(category="performance", agent=agent):
        c = k["data"].get("counters") or {}
        n = max(1.0, c.get("tasks", 0.0))
        out.append({"agent": k["agent"], "strategy_version": k["data"].get("strategy_version"), "format": k["scope_key"], "tasks": int(c.get("tasks", 0)),
                    "verified_rate": round(c.get("verified", 0) / n, 2), "defects_introduced_per_task": round((c.get("defects_introduced", 0) + c.get("defects_unresolved", 0)) / n, 2),
                    "edits_per_task": round(c.get("edits", 0) / n, 2), "rejected_edits": int(c.get("edits_rejected", 0)),
                    "defects_detected": int(c.get("defects_detected", 0)) if k["agent"] == "quality_control" else None, "human_minutes": "unknown (not recorded)"})
    return sorted(out, key=lambda r: (r["agent"], r["strategy_version"] or 0))


def learn_action(bt: Any, args: dict[str, Any]) -> dict[str, Any]:
    from bangonpro.ai_engine.mcp.execution import learning_line
    from bangonpro.ai_engine.mcp.protocol import ToolError

    intel = Intelligence.for_job(bt.ws.job)
    ctx = job_context(bt.ws.job)
    act = args["action"]
    kind = args.get("kind")
    try:
        if act == "status":
            from bangonpro.ai_engine.mcp.reasoning import reasoning_status

            rs = reasoning_status()
            return {"summary": intel.summary(), "line": learning_line(intel), "settings": intel.settings(), "context": {k: ctx[k] for k in ("series", "format", "episode")},
                    "providers": {"priority": rs["priority"], "active": rs.get("active"), "app_driven": rs["app_driven"],
                                  "states": {p: v["state"] for p, v in rs["providers"].items() if v["authorised"]}, "failover_events": rs["failover_events"]}}
        if act == "lessons":
            if args.get("agents") or args.get("text"):
                return {"lessons": intel.retrieve(ctx, list(args.get("agents") or []) or list(strat.STRATEGY_SPACE), text=args.get("text", ""))}
            return {"knowledge": [{k: r[k] for k in ("id", "category", "scope", "scope_key", "agent", "key", "statement", "confidence", "support", "contradict", "status", "version")}
                                  for r in intel.knowledge(agent=args.get("agent")) if r["category"] != "performance"][:100]}
        if act == "skills":
            return {"skills": [{k: s[k] for k in ("id", "version", "name", "agent", "status", "uses", "successes", "failures", "origin_job", "validated_on", "reason")} | {"steps": s["spec"]["steps"]}
                               for s in intel.latest_skills(("candidate", "validated", "active", "retired"))]}
        if act == "strategies":
            return {"space": strat.STRATEGY_SPACE, "active": strat.active_versions(intel), "versions": strat.list_strategies(intel, args.get("agent"))}
        if act == "performance":
            return {"performance": performance(intel, args.get("agent"))}
        if act == "history":
            q = "SELECT * FROM strategy_history" + (" WHERE agent=?" if args.get("agent") else "") + " ORDER BY rowid DESC LIMIT 50"
            return {"history": [dict(r) for r in intel.db.execute(q, (args["agent"],) if args.get("agent") else ())]}
        if act == "optimise":
            return {"results": strat.optimise(bt, intel, ctx)}
        if act in ("approve", "reject", "disable", "enable", "rollback"):
            if kind == "strategy" or act == "rollback":
                agent = args.get("agent") or ""
                if act == "approve":
                    return {"strategy": strat.approve(intel, agent, int(args["version"]))}
                if act == "reject":
                    strat.reject(intel, agent, int(args["version"]))
                    return {"rejected": [agent, args["version"]]}
                if act == "disable":
                    return {"active_version": strat.rollback(intel, agent, 0, reason="disabled by the editor")}
                if act == "rollback":
                    return {"active_version": strat.rollback(intel, agent, args.get("version"))}
                raise ToolError("INVALID_ARGUMENT", "enable a strategy with approve or rollback to its version")
            if kind == "skill":
                status = {"approve": "active", "enable": "validated", "reject": "retired", "disable": "retired"}[act]
                return {"skill": {k: v for k, v in sk.set_skill_status(intel, args["id"], status).items() if k != "spec"}}
            status = {"approve": "active", "enable": "validated", "reject": "rejected", "disable": "disabled"}[act]
            return {"knowledge": intel.set_status(args["id"], status, f"{act} by the editor")}
        if act == "settings":
            if args.get("setting"):
                return {"settings": intel.set_setting(args["setting"], args.get("value"))}
            return {"settings": intel.settings()}
        if act == "export":
            target = Path(args["path"]).expanduser() if args.get("path") else bt.ws.job_dir / "learning_export.json"
            if args.get("path") and not any(str(target.resolve()).startswith(str(r)) for r in bt.ws.allowed_roots):
                raise ToolError("PATH_NOT_ALLOWED", "Export only into the job folder or an approved folder")
            return intel.export(target, series=args.get("series"))
        if act == "forget":
            return {"removed": intel.forget(series=args.get("series"), everything=bool(args.get("everything")))}
    except (KeyError, ValueError, strat.StrategyError, sk.SkillError) as exc:
        raise ToolError("INVALID_ARGUMENT", str(exc)[:300]) from exc
    raise ToolError("INVALID_ARGUMENT", f"Unknown action '{act}'")


__all__ = ["learn_action", "performance"]
