"""Subject-aware 9:16 vertical clips (V1.3), built as editable native Kdenlive projects.

For each selected social clip (master-clock window from Editorial Intelligence):

1. **Pieces** follow the edited programme (the rough cut's kept shots), so removals and
   protected beats carry over. A wide or reaction shot is replaced by the active speaker's
   close-up when one exists: a 9:16 crop of a two-person wide cuts someone out.
2. **Framing** comes from the shared sampled visual analysis (``visual_analysis[cam]`` subject
   boxes on the master clock): a stable subject gets one fixed crop, a moving subject gets
   smoothed, speed-limited keyframes. With no subject evidence the crop is centred and the
   seconds are reported as uncertain; nothing is invented.
3. The clip is emitted as Timeline Command API v1 operations (``create_sequence`` 1080x1920,
   ``import_media``, ``place_clip``, native ``qtblend`` Transform with ``rect`` keyframes), so it
   opens in Kdenlive as a normal, fully editable vertical timeline.
4. ``render_and_check`` renders a proxy (or the final file) with melt and verifies the
   **rendered output**: size, duration, audio, black frames and whether the subject sits inside
   the frame on sampled frames.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

OUT_W, OUT_H = 1080, 1920
PROXY_W, PROXY_H = 360, 640  # review proxy of the same 9:16 project
STABLE_STD = 0.04  # subject-centre std (fraction of width) below which the crop is held still
MAX_SPEED = 0.12  # fraction of the source width per second the crop may pan
_CLOSE_ROLES = ("host", "guest", "panel", "desk", "presenter", "anchor", "contestant", "subject", "interviewer")


def _role(st: Any, cam: str) -> str:
    roles = st.profile.get("camera_roles") or {}
    return str(roles.get(cam) or getattr(st.media_assets.get(cam), "camera_role", "") or "")


def _centres(st: Any, cam: str, t0: float, t1: float) -> list[tuple[float, float]]:
    s = ((st.metadata.get("visual_analysis") or {}).get(cam) or {}).get("samples") or {}
    out = []
    for t, box in zip(s.get("t", []), s.get("box", []), strict=False):
        if t is not None and box and t0 - 0.5 <= t <= t1 + 0.5:
            out.append((float(t), (float(box[0]) + float(box[2])) / 2.0))
    return out


def plan_vertical_clip(st: Any, clip: dict[str, Any]) -> dict[str, Any]:
    """Pieces (camera, master range, crop keyframes) for one master-clock clip window."""
    ms, me = float(clip["start_sec"]), float(clip["end_sec"])
    spk_map = dict(st.profile.get("speaker_camera_map") or {})
    pieces: list[dict[str, Any]] = []
    uncertain = 0.0
    for sh in st.rough_cut_shots:
        a, b = max(ms, sh.master_sync_in_sec), min(me, sh.master_sync_out_sec)
        if b - a < 0.2:
            continue
        cam = sh.camera_id
        close = spk_map.get(sh.speaker_id)
        swapped = False
        if _role(st, cam) not in _CLOSE_ROLES and close in st.media_assets and getattr(st.media_assets[close], "has_video", False):
            cam, swapped = close, True
        pts = _centres(st, cam, a, b)
        if not pts:
            kf = [(a, 0.5)]
            uncertain += b - a
            framing = "centred (no subject evidence)"
        else:
            xs = np.array([x for _, x in pts])
            if float(xs.std()) <= STABLE_STD or len(pts) < 3:
                kf = [(a, float(np.median(xs)))]
                framing = "held on subject"
            else:
                sm = np.convolve(xs, np.ones(3) / 3.0, mode="same")
                sm[0], sm[-1] = xs[0], xs[-1]
                kf, last_t, last_x = [], None, None
                for (t, _), x in zip(pts, sm, strict=False):
                    t = min(max(t, a), b)
                    if last_t is not None:
                        lim = MAX_SPEED * max(0.0, t - last_t)
                        x = float(np.clip(x, last_x - lim, last_x + lim))
                    if not kf or t - kf[-1][0] >= 0.5:
                        kf.append((t, float(x)))
                        last_t, last_x = t, x
                framing = "tracking subject"
        pieces.append({"shot_id": sh.shot_id, "camera_id": cam, "speaker_id": sh.speaker_id, "master_in": round(a, 3), "master_out": round(b, 3),
                       "audio_media_ref": sh.audio_media_ref, "audio_in_sec": round(sh.audio_source_in_sec + (a - sh.master_sync_in_sec), 4),
                       "keyframes": [(round(t, 3), round(x, 4)) for t, x in kf], "framing": framing, "swapped_to_close_up": swapped})
    return {"clip": clip, "pieces": pieces, "uncertain_sec": round(uncertain, 2), "duration_sec": round(sum(p["master_out"] - p["master_in"] for p in pieces), 2)}


def _rect(src_w: int, src_h: int, cx: float, out_w: int = OUT_W, out_h: int = OUT_H) -> str:
    scale = out_h / float(max(1, src_h))
    ws = src_w * scale
    x = min(0.0, max(out_w - ws, out_w / 2.0 - cx * ws))
    return f"{x:.0f} 0 {ws:.0f} {out_h} 1"


def _tc(sec: float) -> str:
    sec = max(0.0, sec)
    return f"{int(sec // 3600):02d}:{int(sec % 3600 // 60):02d}:{sec % 60:06.3f}"


def vertical_batch(st: Any, plan: dict[str, Any], import_ops: dict[str, dict[str, Any]], index: int, *, size: tuple[int, int] = (OUT_W, OUT_H)) -> dict[str, Any]:
    from bangonpro.ai_engine.agents import audio_graphics_qc as agq

    b = agq.new_agent_builder(st, agent_id="distribution", agent_role="distribution", transaction_label=f"Distribution: vertical clip {index}")
    b.add_operation("create_sequence", params={"profile_id": "vertical_9x16", "display_name": f"Vertical clip {index}", "width": size[0], "height": size[1],
                                                "fps_num": st.fps_num, "fps_den": st.fps_den, "color_space": "bt709"}, rationale="9:16 vertical sequence")
    b.add_operation("create_track", track_id="V1", params={"track_type": "video", "name": "Vertical V1"})
    b.add_operation("create_track", track_id="A1", params={"track_type": "audio", "name": "Dialogue A1"})
    refs = {f"media_{p['camera_id']}" for p in plan["pieces"]} | {p["audio_media_ref"] for p in plan["pieces"]}
    for ref in sorted(refs):
        op = import_ops.get(ref)
        if op:
            b.add_operation("import_media", media_ref=ref, params=dict(op["params"]), rationale="source media (read-only)")
    cursor = 0
    for k, p in enumerate(plan["pieces"], start=1):
        a = st.media_assets[p["camera_id"]]
        n = max(1, st.sec_to_frame(p["master_out"] - p["master_in"]))
        v_in = st.sec_to_frame(max(0.0, st.sync_result.source_time(p["camera_id"], p["master_in"]))) if st.sync_result else st.sec_to_frame(p["master_in"])
        a_in = st.sec_to_frame(max(0.0, p["audio_in_sec"]))
        cid = f"v{index:02d}_{k:03d}"
        b.add_operation("place_clip", media_ref=f"media_{p['camera_id']}", clip_id=f"{cid}_v", track_id="V1", in_frame=v_in, out_frame=v_in + n - 1, timeline_start=cursor,
                        params={"camera_id": p["camera_id"], "stream": "video"}, confidence=0.9, rationale=f"{p['framing']}" + (" (close-up instead of wide)" if p["swapped_to_close_up"] else ""))
        b.add_operation("place_clip", media_ref=p["audio_media_ref"], clip_id=f"{cid}_a", track_id="A1", in_frame=a_in, out_frame=a_in + n - 1, timeline_start=cursor,
                        params={"stream": "audio"}, confidence=0.95, rationale="programme dialogue")
        kfs = ";".join(f"{_tc(t - p['master_in'])}={_rect(a.width or 1920, a.height or 1080, x, size[0], size[1])}" for t, x in p["keyframes"])
        props = {"rect": kfs, "rotate_center": 1, "compositing": 0, "distort": 0}
        b.add_operation("add_effect", clip_id=f"{cid}_v", track_id="V1", params={"effect_id": "qtblend", "mlt_service": "qtblend", "kdenlive_id": "qtblend", "parameters": props, "properties": props, "agent_id": "distribution"},
                        confidence=0.85 if p["framing"] != "centred (no subject evidence)" else 0.5, rationale=f"9:16 reframe: {p['framing']}")
        cursor += n
    return b.build()


def emit_vertical_project(batch: dict[str, Any], path: Path) -> Path:
    from bangonpro.ai_engine.project_builder import compile_command_batches, emit_kdenlive_xml

    return emit_kdenlive_xml(compile_command_batches([batch]), path)


def _black_seconds(path: Path) -> float:
    res = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vf", "blackdetect=d=0.2:pix_th=0.08", "-an", "-f", "null", "-"], capture_output=True, text=True, timeout=600, check=False)
    return round(sum(float(x) for x in re.findall(r"black_duration:(\d+(?:\.\d+)?)", res.stderr)), 2)


def render_and_check(project: Path, out: Path, plan: dict[str, Any], *, proxy: bool = True) -> dict[str, Any]:
    """Render with melt and verify the rendered vertical clip itself."""
    from bangonpro.ai_engine.department.agents import sample_frame_rgb
    from bangonpro.ai_engine.department.media_visual import frame_stats
    from bangonpro.ai_engine.project_builder import render_kdenlive_project_with_melt

    issues: list[str] = []
    try:
        probe = render_kdenlive_project_with_melt(project, out, video_bitrate="600k" if proxy else "8M", preset="ultrafast" if proxy else "veryfast",
                                                  consumer_props=["real_time=-2"])
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return {"status": "failed", "path": str(out), "issues": [f"Render failed: {exc}"[:300]]}
    w, h = int(probe.get("width", 0) or 0), int(probe.get("height", 0) or 0)
    dur = float(probe.get("duration_sec", 0.0) or 0.0)
    want = (PROXY_W, PROXY_H) if proxy else (OUT_W, OUT_H)
    if (w, h) != want:
        issues.append(f"Rendered {w}x{h}, expected {want[0]}x{want[1]}")
    if abs(dur - plan["duration_sec"]) > 0.5:
        issues.append(f"Rendered {dur:.1f}s, planned {plan['duration_sec']:.1f}s")
    black = _black_seconds(out)
    if black > 0.3:
        issues.append(f"{black:.1f}s of black picture")
    # Framing on the rendered frames: the subject box centre must sit in the middle 60 % of the width
    framed, checked = 0, 0
    for frac in (0.2, 0.5, 0.8):
        fr = sample_frame_rgb(str(out), dur * frac, width=96)
        if fr is None:
            continue
        box = frame_stats(fr.astype(np.float64) / 255.0, None).get("box")
        if box:
            checked += 1
            framed += int(0.2 <= (box[0] + box[2]) / 2.0 <= 0.8)
    if checked and framed < checked:
        issues.append(f"Subject off-centre in {checked - framed} of {checked} sampled frames")
    return {"status": "rendered" if not issues else "rendered_with_issues", "path": str(out), "width": w, "height": h, "duration_sec": round(dur, 2),
            "black_sec": black, "framing_checked_frames": checked, "framing_ok_frames": framed, "audio": probe.get("audio_codec") is not None, "proxy": proxy, "issues": issues}


__all__ = ["emit_vertical_project", "plan_vertical_clip", "render_and_check", "vertical_batch"]
