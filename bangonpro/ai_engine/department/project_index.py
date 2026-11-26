"""Reusable timestamped project index (V1.3): one place for what the department knows.

Assembled from analysis that already exists in the shared state (no new inference): speech and
speaker turns, conversation moments (questions, laughter, reactions, interruptions, suspense or
reveals when detected), topic changes, highlight and vertical-clip opportunities, camera roles
and availability, subject evidence, audio events and supplied production assets. Every entry
names its evidence source; missing evidence is listed under ``unavailable`` rather than guessed.

Written to ``draft/project_index.json`` whenever a draft is (re)built, so revisions, review and
other tools read the same index.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

from typing import Any

INDEX_VERSION = "1.3.0"


def build_project_index(st: Any) -> dict[str, Any]:
    md = st.metadata
    ec = md.get("editorial_context") or {}
    neural = bool(getattr(st.asr_backend_report, "is_neural_inference", False))
    events: list[dict[str, Any]] = []

    def ev(kind: str, start: float, end: float, source: str, **extra: Any) -> None:
        events.append({"type": kind, "start_sec": round(float(start), 3), "end_sec": round(float(max(start, end)), 3), "evidence": source, **extra})

    for s in st.transcript_segments:
        ev("speech", s.start_sec, s.end_sec, "neural ASR" if s.is_neural_inference else "speech activity (VAD)", speaker_id=s.speaker_id,
           confidence=round(float(s.confidence), 3), overlap=bool(s.is_overlap), text=(s.text[:200] if s.is_neural_inference else None))
    for m in ec.get("moments", []):
        ev(m.get("type", "moment"), m.get("start_sec", 0.0), m.get("end_sec", 0.0), m.get("evidence", "editorial analysis"), speaker_id=m.get("speaker_id"), confidence=m.get("confidence"))
    for iv in getattr(st, "interruption_intervals", []) or []:
        ev("recording_interruption", iv.start_sec, iv.end_sec, "audio continuity")
    for t in ec.get("topics", []):
        if isinstance(t, dict) and "start_sec" in t:
            ev("topic", t["start_sec"], t.get("end_sec", t["start_sec"]), ec.get("topic_method") or "turn/energy structure", title=t.get("title") or t.get("label"))
    for h in md.get("highlights", []) or []:
        ev("highlight_opportunity", h["start_sec"], h["end_sec"], "editorial highlight scoring", score=h.get("score"), reasons=h.get("reasons"))
    for c in md.get("social_clips", []) or []:
        ev("vertical_clip_opportunity", c["start_sec"], c["end_sec"], "editorial clip selection", platform=c.get("platform"), title=c.get("title"))
    for p in ec.get("protected_pauses", []) or []:
        if "start_sec" in p:
            ev("protected_pause", p["start_sec"], p.get("end_sec", p["start_sec"]), "pause classification", kind=p.get("kind"))
    cameras: dict[str, Any] = {}
    va = md.get("visual_analysis") or {}
    roles = st.profile.get("camera_roles") or {}
    for cid, a in st.media_assets.items():
        v = va.get(cid) or {}
        samples = v.get("samples") or {}
        boxes = samples.get("box") or []
        cameras[cid] = {
            "role": roles.get(cid) or a.camera_role, "speaker": a.speaker_label, "video": a.has_video, "audio": a.has_audio,
            "subject_evidence_share": round(sum(1 for b in boxes if b) / len(boxes), 3) if boxes else None,
            "visual_samples": len(samples.get("t") or []),
        }
        for u in v.get("unavailable_intervals", []) or []:
            if isinstance(u, dict):
                ev("camera_unavailable", u.get("start_sec", 0.0), u.get("end_sec", 0.0), "sampled visual analysis", camera_id=cid, reason=u.get("kind") or u.get("reason"))
        for t in v.get("scene_cuts_sec", []) or []:
            ev("visual_change", t, t, "sampled visual analysis", camera_id=cid)
    for cid, an in ((md.get("audio_analysis") or {}).get("mics") or {}).items():
        for e in ((an or {}).get("clipping") or {}).get("events", [])[:50]:
            if isinstance(e, dict) and "start_sec" in e:
                ev("audio_clipping", e["start_sec"], e.get("end_sec", e["start_sec"]), "per-mic audio analysis", camera_id=cid)
    assets = []
    for i in (md.get("media_inventory") or {}).get("inputs", []):
        kind = i.get("source_kind") or ("camera" if i.get("has_video") else "audio")
        assets.append({"id": i.get("camera_id"), "kind": kind, "role": i.get("role_label"), "duration_sec": i.get("duration_sec"), "status": i.get("status")})
    for k in ("intro_asset", "outro_asset"):
        path = (st.profile.get("intro_outro_rules") or {}).get(k)
        if path:
            assets.append({"id": k, "kind": "branding_video", "path": path})
    events.sort(key=lambda e: (e["start_sec"], e["type"]))
    unavailable = []
    if not neural:
        unavailable.append("words: no spoken transcript (install a Whisper model); questions, punchlines and text topics use timing/energy evidence only")
    if not va:
        unavailable.append("visual analysis: not run for this job")
    return {
        "version": INDEX_VERSION,
        "project_id": st.project_id,
        "duration_sec": round(max((a.duration_sec for a in st.media_assets.values()), default=0.0), 3),
        "transcript": "neural ASR" if neural else "speech activity (VAD)",
        "speakers": ec.get("speakers") or sorted({s.speaker_id for s in st.transcript_segments}),
        "cameras": cameras,
        "assets": assets,
        "event_counts": {t: sum(1 for e in events if e["type"] == t) for t in sorted({e["type"] for e in events})},
        "events": events,
        "unavailable": unavailable,
    }


__all__ = ["INDEX_VERSION", "build_project_index"]
