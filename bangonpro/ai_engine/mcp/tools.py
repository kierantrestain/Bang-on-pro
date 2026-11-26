"""Bang On Pro MCP tools: inspect, look, edit, verify, as one of the 14 production agents.

Times are timeline seconds unless a tool says otherwise. Every edit names the agent doing it and
the revision it was based on; it is checked, applied through the department's native command path,
written to the draft project and verified there. Read-only tools are marked so a client can allow
them without asking.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from bangonpro.ai_engine.department.agent_roles import ROLES, permission_violations, role, roster
from bangonpro.ai_engine.department.draft_qc import (
    DraftView,
    check_draft,
    place_asset_ops,
    remove_range_ops,
    switch_camera_ops,
)
from bangonpro.ai_engine.department.registry import AGENT_ORDER
from bangonpro.ai_engine.mcp.protocol import CallContext, Cancelled, Tool, ToolError, ToolOutput
from bangonpro.ai_engine.mcp.vision import (
    FrameCache,
    black_spans,
    contact_sheet,
    loudness,
    render_range,
    thumb_similarity,
)
from bangonpro.ai_engine.mcp.workspace import Workspace

INSTRUCTIONS = (
    "Bang On Pro is a video editor with a production department of 14 specialist agents. You do the editing work as those agents. "
    "Times are timeline seconds. Start with bop_status and bop_project; bop_transcript, bop_search and bop_media tell you what is in the footage; "
    "bop_frames shows you source or rendered frames as a contact sheet (notes you leave are returned next time, so look again only for a new moment). "
    "Edit with bop_switch_camera, bop_remove_range, bop_place_asset, bop_add_title or bop_edit (native operations, see bop_ops), always as the agent "
    "that owns the change (bop_agents) and with the revision you inspected. For a creative brief: bop_task begin (with checks that prove it is done), "
    "edit, then bop_task complete: QC verifies the actual draft and corrects high-confidence defects. bop_history undoes. Nothing is published; "
    "the editor reviews the draft in Bang On Pro."
)

_ACTIVITY = {"bop_status": "checking the project", "bop_project": "reading the timeline", "bop_media": "going through the media", "bop_transcript": "reading what is said",
             "bop_search": "searching the footage", "bop_frames": "looking at frames", "bop_audio": "measuring the sound", "bop_qc": "checking the cut",
             "bop_edit": "editing", "bop_switch_camera": "switching cameras", "bop_remove_range": "cutting", "bop_place_asset": "placing an asset",
             "bop_add_title": "adding a title", "bop_history": "undoing", "bop_run_agent": "running an agent", "bop_revise": "revising", "bop_task": "working on a task",
             "bop_preview": "rendering a preview", "bop_vertical": "making a vertical clip", "bop_lock": "protecting a range", "bop_agents": "reading agent briefs", "bop_ops": "reading the tool guide"}

T_NUM = {"type": "number", "minimum": 0}
T_REV = {"type": "integer", "description": "The revision you inspected (bop_status/bop_project). Stale revisions are rejected."}
T_AGENT = {"type": "string", "enum": list(AGENT_ORDER), "description": "The production agent making this change."}
T_TASK = {"type": "string", "description": "Optional task id from bop_task begin."}
EXPECT_ITEM = {"type": "object", "required": ["check"], "description": "A completion check evaluated on the actual draft.",
               "properties": {"check": {"type": "string", "enum": ["camera_at", "camera_share", "has_clip", "title_at", "effect_at", "transition_at", "caption_contains", "duration", "no_defect"]},
                              "time": {"type": "number"}, "start": {"type": "number"}, "end": {"type": "number"}, "camera": {"type": "string"}, "min": {"type": "number"}, "max": {"type": "number"},
                              "track": {"type": "string"}, "media_ref": {"type": "string"}, "text": {"type": "string"}, "effect": {"type": "string"}, "code": {"type": "string"}}}

OP_GUIDE = {
    "conventions": "Times are timeline seconds; source_in/source_out are seconds in the clip's media; end times are exclusive. Tracks: V1 program picture, V2 graphics/B-roll/titles, A1 dialogue, A2 music.",
    "ops": {
        "place_clip": "media_ref, track, start, source_in, source_out",
        "move_clip": "clip_id, start, track?",
        "trim_clip": "clip_id, source_in, source_out, start? (slip = shift source_in/out, keep start)",
        "split_clip": "clip_id, at, params.new_clip_id?",
        "delete_clip": "clip_id (leaves a gap; use bop_remove_range to close it)",
        "select_camera": "clip_id, params.camera_id (annotation; use bop_switch_camera to change the picture)",
        "add_effect": "clip_id, params {mlt_service, kdenlive_id, properties}",
        "set_effect_param": "clip_id, params {kdenlive_id, property, value}",
        "set_keyframe": "clip_id, params {kdenlive_id, property, keyframes:[{time (clip-relative s), value, interpolation: linear|smooth|hold}]}",
        "set_audio_level": "clip_id, params {gain_db} or {keyframes:[{time, gain_db}]}",
        "add_audio_filter": "track (A1/A2), params {filter_id, mlt_service, mlt_params}",
        "add_transition": "clip_id, params {kind: dissolve|fade_in|fade_out, duration (s)}",
        "add_caption": "start, end, params {text, speaker_id}", "update_caption": "clip_id (caption id), start?, end?, params {text?}", "delete_caption": "clip_id",
        "add_lower_third": "start, duration, params {speaker_name, subtitle, position}", "add_marker": "at, params {label, category}",
        "apply_mask": "clip_id, params {matte_path | splines_json, feather, invert, masked_effect}", "import_media": "media_ref, params {file_path, duration_sec, is_image}",
    },
    "transform_rect": "qtblend 'rect' is 'x y w h opacity' in output pixels, e.g. '1280 40 600 338 1' (top right PiP on 1920x1080); keyframe it with set_keyframe property 'rect'.",
    "colour_effects": "lift_gamma_gain (lift_r/g/b, gamma_r/g/b, gain_r/g/b), avfilter.eq (av.brightness, av.contrast, av.saturation), avfilter.lut3d (av.file)",
    "compound": {"bop_switch_camera": "picture to another camera over a range at the same moment, dialogue untouched (reactions, wides)",
                 "bop_remove_range": "cut a range from every track and close the gap", "bop_place_asset": "B-roll / still graphic on V2 with position and fades",
                 "bop_add_title": "lower third or title card from the native template"},
}


class BopTools:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws
        self.cache = FrameCache(ws.job_dir / "draft" / "observations")
        self.presence_path = ws.job_dir / "draft" / "agent_presence.json"
        self.client = "unknown client"
        self.session_id = uuid.uuid4().hex[:8]
        self.calls = 0

    # ------------------------------------------------------------ presence (Agent Control Centre)
    def on_initialize(self, params: dict[str, Any]) -> None:
        info = params.get("clientInfo") or {}
        self.client = str(info.get("title") or info.get("name") or "unknown client")
        self._presence({"client": self.client, "session_id": self.session_id, "connected_at": _now(), "status": "connected"})

    def on_call(self, name: str, args: dict[str, Any], ctx: CallContext, phase: str) -> None:
        if phase == "start":
            self.calls += 1
        self._presence({"client": ctx.client_name, "session_id": self.session_id, "status": "connected", "calls": self.calls,
                        "activity": (_ACTIVITY.get(name, name) if phase == "start" else "idle"), "agent": args.get("agent"), "tool": name, "at": _now()})

    def _presence(self, d: dict[str, Any]) -> None:
        try:
            cur = json.loads(self.presence_path.read_text(encoding="utf-8")) if self.presence_path.is_file() else {}
            cur.update(d)
            self.presence_path.write_text(json.dumps(cur, indent=1), encoding="utf-8")
        except OSError:
            pass

    def disconnect(self) -> None:
        self._presence({"status": "disconnected", "activity": "idle", "at": _now()})

    # ------------------------------------------------------------ helpers
    def _view(self) -> DraftView:
        return self.ws.view()

    def _frame_op(self, op: dict[str, Any], v: DraftView) -> dict[str, Any]:
        t = op["type"]
        p = dict(op.get("params") or {})
        out: dict[str, Any] = {"type": t, "params": p}
        for k in ("clip_id", "media_ref"):
            if op.get(k):
                out[k] = op[k]
        if op.get("track"):
            out["track_id"] = op["track"]
        f = v.f
        if t == "place_clip":
            if not all(k in op for k in ("start", "source_in", "source_out")):
                raise ToolError("INVALID_OPERATION", "place_clip needs start, source_in and source_out")
            out["frame_range"] = {"in_frame": f(op["source_in"]), "out_frame": f(op["source_out"]) - 1, "timeline_start": f(op["start"])}
        elif t == "move_clip":
            p["timeline_start"] = f(op["start"])
        elif t == "trim_clip":
            if "source_in" in op:
                p["in_frame"] = f(op["source_in"])
            if "source_out" in op:
                p["out_frame"] = f(op["source_out"]) - 1
            if "start" in op:
                p["timeline_start"] = f(op["start"])
        elif t == "split_clip":
            p["split_frame"] = f(op["at"])
        elif t in ("add_caption", "update_caption"):
            if "start" in op or "end" in op:
                cap = next((c for c in v.c.captions if c.get("caption_id") == op.get("clip_id")), {})
                a = f(op["start"]) if "start" in op else cap.get("in_frame", 0)
                b = f(op["end"]) - 1 if "end" in op else cap.get("out_frame", a)
                out["frame_range"] = {"in_frame": a, "out_frame": max(a, b), "timeline_start": a}
        elif t == "add_lower_third":
            a, d = f(op.get("start", 0)), f(op.get("duration", 4.0))
            out["frame_range"] = {"in_frame": a, "out_frame": a + d - 1, "timeline_start": a}
            p.setdefault("duration_frames", d)
            out.setdefault("track_id", "V2")
            out.setdefault("clip_id", f"title_{a}_{uuid.uuid4().hex[:4]}")
        elif t == "add_marker":
            a = f(op.get("at", op.get("start", 0)))
            out["frame_range"] = {"in_frame": a, "out_frame": a, "timeline_start": a}
            out.setdefault("track_id", "V1")
        if t in ("set_keyframe",) and p.get("keyframes"):
            p["keyframes"] = [{"frame": f(k.get("time", 0)) if "frame" not in k else int(k["frame"]), "value": k["value"], "interpolation": k.get("interpolation", "linear")} for k in p["keyframes"]]
        if t == "set_audio_level" and p.get("keyframes"):
            p["keyframes"] = [{"frame": f(k.get("time", 0)) if "frame" not in k else int(k["frame"]), "gain_db": k["gain_db"]} for k in p["keyframes"]]
        if t == "add_transition" and "duration" in p:
            p["duration_frames"] = f(p.pop("duration"))
        if t == "import_media" and p.get("file_path"):
            p["file_path"] = str(self.ws.path_allowed(p["file_path"]))
        return out

    def _apply(self, ops: list[dict[str, Any]], args: dict[str, Any], agent: str, label: str) -> dict[str, Any]:
        res = self.ws.apply(ops, agent_id=agent, expected_revision=args.get("expected_revision"), label=label, task_id=args.get("task_id"), dry_run=bool(args.get("dry_run")))
        if args.get("task_id") and not res.get("dry_run"):
            self._task_event(args["task_id"], {"agent": agent, "edit": res.get("entry_id"), "label": label, "revision": res["revision"]})
        return res

    # ------------------------------------------------------------ read tools
    def status(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        job = self.ws.job
        layer = self.ws.layer
        layer.reload()
        agents = {a: {"status": r.get("status"), "decision": r.get("decision"), "stale": r.get("stale", False)} for a, r in job.doc["agents"].items() if r.get("active")}
        from bangonpro.ai_engine.mcp.reasoning import reasoning_status

        return {"connected": True, "backend": "draft", "client": ctx.client_name, "job_id": job.doc["job_id"], "job_status": job.doc["status"], "profile": job.doc["profile_id"],
                "revision": layer.revision, "project": str(self.ws.project_path()), "manual_edit_drift": self.ws.manual_drift(),
                "agent_edits": len(layer.active_entries()), "undo_available": bool(layer.active_entries()), "redo_available": bool(layer.doc.get("redo")),
                "stale_agent_edits": [e["entry_id"] for e in layer.active_entries() if e.get("replay_status") in ("stale", "failed")],
                "locked_tracks": layer.doc.get("locked_tracks", []), "agents": agents, "open_tasks": [t for t, d in (layer.doc.get("tasks") or {}).items() if d.get("status") == "open"],
                "reasoning": reasoning_status(), "live_app": _live_status()}

    def project(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        v = self._view()
        summ = v.summary()
        a, b = float(args.get("start", 0.0)), float(args.get("end", 1e9))
        tracks = set(args.get("tracks") or summ["tracks"].keys())
        rows = [r for tid, lst in summ["tracks"].items() if tid in tracks for r in lst if r["end"] > a and r["start"] < b]
        rows.sort(key=lambda r: (r["track"], r["start"]))
        off, lim = int(args.get("offset", 0)), int(args.get("limit", 80))
        page = rows[off:off + lim]
        st = v.st
        return {"revision": self.ws.revision, "duration_sec": summ["duration_sec"], "fps": summ["fps"], "size": [summ["width"], summ["height"]],
                "clips": page, "total_clips": len(rows), "partial": off + lim < len(rows), "next_offset": off + lim if off + lim < len(rows) else None,
                "captions": [c for c in summ["captions"] if c["end"] > a and c["start"] < b][:40], "transitions": summ["transitions"],
                "markers": [m for m in summ["markers"] if a <= m["time"] < b][:30], "cameras": {c: {"role": v.role_of_cam.get(c), "speaker": v.camera_owner.get(c)} for c in v.media_of_cam},
                "locked": self.ws.locked_timeline_ranges(v), "profile": {"min_shot_duration_sec": st.profile.get("min_shot_duration_sec"), "format": st.profile.get("format_type")}}

    def media(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        st = self.ws.state()
        reg = self.ws.compiled().media_registry
        cams = [{"media_ref": m.media_ref, "camera_id": m.camera_id, "role": m.camera_role, "speaker": m.speaker_label, "duration_sec": round(m.duration_sec, 2),
                 "video": m.has_video, "size": [m.width, m.height], "sync_offset_sec": (st.sync_result.offsets_sec.get(m.camera_id) if st.sync_result else None)} for m in st.media_assets.values()]
        assets = [{"media_ref": k, "file": Path(str(val.get("file_path", ""))).name, "image": bool(val.get("is_image"))} for k, val in reg.items() if k not in st.media_assets and all(k != m.media_ref for m in st.media_assets.values())]
        supplied = []
        for root in [r for r in self.ws.allowed_roots if r != self.ws.job_dir]:
            for p in sorted(root.rglob("*"))[:200]:
                if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".mp4", ".mov", ".m4v", ".wav", ".mp3"):
                    supplied.append(str(p))
        return {"cameras": cams, "placed_assets": assets, "supplied_files": supplied[:60], "transcript_quality": getattr(st.asr_backend_report, "transcript_quality", "unknown")}

    def transcript(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        v = self._view()
        a, b = float(args.get("start", 0.0)), float(args.get("end", 1e9))
        rows = [s for s in v.speech() if s["end"] > a and s["start"] < b and (not args.get("speaker") or s["speaker_id"] == args["speaker"])]
        off, lim = int(args.get("offset", 0)), int(args.get("limit", 60))
        neural = any(r["neural"] for r in rows)
        return {"clock": "timeline", "segments": [{k: r[k] for k in ("speaker_id", "start", "end", "overlap", "confidence", "text")} for r in rows[off:off + lim]],
                "total": len(rows), "partial": off + lim < len(rows), "evidence": "neural ASR words" if neural else "speech activity only (no words): speaker turns are measured, text is unavailable"}

    def search(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        from bangonpro.ai_engine.department.project_index import build_project_index

        v = self._view()
        idx = build_project_index(v.st)
        kinds = set(args.get("kinds") or [])
        text = str(args.get("text") or "").lower()
        a, b = float(args.get("start", 0.0)), float(args.get("end", 1e9))
        hits = []
        for e in idx.get("events", []):
            if kinds and e["type"] not in kinds:
                continue
            blob = json.dumps(e, default=str).lower()
            if text and text not in blob:
                continue
            ta = v.timeline_of_master(e["start_sec"])
            tb = v.timeline_of_master(max(e["start_sec"], e["end_sec"] - 1e-3))
            if ta is None or not (ta < b and (tb or ta) >= a):
                continue
            hits.append({**{k: val for k, val in e.items() if val is not None and k not in ("start_sec", "end_sec")}, "start": ta, "end": tb})
        lim = int(args.get("limit", 40))
        return {"clock": "timeline", "results": hits[:lim], "total": len(hits), "partial": len(hits) > lim, "kinds_available": sorted({e["type"] for e in idx.get("events", [])}),
                "missing_evidence": idx.get("missing_evidence") or idx.get("unavailable")}

    def frames(self, args: dict[str, Any], ctx: CallContext) -> ToolOutput:
        v = self._view()
        width = int(args.get("width", 320))
        times = [float(t) for t in (args.get("times") or [])][:12]
        frames: list[tuple[bytes | None, str]] = []
        meta = []
        notes = self.cache.notes()
        if args.get("media_ref"):
            asset = next((m for m in v.st.media_assets.values() if m.media_ref == args["media_ref"]), None)
            path = asset.file_path if asset else str((v.c.media_registry.get(args["media_ref"]) or {}).get("file_path", ""))
            if not path:
                raise ToolError("NOT_FOUND", f"Unknown media '{args['media_ref']}'")
            for t in [float(x) for x in (args.get("source_times") or times)][:12]:
                ctx.check()
                frames.append((self.cache.frame(path, t, width), f"{args['media_ref']} @{t:.1f}s"))
                k = self.cache.key(args["media_ref"], round(t, 1))
                meta.append({"source_time": t, "media_ref": args["media_ref"], "note": notes.get(k)})
            evidence = "source frames"
        elif args.get("rendered"):
            if not times:
                raise ToolError("INVALID_ARGUMENT", "Give times for rendered frames")
            prev = self._render(min(times) - 0.2, max(times) + 0.2, ctx)
            for t in times:
                ctx.check()
                data = self.cache.frame(prev["path"], t - prev["start"], width, tag=f"rev{self.ws.revision}")
                frames.append((data, f"t={t:.1f}s rendered"))
                meta.append({"time": t, "camera": v.camera_at(v.f(t)), "note": notes.get(self.cache.key("timeline", self.ws.revision, round(t, 1)))})
            evidence = "frames of the rendered draft (effects, graphics and captions included)"
        else:
            for t in times:
                ctx.check()
                _, cl = v.picture_master_at(v.f(t))
                if cl is None:
                    frames.append((None, f"t={t:.1f}s"))
                    meta.append({"time": t, "camera": None, "on_screen": "nothing on V1"})
                    continue
                asset = next((m for m in v.st.media_assets.values() if m.media_ref == cl.media_ref), None)
                src_t = (cl.in_frame + (v.f(t) - cl.timeline_start)) / v.fps
                cam = v.cam_of_media.get(cl.media_ref or "")
                frames.append((self.cache.frame(asset.file_path, src_t, width) if asset else None, f"t={t:.1f}s {cam}"))
                over = [g.params.get("title") or g.media_ref for g in v.c.v2_clips if g.timeline_start <= v.f(t) <= g.timeline_end_inclusive]
                meta.append({"time": t, "camera": cam, "clip_id": cl.clip_id, "source_time": round(src_t, 3), "graphics_over": over,
                             "note": notes.get(self.cache.key("timeline", self.ws.revision, round(t, 1)))})
            evidence = "source frames of the clip on screen (effects and graphics not rendered; use rendered=true for the actual output)"
        if args.get("note"):
            n = args["note"]
            for m in meta:
                key = self.cache.key(args["media_ref"], round(m["source_time"], 1)) if args.get("media_ref") else self.cache.key("timeline", self.ws.revision, round(m["time"], 1))
                self.cache.add_note(key, {"text": str(n.get("text", ""))[:400], "agent": n.get("agent"), "revision": self.ws.revision, "at": _now()})
        sheet = contact_sheet(frames, cols=min(4, max(1, len(frames))), cell_w=width)
        return ToolOutput({"evidence": evidence, "frames": meta, "revision": self.ws.revision, "cache": {"hits": self.cache.hits, "misses": self.cache.misses}}, [(sheet, "image/jpeg")])

    def _render(self, start: float, end: float, ctx: CallContext) -> dict[str, Any]:
        v = self._view()
        start, end = max(0.0, start), min(v.s(v.end_frame), max(end, start + 0.5))
        rev = self.ws.revision
        out = self.ws.job_dir / "draft" / "previews" / f"preview_r{rev}_{v.f(start)}_{v.f(end)}.mp4"
        if not out.is_file():
            ctx.progress(0, 1, "rendering preview")
            try:
                render_range(self.ws.project_path(), out, v.f(start), v.f(end), cancelled=lambda: ctx.cancelled)
            except InterruptedError as exc:
                raise Cancelled("Render cancelled; nothing was kept") from exc
            except RuntimeError as exc:
                raise ToolError("RENDER_FAILED", str(exc), hint="Source-frame inspection (rendered=false) still works", retryable=True) from exc
            ctx.progress(1, 1, "rendered")
        return {"path": str(out), "start": start, "end": end, "revision": rev}

    def audio(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        v = self._view()
        a, b = float(args.get("start", 0.0)), float(args.get("end", v.s(v.end_frame)))
        if args.get("rendered", True):
            prev = self._render(a, b, ctx)
            m = loudness(prev["path"])
            return {"evidence": "rendered draft (all audio filters applied)", "start": a, "end": b, **m, "target": _targets(v.st),
                    "note": "The draft is not loudness-normalised; Distribution's two-pass loudnorm sets the master to the target. Use this for balance and peaks."}
        clips = [c for c in v.c.a1_clips if c.timeline_start < v.f(b) and c.timeline_end_inclusive >= v.f(a)]
        rows = []
        for c in clips[:20]:
            asset = next((m for m in v.st.media_assets.values() if m.media_ref == c.media_ref), None)
            if asset:
                rows.append({"clip_id": c.clip_id, **loudness(asset.file_path, c.in_frame / v.fps, c.duration_frames / v.fps)})
        return {"evidence": "source audio of the A1 clips (before filters)", "clips": rows, "target": _targets(v.st)}

    def ops(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        return OP_GUIDE

    def agents(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        job = self.ws.job
        if args.get("agent"):
            r = role(args["agent"])
            rec = job.doc["agents"].get(args["agent"], {})
            r.update({"status": rec.get("status"), "confidence": rec.get("confidence"), "tools": list(r.get("tools", ())), "ops": list(r["ops"]), "tracks": list(r["tracks"])})
            r.pop("effects", None)
            if args["agent"] == "learning":
                r["approved_preferences"] = _preferences(job)
            return r
        return {"agents": roster(), "series_preferences": _preferences(job)}

    def qc(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        v = self._view()
        defects = check_draft(v)
        out: dict[str, Any] = {"revision": self.ws.revision, "defects": defects, "high_confidence": sum(1 for d in defects if d["confidence"] >= 0.85),
                               "evidence": "actual draft timeline + shared analysis"}
        if args.get("rendered"):
            out["rendered"] = self._rendered_checks(v, float(args.get("start", 0.0)), float(args.get("end", v.s(v.end_frame))), ctx)
        return out

    def _rendered_checks(self, v: DraftView, a: float, b: float, ctx: CallContext) -> dict[str, Any]:
        """Quality Control on the rendered output itself: duration, black picture, camera on screen, loudness."""
        prev = self._render(a, b, ctx)
        from bangonpro.ai_engine.agents.ingest_sync import probe_media_file

        probe = probe_media_file(prev["path"])
        issues = []
        dur = float(probe.get("duration_sec") or 0.0)
        if abs(dur - (prev["end"] - prev["start"])) > 0.3:
            issues.append({"code": "RENDER_DURATION", "message": f"Rendered {dur:.2f}s for a {prev['end'] - prev['start']:.2f}s range", "agent": "distribution"})
        for s0, s1 in black_spans(Path(prev["path"])):
            t0 = prev["start"] + s0
            if v.camera_at(v.f(t0 + 0.05)):
                issues.append({"code": "UNEXPECTED_BLACK", "message": f"Black picture {t0:.2f}-{prev['start'] + s1:.2f}s while V1 has a clip", "agent": "quality_control", "start": round(t0, 2)})
        checked = 0
        for sh in [s for s in v.program_shots() if s.end > v.f(a) and s.start < v.f(b)][:8]:
            ctx.check()
            t = (max(sh.start, v.f(a)) + min(sh.end, v.f(b))) / 2 / v.fps
            rendered = self.cache.frame(prev["path"], t - prev["start"], 160, tag=f"rev{prev['revision']}")
            cl = v.clip(sh.clip_id)
            asset = next((m for m in v.st.media_assets.values() if m.media_ref == sh.media_ref), None)
            if cl is None or asset is None:
                continue
            src = self.cache.frame(asset.file_path, (cl.in_frame + (v.f(t) - cl.timeline_start)) / v.fps, 160)
            sim = thumb_similarity(rendered, src)
            checked += 1
            if sim is not None and sim < 0.75 and not any(g.timeline_start <= v.f(t) <= g.timeline_end_inclusive for g in v.c.v2_clips) and not cl.filters:
                issues.append({"code": "RENDER_CAMERA_MISMATCH", "message": f"Rendered frame at {t:.2f}s does not match {sh.camera_id} (similarity {sim})", "agent": "quality_control", "start": round(t, 2)})
        lo = loudness(prev["path"])
        return {"preview": prev["path"], "duration_sec": round(dur, 2), "shots_compared": checked, "loudness": lo, "issues": issues}

    # ------------------------------------------------------------ write tools
    def edit(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        v = self._view()
        ops = [self._frame_op(op, v) for op in args["ops"]]
        return self._apply(ops, args, args["agent"], args.get("label") or f"{args['agent']}: {', '.join(o['type'] for o in ops)[:80]}")

    def switch_camera(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        v = self._view()
        try:
            ops = switch_camera_ops(v, float(args["start"]), float(args["end"]), args["camera"], reason=str(args.get("reason") or ""))
        except ValueError as exc:
            raise ToolError("INVALID_OPERATION", str(exc)) from exc
        if not ops:
            return {"revision": self.ws.revision, "unchanged": True, "message": f"{args['camera']} is already on screen there"}
        return self._apply(ops, args, args.get("agent", "multicam_director"), f"Show {args['camera']} {args['start']}-{args['end']}s{' (' + args['reason'] + ')' if args.get('reason') else ''}")

    def remove_range(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        v = self._view()
        try:
            ops = remove_range_ops(v, float(args["start"]), float(args["end"]))
        except ValueError as exc:
            raise ToolError("INVALID_OPERATION", str(exc)) from exc
        return self._apply(ops, args, args.get("agent", "rough_cut"), f"Remove {args['start']}-{args['end']}s and close the gap")

    def place_asset(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        v = self._view()
        file_path = str(self.ws.path_allowed(args["file"])) if args.get("file") else None
        media_ref = args.get("media_ref") or ("asset_" + Path(file_path).stem.replace(" ", "_").lower()[:24] if file_path else None)
        if not media_ref:
            raise ToolError("INVALID_ARGUMENT", "Give file or media_ref")
        is_image = bool(file_path and Path(file_path).suffix.lower() in (".png", ".jpg", ".jpeg"))
        media_dur = None
        if file_path and not is_image:
            from bangonpro.ai_engine.agents.ingest_sync import probe_media_file

            media_dur = float(probe_media_file(file_path).get("duration_sec") or 0.0)
            if float(args.get("source_in", 0.0)) + float(args["duration"]) > media_dur + 1e-3:
                raise ToolError("INVALID_OPERATION", f"The file is {media_dur:.2f}s long")
        rect = _rect(args.get("position"), v.c.width, v.c.height)
        ops = place_asset_ops(v, media_ref, file_path, float(args["start"]), float(args["duration"]), source_in_sec=float(args.get("source_in", 0.0)), is_image=is_image,
                              rect=rect, fade_sec=float(args.get("fade", 0.0)), media_duration_sec=media_dur, name=Path(file_path).name if file_path else "")
        return self._apply(ops, args, args.get("agent", "graphics"), f"Place {Path(file_path).name if file_path else media_ref} at {args['start']}s")

    def add_title(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        v = self._view()
        op = {"type": "add_lower_third", "start": args["start"], "duration": args.get("duration", 4.0),
              "params": {"speaker_name": args["text"], "title": args["text"], "subtitle": args.get("subtitle", ""), "position": args.get("position", "bottom_left")}}
        return self._apply([self._frame_op(op, v)], args, args.get("agent", "graphics"), f"Title '{args['text']}' at {args['start']}s")

    def history(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        act = args.get("action", "list")
        if act == "undo":
            return self.ws.undo(args.get("agent", "user"))
        if act == "redo":
            return self.ws.redo(args.get("agent", "user"))
        self.ws.layer.reload()
        return {"revision": self.ws.layer.revision, "edits": [{k: e.get(k) for k in ("entry_id", "agent_id", "label", "task_id", "at", "undone", "approved", "replay_status")} for e in self.ws.layer.doc["entries"]][-40:],
                "activity": self.ws.layer.doc.get("activity", [])[-20:]}

    def lock(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        with self.ws.lock:
            self.ws.layer.reload()
            if args.get("track"):
                lt = self.ws.layer.doc.setdefault("locked_tracks", [])
                if args["track"] not in lt:
                    lt.append(args["track"])
            if "start" in args and "end" in args:
                self.ws.layer.doc.setdefault("locked_timeline", []).append([float(args["start"]), float(args["end"])])
            self.ws.layer.log(args.get("agent", "user"), "lock", json.dumps({k: args.get(k) for k in ("track", "start", "end")}))
            self.ws.layer.save()
        return {"locked_tracks": self.ws.layer.doc.get("locked_tracks", []), "locked_ranges": self.ws.layer.doc.get("locked_timeline", []), "note": "Only the editor can unlock (Bang On Pro or the CLI)."}

    def run_agent(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        """Run a specialist's deterministic engine (the department's own path) and report its result."""
        agent = args["agent"]
        if agent == "executive":
            raise ToolError("INVALID_ARGUMENT", "The executive plan is fixed per job; use bop_task for new work")
        with self.ws.lock:
            job = self.ws.job
            ctx.progress(0, 1, f"running {agent}")

            def prog(*_a: Any, **_k: Any) -> None:
                if ctx.cancelled:
                    job.cancel("Cancelled by the reasoning client")

            scope = None
            if args.get("start") is not None and args.get("end") is not None:
                from bangonpro.ai_engine.department.revisions import (
                    previous_spans,
                    timeline_to_master,
                )

                rng = timeline_to_master(job._latest_state(), float(args["start"]), float(args["end"]))
                if rng is None:
                    raise ToolError("INVALID_ARGUMENT", "The range is outside the draft")
                scope = {"range": list(rng), "locks": job._locks(rng), "previous_spans": previous_spans(job._latest_state())}
            doc = job.rerun(agent, profile_overrides=args.get("overrides") or None, progress_callback=prog, scope=scope)
            self.ws._cache.clear()
            ctx.progress(1, 1, "done")
            rec = doc["agents"][agent]
            return {"agent": agent, "status": rec.get("status"), "job_status": doc["status"], "confidence": rec.get("confidence"), "revision": self.ws.revision,
                    "stale_agent_edits": [e["entry_id"] for e in self.ws.layer.active_entries() if e.get("replay_status") in ("stale", "failed")]}

    def revise(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        with self.ws.lock:
            rng = (float(args["start"]), float(args["end"])) if args.get("start") is not None and args.get("end") is not None else None
            rec = self.ws.job.revise(args["instruction"], range_timeline=rng)
            self.ws._cache.clear()
            return {k: rec.get(k) for k in ("revision", "status", "actions", "not_understood", "explanation", "rerun_from", "range_sec")} | {"draft_revision": self.ws.revision}

    def preview(self, args: dict[str, Any], ctx: CallContext) -> ToolOutput:
        v = self._view()
        a, b = float(args.get("start", 0.0)), float(args.get("end", min(v.s(v.end_frame), float(args.get("start", 0.0)) + 30.0)))
        checks = self._rendered_checks(v, a, b, ctx)
        shots = [s for s in v.program_shots() if s.end > v.f(a) and s.start < v.f(b)][:8]
        frames = [(self.cache.frame(checks["preview"], ((s.start + s.end) / 2 / v.fps) - a, 240, tag=f"rev{self.ws.revision}"), f"{(s.start + s.end) / 2 / v.fps:.1f}s {s.camera_id}") for s in shots]
        return ToolOutput({"evidence": "rendered draft", **checks}, [(contact_sheet(frames, cols=4, cell_w=240), "image/jpeg")])

    def vertical(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        from bangonpro.ai_engine.department.reframe import (
            emit_vertical_project,
            plan_vertical_clip,
            render_and_check,
            vertical_batch,
        )

        v = self._view()
        a, b = float(args["start"]), float(args["end"])
        ma, mb = v.picture_master_at(v.f(a))[0], v.picture_master_at(max(v.f(a), v.f(b) - 1))[0]
        if ma is None or mb is None:
            raise ToolError("INVALID_ARGUMENT", "The range is outside the program picture")
        st = v.st
        plan = plan_vertical_clip(st, {"start_sec": ma, "end_sec": mb, "title": args.get("title") or f"Clip {a:.0f}s"})
        if not plan.get("pieces"):
            raise ToolError("INVALID_ARGUMENT", "The range has no program shots to reframe")
        imports = {op["media_ref"]: op for bt in self.ws.base_batches() for op in bt["operations"] if op["type"] == "import_media"}
        idx = int(time.time()) % 10000
        batch = vertical_batch(st, plan, imports, idx)
        out_dir = self.ws.job_dir / "draft" / "vertical"
        out_dir.mkdir(parents=True, exist_ok=True)
        proj = emit_vertical_project(batch, out_dir / f"vertical_{idx:04d}.kdenlive")
        ctx.progress(0, 1, "rendering vertical proxy")
        from bangonpro.ai_engine.department.reframe import PROXY_H, PROXY_W

        proxy_proj = emit_vertical_project(vertical_batch(st, plan, imports, idx, size=(PROXY_W, PROXY_H)), out_dir / "proxy" / f"vertical_{idx:04d}_proxy.kdenlive")
        check = render_and_check(proxy_proj, out_dir / f"vertical_{idx:04d}_proxy.mp4", plan)
        self.ws.layer.reload()
        self.ws.layer.log(args.get("agent", "distribution"), "vertical", f"Vertical clip {a}-{b}s -> {proj.name}")
        self.ws.layer.save()
        return {"project": str(proj), "render_check": check, "framing": sorted({pc.get("framing") for pc in plan["pieces"]}), "uncertain_framing_sec": plan.get("uncertain_sec"),
                "duration_sec": plan.get("duration_sec")}

    def live(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        """The running Bang On Pro: read its timeline (the editor's manual changes included) and propose
        native batches that the editor approves per operation in the AI Assistant dock."""
        from bangonpro.ai_engine.mcp.live import LiveClient, LiveError

        cli = LiveClient(args.get("socket"))
        act = args.get("action", "status")
        try:
            if act == "status":
                return cli.status()
            if act == "state":
                stt = cli.state()
                lim = int(args.get("limit", 200))
                return {**stt, "clips": stt["clips"][:lim], "partial": len(stt["clips"]) > lim, "frames": "native engine frames (out_frame exclusive)"}
            if act in ("undo", "redo"):
                return getattr(cli, act)(f"mcp:{args.get('agent', 'user')}")
            if act == "propose":
                agent = args.get("agent")
                if not agent:
                    raise ToolError("INVALID_ARGUMENT", "propose needs agent")
                stt = cli.state()
                tracks = {str(c.get("clip_id")): str(c.get("track_id")) for c in stt["clips"]}
                bad = permission_violations(agent, args.get("ops") or [], tracks)
                if bad:
                    raise ToolError("PERMISSION_DENIED", "; ".join(bad[:4]))
                ops = [dict(op, op_id=f"{agent}_live_{i:03d}") for i, op in enumerate(args.get("ops") or [], 1)]
                batch = {"api_version": "1.0", "request_id": f"live_{uuid.uuid4().hex[:10]}", "project_id": str(args.get("project_id") or "live"), "sequence_id": str(args.get("sequence_id") or "seq_main"),
                         "expected_revision": int(args.get("expected_revision", stt.get("revision") or 0)), "actor": {"agent_id": agent, "role": agent, "requires_approval": True},
                         "transaction_label": args.get("label") or f"{agent} proposal", "operations": ops}
                return cli.execute(batch)
        except LiveError as exc:
            raise ToolError("APP_NOT_CONNECTED", str(exc), hint="Open Bang On Pro (its Timeline Command server starts with the app) or keep working on the draft", retryable=True) from exc
        raise ToolError("INVALID_ARGUMENT", f"Unknown action '{act}'")

    # ------------------------------------------------------------ tasks (bounded execution cycle)
    def _tasks(self) -> dict[str, Any]:
        return self.ws.layer.doc.setdefault("tasks", {})

    def _task_event(self, task_id: str, ev: dict[str, Any]) -> None:
        with self.ws.lock:
            self.ws.layer.reload()
            t = self._tasks().get(task_id)
            if t is not None:
                t.setdefault("events", []).append({"at": _now(), **ev})
                t["edits"] = t.get("edits", 0) + (1 if ev.get("edit") else 0)
                self.ws.layer.save()

    def task(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        from bangonpro.ai_engine.mcp import execution

        return execution.task_action(self, args, ctx)

    # ------------------------------------------------------------ registry
    def tool_list(self) -> list[Tool]:
        rng = {"start": T_NUM, "end": T_NUM}
        op_item = {"type": "object", "additionalProperties": False, "required": ["type"], "properties": {
            "type": {"type": "string", "enum": sorted(OP_GUIDE["ops"])}, "clip_id": {"type": "string"}, "track": {"type": "string", "enum": ["V1", "V2", "A1", "A2"]},
            "media_ref": {"type": "string"}, "start": T_NUM, "end": T_NUM, "source_in": T_NUM, "source_out": T_NUM, "at": T_NUM, "duration": T_NUM, "params": {"type": "object"}}}
        return [
            Tool("bop_status", "Status", "Connection, draft revision, job and agent status, undo/redo, manual-edit drift, open tasks, reasoning client.", {}, self.status, read_only=True, idempotent=True),
            Tool("bop_project", "Inspect timeline", "Tracks and clips (id, track, start/end, source in/out, camera, effects, titles), captions, transitions, markers, cameras, locked ranges. Filter by range/tracks; paged.",
                 {"properties": {**rng, "tracks": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer", "minimum": 1, "maximum": 400}, "offset": {"type": "integer", "minimum": 0}}},
                 self.project, read_only=True, idempotent=True),
            Tool("bop_media", "List media", "Cameras (role, speaker, duration, sync offset), placed assets and supplied files in approved folders.", {}, self.media, read_only=True, idempotent=True),
            Tool("bop_transcript", "Transcript", "What is said where on the timeline: speaker turns, overlaps, confidence and words when a neural transcript exists. Paged.",
                 {"properties": {**rng, "speaker": {"type": "string"}, "limit": {"type": "integer", "minimum": 1}, "offset": {"type": "integer", "minimum": 0}}}, self.transcript, read_only=True, idempotent=True),
            Tool("bop_search", "Search footage", "Search the project index (speech, moments, reactions, topics, highlights, vertical opportunities, visual changes) by kind, text and range; results in timeline time with evidence.",
                 {"properties": {"kinds": {"type": "array", "items": {"type": "string"}}, "text": {"type": "string"}, **rng, "limit": {"type": "integer", "minimum": 1}}}, self.search, read_only=True, idempotent=True),
            Tool("bop_frames", "Look at frames", "A contact sheet of timeline frames (source frames of the clip on screen), rendered draft frames (rendered=true) or source frames of media_ref. "
                 "Max 12 frames. Add note to remember what you saw.",
                 {"properties": {"times": {"type": "array", "items": T_NUM, "maxItems": 12}, "media_ref": {"type": "string"}, "source_times": {"type": "array", "items": T_NUM, "maxItems": 12},
                                 "rendered": {"type": "boolean"}, "width": {"type": "integer", "minimum": 96, "maximum": 640},
                                 "note": {"type": "object", "properties": {"text": {"type": "string"}, "agent": {"type": "string"}}, "additionalProperties": False}}}, self.frames, read_only=True),
            Tool("bop_audio", "Measure audio", "EBU R128 loudness and true peak of the rendered draft range (default) or the source dialogue clips (rendered=false), with the series target.",
                 {"properties": {**rng, "rendered": {"type": "boolean"}}}, self.audio, read_only=True, idempotent=True),
            Tool("bop_ops", "Operation guide", "Native operations and their arguments, transforms, colour effects and the compound editing tools.", {}, self.ops, read_only=True, idempotent=True),
            Tool("bop_agents", "Agent briefs", "The 14 production agents: brief, allowed operations and tracks, completion checks, status; with agent: one in full. Includes approved series preferences.",
                 {"properties": {"agent": T_AGENT}}, self.agents, read_only=True, idempotent=True),
            Tool("bop_qc", "Quality check", "Quality Control on the actual draft: wrong cameras, flash shots, picture gaps, sync, misplaced or short graphics, colour consistency, level jumps, captions. "
                 "rendered=true also renders the range and checks the output (duration, black, camera on screen, loudness).",
                 {"properties": {"rendered": {"type": "boolean"}, **rng}}, self.qc, read_only=True),
            Tool("bop_edit", "Edit (native operations)", "Apply native timeline operations as one undoable edit (see bop_ops). All or nothing; verified in the written project.",
                 {"required": ["agent", "expected_revision", "ops"], "properties": {"agent": T_AGENT, "expected_revision": T_REV, "ops": {"type": "array", "items": op_item, "minItems": 1, "maxItems": 60},
                                                                                   "label": {"type": "string"}, "task_id": T_TASK, "dry_run": {"type": "boolean"}}}, self.edit),
            Tool("bop_switch_camera", "Switch camera", "Show another camera over [start, end) at the same moment (reaction, wide, speaker). Dialogue is untouched; the camera's grade comes with it. "
                 "Put 'reaction' in reason for a deliberate listener cutaway so Quality Control treats it as intentional.",
                 {"required": ["expected_revision", "start", "end", "camera"], "properties": {"agent": T_AGENT, "expected_revision": T_REV, **rng, "camera": {"type": "string"},
                                                                                               "reason": {"type": "string"}, "task_id": T_TASK, "dry_run": {"type": "boolean"}}}, self.switch_camera),
            Tool("bop_remove_range", "Remove range", "Cut [start, end) out of every track and close the gap; captions after it move up.",
                 {"required": ["expected_revision", "start", "end"], "properties": {"agent": T_AGENT, "expected_revision": T_REV, **rng, "task_id": T_TASK, "dry_run": {"type": "boolean"}}},
                 self.remove_range, destructive=True),
            Tool("bop_place_asset", "Place B-roll or graphic", "Place a supplied video or image on V2 at start for duration; position: full, top_right, top_left, bottom_right, bottom_left, center or 'x y w h'; optional fade (s).",
                 {"required": ["expected_revision", "start", "duration"], "properties": {"agent": T_AGENT, "expected_revision": T_REV, "file": {"type": "string"}, "media_ref": {"type": "string"},
                                                                                          "start": T_NUM, "duration": {"type": "number", "exclusiveMinimum": 0}, "source_in": T_NUM,
                                                                                          "position": {"type": "string"}, "fade": T_NUM, "task_id": T_TASK, "dry_run": {"type": "boolean"}}}, self.place_asset),
            Tool("bop_add_title", "Add title", "A lower third or title card from the native template on V2.",
                 {"required": ["expected_revision", "text", "start"], "properties": {"agent": T_AGENT, "expected_revision": T_REV, "text": {"type": "string", "minLength": 1}, "subtitle": {"type": "string"},
                                                                                      "start": T_NUM, "duration": {"type": "number", "exclusiveMinimum": 0}, "position": {"type": "string"}, "task_id": T_TASK}}, self.add_title),
            Tool("bop_history", "History", "list the agent edits, or undo / redo the last one.", {"properties": {"action": {"type": "string", "enum": ["list", "undo", "redo"]}, "agent": T_AGENT}}, self.history),
            Tool("bop_lock", "Protect", "Protect a timeline range or a track from further agent edits (only the editor can unlock).",
                 {"properties": {**rng, "track": {"type": "string", "enum": ["V1", "V2", "A1", "A2"]}, "agent": T_AGENT}}, self.lock, idempotent=True),
            Tool("bop_run_agent", "Run specialist engine", "Rerun one specialist's deterministic engine (and the agents after it), optionally with profile overrides and limited to a timeline range. Long; cancellable.",
                 {"required": ["agent"], "properties": {"agent": T_AGENT, "overrides": {"type": "object"}, **rng}}, self.run_agent),
            Tool("bop_revise", "Rule-based revision", "The department's built-in revision interpreter for common instructions (tighten, more reactions, fewer cuts, vertical clips...). Unclear requests change nothing.",
                 {"required": ["instruction"], "properties": {"instruction": {"type": "string", "minLength": 2}, **rng}}, self.revise),
            Tool("bop_task", "Task", "Bounded execution cycle for a brief. begin {instruction, agents, expect}: context pack + QC baseline. lease {agent, tracks, start, end}. note {agent, finding}. "
                 "complete: checks 'expect' on the actual draft, QC regression check, automatic correction of high-confidence defects (budgeted), verified result.",
                 {"required": ["action"], "properties": {"action": {"type": "string", "enum": ["begin", "lease", "note", "complete", "status", "cancel"]}, "task_id": T_TASK, "instruction": {"type": "string"},
                                                         "agents": {"type": "array", "items": T_AGENT}, "agent": T_AGENT, "tracks": {"type": "array", "items": {"type": "string"}}, **rng,
                                                         "finding": {"type": "object"}, "expect": {"type": "array", "items": EXPECT_ITEM}, "auto_correct": {"type": "boolean"}}}, self.task),
            Tool("bop_preview", "Render preview", "Render a low-resolution preview of a range (default 30 s) and check it; returns a contact sheet of the rendered shots.",
                 {"properties": rng}, self.preview, read_only=True),
            Tool("bop_live", "Live app", "The running Bang On Pro editor: status, state (its real timeline incl. manual changes), propose native ops for the editor's per-operation approval, undo, redo.",
                 {"required": ["action"], "properties": {"action": {"type": "string", "enum": ["status", "state", "propose", "undo", "redo"]}, "agent": T_AGENT, "ops": {"type": "array", "items": {"type": "object"}},
                                                         "expected_revision": {"type": "integer"}, "label": {"type": "string"}, "project_id": {"type": "string"}, "sequence_id": {"type": "string"},
                                                         "limit": {"type": "integer", "minimum": 1}, "socket": {"type": "string"}}}, self.live),
            Tool("bop_vertical", "Vertical clip", "A 9:16 vertical clip of [start, end) as an editable project with subject-aware framing and a checked proxy render.",
                 {"required": ["start", "end"], "properties": {**rng, "title": {"type": "string"}, "agent": T_AGENT}}, self.vertical),
        ]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _targets(st: Any) -> dict[str, Any]:
    ar = st.profile.get("audio_rules") or {}
    return {"integrated_lufs": ar.get("target_lufs", -14.0), "true_peak_dbtp": ar.get("true_peak_dbtp", -1.0)}


def _rect(position: Any, w: int, h: int) -> str | None:
    if not position or position == "full":
        return None
    pos = str(position)
    if len(pos.split()) in (4, 5):
        return pos if len(pos.split()) == 5 else pos + " 1"
    pw, ph = round(w * 0.32), round(h * 0.32)
    m = round(w * 0.03)
    xy = {"top_right": (w - pw - m, m), "top_left": (m, m), "bottom_right": (w - pw - m, h - ph - m), "bottom_left": (m, h - ph - m), "center": ((w - pw) // 2, (h - ph) // 2)}
    if pos not in xy:
        raise ToolError("INVALID_ARGUMENT", f"Unknown position '{pos}'", hint="full, top_right, top_left, bottom_right, bottom_left, center or 'x y w h'")
    x, y = xy[pos]
    return f"{x} {y} {pw} {ph} 1"


def _preferences(job: Any) -> dict[str, Any]:
    try:
        from bangonpro.ai_engine.department.learning import ProfilePreferenceStore

        store = ProfilePreferenceStore(str(job.doc["profile_id"]), str(job.doc.get("series_id") or "default"), job.doc["options"].get("prefs_root"))
        return {"series": job.doc.get("series_id") or "default", "version": store.active_version(), "approved": store.active_overlay()}
    except Exception as exc:  # noqa: BLE001 - preferences are advisory context
        return {"unavailable": str(exc)[:120]}


def _live_status() -> dict[str, Any]:
    from bangonpro.ai_engine.mcp.live import LiveClient

    return LiveClient().status()


def build_tools(ws: Workspace) -> BopTools:
    return BopTools(ws)


__all__ = ["INSTRUCTIONS", "OP_GUIDE", "ROLES", "BopTools", "build_tools"]
