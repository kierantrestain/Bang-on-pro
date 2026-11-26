"""Production job queue with series batches and crash-safe unattended recovery (V1.2.2).

``<queue_root>/queue.json`` holds every queued production (persistent, atomic
writes). One worker at a time runs them sequentially, highest priority first,
then oldest first. The worker owns ``<queue_root>/worker.lock`` (PID, host,
heartbeat) for as long as it runs:

* a second worker refuses to start while the lock's PID is alive and its
  heartbeat is fresh;
* a lock whose PID is gone, or whose heartbeat is older than
  ``STALE_AFTER_SEC`` (a hung or rebooted machine), is *stale* and is taken over;
* on start the worker recovers entries left ``running`` by a worker that died:
  they are marked ``interrupted`` and run first, by resuming the job from its
  last checkpoint (finished agents are never redone).

The queue only processes while a worker runs: the Agent Control Centre starts one
while Bang On Pro is open (and again on the next launch if work is left), and
``python -m bangonpro.ai_engine.department queue run`` starts one by hand.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bangonpro.ai_engine.department.checkpoint import write_json_atomic
from bangonpro.ai_engine.department.errors import explain

STALE_AFTER_SEC = 120.0
HEARTBEAT_SEC = 5.0
QUEUE_STATES = ("queued", "interrupted", "running", "completed", "needs_attention", "failed", "cancelled")
ACTIVE_STATES = frozenset({"queued", "interrupted", "running"})
# Job statuses that end a queue entry successfully (the draft waits for the editor).
DONE_JOB_STATES = frozenset({"draft_ready", "awaiting_review", "planned_manual"})
ATTENTION_JOB_STATES = frozenset({"completed_with_errors", "needs_clarification", "blocked_insufficient_storage"})
SPEC_KEYS = ("brief", "inputs", "profile", "mode", "kdenlive_project", "project_id", "sequence_id", "options")


class QueueError(RuntimeError):
    pass


class WorkerBusyError(QueueError):
    """Another live worker owns the queue."""


def _utc() -> str:
    return datetime.now(UTC).isoformat()


def default_queue_root() -> Path:
    env = os.environ.get("BANGONPRO_QUEUE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".local" / "share" / "bangonpro" / "queue").resolve()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    # A zombie (killed child not yet reaped) is not a live worker.
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


class JobQueue:
    def __init__(self, root: str | Path | None = None, *, job_root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve() if root else default_queue_root()
        self.path = self.root / "queue.json"
        self.lock_path = self.root / "worker.lock"
        self._mutex_path = self.root / ".queue.mutex"
        self.job_root = Path(job_root).expanduser().resolve() if job_root else self.root / "jobs"

    # ------------------------------------------------------------ storage
    @contextmanager
    def _locked(self):
        """Serialise read-modify-write of queue.json across processes (UI, CLI, worker)."""
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._mutex_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - non-POSIX
                pass
            yield
        finally:
            os.close(fd)

    def _load(self) -> dict[str, Any]:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and isinstance(doc.get("entries"), list):
                return doc
        except (OSError, ValueError):
            pass
        return {"schema_version": "1.0", "entries": []}

    def _save(self, doc: dict[str, Any]) -> None:
        doc["updated_at_utc"] = _utc()
        write_json_atomic(self.path, doc)

    def _update(self, queue_id: str, **changes: Any) -> dict[str, Any]:
        with self._locked():
            doc = self._load()
            entry = self._find(doc, queue_id)
            entry.update(changes)
            self._save(doc)
            return dict(entry)

    @staticmethod
    def _find(doc: dict[str, Any], queue_id: str) -> dict[str, Any]:
        for e in doc["entries"]:
            if e["queue_id"] == queue_id or e.get("job_id") == queue_id:
                return e
        raise QueueError(f"No queued production '{queue_id}'")

    # ------------------------------------------------------------ editing
    def add(self, spec: dict[str, Any], *, priority: int = 0, series_batch_id: str | None = None, label: str = "") -> dict[str, Any]:
        unknown = set(spec) - set(SPEC_KEYS)
        if unknown:
            raise QueueError(f"Unknown job settings: {', '.join(sorted(unknown))}")
        if not spec.get("inputs") and not spec.get("kdenlive_project"):
            raise QueueError("A queued production needs source media (inputs) or a Kdenlive project")
        opts = dict(spec.get("options") or {})
        from bangonpro.ai_engine.department.agents import JobOptions

        allowed = {f.name for f in fields(JobOptions)} - {"provider_config"}
        bad = set(opts) - allowed
        if bad:
            raise QueueError(f"Unknown job options: {', '.join(sorted(bad))}")
        qid = f"q_{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}_{uuid.uuid4().hex[:6]}"
        entry = {
            "queue_id": qid,
            "job_id": f"job_{qid[2:]}",
            "job_root": str(self.job_root),
            "label": label or opts.get("episode_title") or spec.get("brief", "")[:60] or qid,
            "spec": {**spec, "options": opts},
            "priority": int(priority),
            "series_batch_id": series_batch_id,
            "status": "queued",
            "added_at_utc": _utc(),
            "started_at_utc": None,
            "finished_at_utc": None,
            "attempts": 0,
            "recovered": False,
            "job_status": None,
            "error": None,
            "help": None,
        }
        entry["job_dir"] = str(Path(entry["job_root"]) / entry["job_id"])
        with self._locked():
            doc = self._load()
            doc["entries"].append(entry)
            self._save(doc)
        return entry

    def add_series(
        self,
        *,
        profile: str,
        series_id: str,
        episodes: list[dict[str, Any]],
        mode: str = "autonomous_first_cut",
        brief: str = "",
        options: dict[str, Any] | None = None,
        priority: int = 0,
    ) -> list[dict[str, Any]]:
        """Queue several episodes with one profile and the series' approved preferences."""
        if not episodes:
            raise QueueError("A series batch needs at least one episode")
        batch = f"series_{uuid.uuid4().hex[:8]}"
        out = []
        for n, ep in enumerate(episodes, start=1):
            opts = {**(options or {}), "series_id": series_id, "episode_title": ep.get("title") or f"Episode {n}"}
            spec = {"brief": ep.get("brief") or brief, "inputs": ep.get("inputs"), "profile": profile, "mode": mode, "options": opts}
            if ep.get("kdenlive_project"):
                spec["kdenlive_project"] = ep["kdenlive_project"]
            out.append(self.add(spec, priority=priority, series_batch_id=batch, label=opts["episode_title"]))
        return out

    def entries(self) -> list[dict[str, Any]]:
        order = {s: i for i, s in enumerate(QUEUE_STATES)}
        items = self._load()["entries"]
        return sorted(items, key=lambda e: (order.get(e["status"], 9), -int(e.get("priority", 0)), e["added_at_utc"]))

    def get(self, queue_id: str) -> dict[str, Any]:
        return dict(self._find(self._load(), queue_id))

    def cancel(self, queue_id: str) -> dict[str, Any]:
        """Cancel a waiting entry, or ask the running job to stop at its next checkpoint."""
        with self._locked():
            doc = self._load()
            e = self._find(doc, queue_id)
            if e["status"] in ("queued", "interrupted"):
                e.update({"status": "cancelled", "finished_at_utc": _utc()})
            elif e["status"] == "running":
                Path(e["job_dir"]).mkdir(parents=True, exist_ok=True)
                (Path(e["job_dir"]) / "CANCEL").write_text("Cancelled from the production queue", encoding="utf-8")
                e["cancel_requested"] = True
            else:
                raise QueueError(f"'{queue_id}' is already {e['status']}")
            self._save(doc)
            return dict(e)

    def remove(self, queue_id: str) -> dict[str, Any]:
        """Remove an entry from the list (the job folder and its draft are kept)."""
        with self._locked():
            doc = self._load()
            e = self._find(doc, queue_id)
            if e["status"] == "running":
                raise QueueError("Cancel the running production before removing it")
            doc["entries"] = [x for x in doc["entries"] if x is not e]
            self._save(doc)
            return dict(e)

    def set_priority(self, queue_id: str, priority: int) -> dict[str, Any]:
        return self._update(queue_id, priority=int(priority))

    def next_entry(self) -> dict[str, Any] | None:
        waiting = [e for e in self.entries() if e["status"] in ("interrupted", "queued")]
        # Interrupted work resumes before new work; then priority, then FIFO.
        waiting.sort(key=lambda e: (0 if e["status"] == "interrupted" else 1, -int(e.get("priority", 0)), e["added_at_utc"]))
        return waiting[0] if waiting else None

    # ------------------------------------------------------------ worker lock
    def read_lock(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def lock_state(self, *, stale_after_sec: float = STALE_AFTER_SEC) -> dict[str, Any]:
        lock = self.read_lock()
        if lock is None:
            return {"state": "free" if not self.lock_path.exists() else "stale", "lock": None}
        pid = int(lock.get("pid", 0) or 0)
        same_host = lock.get("host") == socket.gethostname()
        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(str(lock.get("heartbeat_utc")))).total_seconds()
        except (TypeError, ValueError):
            age = float("inf")
        alive = pid_alive(pid) if same_host else age <= stale_after_sec
        if not alive:
            return {"state": "stale", "reason": f"worker process {pid} is not running", "lock": lock, "heartbeat_age_sec": round(age, 1)}
        if age > stale_after_sec:
            return {"state": "stale", "reason": f"no heartbeat for {age:.0f} s", "lock": lock, "heartbeat_age_sec": round(age, 1)}
        return {"state": "held", "lock": lock, "heartbeat_age_sec": round(age, 1)}

    def acquire_worker_lock(self, *, stale_after_sec: float = STALE_AFTER_SEC) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        recovered: dict[str, Any] | None = None
        for _ in range(5):
            payload = {"pid": os.getpid(), "host": socket.gethostname(), "started_at_utc": _utc(), "heartbeat_utc": _utc(), "token": uuid.uuid4().hex}
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                st = self.lock_state(stale_after_sec=stale_after_sec)
                if st["state"] == "held":
                    raise WorkerBusyError(f"Another queue worker is running (pid {st['lock'].get('pid')})") from None
                recovered = {"reason": st.get("reason", "unreadable lock"), "previous": st.get("lock")}
                try:
                    os.replace(self.lock_path, self.lock_path.with_name(f"worker.lock.stale-{int(time.time())}"))
                except FileNotFoundError:
                    pass  # another process took it over first; retry
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            payload["recovered_stale_lock"] = recovered
            return payload
        raise WorkerBusyError("Could not take the queue worker lock")

    def heartbeat(self, token: str) -> bool:
        lock = self.read_lock()
        if not lock or lock.get("token") != token:
            return False  # lost ownership (another worker took over a stale lock)
        lock["heartbeat_utc"] = _utc()
        write_json_atomic(self.lock_path, lock)
        return True

    def release_worker_lock(self, token: str) -> None:
        lock = self.read_lock()
        if lock and lock.get("token") == token:
            self.lock_path.unlink(missing_ok=True)

    # ------------------------------------------------------------ worker
    def recover_interrupted(self) -> list[str]:
        """Entries a dead worker left ``running`` become ``interrupted`` (resumed first)."""
        out = []
        with self._locked():
            doc = self._load()
            for e in doc["entries"]:
                if e["status"] == "running":
                    e.update({"status": "interrupted", "recovered": True, "interrupted_at_utc": _utc()})
                    out.append(e["queue_id"])
            if out:
                self._save(doc)
        return out

    def _run_entry(self, entry: dict[str, Any], progress_callback: Any = None) -> dict[str, Any]:
        from bangonpro.ai_engine.department.agents import JobOptions
        from bangonpro.ai_engine.department.jobs import ProductionJob

        job_dir = Path(entry["job_dir"])
        if (job_dir / "job.json").exists():
            job = ProductionJob(job_dir)
            job.resume(progress_callback=progress_callback)  # finished agents are kept
        else:
            spec = entry["spec"]
            opts_raw = dict(spec.get("options") or {})
            mode = spec.get("mode") or "autonomous_first_cut"
            opts = JobOptions(**{**opts_raw, "mode": mode})
            job = ProductionJob.create(
                job_root=entry["job_root"],
                brief=spec.get("brief", ""),
                inputs=spec.get("inputs") or None,
                profile_ref=spec.get("profile"),
                mode=mode,
                options=opts,
                project_id=spec.get("project_id"),
                sequence_id=spec.get("sequence_id"),
                kdenlive_project=spec.get("kdenlive_project"),
                job_id=entry["job_id"],
            )
            job.run(progress_callback=progress_callback)
        return job.doc

    def run_worker(
        self,
        *,
        max_jobs: int | None = None,
        stale_after_sec: float = STALE_AFTER_SEC,
        heartbeat_sec: float = HEARTBEAT_SEC,
        progress_callback: Any = None,
    ) -> dict[str, Any]:
        """Process the queue until it is empty (or ``max_jobs`` ran). Returns a run report."""
        lock = self.acquire_worker_lock(stale_after_sec=stale_after_sec)
        token = lock["token"]
        stop = threading.Event()
        lost = threading.Event()

        def _beat() -> None:
            while not stop.wait(heartbeat_sec):
                if not self.heartbeat(token):
                    lost.set()
                    return

        beater = threading.Thread(target=_beat, name="bangonpro-queue-heartbeat", daemon=True)
        beater.start()
        report: dict[str, Any] = {"worker_pid": os.getpid(), "recovered_stale_lock": lock.get("recovered_stale_lock"), "recovered_entries": [], "ran": []}
        try:
            report["recovered_entries"] = self.recover_interrupted()
            while max_jobs is None or len(report["ran"]) < max_jobs:
                if lost.is_set():
                    report["stopped"] = "lost worker lock"
                    break
                entry = self.next_entry()
                if entry is None:
                    break
                self._update(entry["queue_id"], status="running", started_at_utc=entry.get("started_at_utc") or _utc(), attempts=int(entry.get("attempts", 0)) + 1, worker_pid=os.getpid())
                try:
                    doc = self._run_entry(entry, progress_callback)
                    js = str(doc.get("status"))
                    if js in DONE_JOB_STATES:
                        status = "completed"
                    elif js == "cancelled":
                        status = "cancelled"
                    elif js in ATTENTION_JOB_STATES:
                        status = "needs_attention"
                    else:
                        status = "failed"
                    help_ = doc.get("error_help") or (explain(doc["error"]) if doc.get("error") else None)
                    self._update(entry["queue_id"], status=status, job_status=js, finished_at_utc=_utc(), error=doc.get("error"), help=help_, processing_elapsed_sec=doc.get("processing_elapsed_sec"))
                except Exception as exc:  # noqa: BLE001 - one broken job must not stop the queue
                    self._update(entry["queue_id"], status="failed", finished_at_utc=_utc(), error=f"{type(exc).__name__}: {exc}", help=explain(exc))
                report["ran"].append(entry["queue_id"])
        finally:
            stop.set()
            beater.join(timeout=2.0)
            self.release_worker_lock(token)
        return report

    def status(self) -> dict[str, Any]:
        entries = self.entries()
        return {
            "queue_root": str(self.root),
            "job_root": str(self.job_root),
            "worker": self.lock_state(),
            "counts": {s: sum(1 for e in entries if e["status"] == s) for s in QUEUE_STATES},
            "entries": entries,
        }


def job_history(job_root: str | Path, *, limit: int = 50) -> list[dict[str, Any]]:
    """Recent production jobs under ``job_root``, newest first (read-only)."""
    root = Path(job_root).expanduser()
    if not root.is_dir():
        return []
    rows = []
    for d in root.iterdir():
        jp = d / "job.json"
        if not jp.is_file():
            continue
        try:
            doc = json.loads(jp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        agents = doc.get("agents", {})
        rows.append(
            {
                "job_id": doc.get("job_id", d.name),
                "job_dir": str(d.resolve()),
                "status": doc.get("status"),
                "mode": doc.get("mode"),
                "profile_id": doc.get("profile_id"),
                "series_id": doc.get("series_id"),
                "episode_title": (doc.get("options") or {}).get("episode_title") or "",
                "created_at_utc": doc.get("created_at_utc"),
                "updated_at_utc": doc.get("updated_at_utc"),
                "processing_elapsed_sec": doc.get("processing_elapsed_sec"),
                "agents_completed": sum(1 for r in agents.values() if r.get("status") == "completed"),
                "agents_active": sum(1 for r in agents.values() if r.get("active")),
                "has_draft": bool((doc.get("artifacts") or {}).get("kdenlive_project")),
                "error": doc.get("error"),
            }
        )
    rows.sort(key=lambda r: str(r.get("created_at_utc") or ""), reverse=True)
    return rows[: max(1, int(limit))]


__all__ = ["DONE_JOB_STATES", "JobQueue", "QueueError", "WorkerBusyError", "default_queue_root", "job_history", "pid_alive"]
