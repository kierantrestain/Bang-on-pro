"""Editing-time measurement for the 60-85 % human-time-reduction objective.

Records *measured* human active editing sessions (manual baseline vs
agent-assisted) per deliverable and computes::

    saving % = (manual_baseline_time - assisted_human_time) / manual_baseline_time x 100

Human time includes setup, review, corrections and final approval. Idle gaps
longer than ``idle_cutoff_sec`` between recorded activity events are excluded,
and so is time between a ``pause`` and the next ``resume`` (the editor's manual
pause). The Agent Control Centre records activity events automatically while a
timed session is open (setup while configuring, review while reviewing,
correction on reject / rerun / operation edits, approval on promote, editing on
any other input in the editor), at most one event per category change or per
heartbeat interval.

Machine processing time is **never** counted as human time: it comes from the
linked job (``processing_elapsed_sec`` and per-agent ``duration_sec``) and is
reported in its own columns. A deliverable without both a finished manual
baseline and a finished agent-assisted session is reported as *not measured*.
A separate *modelled estimate* (operation counts x per-task manual times) is
provided only as a planning aid and is always labelled as not measured.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bangonpro.ai_engine.department.checkpoint import write_json_atomic

SESSION_KINDS = ("manual_baseline", "agent_assisted")
ACTIVITY_CATEGORIES = ("setup", "editing", "review", "correction", "approval")
MARKER_CATEGORIES = ("pause", "resume")
EVENT_CATEGORIES = ACTIVITY_CATEGORIES + MARKER_CATEGORIES
NOT_MEASURED = "not measured"

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

    def _session(self, doc: dict[str, Any], session_id: str) -> dict[str, Any]:
        ses = next((s for s in doc["sessions"] if s["session_id"] == session_id), None)
        if ses is None:
            raise ValueError(f"No timing session '{session_id}'")
        return ses

    def start(
        self,
        *,
        deliverable_id: str,
        kind: str,
        editor: str,
        job_id: str | None = None,
        footage: str = "",
        evidence_class: str = "physical_hardware",
        job_dir: str | None = None,
        profile_id: str | None = None,
    ) -> str:
        if kind not in SESSION_KINDS:
            raise ValueError(f"kind must be one of {SESSION_KINDS}")
        if not str(deliverable_id).strip():
            raise ValueError("A timing session needs a deliverable name (for example the episode title)")
        doc = self._load()
        sid = f"ses_{uuid.uuid4().hex[:10]}"
        doc["sessions"].append(
            {
                "session_id": sid,
                "deliverable_id": deliverable_id,
                "kind": kind,
                "editor": editor,
                "job_id": job_id,
                "job_dir": job_dir,
                "profile_id": profile_id,
                "footage": footage,
                "evidence_class": evidence_class,
                "started_at_utc": _now(),
                "events": [],
                "finished_at_utc": None,
                "quality_rating": None,
                "notes": "",
            }
        )
        self._save(doc)
        return sid

    def activity(self, session_id: str, category: str, *, at_utc: str | None = None, detail: str = "") -> None:
        if category not in EVENT_CATEGORIES:
            raise ValueError(f"category must be one of {EVENT_CATEGORIES}")
        doc = self._load()
        ses = self._session(doc, session_id)
        if ses.get("finished_at_utc"):
            raise ValueError(f"Timing session '{session_id}' is already finished")
        ses["events"].append({"ts_utc": at_utc or _now(), "category": category, "detail": detail})
        self._save(doc)

    def pause(self, session_id: str, *, at_utc: str | None = None) -> None:
        self.activity(session_id, "pause", at_utc=at_utc)

    def resume(self, session_id: str, *, at_utc: str | None = None) -> None:
        self.activity(session_id, "resume", at_utc=at_utc)

    def link_job(self, session_id: str, *, job_id: str, job_dir: str | None = None) -> None:
        doc = self._load()
        ses = self._session(doc, session_id)
        ses["job_id"] = job_id
        if job_dir:
            ses["job_dir"] = job_dir
        self._save(doc)

    def finish(self, session_id: str, *, quality_rating: float | None = None, notes: str = "", at_utc: str | None = None) -> None:
        if quality_rating is not None and not 1.0 <= float(quality_rating) <= 5.0:
            raise ValueError("Quality rating must be between 1 and 5")
        doc = self._load()
        ses = self._session(doc, session_id)
        ses["finished_at_utc"] = at_utc or _now()
        ses["quality_rating"] = float(quality_rating) if quality_rating is not None else None
        ses["notes"] = notes
        self._save(doc)

    def sessions(self) -> list[dict[str, Any]]:
        return list(self._load()["sessions"])

    def open_sessions(self) -> list[dict[str, Any]]:
        return [s for s in self.sessions() if not s.get("finished_at_utc")]


def active_seconds(session: dict[str, Any], idle_cutoff_sec: float = 300.0) -> dict[str, float]:
    """Sum active time between consecutive activity events, ignoring idle gaps and pauses.

    The gap before an event counts towards that event's category. The gap before a
    ``pause`` counts towards the category that was in progress; the paused interval itself
    (up to the next ``resume``, work event or finish) is excluded and reported as ``paused``.
    """
    times = [datetime.fromisoformat(session["started_at_utc"])]
    cats = ["setup"]
    for e in session.get("events", []):
        times.append(datetime.fromisoformat(e["ts_utc"]))
        cats.append(e["category"])
    if session.get("finished_at_utc"):
        times.append(datetime.fromisoformat(session["finished_at_utc"]))
        cats.append("approval")
    per: dict[str, float] = {c: 0.0 for c in ACTIVITY_CATEGORIES}
    paused_sec = idle_sec = 0.0
    paused = False
    current = "setup"
    for i in range(1, len(times)):
        gap = (times[i] - times[i - 1]).total_seconds()
        cat = cats[i]
        if paused:
            paused_sec += max(0.0, gap)
            if cat != "pause":
                paused = False  # resume marker, a new work event or the finish ends the pause
                if cat in ACTIVITY_CATEGORIES:
                    current = cat
            continue
        if cat == "resume":
            continue  # resume without a pause: nothing to exclude
        target = current if cat == "pause" else cat
        if 0 < gap <= idle_cutoff_sec:
            per[target] += gap
        elif gap > idle_cutoff_sec:
            idle_sec += gap
        if cat == "pause":
            paused = True
        else:
            current = cat
    per["total"] = sum(per[c] for c in ACTIVITY_CATEGORIES)
    per["paused"] = paused_sec
    per["idle_excluded"] = idle_sec
    return per


def _job_figures(job: dict[str, Any]) -> dict[str, Any]:
    """Machine time, decisions and corrective work recorded by a production job."""
    agents = job.get("agents") or {}
    decisions = {"accepted": 0, "auto_accepted": 0, "rejected": 0, "pending": 0}
    ops = {"applied": 0, "rejected": 0, "pending": 0}
    for meta in (job.get("batches") or {}).values():
        n = int(meta.get("operation_count", 0) or 0)
        dropped = len(meta.get("rejected_op_ids") or [])
        d = meta.get("decision")
        if d == "rejected":
            ops["rejected"] += n
        elif d in ("accepted", "auto_accepted"):
            ops["applied"] += n - dropped
            ops["rejected"] += dropped
        else:
            ops["pending"] += n
    for r in agents.values():
        d = r.get("decision")
        if d in decisions:
            decisions[d] += 1
    reruns = sum(max(0, int(r.get("attempts", 0) or 0) - 1) for r in agents.values())
    per_agent = {a: r.get("duration_sec") for a, r in agents.items() if r.get("duration_sec") is not None}
    review_total = sum(len(r.get("review_items") or []) for r in agents.values())
    review_addressed = sum(len(r.get("review_items") or []) for r in agents.values() if r.get("decision") in ("accepted", "rejected"))
    return {
        "job_id": job.get("job_id"),
        # Machine time: never added to human time. Quality-loop fields are 0 when the job predates them.
        "processing_elapsed_sec": job.get("processing_elapsed_sec"),
        "quality_loop_processing_sec": float(job.get("quality_loop_processing_sec") or 0.0),
        "quality_loop_attempts": int(job.get("quality_loop_attempts") or 0),
        "agent_processing_sec": per_agent,
        "external_cost_usd": (job.get("budget") or {}).get("spent_usd"),
        "quality_loop_cost_usd": float(job.get("quality_loop_cost_usd") or 0.0),
        # Human corrective work and review workload (the key indicators).
        "agent_decisions": decisions,
        "agent_reruns": reruns,
        "op_level_rejections": sum(len(d.get("rejected_op_ids") or []) for d in (job.get("decisions") or [])),
        "review_items_total": review_total,
        "review_items_addressed": review_addressed,
        # Context only: automated operation counts are not a success metric.
        "operations": ops,
    }


def _mean(values: list[float | None]) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def savings_report(sessions: list[dict[str, Any]], jobs: dict[str, dict[str, Any]] | None = None, idle_cutoff_sec: float = 300.0) -> dict[str, Any]:
    jobs = jobs or {}
    by_deliverable: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for s in sessions:
        by_deliverable.setdefault(s["deliverable_id"], {})
        if s.get("finished_at_utc"):
            by_deliverable[s["deliverable_id"]].setdefault(s["kind"], []).append(s)
    rows: list[dict[str, Any]] = []
    for did, kinds in sorted(by_deliverable.items()):
        missing = [k for k in SESSION_KINDS if k not in kinds]
        assisted_sessions = kinds.get("agent_assisted", [])
        job = jobs.get((assisted_sessions[0].get("job_id") if assisted_sessions else "") or "", {})
        machine = _job_figures(job) if job else None
        if missing:
            rows.append(
                {
                    "deliverable_id": did,
                    "status": "not_measured",
                    "kinds_present": sorted(kinds),
                    "missing": missing,
                    "human_time_saved_pct": NOT_MEASURED,
                    "machine": machine,
                    "reason": "needs a finished " + " and a finished ".join(m.replace("_", " ") for m in missing) + " session",
                }
            )
            continue
        base_parts = [active_seconds(s, idle_cutoff_sec) for s in kinds["manual_baseline"]]
        asst_parts = [active_seconds(s, idle_cutoff_sec) for s in assisted_sessions]
        base = sum(p["total"] for p in base_parts) / len(base_parts)
        assisted = sum(p["total"] for p in asst_parts) / len(asst_parts)
        saving = (base - assisted) / base * 100.0 if base > 0 else None
        breakdown = {c: round(sum(p[c] for p in asst_parts) / len(asst_parts), 1) for c in ACTIVITY_CATEGORIES}
        corrections = sum(1 for s in assisted_sessions for e in s.get("events", []) if e.get("category") == "correction") / len(assisted_sessions)
        q_base = [s.get("quality_rating") for s in kinds["manual_baseline"]]
        q_asst = [s.get("quality_rating") for s in assisted_sessions]
        rows.append(
            {
                "deliverable_id": did,
                "status": "measured",
                "manual_baseline_sec": round(base, 1),
                "agent_assisted_human_sec": round(assisted, 1),
                "human_time_saved_pct": round(saving, 2) if saving is not None else NOT_MEASURED,
                "assisted_breakdown_sec": breakdown,
                "corrective_work": {"correction_sec": breakdown["correction"], "correction_events": round(corrections, 1)},
                "paused_sec_excluded": round(sum(p["paused"] for p in asst_parts + base_parts), 1),
                "machine": machine,
                "processing_elapsed_sec": machine["processing_elapsed_sec"] if machine else NOT_MEASURED,
                "external_cost_usd": machine["external_cost_usd"] if machine else NOT_MEASURED,
                "quality_baseline": q_base,
                "quality_assisted": q_asst,
                "quality_baseline_mean": _mean(q_base) if _mean(q_base) is not None else NOT_MEASURED,
                "quality_assisted_mean": _mean(q_asst) if _mean(q_asst) is not None else NOT_MEASURED,
                "sessions": {"manual_baseline": len(base_parts), "agent_assisted": len(asst_parts)},
                "evidence_class": sorted({s.get("evidence_class", "unknown") for s in kinds["manual_baseline"] + assisted_sessions}),
                "meets_60pct_target": bool(isinstance(saving, float) and saving >= 60.0),
            }
        )
    measured = [r for r in rows if r["status"] == "measured" and isinstance(r["human_time_saved_pct"], float)]
    mean = round(sum(r["human_time_saved_pct"] for r in measured) / len(measured), 2) if measured else None
    return {
        "formula": "(manual_baseline - agent_assisted_human_time) / manual_baseline x 100",
        "human_time_definition": "active editor time (setup, editing, review, correction, approval); idle gaps and pauses excluded; machine processing reported separately",
        "measured_pairs": len(measured),
        "not_measured_deliverables": sum(1 for r in rows if r["status"] == "not_measured"),
        "mean_human_time_saved_pct": mean,
        "target_60_85_pct_achieved": bool(measured and mean is not None and mean >= 60.0),
        "claim_status": "measured" if measured else "NOT MEASURED - no paired human editing sessions recorded",
        "rows": rows,
    }


CSV_COLUMNS = (
    "deliverable_id",
    "status",
    "manual_baseline_min",
    "assisted_human_min",
    "human_time_saved_pct",
    "assisted_setup_min",
    "assisted_editing_min",
    "assisted_review_min",
    "assisted_correction_min",
    "assisted_approval_min",
    "correction_events",
    "agents_rejected",
    "agent_reruns",
    "op_edits",
    "review_items_total",
    "review_items_addressed",
    "agents_accepted",
    "machine_processing_min",
    "quality_loop_machine_min",
    "external_cost_usd",
    "quality_loop_cost_usd",
    "quality_baseline_mean",
    "quality_assisted_mean",
    "evidence_class",
    "meets_60pct_target",
)


def _minutes(sec: Any) -> Any:
    return round(float(sec) / 60.0, 1) if isinstance(sec, (int, float)) else NOT_MEASURED


def report_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in report["rows"]:
        m = r.get("machine") or {}
        bd = r.get("assisted_breakdown_sec") or {}
        measured = r["status"] == "measured"
        out.append(
            {
                "deliverable_id": r["deliverable_id"],
                "status": r["status"],
                "manual_baseline_min": _minutes(r.get("manual_baseline_sec")),
                "assisted_human_min": _minutes(r.get("agent_assisted_human_sec")),
                "human_time_saved_pct": r["human_time_saved_pct"],
                **{f"assisted_{c}_min": _minutes(bd.get(c)) if measured else NOT_MEASURED for c in ACTIVITY_CATEGORIES},
                "correction_events": (r.get("corrective_work") or {}).get("correction_events", NOT_MEASURED),
                "agents_rejected": (m.get("agent_decisions") or {}).get("rejected", 0) if m else NOT_MEASURED,
                "agent_reruns": m.get("agent_reruns", NOT_MEASURED) if m else NOT_MEASURED,
                "op_edits": m.get("op_level_rejections", NOT_MEASURED) if m else NOT_MEASURED,
                "review_items_total": m.get("review_items_total", NOT_MEASURED) if m else NOT_MEASURED,
                "review_items_addressed": m.get("review_items_addressed", NOT_MEASURED) if m else NOT_MEASURED,
                "agents_accepted": (m.get("agent_decisions") or {}).get("accepted", 0) if m else NOT_MEASURED,
                "machine_processing_min": _minutes(m.get("processing_elapsed_sec")) if m else NOT_MEASURED,
                "quality_loop_machine_min": _minutes(m.get("quality_loop_processing_sec")) if m else NOT_MEASURED,
                "external_cost_usd": m.get("external_cost_usd", NOT_MEASURED) if m else NOT_MEASURED,
                "quality_loop_cost_usd": m.get("quality_loop_cost_usd", NOT_MEASURED) if m else NOT_MEASURED,
                "quality_baseline_mean": r.get("quality_baseline_mean", NOT_MEASURED),
                "quality_assisted_mean": r.get("quality_assisted_mean", NOT_MEASURED),
                "evidence_class": ";".join(r.get("evidence_class") or []) or NOT_MEASURED,
                "meets_60pct_target": r.get("meets_60pct_target", False),
            }
        )
    return out


def render_table(report: dict[str, Any]) -> str:
    rows = report_rows(report)
    cols = (
        "deliverable_id", "status", "manual_baseline_min", "assisted_human_min", "human_time_saved_pct",
        "assisted_correction_min", "assisted_review_min", "agents_rejected", "agent_reruns", "op_edits", "review_items_total",
        "quality_baseline_mean", "quality_assisted_mean", "machine_processing_min", "external_cost_usd",
    )
    heads = (
        "Deliverable", "Status", "Manual min", "Assisted min", "Saved %",
        "Correction min", "Review min", "Rejected", "Reruns", "Op edits", "Review items",
        "Quality manual", "Quality assisted", "Machine min", "External $",
    )
    table = [heads] + [tuple(str(r[c]) for c in cols) for r in rows]
    widths = [max(len(row[i]) for row in table) for i in range(len(cols))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in table]
    lines.insert(1, "  ".join("-" * w for w in widths))
    mean = report["mean_human_time_saved_pct"]
    lines += ["", f"Measured pairs: {report['measured_pairs']}  |  Mean human time saved: {mean if mean is not None else NOT_MEASURED}{' %' if mean is not None else ''}  |  {report['claim_status']}"]
    lines.append("Human time excludes idle gaps, pauses and machine processing (machine time, including quality-loop attempts, is shown separately).")
    lines.append("Corrective work (rejections, reruns, operation edits) and review workload are the key indicators; automated operation counts are not a success measure.")
    return "\n".join(lines) + "\n"


def write_report_files(report: dict[str, Any], out_dir: str | Path) -> dict[str, str]:
    out = Path(out_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    jp = write_json_atomic(out / f"measurement_report_{stamp}.json", report)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(CSV_COLUMNS))
    w.writeheader()
    for r in report_rows(report):
        w.writerow(r)
    cp = out / f"measurement_report_{stamp}.csv"
    cp.write_text(buf.getvalue(), encoding="utf-8")
    return {"json": str(jp), "csv": str(cp)}


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
        "evidence": "none: planning aid only, never reported alongside measured savings",
        "operation_counts": by_type,
        "modelled_manual_sec": round(manual, 1),
        "modelled_review_sec": round(review, 1),
        "modelled_saving_pct": round((manual - review) / manual * 100.0, 1) if manual else None,
        "caveat": "Per-task times are assumptions; real savings require paired manual/assisted sessions (MeasurementLog).",
    }
