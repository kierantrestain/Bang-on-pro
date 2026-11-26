"""V1.4 agent edit layer: direct, attributed timeline edits on a department draft.

Specialist agents (driven by a connected reasoning client or by the department's own correction
loop) edit the draft with ordinary Timeline Command API v1 batches. Each committed batch is an
*entry* in ``draft/agent_edits.json`` and is replayed after the agents' own batches every time the
draft is rebuilt, so the edit survives reruns of unrelated agents and is a real, editable change in
the emitted ``.kdenlive`` project.

An entry stores the signature of every clip it touched as it was when the edit was made. If a later
rerun changes one of those clips, the entry is **not** replayed: it is marked ``stale`` and reported,
never re-targeted at different footage. The layer has its own monotonic revision (optimistic
concurrency), undo/redo stacks, and the SHA-256 of the project file it last wrote, so a project that
was edited by hand is detected instead of being silently overwritten.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from bangonpro.ai_engine.project_builder import (
    CompiledClip,
    CompiledTimeline,
    compile_command_batches,
    replay_batch,
)

LAYER_FILE = "agent_edits.json"
LAYER_VERSION = 1


def clip_signature(c: CompiledClip) -> str:
    fx = ",".join(f"{f.get('kdenlive_id')}:{json.dumps(f.get('properties') or {}, sort_keys=True, default=str)}" for f in c.filters)
    return hashlib.sha1(f"{c.track_id}|{c.timeline_start}|{c.in_frame}|{c.out_frame}|{c.media_ref}|{fx}".encode()).hexdigest()[:16]


def referenced_clip_ids(batch: dict[str, Any]) -> list[str]:
    """Clips an edit depends on (it targets them); clips it creates are not preconditions."""
    created = {op.get("clip_id") for op in batch["operations"] if op["type"] in ("place_clip", "add_lower_third", "add_caption")}
    created |= {(op.get("params") or {}).get("new_clip_id") for op in batch["operations"] if op["type"] == "split_clip"}
    out: list[str] = []
    for op in batch["operations"]:
        cid = op.get("clip_id")
        if cid and cid not in created and op["type"] not in ("update_caption", "delete_caption") and cid not in out:
            out.append(cid)
    return out


def file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


class EditLayer:
    """The persisted layer for one job draft (``<job>/draft/agent_edits.json``)."""

    def __init__(self, draft_dir: str | Path) -> None:
        self.draft_dir = Path(draft_dir)
        self.path = self.draft_dir / LAYER_FILE
        self.doc: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.is_file():
            try:
                doc = json.loads(self.path.read_text(encoding="utf-8"))
                if doc.get("version") == LAYER_VERSION:
                    return doc
            except (OSError, ValueError):
                pass
        return {"version": LAYER_VERSION, "revision": 0, "entries": [], "redo": [], "emitted": {}, "locked_tracks": [], "leases": [], "activity": []}

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def reload(self) -> None:
        self.doc = self._load()

    def save(self) -> None:
        self.draft_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.doc, indent=1, default=str), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def revision(self) -> int:
        return int(self.doc.get("revision", 0))

    def bump(self) -> int:
        self.doc["revision"] = self.revision + 1
        return self.revision

    def active_entries(self) -> list[dict[str, Any]]:
        return [e for e in self.doc["entries"] if not e.get("undone")]

    def log(self, agent_id: str, action: str, summary: str, **extra: Any) -> None:
        act = self.doc.setdefault("activity", [])
        act.append({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "agent_id": agent_id, "action": action, "summary": summary[:300], **extra})
        del act[:-200]

    # ------------------------------------------------------------------ replay
    def replay(self, base_batches: list[dict[str, Any]], *, extra: dict[str, Any] | None = None) -> tuple[CompiledTimeline | None, list[dict[str, Any]], list[dict[str, Any]]]:
        """Compile base + entries (+ ``extra``). Returns ``(compiled, applied_batches, report)``.

        ``report`` has one row per entry (and for ``extra``): ``{"entry", "status", "messages"}`` where
        status is ``applied``, ``stale`` (a touched clip changed) or ``failed`` (an op did not apply).
        """
        if not base_batches:
            return None, [], []
        compiled = compile_command_batches(base_batches)
        applied: list[dict[str, Any]] = list(base_batches)
        report: list[dict[str, Any]] = []
        todo = [(e["entry_id"], e["batch"], e.get("preconditions") or {}) for e in self.active_entries()]
        if extra is not None:
            todo.append(("__new__", extra, {}))
        for entry_id, batch, pre in todo:
            changed = []
            for cid, sig in pre.items():
                found = compiled.find(cid)
                if found is None or clip_signature(found[1]) != sig:
                    changed.append(cid)
            if changed:
                report.append({"entry": entry_id, "status": "stale", "messages": [f"Clip '{c}' changed or was removed since this edit was made" for c in changed]})
                continue
            trial = copy.deepcopy(compiled)
            results: list[dict[str, Any]] = []
            replay_batch(trial, batch, results)
            failed = [r for r in results if not r["applied"]]
            if failed:
                report.append({"entry": entry_id, "status": "failed", "messages": [f"{r['op_id']} ({r['type']}): {r['message']}" for r in failed]})
                continue
            compiled = trial
            applied.append(batch)
            report.append({"entry": entry_id, "status": "applied", "messages": [r["message"] for r in results if r["message"]]})
        return compiled, applied, report

    def preconditions(self, base_batches: list[dict[str, Any]], batch: dict[str, Any]) -> dict[str, str]:
        compiled, _, _ = self.replay(base_batches)
        out: dict[str, str] = {}
        if compiled is None:
            return out
        for cid in referenced_clip_ids(batch):
            found = compiled.find(cid)
            if found is not None:
                out[cid] = clip_signature(found[1])
        return out


def regenerate_captions(compiled: CompiledTimeline, draft_dir: Path, caption_style: dict[str, Any] | None = None) -> None:
    """Captions changed by an agent: rewrite the burned-in ASS and the SRT from the edited cues."""
    from bangonpro.ai_engine.agents.audio_graphics_qc import (
        generate_ass_subtitles,
        generate_srt_subtitles,
    )
    from bangonpro.ai_engine.state import CaptionCue

    fps = compiled.fps
    style = caption_style or next((c.get("caption_style") for c in compiled.captions if c.get("caption_style")), None) or {}
    cues = [
        CaptionCue(index=i + 1, start_sec=c["in_frame"] / fps, end_sec=(c["out_frame"] + 1) / fps, speaker_id=str(c.get("speaker_id") or ""), text=str(c.get("text") or ""))
        for i, c in enumerate(sorted(compiled.captions, key=lambda c: c["in_frame"]))
    ]
    ass = draft_dir / "captions_agent_edit.ass"
    ass.write_text(generate_ass_subtitles(cues, width=compiled.width, height=compiled.height, caption_style=style), encoding="utf-8")
    (draft_dir / "captions_agent_edit.srt").write_text(generate_srt_subtitles(cues, caption_style=style), encoding="utf-8")
    compiled.ass_subtitle_path = str(ass)


def protect_manual_edits(project: Path, layer: EditLayer) -> str | None:
    """Before a project file is rewritten: if it was changed by hand, keep that version beside it."""
    emitted = (layer.doc.get("emitted") or {}).get("sha256")
    current = file_sha256(project) if project.is_file() else None
    if emitted and current and current != emitted:
        keep = project.with_name(f"{project.stem}_manual_{time.strftime('%Y%m%d_%H%M%S')}{project.suffix}")
        shutil.copy2(project, keep)
        layer.log("human", "manual_edit_preserved", f"The draft was edited by hand; that version was kept as {keep.name}")
        return str(keep)
    return None


__all__ = ["EditLayer", "clip_signature", "file_sha256", "protect_manual_edits", "referenced_clip_ids", "regenerate_captions"]
