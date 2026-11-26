"""Persistent shared intelligence of the production department (ACE-style evolving playbooks).

Production intelligence belongs to Bang On Pro, not to a provider's conversation history. Everything is
one local SQLite file (WAL, transactional) next to the preference store:
``$BANGONPRO_INTEL_DIR`` or ``<prefs dir>/../intelligence/knowledge.sqlite``.

* **Evidence** (append-only, idempotent by content id): instructions, tool calls, verified native edits,
  QC findings and corrections, completion checks, human approvals / rejections, manual changes and
  draft-vs-final differences, reasoning usage and processing time. Each row carries the job, task,
  series, format, agent, strategy version, whether it is an objective technical fact or a subjective
  preference, whether it was independently verified, and its source label (synthetic footage, mocked
  reasoning, authenticated CLI, MCP client, skill, evaluation). Credentials, prompts, model answers and
  media are never stored.
* **Knowledge** in six categories (episodic, semantic, procedural, preference, failure, performance),
  scoped global / format / series / episode and by specialist. Rows are updated incrementally (support,
  contradictions, value histogram, version, history), never rewritten wholesale. Conflicting values are
  resolved by evidence (most support, then most recent); more specific scopes win at retrieval; other
  series are never retrieved.
* **Retrieval** returns only what applies to the agents and context of a task, ranked and cut to a
  character budget, labelled as data.

The Learning Agent runs :func:`consolidate_task` (deterministic) after each task; Quality Control's
independent verification is the only thing that turns an agent's claim into validated experience.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

CATEGORIES = ("episodic", "semantic", "procedural", "preference", "failure", "performance")
SCOPES = ("global", "format", "series", "episode")
_SPECIFICITY = {"episode": 4, "series": 3, "format": 2, "global": 1}
RETRIEVABLE = ("active", "validated")
DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,                 # capture + consolidation + retrieval
    "apply_strategies": True,        # active strategies shape deterministic execution
    "auto_promote_procedural": True,  # independently validated, low-risk procedural strategies may be promoted without a human
    "trial_candidate_skills": True,  # a candidate skill may be tried (fully verified, undone on failure) before reasoning is asked
    "retention_days": 365,
    "scopes": list(SCOPES),
}
_SECRET_KEY = re.compile(r"(api[_-]?key|token|secret|password|passwd|authori[sz]ation|cookie|credential|session[_-]?key|bearer)", re.I)
_SECRET_VAL = re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|xai-[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+)")
_PRIVATE_KEYS = {"prompt", "prompts", "response", "raw", "thinking", "thought", "reasoning_trace", "messages", "transcript_text", "frames", "image", "images"}
_STOP = set("a an the and or of to in on at for with by is it this that be as from into over make put add cut give their his her its when then than more less please".split())


def default_intel_dir() -> Path:
    env = os.environ.get("BANGONPRO_INTEL_DIR")
    if env:
        return Path(env).expanduser().resolve()
    from bangonpro.ai_engine.department.learning import default_prefs_dir

    return default_prefs_dir().parent / "intelligence"


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9']+", (text or "").lower()) if w not in _STOP and len(w) > 2}


def clean(obj: Any, depth: int = 0) -> Any:
    """What may be stored: no credentials, no private reasoning, no media, bounded size."""
    if depth > 6:
        return "…"
    if isinstance(obj, dict):
        out = {}
        for k, v in list(obj.items())[:60]:
            ks = str(k)
            if _SECRET_KEY.search(ks) or ks in _PRIVATE_KEYS:
                continue
            out[ks] = clean(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [clean(v, depth + 1) for v in list(obj)[:60]]
    if isinstance(obj, str):
        s = _SECRET_VAL.sub("[redacted]", obj)
        if len(s) > 3 and ("/" in s or "\\" in s) and Path(s).suffix:  # keep file names, not private folder paths
            s = Path(s).name
        return s[:500]
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return str(obj)[:200]


def _id(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:24]


SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY, at TEXT, job_id TEXT, task_id TEXT, series_id TEXT, format TEXT, episode_id TEXT, agent TEXT,
  strategy_version INTEGER, kind TEXT, nature TEXT, verified INTEGER, source TEXT, footage TEXT, payload TEXT);
CREATE INDEX IF NOT EXISTS ev_scope ON evidence(series_id, format, agent, kind);
CREATE INDEX IF NOT EXISTS ev_task ON evidence(task_id);
CREATE TABLE IF NOT EXISTS knowledge(id TEXT PRIMARY KEY, category TEXT, scope TEXT, scope_key TEXT, agent TEXT, key TEXT, statement TEXT, data TEXT,
  confidence REAL, support INTEGER, contradict INTEGER, status TEXT, version INTEGER, provenance TEXT, applicability TEXT, source TEXT, created TEXT, updated TEXT,
  UNIQUE(category, scope, scope_key, agent, key));
CREATE INDEX IF NOT EXISTS kn_lookup ON knowledge(agent, status, scope, scope_key);
CREATE TABLE IF NOT EXISTS knowledge_history(knowledge_id TEXT, version INTEGER, at TEXT, statement TEXT, data TEXT, confidence REAL, status TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS skills(id TEXT, version INTEGER, name TEXT, agent TEXT, format TEXT, series_id TEXT, status TEXT, spec TEXT, provenance TEXT,
  uses INTEGER DEFAULT 0, successes INTEGER DEFAULT 0, failures INTEGER DEFAULT 0, origin_job TEXT, validated_on TEXT, created TEXT, updated TEXT, reason TEXT,
  PRIMARY KEY(id, version));
CREATE TABLE IF NOT EXISTS strategies(agent TEXT, version INTEGER, parent INTEGER, params TEXT, status TEXT, risk TEXT, evaluation TEXT, reason TEXT,
  provenance TEXT, created TEXT, decided TEXT, PRIMARY KEY(agent, version));
CREATE TABLE IF NOT EXISTS strategy_active(agent TEXT PRIMARY KEY, version INTEGER, since TEXT);
CREATE TABLE IF NOT EXISTS strategy_history(agent TEXT, version INTEGER, at TEXT, action TEXT, reason TEXT, evidence TEXT);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
"""

_LOCK = threading.RLock()


class Intelligence:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve() if root else default_intel_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "knowledge.sqlite"
        self.db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with contextlib.suppress(sqlite3.DatabaseError):
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    @classmethod
    def for_job(cls, job: Any) -> Intelligence:
        pr = (job.doc.get("options") or {}).get("prefs_root")
        return cls(Path(pr).expanduser().resolve().parent / "intelligence") if pr and not os.environ.get("BANGONPRO_INTEL_DIR") else cls()

    def close(self) -> None:
        self.db.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with _LOCK:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    # ------------------------------------------------------------------ settings
    def settings(self) -> dict[str, Any]:
        out = dict(DEFAULT_SETTINGS)
        for r in self.db.execute("SELECT k, v FROM settings"):
            with contextlib.suppress(ValueError):
                out[r["k"]] = json.loads(r["v"])
        return out

    def set_setting(self, key: str, value: Any) -> dict[str, Any]:
        if key not in DEFAULT_SETTINGS:
            raise ValueError(f"unknown setting '{key}' (one of {sorted(DEFAULT_SETTINGS)})")
        if key == "scopes" and (not isinstance(value, list) or any(s not in SCOPES for s in value)):
            raise ValueError(f"scopes must be a subset of {SCOPES}")
        self.db.execute("INSERT OR REPLACE INTO settings(k, v) VALUES(?, ?)", (key, json.dumps(value)))
        return self.settings()

    @property
    def enabled(self) -> bool:
        return bool(self.settings().get("enabled", True))

    # ------------------------------------------------------------------ evidence
    def record(self, rows: list[dict[str, Any]], ctx: dict[str, Any]) -> list[str]:
        """Store evidence rows in one transaction; re-recording the same row is a no-op."""
        ids = []
        with self.transaction() as db:
            for r in rows:
                payload = clean(r.get("payload") or {})
                rid = r.get("id") or _id(ctx.get("job_id"), r.get("task_id") or ctx.get("task_id"), r["kind"], r.get("agent"), r.get("key") or payload)
                db.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (rid, _utc(), ctx.get("job_id"), r.get("task_id") or ctx.get("task_id"), ctx.get("series"), ctx.get("format"), ctx.get("episode"), r.get("agent"),
                            r.get("strategy_version"), r["kind"], r.get("nature", "objective"), int(bool(r.get("verified"))), r.get("source") or ctx.get("source"),
                            ctx.get("footage"), json.dumps(payload, separators=(",", ":"), default=str)))
                ids.append(rid)
        return ids

    def evidence(self, **where: Any) -> list[dict[str, Any]]:
        q, args = _where(where)
        return [dict(r) | {"payload": json.loads(r["payload"] or "{}")} for r in self.db.execute(f"SELECT * FROM evidence{q} ORDER BY at, rowid", args)]

    # ------------------------------------------------------------------ knowledge
    def upsert(self, category: str, scope: str, scope_key: str, agent: str, key: str, statement: str, data: dict[str, Any], *, evidence: list[str],
               verified: bool, status: str | None = None, source: str = "", value: Any = None, contradicts: bool = False, applicability: dict[str, Any] | None = None) -> str:
        """Incremental update: support, value histogram (conflict resolution), version and history."""
        if category not in CATEGORIES or scope not in SCOPES:
            raise ValueError(f"bad category/scope {category}/{scope}")
        kid = _id(category, scope, scope_key, agent, key)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM knowledge WHERE id=?", (kid,)).fetchone()
            data = clean(data)
            if row is None:
                values = {json.dumps(value): 1} if value is not None else {}
                conf = 0.7 if verified else 0.4
                d = {**data, **({"values": values, "value": value} if values else {})}
                st = status or ("validated" if verified else "candidate")
                db.execute("INSERT INTO knowledge VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (kid, category, scope, scope_key, agent, key, statement[:400], json.dumps(d), conf, 1, int(contradicts), st, 1, json.dumps(evidence[:20]),
                            json.dumps(clean(applicability or {})), source, _utc(), _utc()))
                db.execute("INSERT INTO knowledge_history VALUES(?,?,?,?,?,?,?,?)", (kid, 1, _utc(), statement[:400], json.dumps(d), conf, st, "created"))
                return kid
            prov = json.loads(row["provenance"] or "[]")
            if set(evidence) <= set(prov):  # same evidence again: idempotent
                return kid
            old = json.loads(row["data"] or "{}")
            d = {**old, **data}
            values = dict(old.get("values") or {})
            if value is not None:
                values[json.dumps(value)] = values.get(json.dumps(value), 0) + 1
                # most supported value wins; ties go to the newest evidence
                best = max(values.items(), key=lambda kv: (kv[1], kv[0] == json.dumps(value)))
                d["values"], d["value"] = values, json.loads(best[0])
                contradicts = contradicts or len(values) > 1 and json.dumps(value) != best[0]
            support = int(row["support"]) + (0 if contradicts else 1)
            contra = int(row["contradict"]) + int(contradicts)
            conf = max(0.05, min(0.95, (0.7 if verified else 0.4) + 0.1 * (support - 1) - 0.15 * contra))
            st = row["status"] if row["status"] in ("disabled", "rejected", "retired") else (status or ("validated" if verified or row["status"] == "validated" else row["status"]))
            ver = int(row["version"]) + 1
            db.execute("UPDATE knowledge SET statement=?, data=?, confidence=?, support=?, contradict=?, status=?, version=?, provenance=?, updated=? WHERE id=?",
                       (statement[:400], json.dumps(d), conf, support, contra, st, ver, json.dumps((prov + evidence)[-40:]), _utc(), kid))
            db.execute("INSERT INTO knowledge_history VALUES(?,?,?,?,?,?,?,?)", (kid, ver, _utc(), statement[:400], json.dumps(d), conf, st, "contradicted" if contradicts else "supported"))
        return kid

    def knowledge(self, **where: Any) -> list[dict[str, Any]]:
        q, args = _where(where)
        return [_krow(r) for r in self.db.execute(f"SELECT * FROM knowledge{q} ORDER BY updated DESC", args)]

    def set_status(self, kid: str, status: str, reason: str = "") -> dict[str, Any]:
        if status not in ("active", "validated", "candidate", "needs_approval", "disabled", "rejected", "retired"):
            raise ValueError("bad status")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM knowledge WHERE id=?", (kid,)).fetchone()
            if row is None:
                raise KeyError(kid)
            ver = int(row["version"]) + 1
            db.execute("UPDATE knowledge SET status=?, version=?, updated=? WHERE id=?", (status, ver, _utc(), kid))
            db.execute("INSERT INTO knowledge_history VALUES(?,?,?,?,?,?,?,?)", (kid, ver, _utc(), row["statement"], row["data"], row["confidence"], status, reason or status))
        return self.knowledge(id=kid)[0]

    def retrieve(self, ctx: dict[str, Any], agents: list[str], *, text: str = "", budget_chars: int = 1600, categories: tuple[str, ...] = ("failure", "semantic", "preference", "procedural")) -> list[dict[str, Any]]:
        """Relevant validated knowledge for these agents in this context, most specific first, within budget."""
        allowed = set(self.settings().get("scopes") or SCOPES)
        keys = {"global": "*", "format": ctx.get("format"), "series": ctx.get("series"), "episode": ctx.get("episode")}
        conds = " OR ".join(f"(scope='{s}' AND scope_key=?)" for s in SCOPES if s in allowed)
        args: list[Any] = [keys[s] for s in SCOPES if s in allowed]
        ags = list(dict.fromkeys([*agents, "*"]))
        q = (f"SELECT * FROM knowledge WHERE ({conds}) AND agent IN ({','.join('?' * len(ags))}) AND status IN ('active','validated') "
             f"AND category IN ({','.join('?' * len(categories))})")
        rows = [_krow(r) for r in self.db.execute(q, [*args, *ags, *categories])]
        best: dict[tuple[str, str, str], dict[str, Any]] = {}
        for r in rows:  # the most specific scope wins for the same lesson
            k = (r["category"], r["agent"], r["key"])
            if k not in best or _SPECIFICITY[r["scope"]] > _SPECIFICITY[best[k]["scope"]]:
                best[k] = r
        want = tokens(text)
        ranked = sorted(best.values(), key=lambda r: (-(len(want & tokens(r["statement"] + " " + r["key"])) > 0), -_SPECIFICITY[r["scope"]], -r["confidence"], -r["support"]))
        out, used = [], 0
        for r in ranked:
            item = {"id": r["id"], "category": r["category"], "agent": r["agent"], "scope": f"{r['scope']}:{r['scope_key']}", "lesson": r["statement"],
                    "confidence": round(r["confidence"], 2), "support": r["support"]}
            if r["data"].get("value") is not None:
                item["value"] = r["data"]["value"]
            n = len(json.dumps(item))
            if used + n > budget_chars:
                break
            out.append(item)
            used += n
        return out

    # ------------------------------------------------------------------ skills / strategies storage
    def skills(self, **where: Any) -> list[dict[str, Any]]:
        q, args = _where(where)
        return [dict(r) | {"spec": json.loads(r["spec"]), "provenance": json.loads(r["provenance"] or "[]")}
                for r in self.db.execute(f"SELECT * FROM skills{q} ORDER BY id, version", args)]

    def latest_skills(self, statuses: tuple[str, ...] = ("candidate", "validated", "active")) -> list[dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for s in self.skills():
            out[s["id"]] = s  # ordered by version: last wins
        return [s for s in out.values() if s["status"] in statuses]

    def put_skill(self, skill: dict[str, Any]) -> None:
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO skills(id, version, name, agent, format, series_id, status, spec, provenance, uses, successes, failures, origin_job, validated_on, created, updated, reason) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (skill["id"], skill["version"], skill["name"], skill["agent"], skill.get("format"), skill.get("series_id"), skill["status"], json.dumps(skill["spec"]),
                        json.dumps(skill.get("provenance") or []), skill.get("uses", 0), skill.get("successes", 0), skill.get("failures", 0), skill.get("origin_job"),
                        skill.get("validated_on"), skill.get("created") or _utc(), _utc(), skill.get("reason", "")))

    def summary(self) -> dict[str, Any]:
        k = {f"{r['category']}:{r['status']}": r["n"] for r in self.db.execute("SELECT category, status, COUNT(*) n FROM knowledge GROUP BY category, status")}
        sk = self.latest_skills(("candidate", "validated", "active", "retired"))
        strat = {r["agent"]: r["version"] for r in self.db.execute("SELECT agent, version FROM strategy_active")}
        pending = [dict(r) for r in self.db.execute("SELECT agent, version, reason FROM strategies WHERE status='awaiting_approval'")]
        pending_k = [{"id": r["id"], "agent": r["agent"], "lesson": r["statement"]} for r in self.knowledge(status="needs_approval")]
        n_ev = self.db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        return {"enabled": self.enabled, "evidence": n_ev, "knowledge": k, "skills": {s: sum(1 for x in sk if x["status"] == s) for s in ("candidate", "validated", "active", "retired")},
                "active_strategies": strat, "awaiting_approval": {"strategies": pending, "knowledge": pending_k[:10]}}

    # ------------------------------------------------------------------ retention, export, removal
    def prune(self, days: int | None = None) -> int:
        days = int(days if days is not None else self.settings().get("retention_days", 365))
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))
        with self.transaction() as db:
            n = db.execute("DELETE FROM evidence WHERE at < ?", (cutoff,)).rowcount
        return int(n)

    def forget(self, *, series: str | None = None, everything: bool = False) -> dict[str, int]:
        """Remove learned material: one series (its evidence, knowledge and skills) or all of it."""
        if not everything and not series:
            raise ValueError("give series or everything=True")
        with self.transaction() as db:
            if everything:
                out = {t: db.execute(f"DELETE FROM {t}").rowcount for t in ("evidence", "knowledge", "knowledge_history", "skills", "strategies", "strategy_active", "strategy_history")}
            else:
                out = {"evidence": db.execute("DELETE FROM evidence WHERE series_id=?", (series,)).rowcount,
                       "knowledge": db.execute("DELETE FROM knowledge WHERE scope='series' AND scope_key=?", (series,)).rowcount,
                       "skills": db.execute("DELETE FROM skills WHERE series_id=?", (series,)).rowcount}
        return out

    def export(self, out_path: str | Path, *, series: str | None = None) -> dict[str, Any]:
        doc = {"format": "bangonpro.intelligence", "version": 1, "exported_at": _utc(), "series": series,
               "knowledge": [k for k in self.knowledge() if series is None or (k["scope"] != "series" or k["scope_key"] == series)],
               "skills": [s for s in self.skills() if series is None or s["series_id"] in (series, None)],
               "strategies": [dict(r) for r in self.db.execute("SELECT * FROM strategies")], "active": [dict(r) for r in self.db.execute("SELECT * FROM strategy_active")],
               "history": [dict(r) for r in self.db.execute("SELECT * FROM strategy_history")]}
        Path(out_path).write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
        return {"path": str(out_path), "knowledge": len(doc["knowledge"]), "skills": len(doc["skills"]), "strategies": len(doc["strategies"])}


def _where(where: dict[str, Any]) -> tuple[str, list[Any]]:
    cols = {k: v for k, v in where.items() if v is not None}
    for k in cols:
        if not re.fullmatch(r"[a-z_]+", k):
            raise ValueError(k)
    return ((" WHERE " + " AND ".join(f"{k}=?" for k in cols)) if cols else ""), list(cols.values())


def _krow(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["data"] = json.loads(d["data"] or "{}")
    d["provenance"] = json.loads(d["provenance"] or "[]")
    d["applicability"] = json.loads(d["applicability"] or "{}")
    return d


# ---------------------------------------------------------------------- job context + capture
def job_context(job: Any, *, source: str = "", task_id: str | None = None) -> dict[str, Any]:
    return {"job_id": job.doc.get("job_id"), "series": str(job.doc.get("series_id") or "default"), "format": str(job.doc.get("profile_id") or "unknown"),
            "episode": job.doc.get("job_id"), "source": source, "task_id": task_id, "footage": os.environ.get("BANGONPRO_EVIDENCE_LABEL", "production")}


# What an accepted correction teaches, per defect (statement template, strategy parameter it argues for).
LESSONS: dict[str, dict[str, Any]] = {
    "GRAPHIC_TOO_SHORT": {"agent": "graphics", "say": "Titles and straps under 2 s are flagged by QC; hold them at least {value:.1f}s (the verified correction).",
                          "param": "title_duration_sec"},
    "GRAPHIC_WRONG_SPEAKER": {"agent": "graphics", "say": "A name strap must sit over that speaker's own close-up; QC moved one that was placed while another camera was on screen.",
                              "param": "align_titles_to_speaker"},
    "FLASH_SHOT": {"agent": "multicam_director", "say": "Camera switches under 0.5 s read as flash frames; keep reactions at least {value:.1f}s.", "param": "reaction_min_sec"},
    "WRONG_CAMERA": {"agent": "multicam_director", "say": "Show the speaker's close-up while one person talks for more than 1.5 s (reactions excepted)."},
    "AV_DESYNC": {"agent": "synchronisation", "say": "After edits, slip the source in point to keep picture within one frame of the dialogue."},
    "INCONSISTENT_COLOUR": {"agent": "colourist", "say": "Every shot from one camera carries that camera's grade; copy it with the shot."},
    "AUDIO_LEVEL_JUMP": {"agent": "audio_engineer", "say": "Do not change one dialogue clip's gain more than 3 dB from its neighbour on the same mic."},
    "PICTURE_GAP": {"agent": "rough_cut", "say": "Never leave a picture gap while dialogue plays; extend the previous shot."},
    "CAPTION_OUTSIDE_PROGRAM": {"agent": "captions", "say": "Remove captions after the end of the programme when the cut shortens."},
}


def _lesson_value(code: str, corr: dict[str, Any]) -> Any:
    now = corr.get("now") or []
    if code == "GRAPHIC_TOO_SHORT" and now:
        return round(max(float(n["end"]) - float(n["start"]) for n in now), 1)
    if code == "GRAPHIC_WRONG_SPEAKER":
        return True
    if code == "FLASH_SHOT":
        return 1.0
    return None


def capture_task(ws: Any, task: dict[str, Any], result: dict[str, Any], *, source: str, usage: dict[str, Any] | None = None, processing_sec: float | None = None,
                 intel: Intelligence | None = None) -> dict[str, Any]:
    """Evidence for one finished task (one transaction), then deterministic consolidation."""
    from bangonpro.ai_engine.department import strategies as strat

    intel = intel or Intelligence.for_job(ws.job)
    if not intel.enabled:
        return {"captured": 0, "learning": "disabled"}
    tid = task["task_id"]
    ctx = job_context(ws.job, source=source, task_id=tid)
    versions = strat.active_versions(intel)
    rows: list[dict[str, Any]] = [{"kind": "instruction", "agent": "executive", "verified": True, "payload": {"instruction": task.get("instruction", "")[:300], "agents": task.get("agents"),
                                   "expect": task.get("expect")}}]
    for ev in task.get("events") or []:
        if ev.get("step"):
            st = ev["step"]
            rows.append({"kind": "tool_call", "agent": ev.get("agent"), "verified": bool(ev.get("edit")), "key": [ev.get("edit"), st.get("tool")], "strategy_version": versions.get(ev.get("agent") or ""),
                         "payload": {"tool": st.get("tool"), "args": st.get("args"), "entry": ev.get("edit"), "applied_strategy": ev.get("applied_strategy")}})
        if ev.get("rejected"):
            rows.append({"kind": "edit_rejected", "agent": ev.get("agent"), "verified": True, "key": [ev.get("at"), ev["rejected"]], "payload": {"code": ev["rejected"], "message": ev.get("message")}})
    for e in ws.layer.doc.get("entries") or []:
        if e.get("task_id") != tid:
            continue
        rows.append({"kind": "edit_undone" if e.get("undone") else "edit_verified", "agent": e.get("agent_id"), "verified": not e.get("undone"), "key": e["entry_id"],
                     "strategy_version": versions.get(e.get("agent_id") or ""),
                     "payload": {"entry": e["entry_id"], "label": e.get("label"), "ops": [o.get("type") for o in (e.get("batch") or {}).get("operations", [])][:20], "spans": e.get("spans")}})
    for c in result.get("corrections") or []:
        d = c.get("defect") or {}
        rows.append({"kind": "qc_correction", "agent": c.get("agent"), "verified": str(c.get("result", "")).startswith("accepted"), "key": [d.get("code"), d.get("start"), c.get("entry_id")],
                     "payload": {"defect": d, "result": c.get("result"), "now": c.get("now")}})
    for u in result.get("unresolved") or []:
        rows.append({"kind": "qc_finding", "agent": u.get("agent"), "verified": False, "key": [u.get("code"), u.get("message")], "payload": u})
    for ch in result.get("checks") or []:
        rows.append({"kind": "check", "agent": "quality_control", "verified": True, "key": [ch.get("check"), ch.get("time"), ch.get("start")], "payload": {k: ch.get(k) for k in ("check", "ok", "detail")}})
    rows.append({"kind": "outcome", "agent": "executive", "verified": True, "payload": {"status": result.get("status"), "edits": result.get("edits"), "corrections": len(result.get("corrections") or []),
                 "unresolved": len(result.get("unresolved") or []), "processing_sec": processing_sec, "reasoning": usage or "unknown", "source": source}})
    ids = intel.record(rows, ctx)
    learned = consolidate_task(intel, ctx, task, result, ids, versions=versions, source=source, view=ws.view())
    return {"captured": len(ids), **learned}


def consolidate_task(intel: Intelligence, ctx: dict[str, Any], task: dict[str, Any], result: dict[str, Any], evidence_ids: list[str], *, versions: dict[str, int], source: str,
                     view: Any = None) -> dict[str, Any]:
    """Learning Agent: turn verified evidence into knowledge, incrementally and deterministically."""
    from bangonpro.ai_engine.department import skills as sk

    tid, status = task["task_id"], result.get("status")
    out: dict[str, Any] = {"lessons": [], "skill": None}
    evaluation = source == "evaluation"
    intel.upsert("episodic", "episode", str(ctx["episode"]), "executive", tid, f"'{task.get('instruction', '')[:90]}' -> {status}, {result.get('edits', 0)} edits, "
                 f"{len(result.get('corrections') or [])} QC corrections", {"status": status, "agents": task.get("agents"), "source": source}, evidence=evidence_ids[:1], verified=True, source=source)
    for c in result.get("corrections") or []:
        d = c.get("defect") or {}
        code = d.get("code")
        les = LESSONS.get(code or "")
        if not les:
            continue
        accepted = str(c.get("result", "")).startswith("accepted")
        value = _lesson_value(code, c) if accepted else None
        stmt = les["say"].format(value=value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0) if accepted else \
            f"QC's automatic repair for {code} did not hold ({str(c.get('result'))[:80]}); a specialist must handle it."
        kid = intel.upsert("failure", "series", ctx["series"], d.get("agent") or les["agent"], code if accepted else f"{code}:repair_failed", stmt,
                           {"code": code, "param": les.get("param"), "repair": c.get("result"), "example": {"start": d.get("start"), "message": d.get("message")}},
                           evidence=evidence_ids, verified=True, source=source, value=value)
        out["lessons"].append(kid)
        _promote_semantic(intel, ctx, d.get("agent") or les["agent"], code if accepted else f"{code}:repair_failed")
    if status == "verified" and not evaluation and task.get("instruction"):
        skill = sk.acquire_from_task(intel, ctx, task, view) if not task.get("skill") else None
        if skill:
            out["skill"] = skill["id"]
            intel.upsert("procedural", "series", ctx["series"], skill["agent"], skill["id"], f"Skill '{skill['name']}' ({len(skill['spec']['steps'])} steps) for: {skill['spec']['instruction'][:90]}",
                         {"skill": skill["id"], "version": skill["version"]}, evidence=evidence_ids[:3], verified=True, status="active", source=source)
    _update_performance(intel, ctx, task, result, versions, source)
    return out


def _promote_semantic(intel: Intelligence, ctx: dict[str, Any], agent: str, key: str) -> None:
    """A failure lesson seen independently in two series of a format becomes established format knowledge."""
    rows = [r for r in intel.knowledge(category="failure", scope="series", agent=agent, key=key) if r["status"] in RETRIEVABLE]
    series = {r["scope_key"] for r in rows}
    if len(series) >= 2:
        best = max(rows, key=lambda r: r["support"])
        intel.upsert("semantic", "format", ctx["format"], agent, key, best["statement"], {"series_count": len(series), "from": [r["id"] for r in rows]},
                     evidence=[i for r in rows for i in r["provenance"][:3]], verified=True, source="consolidation", value=best["data"].get("value"))


def _update_performance(intel: Intelligence, ctx: dict[str, Any], task: dict[str, Any], result: dict[str, Any], versions: dict[str, int], source: str) -> None:
    by_agent: dict[str, dict[str, float]] = {}

    def bump(agent: str | None, k: str, n: float = 1.0) -> None:
        if agent:
            d = by_agent.setdefault(agent, {})
            d[k] = d.get(k, 0.0) + n

    for a in task.get("agents") or []:
        bump(a, "tasks")
        bump(a, "verified", 1.0 if result.get("status") == "verified" else 0.0)
    for c in result.get("corrections") or []:
        bump((c.get("defect") or {}).get("agent"), "defects_introduced")
        bump("quality_control", "defects_detected")
    for u in result.get("unresolved") or []:
        bump(u.get("agent"), "defects_unresolved")
    for ev in task.get("events") or []:
        if ev.get("edit"):
            bump(ev.get("agent"), "edits")
        if ev.get("rejected"):
            bump(ev.get("agent"), "edits_rejected")
    for agent, inc in by_agent.items():
        ver = versions.get(agent, 0)
        key = f"v{ver}"
        cur = next(iter(intel.knowledge(category="performance", scope="format", scope_key=ctx["format"], agent=agent, key=key)), None)
        counters = dict((cur or {}).get("data", {}).get("counters") or {})
        seen = set((cur or {}).get("data", {}).get("tasks_seen") or [])
        if task["task_id"] in seen:
            continue
        for k, n in inc.items():
            counters[k] = counters.get(k, 0.0) + n
        seen.add(task["task_id"])
        intel.upsert("performance", "format", ctx["format"], agent, key, f"{agent} strategy v{ver}: {int(counters.get('tasks', 0))} tasks, {int(counters.get('defects_introduced', 0))} defects introduced",
                     {"counters": counters, "tasks_seen": sorted(seen)[-200:], "strategy_version": ver}, evidence=[f"task:{task['task_id']}"], verified=True, status="active", source=source)


def capture_correction(profile_id: str, series_id: str, correction: dict[str, Any], *, root: str | Path | None = None) -> None:
    """Human feedback recorded by the preference store: approvals, rejections and draft-vs-final differences."""
    kind = correction.get("kind")
    if kind == "agent_task":  # already captured in full by capture_task
        return
    intel = Intelligence(root) if root else Intelligence()
    if not intel.enabled:
        return
    ctx = {"job_id": correction.get("job_id"), "series": series_id, "format": profile_id, "episode": correction.get("job_id"), "source": "editor",
           "footage": os.environ.get("BANGONPRO_EVIDENCE_LABEL", "production")}
    if correction.get("decision") is not None:
        approved = str(correction["decision"]).startswith("approve")
        agent = correction.get("agent_id")
        ids = intel.record([{"kind": "human_approval" if approved else "human_rejection", "agent": agent, "verified": True, "nature": "subjective", "payload": correction}], ctx)
        if not approved:
            intel.upsert("preference", "series", series_id, agent or "*", f"rejected:{agent}", f"The editor rejected the {agent} pass ({str(correction.get('note') or '')[:80]}).",
                         {"rejected_ops": correction.get("rejected_ops")}, evidence=ids, verified=False, status="needs_approval", source="editor")
        return
    obs = {k: v for k, v in correction.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    ids = intel.record([{"kind": "manual_change", "agent": "learning", "verified": True, "nature": "subjective", "payload": correction}], ctx)
    for k, v in list(obs.items())[:12]:
        intel.upsert("preference", "series", series_id, "*", f"final:{k}", f"Editor's finished cut: {k} = {v}", {"observed": v}, evidence=ids, verified=False, status="needs_approval",
                     source="editor", value=v)


__all__ = ["CATEGORIES", "DEFAULT_SETTINGS", "LESSONS", "SCOPES", "Intelligence", "capture_correction", "capture_task", "clean", "consolidate_task", "default_intel_dir", "job_context", "tokens"]
