"""Editor-facing production stages derived from real agent state (V1.6).

Editors do not need to know there are 14 agents. They need to know what the
production is doing now, what is finished, what failed and what is left. This
module folds the agents' recorded state (``job.json``) into eight plain stages.

Rules that keep the display truthful:

* A stage's state comes only from its agents' recorded states. Nothing is
  estimated, and no completion time is predicted.
* ``fraction`` is reported only when the running agent itself reported a
  measurable fraction strictly between 0 and 1 (frames analysed, bytes hashed,
  windows processed). Otherwise it is ``None`` and the app shows a busy
  indicator instead of an invented percentage.
* Stages whose agents are all inactive are ``skipped`` with the agents'
  activation reasons, so "why did nothing happen to my colour?" has an answer.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

from typing import Any

# (stage id, label shown to the editor, agents in that stage)
EDITOR_STAGES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("analyse", "Analysing footage", ("executive", "learning", "media_ingest", "synchronisation")),
    ("understand", "Transcribing and understanding dialogue", ("editorial_intelligence",)),
    ("rough_cut", "Building the rough cut", ("multicam_director", "rough_cut")),
    ("audio", "Improving audio", ("audio_engineer",)),
    ("colour", "Colour correction", ("colourist",)),
    ("graphics", "Captions and graphics", ("captions", "graphics", "vfx")),
    ("qc", "Quality control", ("quality_control",)),
    ("prepare", "Preparing edited sequence", ("distribution",)),
)
STAGE_OF_AGENT = {a: sid for sid, _, agents in EDITOR_STAGES for a in agents}
STAGE_LABELS = {sid: label for sid, label, _ in EDITOR_STAGES}

_FAILED = {"failed", "blocked", "cancelled", "interrupted"}


def editor_stages(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """``[{id, label, state, fraction, message, agents, problems}]`` for the job document."""
    agents = doc.get("agents") or {}
    finalising = doc.get("finalising") or {}
    job_status = str(doc.get("status") or "")
    out: list[dict[str, Any]] = []
    for sid, label, ids in EDITOR_STAGES:
        recs = [agents[a] for a in ids if a in agents]
        active = [r for r in recs if r.get("active")]
        problems = [
            {"agent": r.get("agent_id"), "message": str(r.get("error") or "")[:300], "help": r.get("help")}
            for r in active if r.get("status") in _FAILED and r.get("status") != "cancelled"
        ]
        states = {r.get("status") for r in active}
        fraction = None
        message = ""
        if not active:
            state = "skipped"
            message = "; ".join(sorted({str(r.get("activation_reason") or "") for r in recs if r.get("activation_reason")}))[:200]
        elif "running" in states:
            state = "running"
            run = next(r for r in active if r.get("status") == "running")
            message = str(run.get("message") or "")
            # Only a strictly partial, agent-reported fraction is measurable; 0 and 1 carry no information.
            done = sum(1 for r in active if r.get("status") in ("completed", "skipped"))
            p = run.get("progress")
            if isinstance(p, (int, float)) and 0.0 < float(p) < 1.0:
                fraction = round((done + float(p)) / len(active), 3)
        elif problems:
            state = "failed"
        elif states & {"cancelled", "interrupted"}:
            state = "stopped"
        elif states <= {"completed", "skipped"}:
            state = "done"
            reviews = sum(len(r.get("review_items") or []) for r in active)
            message = f"{reviews} item(s) to review" if reviews else ""
        else:
            state = "pending"
        out.append({"id": sid, "label": label, "state": state, "fraction": fraction, "message": message, "agents": [r.get("agent_id") for r in active], "problems": problems})
    # Finalisation (source verification, building the sequence, final checks) belongs to the last stage.
    last = out[-1]
    if finalising and job_status == "running" and last["state"] in ("done", "skipped", "pending"):
        last["state"] = "running"
        last["message"] = str(finalising.get("message") or "")
        f = finalising.get("fraction")
        last["fraction"] = round(float(f), 3) if isinstance(f, (int, float)) and 0.0 < float(f) < 1.0 else None
    return out


def current_stage(stages: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((s for s in stages if s["state"] == "running"), None)


def stage_counts(stages: list[dict[str, Any]]) -> dict[str, int]:
    counted = [s for s in stages if s["state"] != "skipped"]
    return {"total": len(counted), "done": sum(1 for s in counted if s["state"] == "done"), "failed": sum(1 for s in counted if s["state"] == "failed")}


__all__ = ["EDITOR_STAGES", "STAGE_LABELS", "STAGE_OF_AGENT", "current_stage", "editor_stages", "stage_counts"]
