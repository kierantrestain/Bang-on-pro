"""The editing workspace behind the MCP tools: one department job draft, edited by agents.

Every write goes through :meth:`Workspace.apply`, which in order: checks the caller's expected
revision (stale requests are rejected), checks that the draft file was not changed by hand since it
was written, checks the agent's permissions, dry-runs the batch on the replayed draft (every op must
apply; nothing is half-done), checks locked ranges, locked tracks, approved edits and other agents'
leases against the spans that actually change, commits the entry, rebuilds the draft through the
department (``ProductionJob.rebuild_draft``) and verifies the emitted ``.kdenlive`` file.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

import defusedxml.ElementTree as DET

from bangonpro.ai_engine.commands import (
    TimelineCommandValidationError,
    validate_timeline_command_batch,
)
from bangonpro.ai_engine.department.agent_roles import permission_violations
from bangonpro.ai_engine.department.draft_qc import DraftView
from bangonpro.ai_engine.department.edit_layer import (
    EditLayer,
    clip_signature,
    file_sha256,
)
from bangonpro.ai_engine.mcp.protocol import ToolError

LEASE_SEC = 600.0


class Workspace:
    def __init__(self, job_dir: str | Path, *, allowed_roots: list[str | Path] | None = None) -> None:
        from bangonpro.ai_engine.department import ProductionJob

        self.job_dir = Path(job_dir).resolve()
        if not (self.job_dir / "job.json").is_file():
            raise ToolError("NO_JOB", f"No Bang On Pro job at {self.job_dir}", hint="Run a production job first or pass --job <job_dir>")
        self.allowed_roots = [Path(r).resolve() for r in (allowed_roots or [])] + [self.job_dir]
        self._ProductionJob = ProductionJob
        self.lock = threading.RLock()
        self._job: Any = None
        self._job_mtime = -1.0
        self._cache: dict[str, Any] = {}
        self.layer = EditLayer(self.job_dir / "draft")
        with self.lock:
            # The folders the job's own footage lives in are approved project resources too.
            for m in self.state().media_assets.values():
                parent = Path(m.file_path).resolve().parent
                if parent not in self.allowed_roots:
                    self.allowed_roots.insert(0, parent)
            if not self.layer.exists:
                job = self.job
                st = job._latest_state()
                self.layer.doc["revision"] = int(job.doc["project"]["base_revision"]) + len(job.included_batches(st))
                self.layer.save()
                job.rebuild_draft(st)  # records the emitted file's hash for manual-edit detection
                self.layer.reload()

    # ------------------------------------------------------------------ state
    @property
    def job(self) -> Any:
        mt = (self.job_dir / "job.json").stat().st_mtime
        if self._job is None or mt != self._job_mtime:
            self._job = self._ProductionJob(self.job_dir)
            self._job_mtime = mt
            self._cache.clear()
        return self._job

    def state(self) -> Any:
        job = self.job
        if "state" not in self._cache:
            self._cache["state"] = job._latest_state()
        return self._cache["state"]

    def base_batches(self) -> list[dict[str, Any]]:
        if "base" not in self._cache:
            self._cache["base"] = self.job.included_batches(self.state())
        return self._cache["base"]

    @property
    def revision(self) -> int:
        self.layer.reload()
        return self.layer.revision

    def compiled(self) -> Any:
        key = f"compiled@{self.revision}"
        if key not in self._cache:
            compiled, _, _ = self.layer.replay(self.base_batches())
            if compiled is None:
                raise ToolError("NO_DRAFT", "The job has no timeline yet", hint="Run the department up to the Rough Cut Agent first")
            self._cache[key] = compiled
        return self._cache[key]

    def view(self) -> DraftView:
        return DraftView(self.state(), self.compiled())

    def project_path(self) -> Path:
        return self.job.draft_dir / f"{self.state().sequence_id}_draft.kdenlive"

    def manual_drift(self) -> bool:
        emitted = (self.layer.doc.get("emitted") or {}).get("sha256")
        cur = file_sha256(self.project_path())
        return bool(emitted and cur and cur != emitted)

    def path_allowed(self, p: str | Path) -> Path:
        rp = Path(p).expanduser().resolve()
        if not any(rp == r or r in rp.parents for r in self.allowed_roots):
            raise ToolError("PATH_NOT_ALLOWED", f"{rp} is outside the approved project folders", hint="Start the server with --allow <folder> for supplied assets")
        if not rp.is_file():
            raise ToolError("NOT_FOUND", f"{rp} does not exist")
        return rp

    # ------------------------------------------------------------------ protection
    def locked_timeline_ranges(self, v: DraftView) -> list[dict[str, Any]]:
        out = []
        for a, b in self.job.doc.get("locked_ranges", []):
            ta, tb = v.timeline_of_master(float(a)), v.timeline_of_master(float(b) - 1e-3)
            if ta is not None and tb is not None:
                out.append({"start": ta, "end": tb, "source": "locked range (master clock)"})
        for r in self.layer.doc.get("locked_timeline", []):
            out.append({"start": r[0], "end": r[1], "source": "locked by agent request"})
        for e in self.layer.active_entries():
            if e.get("approved"):
                for sp in e.get("spans", []):
                    out.append({"start": sp[1], "end": sp[2], "track": sp[0], "source": f"approved edit {e['entry_id']}"})
        return out

    def leases(self) -> list[dict[str, Any]]:
        now = time.time()
        self.layer.doc["leases"] = [ls for ls in self.layer.doc.get("leases", []) if ls["expires"] > now]
        return self.layer.doc["leases"]

    def take_lease(self, agent_id: str, task_id: str, tracks: list[str], start: float, end: float) -> dict[str, Any]:
        with self.lock:
            self.layer.reload()
            for ls in self.leases():
                if ls["agent_id"] != agent_id and set(ls["tracks"]) & set(tracks) and ls["start"] < end and start < ls["end"]:
                    raise ToolError("CONFLICT", f"{ls['agent_id']} holds {','.join(ls['tracks'])} {ls['start']}-{ls['end']}s for task {ls['task_id']}", retryable=True, details=ls)
            lease = {"lease_id": uuid.uuid4().hex[:8], "agent_id": agent_id, "task_id": task_id, "tracks": tracks, "start": start, "end": end, "expires": time.time() + LEASE_SEC}
            self.layer.doc["leases"].append(lease)
            self.layer.save()
            return lease

    def release_leases(self, task_id: str) -> None:
        with self.lock:
            self.layer.reload()
            self.layer.doc["leases"] = [ls for ls in self.layer.doc.get("leases", []) if ls["task_id"] != task_id]
            self.layer.save()

    # ------------------------------------------------------------------ writing
    def _batch(self, ops: list[dict[str, Any]], agent_id: str, label: str) -> dict[str, Any]:
        st = self.state()
        out_ops = []
        for i, op in enumerate(ops, 1):
            o = {k: val for k, val in op.items() if k in ("type", "clip_id", "track_id", "media_ref", "frame_range", "params", "rationale") and val is not None}
            o["op_id"] = f"{agent_id}_e{self.revision + 1}_{i:03d}"
            o["confidence"] = float(op.get("confidence", 1.0))
            if "frame_range" in o:
                o["frame_range"] = {k: max(0, int(val)) for k, val in o["frame_range"].items()} | {"fps_num": st.fps_num, "fps_den": st.fps_den}
            o.setdefault("params", {})["agent_id"] = agent_id
            out_ops.append(o)
        batch = {"api_version": "1.0", "request_id": f"agent_{uuid.uuid4().hex[:10]}", "project_id": st.project_id, "sequence_id": st.sequence_id,
                 "expected_revision": self.revision, "actor": {"agent_id": agent_id, "role": agent_id, "requires_approval": False},
                 "transaction_label": label or f"{agent_id} edit", "dry_run": False, "operations": out_ops}
        try:
            return validate_timeline_command_batch(batch)
        except TimelineCommandValidationError as exc:
            raise ToolError("INVALID_OPERATION", str(exc)) from exc

    @staticmethod
    def _spans(compiled: Any) -> dict[str, tuple[str, int, int, str]]:
        return {c.clip_id: (tid if tid != "V1b" else "V1", c.timeline_start, c.timeline_end_inclusive + 1, clip_signature(c)) for tid, lst in compiled.track_lists().items() for c in lst}

    def apply(self, ops: list[dict[str, Any]], *, agent_id: str, expected_revision: int | None, label: str = "", task_id: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        if not ops:
            raise ToolError("INVALID_OPERATION", "No operations given")
        with self.lock:
            rev = self.revision
            if expected_revision is not None and int(expected_revision) != rev:
                raise ToolError("STALE_REVISION", f"The draft is at revision {rev}, not {expected_revision}; inspect it again before editing", retryable=True,
                                details={"current_revision": rev})
            if self.manual_drift():
                raise ToolError("MANUAL_EDIT_DRIFT", "The draft project was changed by hand since it was written; agents will not overwrite it",
                                hint="Ask the editor to promote and lock, or to discard the manual change", details={"project": str(self.project_path())})
            before = self.compiled()
            bspans = self._spans(before)
            clip_tracks = {k: val[0] for k, val in bspans.items()}
            bad = permission_violations(agent_id, [op for op in ops if not _inherited_grade(op, ops, before)], clip_tracks)
            if bad:
                raise ToolError("PERMISSION_DENIED", "; ".join(bad[:4]), hint="Delegate this change to the agent that owns it (bop_agents)")
            locked_tracks = set(self.layer.doc.get("locked_tracks", []))
            batch = self._batch(ops, agent_id, label)
            after, _, report = self.layer.replay(self.base_batches(), extra=batch)
            row = report[-1] if report else {"status": "failed", "messages": ["no draft"]}
            if row["status"] != "applied":
                raise ToolError("OPERATION_FAILED", "; ".join(row["messages"][:6]), hint="Nothing was changed. Re-inspect the clips (bop_project) and retry")
            aspans = self._spans(after)
            changed = sorted({k for k in bspans if k not in aspans or aspans[k][3] != bspans[k][3]} | {k for k in aspans if k not in bspans})
            for tid, lst in after.track_lists().items():  # one clip per place on a track, as in the timeline
                lst = sorted(lst, key=lambda c: c.timeline_start)
                for a_c, b_c in zip(lst, lst[1:], strict=False):
                    if b_c.timeline_start <= a_c.timeline_end_inclusive and (a_c.clip_id in changed or b_c.clip_id in changed):
                        raise ToolError("OPERATION_FAILED", f"'{b_c.clip_id}' would overlap '{a_c.clip_id}' on {tid} at {b_c.timeline_start / after.fps:.2f}s",
                                        hint="Place it after the existing clip, shorten it, or move the other clip first. Nothing was changed.")
            touched = [bspans[k] for k in changed if k in bspans] + [aspans[k] for k in changed if k in aspans]
            v = DraftView(self.state(), before)
            fps = v.fps
            for tr, a, b, _ in touched:
                if tr in locked_tracks:
                    raise ToolError("TRACK_LOCKED", f"Track {tr} is locked")
                for lk in self.locked_timeline_ranges(v):
                    if lk.get("track") not in (None, tr):
                        continue
                    if a / fps < lk["end"] and lk["start"] < b / fps:
                        raise ToolError("LOCKED_REGION", f"The edit changes {tr} {a / fps:.2f}-{b / fps:.2f}s inside a protected range {lk['start']:.2f}-{lk['end']:.2f}s ({lk['source']})")
                for ls in self.leases():
                    if ls["agent_id"] != agent_id and tr in ls["tracks"] and a / fps < ls["end"] and ls["start"] < b / fps:
                        raise ToolError("CONFLICT", f"{ls['agent_id']} is working on {tr} {ls['start']}-{ls['end']}s (task {ls['task_id']})", retryable=True, details=ls)
            diff = {"added": [k for k in changed if k not in bspans], "removed": [k for k in changed if k not in aspans], "changed": [k for k in changed if k in bspans and k in aspans]}
            if dry_run:
                return {"dry_run": True, "revision": rev, "would_change": diff, "messages": row["messages"]}
            pre = self.layer.preconditions(self.base_batches(), batch)
            entry = {"entry_id": f"e{rev + 1}", "agent_id": agent_id, "task_id": task_id, "label": batch["transaction_label"], "batch": batch, "preconditions": pre,
                     "spans": [[tr, round(a / fps, 3), round(b / fps, 3)] for tr, a, b, _ in touched], "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            self.layer.doc["entries"].append(entry)
            self.layer.doc["redo"] = []
            new_rev = self.layer.bump()
            self.layer.log(agent_id, "edit", batch["transaction_label"], entry_id=entry["entry_id"], revision=new_rev)
            self.layer.save()
            verification = self._rebuild_and_verify()
            if not verification["ok"]:
                # Never report an edit the written project does not show: take it back out.
                self.undo("verification")
                raise ToolError("VERIFICATION_FAILED", "The written project did not match the edit, so it was undone: " + "; ".join(verification["issues"][:3]),
                                details={"revision": self.revision})
            return {"revision": new_rev, "previous_revision": rev, "entry_id": entry["entry_id"], "diff": diff, "messages": row["messages"], "verified": verification}

    def _rebuild_and_verify(self) -> dict[str, Any]:
        job = self.job
        job.rebuild_draft(self.state())
        self._cache.clear()
        self.layer.reload()
        compiled = self.compiled()
        return verify_emitted(self.project_path(), compiled)

    def undo(self, agent_id: str = "user") -> dict[str, Any]:
        with self.lock:
            self.layer.reload()
            act = self.layer.active_entries()
            if not act:
                raise ToolError("NOTHING_TO_UNDO", "No agent edit to undo")
            last = act[-1]
            if last.get("approved"):
                raise ToolError("PROTECTED", f"{last['entry_id']} was approved by the editor; it can only be undone in Bang On Pro")
            last["undone"] = True
            self.layer.doc["redo"].append(last["entry_id"])
            rev = self.layer.bump()
            self.layer.log(agent_id, "undo", f"undo {last['entry_id']} ({last['label']})", revision=rev)
            self.layer.save()
            return {"revision": rev, "undone": last["entry_id"], "label": last["label"], "verified": self._rebuild_and_verify()}

    def redo(self, agent_id: str = "user") -> dict[str, Any]:
        with self.lock:
            self.layer.reload()
            if not self.layer.doc["redo"]:
                raise ToolError("NOTHING_TO_REDO", "Nothing to redo")
            eid = self.layer.doc["redo"].pop()
            for e in self.layer.doc["entries"]:
                if e["entry_id"] == eid:
                    e["undone"] = False
            rev = self.layer.bump()
            self.layer.log(agent_id, "redo", f"redo {eid}", revision=rev)
            self.layer.save()
            return {"revision": rev, "redone": eid, "verified": self._rebuild_and_verify()}


def _inherited_grade(op: dict[str, Any], ops: list[dict[str, Any]], compiled: Any) -> bool:
    """True for an add_effect that copies, onto a clip placed in this same edit, a grade that another
    clip of the same media already carries (the grade travels with the camera on a switch)."""
    if op.get("type") != "add_effect":
        return False
    placed = next((o for o in ops if o.get("type") == "place_clip" and o.get("clip_id") == op.get("clip_id")), None)
    if placed is None:
        return False
    p = op.get("params") or {}
    want = (p.get("kdenlive_id"), p.get("mlt_service"), p.get("properties") or {})
    for _tid, lst in compiled.track_lists().items():
        for c in lst:
            if c.media_ref == placed.get("media_ref") and any((f.get("kdenlive_id"), f.get("mlt_service"), f.get("properties") or {}) == want for f in c.filters):
                return True
    return False


def verify_emitted(project: Path, compiled: Any) -> dict[str, Any]:
    """Re-read the written project and check it holds exactly the replayed timeline."""
    try:
        root = DET.parse(str(project)).getroot()
    except (OSError, DET.ParseError) as exc:  # type: ignore[attr-defined]
        return {"ok": False, "issues": [f"Project unreadable: {exc}"]}
    issues = []
    want = {"playlist_v1": compiled.v1_clips, "playlist_v1_b": compiled.v1b_clips, "playlist_v2": compiled.v2_clips, "playlist_a1": compiled.a1_clips}
    counts = {}
    for pl in root.iter("playlist"):
        pid = pl.get("id")
        if pid in want:
            entries = pl.findall("entry")
            counts[pid] = len(entries)
            pos = 0
            starts = []
            for el in pl:
                if el.tag == "blank":
                    pos += int(el.get("length", 0))
                elif el.tag == "entry":
                    starts.append(pos)
                    pos += int(el.get("out", 0)) - int(el.get("in", 0)) + 1
            exp = sorted(c.timeline_start for c in want[pid] if c.media_ref or c.params.get("clip_kind") == "kdenlivetitle")
            if starts != exp:
                issues.append(f"{pid}: clip positions in the file differ from the edit ({len(starts)} vs {len(exp)} clips)")
    mixes = sum(1 for t in root.iter("transition") if (t.findtext("property[@name='mlt_service']") or "") == "luma")
    if mixes != sum(1 for t in compiled.transitions if t.get("kind") == "dissolve"):
        issues.append("Dissolve count in the file differs from the edit")
    return {"ok": not issues, "issues": issues, "entries": counts, "project": str(project), "sha256": file_sha256(project)}


__all__ = ["Workspace", "verify_emitted"]
