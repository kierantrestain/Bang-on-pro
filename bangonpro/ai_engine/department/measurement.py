"""Editing-time measurement for the 60-85 % human-time-reduction objective.

Records *measured* human active editing sessions (manual baseline vs
agent-assisted) per deliverable and computes::

    saving % = (manual_baseline_time - assisted_human_time) / manual_baseline_time x 100

Human time includes setup, review, corrections and final approval. Idle gaps
longer than ``idle_cutoff_sec`` between recorded activity events are excluded.
A separate *modelled estimate* (operation counts x per-task manual times) is
provided only as a planning aid and is always labelled as not measured.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bangonpro.ai_engine.department.checkpoint import write_json_atomic

SESSION_KINDS = ("manual_baseline", "agent_assisted")
ACTIVITY_CATEGORIES = ("setup", "editing", "review", "correction", "approval")

# Planning-aid manual task times (seconds) used ONLY for the modelled estimate.
MODELLED_MANUAL_SECONDS: dict[str, float] = {
    "sync_clips": 120.0,
    "import_media": 20.0,
    "place_clip": 25.0,
    "select_camera": 8.0,
    "delete_clip": 12.0,
    "trim_clip": 10.0,
    "add_audio_filter": 45.0,
    "set_audio_level": 20.0,
    "add_caption": 9.0,
    "add_effect": 40.0,
    "apply_mask": 300.0,
    "add_marker": 4.0,
    "export_sequence": 60.0,
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class MeasurementLog:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / "editing_time_sessions.json"

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schema_version": "1.0", "sessions": []}

    def _save(self, doc: dict[str, Any]) -> None:
        write_json_atomic(self.path, doc)

    def start(self, *, deliverable_id: str, kind: str, editor: str, job_id: str | None = None, footage: str = "", evidence_class: str = "physical_hardware") -> str:
        if kind not in SESSION_KINDS:
            raise ValueError(f"kind must be one of {SESSION_KINDS}")
        doc = self._load()
        sid = f"ses_{uuid.uuid4().hex[:10]}"
        doc["sessions"].append({"session_id": sid, "deliverable_id": deliverable_id, "kind": kind, "editor": editor, "job_id": job_id, "footage": footage, "evidence_class": evidence_class, "started_at_utc": _now(), "events": [], "finished_at_utc": None, "quality_rating": None, "notes": ""})
        self._save(doc)
        return sid

    def activity(self, session_id: str, category: str, *, at_utc: str | None = None, detail: str = "") -> None:
        if category not in ACTIVITY_CATEGORIES:
            raise ValueError(f"category must be one of {ACTIVITY_CATEGORIES}")
        doc = self._load()
        ses = next(s for s in doc["sessions"] if s["session_id"] == session_id)
        ses["events"].append({"ts_utc": at_utc or _now(), "category": category, "detail": detail})
        self._save(doc)

    def finish(self, session_id: str, *, quality_rating: float | None = None, notes: str = "", at_utc: str | None = None) -> None:
        doc = self._load()
        ses = next(s for s in doc["sessions"] if s["session_id"] == session_id)
        ses["finished_at_utc"] = at_utc or _now()
        ses["quality_rating"] = quality_rating
        ses["notes"] = notes
        self._save(doc)

    def sessions(self) -> list[dict[str, Any]]:
        return list(self._load()["sessions"])


def active_seconds(session: dict[str, Any], idle_cutoff_sec: float = 300.0) -> dict[str, float]:
    """Sum active time between consecutive activity events, ignoring idle gaps."""
    times = [datetime.fromisoformat(session["started_at_utc"])]
    cats = ["setup"]
    for e in session.get("events", []):
        times.append(datetime.fromisoformat(e["ts_utc"]))
        cats.append(e["category"])
    if session.get("finished_at_utc"):
        times.append(datetime.fromisoformat(session["finished_at_utc"]))
        cats.append("approval")
    per: dict[str, float] = {c: 0.0 for c in ACTIVITY_CATEGORIES}
    for i in range(1, len(times)):
        gap = (times[i] - times[i - 1]).total_seconds()
        if 0 < gap <= idle_cutoff_sec:
            per[cats[i]] += gap
    per["total"] = sum(per[c] for c in ACTIVITY_CATEGORIES)
    return per


def savings_report(sessions: list[dict[str, Any]], jobs: dict[str, dict[str, Any]] | None = None, idle_cutoff_sec: float = 300.0) -> dict[str, Any]:
    jobs = jobs or {}
    by_deliverable: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for s in sessions:
        if s.get("finished_at_utc"):
            by_deliverable.setdefault(s["deliverable_id"], {}).setdefault(s["kind"], []).append(s)
    rows: list[dict[str, Any]] = []
    for did, kinds in sorted(by_deliverable.items()):
        if "manual_baseline" not in kinds or "agent_assisted" not in kinds:
            rows.append({"deliverable_id": did, "status": "incomplete_pair", "kinds_present": sorted(kinds)})
            continue
        base = sum(active_seconds(s, idle_cutoff_sec)["total"] for s in kinds["manual_baseline"]) / len(kinds["manual_baseline"])
        assisted_sessions = kinds["agent_assisted"]
        assisted = sum(active_seconds(s, idle_cutoff_sec)["total"] for s in assisted_sessions) / len(assisted_sessions)
        job = jobs.get(assisted_sessions[0].get("job_id") or "", {})
        saving = (base - assisted) / base * 100.0 if base > 0 else None
        rows.append(
            {
                "deliverable_id": did,
                "status": "measured",
                "manual_baseline_sec": round(base, 1),
                "agent_assisted_human_sec": round(assisted, 1),
                "human_time_saved_pct": round(saving, 2) if saving is not None else None,
                "processing_elapsed_sec": job.get("processing_elapsed_sec"),
                "external_cost_usd": (job.get("budget") or {}).get("spent_usd"),
                "quality_baseline": [s.get("quality_rating") for s in kinds["manual_baseline"]],
                "quality_assisted": [s.get("quality_rating") for s in assisted_sessions],
                "evidence_class": sorted({s.get("evidence_class", "unknown") for s in kinds["manual_baseline"] + assisted_sessions}),
                "meets_60pct_target": bool(saving is not None and saving >= 60.0),
            }
        )
    measured = [r for r in rows if r["status"] == "measured"]
    mean = round(sum(r["human_time_saved_pct"] for r in measured) / len(measured), 2) if measured else None
    return {
        "formula": "(manual_baseline - agent_assisted_human_time) / manual_baseline x 100",
        "measured_pairs": len(measured),
        "mean_human_time_saved_pct": mean,
        "target_60_85_pct_achieved": bool(measured and mean is not None and mean >= 60.0),
        "claim_status": "measured" if measured else "NOT MEASURED - no paired human editing sessions recorded",
        "rows": rows,
    }


def modelled_estimate(job_doc: dict[str, Any], review_sec_per_op: float = 1.5) -> dict[str, Any]:
    """Planning aid only: what the applied operations would cost a human, by task model."""
    manual = 0.0
    review = 0.0
    by_type: dict[str, int] = {}
    for meta in job_doc.get("batches", {}).values():
        if meta.get("decision") == "rejected":
            continue
        for t, n in (meta.get("operation_types") or {}).items():
            by_type[t] = by_type.get(t, 0) + int(n)
            manual += MODELLED_MANUAL_SECONDS.get(t, 15.0) * int(n)
            review += review_sec_per_op * int(n)
    return {
        "label": "MODELLED ESTIMATE - NOT MEASURED HUMAN TIME",
        "operation_counts": by_type,
        "modelled_manual_sec": round(manual, 1),
        "modelled_review_sec": round(review, 1),
        "modelled_saving_pct": round((manual - review) / manual * 100.0, 1) if manual else None,
        "caveat": "Per-task times are assumptions; real savings require paired manual/assisted sessions (MeasurementLog).",
    }
