"""V1.4 draft inspection, defect checks and corrective edits on the *actual* draft timeline.

The V1.3 QC checks read the planner's shot list. Once agents edit the draft directly, the truth is
the compiled draft itself, so everything here works on :class:`DraftView`: the replayed timeline
(V1 program picture including dissolve lanes, A1 dialogue, V2 graphics, captions) joined with the
shared analysis (sync, transcript, speaker-camera map, camera roles).

``check_draft`` returns defects with an owning agent, timeline times, evidence and a confidence.
High-confidence defects that have a deterministic repair carry ``fix``; :func:`fix_ops` turns it into
native operations that the owning agent applies through the edit layer (and QC re-verifies). The
compound builders (:func:`switch_camera_ops`, :func:`remove_range_ops`, :func:`place_asset_ops`)
expand editor actions into validated native operations.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from bangonpro.ai_engine.department.agent_roles import COLOUR_EFFECTS
from bangonpro.ai_engine.project_builder import CompiledClip, CompiledTimeline

FLASH_SHOT_SEC = 0.5
HIGH_CONFIDENCE = 0.85


@dataclass
class Shot:
    clip_id: str
    lane: str
    start: int  # timeline frame, inclusive
    end: int  # timeline frame, exclusive
    camera_id: str
    media_ref: str


class DraftView:
    def __init__(self, st: Any, compiled: CompiledTimeline) -> None:
        self.st, self.c = st, compiled
        self.fps = compiled.fps
        self.cam_of_media = {a.media_ref: a.camera_id for a in st.media_assets.values()}
        self.media_of_cam = {a.camera_id: a.media_ref for a in st.media_assets.values()}
        self.role_of_cam = {a.camera_id: a.camera_role for a in st.media_assets.values()}
        plan = st.metadata.get("camera_plan") or {}
        spk_map = dict(plan.get("speaker_camera_map") or st.profile.get("speaker_camera_map") or {})
        for a in st.media_assets.values():
            if a.speaker_label and a.has_video and a.speaker_label not in spk_map:
                spk_map[a.speaker_label] = a.camera_id
        self.speaker_camera = spk_map
        self.camera_owner: dict[str, str] = {}
        for spk, cam in spk_map.items():
            self.camera_owner.setdefault(cam, spk)

    # ------------------------------------------------------------ time
    def f(self, sec: float) -> int:
        return max(0, round(float(sec) * self.fps))

    def s(self, frame: int) -> float:
        return round(frame / self.fps, 3)

    @property
    def end_frame(self) -> int:
        return self.c.total_frames

    def program_shots(self) -> list[Shot]:
        """Visible picture spans on V1 (a dissolve lane clip is on top of V1 while it plays)."""
        spans: list[Shot] = []
        top = sorted(self.c.v1b_clips, key=lambda c: c.timeline_start)
        for c in sorted(self.c.v1_clips, key=lambda c: c.timeline_start):
            a, b = c.timeline_start, c.timeline_end_inclusive + 1
            for t in top:
                ta, tb = t.timeline_start, t.timeline_end_inclusive + 1
                if tb <= a or ta >= b:
                    continue
                if ta > a:
                    spans.append(Shot(c.clip_id, "V1", a, ta, self.cam_of_media.get(c.media_ref or "", ""), c.media_ref or ""))
                a = max(a, tb)
            if b > a:
                spans.append(Shot(c.clip_id, "V1", a, b, self.cam_of_media.get(c.media_ref or "", ""), c.media_ref or ""))
        for t in top:
            spans.append(Shot(t.clip_id, "V1b", t.timeline_start, t.timeline_end_inclusive + 1, self.cam_of_media.get(t.media_ref or "", ""), t.media_ref or ""))
        return sorted(spans, key=lambda x: x.start)

    def clip(self, clip_id: str) -> CompiledClip | None:
        found = self.c.find(clip_id)
        return found[1] if found else None

    def _master_of(self, clip: CompiledClip, frame: int) -> float | None:
        cam = self.cam_of_media.get(clip.media_ref or "")
        if cam is None or self.st.sync_result is None:
            return None
        return self.st.sync_result.master_time(cam, (clip.in_frame + (frame - clip.timeline_start)) / self.fps)

    def picture_master_at(self, frame: int) -> tuple[float | None, CompiledClip | None]:
        for sh in self.program_shots():
            if sh.start <= frame < sh.end:
                cl = self.clip(sh.clip_id)
                return (self._master_of(cl, frame) if cl else None), cl
        return None, None

    def audio_master_at(self, frame: int) -> float | None:
        for c in self.c.a1_clips:
            if c.timeline_start <= frame <= c.timeline_end_inclusive:
                return self._master_of(c, frame)
        return None

    def camera_at(self, frame: int) -> str | None:
        for sh in self.program_shots():
            if sh.start <= frame < sh.end:
                return sh.camera_id
        return None

    def timeline_of_master(self, m: float) -> float | None:
        """Where a master-clock moment plays on the draft (via the dialogue track), or None if cut."""
        for c in self.c.a1_clips:
            m0 = self._master_of(c, c.timeline_start)
            if m0 is None:
                continue
            m1 = m0 + c.duration_frames / self.fps
            if m0 <= m < m1:
                return round((c.timeline_start + (m - m0) * self.fps) / self.fps, 3)
        return None

    def speech(self) -> list[dict[str, Any]]:
        """Transcript segments as heard on the draft (timeline seconds); cut material is omitted."""
        out = []
        for seg in self.st.transcript_segments:
            if getattr(seg, "deleted", False):
                continue
            a, b = self.timeline_of_master(seg.start_sec), self.timeline_of_master(max(seg.start_sec, seg.end_sec - 1e-3))
            if a is None or b is None or b <= a:
                continue
            out.append({"speaker_id": seg.speaker_id, "start": a, "end": round(b, 3), "overlap": bool(seg.is_overlap), "confidence": float(seg.confidence),
                        "text": seg.text if seg.is_neural_inference else None, "neural": bool(seg.is_neural_inference)})
        return out

    def summary(self) -> dict[str, Any]:
        def row(c: CompiledClip, tid: str) -> dict[str, Any]:
            r: dict[str, Any] = {"id": c.clip_id, "track": tid, "start": self.s(c.timeline_start), "end": self.s(c.timeline_end_inclusive + 1),
                                 "src_in": self.s(c.in_frame), "src_out": self.s(c.out_frame + 1)}
            if c.media_ref:
                r["media"] = c.media_ref
                cam = self.cam_of_media.get(c.media_ref)
                if cam:
                    r["camera"] = cam
            if c.params.get("clip_kind") == "kdenlivetitle":
                r["title"] = c.params.get("title")
            if c.filters:
                r["effects"] = [f.get("kdenlive_id") for f in c.filters]
            return r

        return {"tracks": {tid: [row(c, tid) for c in lst] for tid, lst in self.c.track_lists().items() if lst},
                "captions": [{"id": c.get("caption_id"), "start": self.s(c["in_frame"]), "end": self.s(c["out_frame"] + 1), "text": c.get("text", "")[:80]} for c in self.c.captions],
                "transitions": [{**t, "start": self.s(t["in_frame"]), "end": self.s(t["out_frame"] + 1)} for t in self.c.transitions],
                "markers": [{"time": self.s(m["frame"]), "label": m["label"], "category": m["category"]} for m in self.c.markers],
                "duration_sec": self.s(self.end_frame), "fps": self.fps, "width": self.c.width, "height": self.c.height}


# ====================================================================== checks
def _defect(code: str, severity: str, agent: str, conf: float, start: float, end: float, message: str, evidence: str, fix: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    d = {"code": code, "severity": severity, "agent": agent, "confidence": round(conf, 3), "start": round(start, 3), "end": round(end, 3), "message": message, "evidence": evidence, **extra}
    if fix:
        d["fix"] = fix
    return d


def check_draft(v: DraftView) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    shots = v.program_shots()
    a1_end = max((c.timeline_end_inclusive + 1 for c in v.c.a1_clips), default=0)
    # Picture gaps while dialogue plays
    cursor = 0
    for sh in shots:
        if sh.start > cursor and cursor < a1_end:
            out.append(_defect("PICTURE_GAP", "error", "rough_cut", 0.97, v.s(cursor), v.s(min(sh.start, a1_end)), "No picture while dialogue plays", "timeline structure",
                               {"kind": "fill_gap", "start": cursor, "end": sh.start}))
        cursor = max(cursor, sh.end)
    if shots and cursor < a1_end:
        out.append(_defect("PICTURE_GAP", "error", "rough_cut", 0.97, v.s(cursor), v.s(a1_end), "Picture ends before the dialogue", "timeline structure", {"kind": "fill_gap", "start": cursor, "end": a1_end}))
    # Flash shots
    for i, sh in enumerate(shots):
        if (sh.end - sh.start) / v.fps < FLASH_SHOT_SEC and sh.lane == "V1":
            out.append(_defect("FLASH_SHOT", "error", "multicam_director", 0.95, v.s(sh.start), v.s(sh.end), f"{sh.camera_id} on screen for {(sh.end - sh.start) / v.fps:.2f}s", "timeline structure",
                               {"kind": "absorb_shot", "clip_id": sh.clip_id, "prev": shots[i - 1].clip_id if i else None}))
    # Picture/sound sync
    for sh in shots:
        mid = (sh.start + sh.end) // 2
        pm, _ = v.picture_master_at(mid)
        am = v.audio_master_at(mid)
        if pm is None or am is None:
            continue
        delta = pm - am
        if abs(delta) * v.fps > 1.5:
            out.append(_defect("AV_DESYNC", "error", "synchronisation", 0.92, v.s(sh.start), v.s(sh.end), f"Picture is {delta:+.2f}s from the dialogue", "sync map (master clock)",
                               {"kind": "slip", "clip_id": sh.clip_id, "frames": -round(delta * v.fps)}))
    # Camera choice vs who is speaking
    reaction_ids = {c.clip_id for c in v.c.v1_clips + v.c.v1b_clips if "reaction" in str(c.params.get("reason", "")) or c.params.get("reaction")}
    for seg in v.speech():
        if seg["overlap"] or seg["confidence"] < 0.6 or seg["end"] - seg["start"] < 2.0:
            continue
        want = v.speaker_camera.get(seg["speaker_id"])
        if not want:
            continue
        f0, f1 = v.f(seg["start"]), v.f(seg["end"])
        wrong = 0
        first_wrong = None
        for sh in shots:
            ov = min(sh.end, f1) - max(sh.start, f0)
            if ov <= 0 or sh.clip_id in reaction_ids:
                continue
            owner = v.camera_owner.get(sh.camera_id)
            if owner and owner != seg["speaker_id"]:
                wrong += ov
                first_wrong = first_wrong if first_wrong is not None else max(sh.start, f0)
        if wrong / v.fps >= 1.5 and first_wrong is not None:
            conf = 0.9 if seg["neural"] else 0.86
            out.append(_defect("WRONG_CAMERA", "warning", "multicam_director", conf, v.s(f0), v.s(f1), f"{seg['speaker_id']} speaks but another speaker's close-up is on screen for {wrong / v.fps:.1f}s",
                               "transcript speaker + speaker-camera map", {"kind": "switch", "start": v.s(f0), "end": v.s(f1), "camera_id": want}))
    # Graphics
    spk_names = {k: (val.get("name") or k) for k, val in ((v.st.metadata.get("editorial_context") or {}).get("speakers") or {}).items()} if isinstance((v.st.metadata.get("editorial_context") or {}).get("speakers"), dict) else {}
    name_to_spk = {n: k for k, n in spk_names.items()}
    speech = v.speech()
    for g in v.c.v2_clips:
        if g.params.get("clip_kind") != "kdenlivetitle":
            continue
        dur = g.duration_frames / v.fps
        if dur < 2.0:
            out.append(_defect("GRAPHIC_TOO_SHORT", "warning", "graphics", 0.9, v.s(g.timeline_start), v.s(g.timeline_end_inclusive + 1), f"'{g.params.get('title')}' is on screen {dur:.1f}s", "timeline structure",
                               {"kind": "lengthen", "clip_id": g.clip_id, "frames": v.f(4.0)}))
        spk = name_to_spk.get(str(g.params.get("title")), str(g.params.get("title")))
        cam = v.speaker_camera.get(spk)
        if not cam:
            continue
        on = sum(max(0, min(sh.end, g.timeline_end_inclusive + 1) - max(sh.start, g.timeline_start)) for sh in shots if sh.camera_id == cam)
        if on < 0.5 * g.duration_frames:
            target = next((sp["start"] for sp in speech if sp["speaker_id"] == spk and not sp["overlap"] and sp["end"] - sp["start"] >= dur
                           and all(v.camera_at(v.f(sp["start"] + k * 0.5)) == cam for k in range(int(dur * 2)))), None)
            out.append(_defect("GRAPHIC_WRONG_SPEAKER", "warning", "graphics", 0.88 if target is not None else 0.6, v.s(g.timeline_start), v.s(g.timeline_end_inclusive + 1),
                               f"Lower third '{g.params.get('title')}' while {spk}'s camera is not on screen", "speaker-camera map + timeline",
                               {"kind": "move", "clip_id": g.clip_id, "to": target} if target is not None else None))
    # Colour consistency per camera
    by_cam: dict[str, dict[str, list[str]]] = {}
    for c in v.c.v1_clips + v.c.v1b_clips:
        cam = v.cam_of_media.get(c.media_ref or "")
        if not cam:
            continue
        sig = json.dumps(sorted([(f.get("kdenlive_id"), json.dumps(f.get("properties") or {}, sort_keys=True, default=str)) for f in c.filters if f.get("kdenlive_id") in COLOUR_EFFECTS or f.get("mlt_service") in COLOUR_EFFECTS]))
        by_cam.setdefault(cam, {}).setdefault(sig, []).append(c.clip_id)
    for cam, sigs in by_cam.items():
        if len(sigs) < 2:
            continue
        major = max(sigs, key=lambda k: len(sigs[k]))
        for sig, ids in sigs.items():
            if sig == major:
                continue
            for cid in ids:
                cl = v.clip(cid)
                out.append(_defect("INCONSISTENT_COLOUR", "warning", "colourist", 0.9, v.s(cl.timeline_start), v.s(cl.timeline_end_inclusive + 1), f"{cid} has a different grade from the other {cam} shots",
                                   "effect parameters", {"kind": "match_colour", "clip_id": cid, "like": sigs[major][0]}))
    # Dialogue level jumps on A1 (same mic)
    a1 = sorted(v.c.a1_clips, key=lambda c: c.timeline_start)
    for prev, cur in zip(a1, a1[1:], strict=False):
        g0, g1 = _gain(prev), _gain(cur)
        if prev.media_ref == cur.media_ref and g0 is not None and g1 is not None and abs(g0 - g1) > 3.0:
            out.append(_defect("AUDIO_LEVEL_JUMP", "warning", "audio_engineer", 0.85, v.s(cur.timeline_start), v.s(cur.timeline_end_inclusive + 1), f"Dialogue level jumps {g1 - g0:+.1f} dB at the cut",
                               "clip gain", {"kind": "level", "clip_id": cur.clip_id, "gain_db": g0}))
    # Captions
    for cap in v.c.captions:
        dur = (cap["out_frame"] - cap["in_frame"] + 1) / v.fps
        if cap["in_frame"] >= v.end_frame:
            out.append(_defect("CAPTION_OUTSIDE_PROGRAM", "error", "captions", 0.95, v.s(cap["in_frame"]), v.s(cap["out_frame"] + 1), "Caption after the end of the program", "timeline structure",
                               {"kind": "delete_caption", "caption_id": cap.get("caption_id")}))
        elif dur < 0.7 and len(str(cap.get("text", "")).split()) >= 3:
            out.append(_defect("CAPTION_UNREADABLE", "warning", "captions", 0.8, v.s(cap["in_frame"]), v.s(cap["out_frame"] + 1), f"{len(str(cap.get('text', '')).split())} words in {dur:.2f}s", "caption timing"))
    return sorted(out, key=lambda d: (d["start"], d["code"]))


def _gain(c: CompiledClip) -> float | None:
    for f in c.filters:
        if f.get("kdenlive_id") == "volume":
            try:
                return float(f["properties"].get("level"))
            except (TypeError, ValueError):
                return None
    return 0.0


# ====================================================================== compound edits
def _uid(v: DraftView, base: str) -> str:
    cid, n = base, 1
    while v.c.find(cid) is not None:
        n += 1
        cid = f"{base}_{n}"
    return cid


def switch_camera_ops(v: DraftView, start_sec: float, end_sec: float, camera_id: str, *, reason: str = "") -> list[dict[str, Any]]:
    """Show ``camera_id`` on V1 over [start, end) at the same moment in time; dialogue is untouched."""
    media = v.media_of_cam.get(camera_id)
    if not media:
        raise ValueError(f"Unknown camera '{camera_id}' (cameras: {', '.join(sorted(v.media_of_cam))})")
    f0, f1 = v.f(start_sec), v.f(end_sec)
    if f1 - f0 < 1:
        raise ValueError("The range is empty")
    if any(t.timeline_start < f1 and t.timeline_end_inclusive + 1 > f0 for t in v.c.v1b_clips):
        raise ValueError("The range touches a dissolve; switch outside it or remove the dissolve first")
    asset = next(a for a in v.st.media_assets.values() if a.camera_id == camera_id)
    ops: list[dict[str, Any]] = []
    covered = []
    for c in sorted(v.c.v1_clips, key=lambda c: c.timeline_start):
        a, b = c.timeline_start, c.timeline_end_inclusive + 1
        lo, hi = max(a, f0), min(b, f1)
        if hi <= lo:
            continue
        if v.cam_of_media.get(c.media_ref or "") == camera_id:
            continue
        mid = c.clip_id
        if lo > a:
            mid = _uid(v, f"{c.clip_id}_m{lo}")
            ops.append({"type": "split_clip", "clip_id": c.clip_id, "params": {"split_frame": lo, "new_clip_id": mid}})
        if hi < b:
            ops.append({"type": "split_clip", "clip_id": mid, "params": {"split_frame": hi, "new_clip_id": _uid(v, f"{c.clip_id}_t{hi}")}})
        ops.append({"type": "delete_clip", "clip_id": mid})
        covered.append((lo, hi, c))
    if not covered:
        return []
    # The grade belongs to the camera: the new shot carries the colour treatment of that camera's other shots.
    graded = next((c for c in v.c.v1_clips + v.c.v1b_clips if c.media_ref == media and any(f.get("kdenlive_id") in COLOUR_EFFECTS for f in c.filters)), None)
    for lo, hi, c in covered:
        m = v._master_of(c, lo)
        if m is None:
            raise ValueError("No sync information for this camera")
        src = v.st.sync_result.source_time(camera_id, m)
        in_f = round(src * v.fps)
        if in_f < 0 or (in_f + (hi - lo)) / v.fps > asset.duration_sec + 1e-6:
            raise ValueError(f"{camera_id} has no footage for {v.s(lo)}-{v.s(hi)}s")
        new_id = _uid(v, f"cam_{camera_id}_{lo}")
        ops.append({"type": "place_clip", "clip_id": new_id, "track_id": "V1", "media_ref": media,
                    "frame_range": {"in_frame": in_f, "out_frame": in_f + (hi - lo) - 1, "timeline_start": lo},
                    "params": {"camera_id": camera_id, "stream": "video", "reason": reason or "camera_switch", **({"reaction": True} if "reaction" in reason else {})}})
        if graded is not None:
            for f in graded.filters:
                if f.get("kdenlive_id") in COLOUR_EFFECTS:
                    ops.append({"type": "add_effect", "clip_id": new_id,
                                "params": {"mlt_service": f["mlt_service"], "kdenlive_id": f.get("kdenlive_id"), "properties": dict(f.get("properties") or {})}})
    return ops


def remove_range_ops(v: DraftView, start_sec: float, end_sec: float) -> list[dict[str, Any]]:
    """Remove [start, end) from every track and close the gap (picture, sound, graphics, captions)."""
    f0, f1 = v.f(start_sec), v.f(end_sec)
    d = f1 - f0
    if d < 1:
        raise ValueError("The range is empty")
    if any(t.timeline_end_inclusive + 1 > f0 for t in v.c.v1b_clips):
        raise ValueError("Remove the dissolves after this point first (they would need re-timing)")
    ops: list[dict[str, Any]] = []
    moves: list[tuple[int, dict[str, Any]]] = []
    for tid in ("V1", "A1", "V2", "A2"):
        for c in sorted(v.c.track_lists()[tid], key=lambda c: c.timeline_start):
            a, b = c.timeline_start, c.timeline_end_inclusive + 1
            if b <= f0:
                continue
            if a >= f1:
                moves.append((a, {"type": "move_clip", "clip_id": c.clip_id, "params": {"timeline_start": a - d}}))
                continue
            cur = c.clip_id
            if a < f0:
                right = _uid(v, f"{c.clip_id}_x{f0}")
                ops.append({"type": "split_clip", "clip_id": cur, "params": {"split_frame": f0, "new_clip_id": right}})
                cur = right
            if b > f1:
                tail = _uid(v, f"{c.clip_id}_y{f1}")
                ops.append({"type": "split_clip", "clip_id": cur, "params": {"split_frame": f1, "new_clip_id": tail}})
                moves.append((f1, {"type": "move_clip", "clip_id": tail, "params": {"timeline_start": f0}}))
            ops.append({"type": "delete_clip", "clip_id": cur})
    ops.extend(m for _, m in sorted(moves, key=lambda x: x[0]))
    for cap in v.c.captions:
        if cap["out_frame"] < f0:
            continue
        if cap["in_frame"] >= f1:
            ops.append({"type": "update_caption", "clip_id": cap["caption_id"], "frame_range": {"in_frame": cap["in_frame"] - d, "out_frame": cap["out_frame"] - d}})
        else:
            ops.append({"type": "delete_caption", "clip_id": cap["caption_id"]})
    return ops


def place_asset_ops(v: DraftView, media_ref: str, file_path: str | None, start_sec: float, duration_sec: float, *, source_in_sec: float = 0.0, is_image: bool = False,
                    rect: str | None = None, fade_sec: float = 0.0, media_duration_sec: float | None = None, name: str = "") -> list[dict[str, Any]]:
    """Place supplied B-roll or a still graphic on V2 (optionally positioned and faded)."""
    ops: list[dict[str, Any]] = []
    if media_ref not in v.c.media_registry:
        if not file_path:
            raise ValueError(f"'{media_ref}' is not in the project; give its file")
        meta = {"file_path": file_path, "duration_sec": 3600.0 if is_image else float(media_duration_sec or duration_sec + source_in_sec), "is_image": is_image, "name": name or media_ref,
                "camera_id": name or media_ref, "video_codec": "image" if is_image else "h264"}
        ops.append({"type": "import_media", "media_ref": media_ref, "params": meta})
    f0, n = v.f(start_sec), max(1, v.f(duration_sec))
    in_f = v.f(source_in_sec)
    cid = _uid(v, f"asset_{media_ref}_{f0}")
    ops.append({"type": "place_clip", "clip_id": cid, "track_id": "V2", "media_ref": media_ref, "frame_range": {"in_frame": in_f, "out_frame": in_f + n - 1, "timeline_start": f0},
                "params": {"stream": "video", "asset": True}})
    if rect:
        ops.append({"type": "add_effect", "clip_id": cid, "params": {"mlt_service": "qtblend", "kdenlive_id": "qtblend", "properties": {"rect": rect, "compositing": 0, "distort": 0}}})
    if fade_sec > 0 and v.f(fade_sec) * 2 < n:
        ops.append({"type": "add_transition", "clip_id": cid, "params": {"kind": "fade_in", "duration_frames": v.f(fade_sec)}})
        ops.append({"type": "add_transition", "clip_id": cid, "params": {"kind": "fade_out", "duration_frames": v.f(fade_sec)}})
    return ops


def fix_ops(v: DraftView, defect: dict[str, Any]) -> list[dict[str, Any]]:
    """Native operations that repair a defect with a deterministic fix (empty if none applies)."""
    fx = defect.get("fix") or {}
    kind = fx.get("kind")
    if kind == "switch":
        return switch_camera_ops(v, fx["start"], fx["end"], fx["camera_id"], reason="qc_correction")
    if kind == "slip":
        c = v.clip(fx["clip_id"])
        if c is None or c.in_frame + fx["frames"] < 0:
            return []
        return [{"type": "trim_clip", "clip_id": c.clip_id, "params": {"in_frame": c.in_frame + fx["frames"], "out_frame": c.out_frame + fx["frames"]}}]
    if kind == "absorb_shot":
        c, prev = v.clip(fx["clip_id"]), v.clip(fx["prev"]) if fx.get("prev") else None
        if c is None or prev is None or prev.track_id != "V1" or prev.timeline_end_inclusive + 1 != c.timeline_start:
            return []
        return [{"type": "delete_clip", "clip_id": c.clip_id}, {"type": "trim_clip", "clip_id": prev.clip_id, "params": {"out_frame": prev.out_frame + c.duration_frames}}]
    if kind == "fill_gap":
        prev = max((c for c in v.c.v1_clips if c.timeline_end_inclusive + 1 <= fx["start"]), key=lambda c: c.timeline_start, default=None)
        if prev is None or prev.timeline_end_inclusive + 1 != fx["start"]:
            return []
        return [{"type": "trim_clip", "clip_id": prev.clip_id, "params": {"out_frame": prev.out_frame + (fx["end"] - fx["start"])}}]
    if kind == "move" and fx.get("to") is not None:
        return [{"type": "move_clip", "clip_id": fx["clip_id"], "params": {"timeline_start": v.f(fx["to"])}}]
    if kind == "lengthen":
        c = v.clip(fx["clip_id"])
        if c is None:
            return []
        nxt = min((o.timeline_start for o in v.c.track_lists()[c.track_id] if o.timeline_start > c.timeline_start), default=10**9)
        frames = min(fx["frames"], nxt - c.timeline_start)  # into the free space only, never over the next graphic
        if frames <= c.duration_frames or frames < v.f(2.0):
            return []
        return [{"type": "trim_clip", "clip_id": c.clip_id, "params": {"in_frame": c.in_frame, "out_frame": c.in_frame + frames - 1, "allow_extend": True}}]
    if kind == "level":
        return [{"type": "set_audio_level", "clip_id": fx["clip_id"], "params": {"gain_db": fx["gain_db"]}}]
    if kind == "delete_caption":
        return [{"type": "delete_caption", "clip_id": fx["caption_id"]}]
    if kind == "match_colour":
        c, like = v.clip(fx["clip_id"]), v.clip(fx["like"])
        if c is None or like is None:
            return []
        ops: list[dict[str, Any]] = []
        mine = {f.get("kdenlive_id"): f for f in c.filters}
        for f in like.filters:
            if f.get("kdenlive_id") not in COLOUR_EFFECTS and f.get("mlt_service") not in COLOUR_EFFECTS:
                continue
            have = mine.get(f.get("kdenlive_id"))
            if have is None:
                ops.append({"type": "add_effect", "clip_id": c.clip_id, "params": {"mlt_service": f["mlt_service"], "kdenlive_id": f.get("kdenlive_id"), "properties": dict(f.get("properties") or {})}})
            else:
                for k, val in (f.get("properties") or {}).items():
                    if (have.get("properties") or {}).get(k) != val:
                        ops.append({"type": "set_effect_param", "clip_id": c.clip_id, "params": {"kdenlive_id": f.get("kdenlive_id"), "property": k, "value": val}})
        return ops
    return []


__all__ = ["DraftView", "check_draft", "fix_ops", "place_asset_ops", "remove_range_ops", "switch_camera_ops"]
