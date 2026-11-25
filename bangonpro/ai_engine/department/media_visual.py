"""Sampled per-camera visual analysis (V1.2.2): one bounded, low-resolution pass per camera.

Instead of decoding every frame at full size, each camera is sampled once (``ffmpeg``; keyframes
only for long files, otherwise a time-based ``select`` at ``interval_sec``) to a 96-pixel-wide RGB
thumbnail. Per sample we keep compact statistics only (the thumbnails are discarded immediately, so
memory stays O(1) in the recording length):

* luma mean / 5th / 95th percentile, mean RGB
* near-neutral pixel mean (white-balance estimate) and its pixel share (grey-world fallback flagged)
* skin-likelihood (YCbCr) pixel mean chroma and share
* motion energy (mean absolute difference to the previous sample)
* a subject framing box from the largest skin blob, else the largest motion blob (normalised 0..1)
* black / frozen flags

From the samples: ``unavailable_intervals`` (black, frozen inside otherwise live footage, missing
picture), and ``scene_cuts_sec``. All times are on the **source** clock here; ``to_master_clock``
maps them onto the master clock once sync is known. Results are cached by content hash.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import re
import subprocess
import threading
from typing import Any

import numpy as np
from scipy import ndimage

VISUAL_VERSION = "1.2.2-visual-2"
THUMB_W = 96
MAX_METADATA_SAMPLES = 1200


def skin_mask_ycbcr(rgb01: np.ndarray) -> np.ndarray:
    """Chai & Ngan skin-likelihood rule in YCbCr (BT.601, 0..255 scale) with a luma floor."""
    r, g, b = rgb01[..., 0] * 255.0, rgb01[..., 1] * 255.0, rgb01[..., 2] * 255.0
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = 128.0 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128.0 + 0.5 * r - 0.418688 * g - 0.081312 * b
    return (cb >= 77) & (cb <= 127) & (cr >= 133) & (cr <= 173) & (y > 40) & (y < 245)


def neutral_mask(rgb01: np.ndarray) -> np.ndarray:
    """Near-neutral (low-chroma, mid-luma, unclipped) pixels used for white balance."""
    mx = rgb01.max(axis=-1)
    mn = rgb01.min(axis=-1)
    lum = rgb01.mean(axis=-1)
    return ((mx - mn) < 0.12 * np.maximum(lum, 0.05) + 0.03) & (lum > 0.15) & (mx < 0.96)


def frame_stats(rgb01: np.ndarray, prev: np.ndarray | None) -> dict[str, Any]:
    luma = 0.2126 * rgb01[..., 0] + 0.7152 * rgb01[..., 1] + 0.0722 * rgb01[..., 2]
    flat = rgb01.reshape(-1, 3)
    # Cast-tolerant neutrals: find low-chroma pixels after a grey-world normalisation (a strong cast
    # would otherwise hide the grey card), then measure those pixels in the original frame.
    gw = np.maximum(flat.mean(axis=0), 1e-3)
    neu = neutral_mask(np.clip(rgb01 / gw * float(gw.mean()), 0.0, 1.0)) & (rgb01.max(axis=-1) < 0.96)
    skin = skin_mask_ycbcr(rgb01)
    neu_share = float(neu.mean())
    skin_share = float(skin.mean())
    neutral_rgb = rgb01[neu].mean(axis=0) if neu.sum() >= 12 else flat.mean(axis=0)
    motion = float(np.mean(np.abs(rgb01 - prev))) if prev is not None and prev.shape == rgb01.shape else None
    box, src = None, None
    if skin.sum() >= max(6, 0.004 * skin.size):
        box, src = _largest_blob_box(skin), "skin"
    if box is None and prev is not None and prev.shape == rgb01.shape:
        mv = np.abs(rgb01 - prev).mean(axis=-1) > 0.06
        if mv.sum() >= max(6, 0.004 * mv.size):
            box, src = _largest_blob_box(mv), "motion"
    hist = np.concatenate([np.histogram(rgb01[..., c], bins=8, range=(0.0, 1.0))[0] for c in range(3)]).astype(np.float64)
    hist /= max(1.0, hist.sum() / 3.0)
    return {
        "luma": float(luma.mean()),
        "luma_p5": float(np.percentile(luma, 5)),
        "luma_p95": float(np.percentile(luma, 95)),
        "rgb": [float(x) for x in flat.mean(axis=0)],
        "neutral_rgb": [float(x) for x in neutral_rgb],
        "neutral_share": neu_share,
        "skin_share": skin_share,
        "skin_rgb": [float(x) for x in rgb01[skin].mean(axis=0)] if skin.sum() >= 6 else None,
        "motion": motion,
        "box": box,
        "box_source": src,
        "hist": hist,
    }


def _largest_blob_box(mask: np.ndarray) -> list[float] | None:
    lab, n = ndimage.label(ndimage.binary_opening(mask, iterations=1) if mask.sum() > 40 else mask)
    if n == 0:
        return None
    sizes = ndimage.sum(np.ones_like(lab), lab, index=np.arange(1, n + 1))
    k = int(np.argmax(sizes)) + 1
    if sizes[k - 1] < 4:
        return None
    ys, xs = np.where(lab == k)
    h, w = mask.shape
    return [round(xs.min() / w, 4), round(ys.min() / h, 4), round((xs.max() + 1) / w, 4), round((ys.max() + 1) / h, 4)]


def _sample_stream(path: str, width: int, height: int, interval: float, keyframes_only: bool, max_sec: float | None):
    """Yield ``(pts_sec, rgb uint8 HxWx3)`` from one ffmpeg process (stdout frames + showinfo pts)."""
    vf = f"scale={THUMB_W}:{height}:flags=area,showinfo"
    if not keyframes_only:
        vf = f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{interval:.3f})'," + vf
    cmd = ["ffmpeg", "-v", "info", "-nostats"]
    if keyframes_only:
        cmd += ["-skip_frame", "nokey"]
    cmd += ["-i", path]
    if max_sec:
        cmd += ["-t", f"{max_sec:.3f}"]
    cmd += ["-map", "0:v:0", "-an", "-vf", vf, "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
    pts: list[float] = []
    errs: list[str] = []
    cond = threading.Condition()

    def _read_err() -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            line = raw.decode("utf-8", errors="replace")
            m = re.search(r"pts_time:\s*(-?[0-9.]+)", line)
            if m and "Parsed_showinfo" in line:
                with cond:
                    pts.append(float(m.group(1)))
                    cond.notify_all()
            elif re.search(r"error|corrupt|invalid", line, re.I):
                errs.append(line.strip()[:200])
        with cond:
            cond.notify_all()

    th = threading.Thread(target=_read_err, daemon=True)
    th.start()
    fsize = THUMB_W * height * 3
    i = 0
    assert proc.stdout is not None
    try:
        while True:
            buf = proc.stdout.read(fsize)
            if not buf or len(buf) < fsize:
                break
            with cond:
                cond.wait_for(lambda: len(pts) > i or not th.is_alive(), timeout=10.0)
                t = pts[i] if i < len(pts) else (pts[-1] + interval if pts else i * interval)
            yield t, np.frombuffer(buf, dtype=np.uint8).reshape(height, THUMB_W, 3)
            i += 1
    finally:
        proc.stdout.close()
        proc.wait(timeout=30)
        th.join(timeout=5)
    _sample_stream.last_errors = errs  # type: ignore[attr-defined]


def analyse_camera(path: str, *, duration: float, width: int, height: int, interval_sec: float | None = None, full_decode_max_sec: float = 900.0) -> dict[str, Any]:
    """Sample one camera and summarise (source clock)."""
    interval = float(interval_sec) if interval_sec else max(1.0, duration / 1800.0)
    keyframes = duration > full_decode_max_sec
    th = max(2, round(THUMB_W * (height / max(1, width)) / 2) * 2) if width and height else 54
    times: list[float] = []
    rows: list[dict[str, Any]] = []
    prev = None
    prev_hist = None
    for t, fr in _sample_stream(path, width, th, interval, keyframes, None):
        rgb = fr.astype(np.float32) / 255.0
        st = frame_stats(rgb, prev)
        st["hist_change"] = float(0.5 * np.abs(st["hist"] - prev_hist).sum() / 3.0) if prev_hist is not None else None
        prev_hist = st.pop("hist")
        prev = rgb
        times.append(float(t))
        rows.append(st)
    errs = list(getattr(_sample_stream, "last_errors", []) or [])
    return summarise(times, rows, duration=duration, interval=interval, keyframes_only=keyframes, decode_errors=errs)


def summarise(times: list[float], rows: list[dict[str, Any]], *, duration: float, interval: float, keyframes_only: bool, decode_errors: list[str] | None = None) -> dict[str, Any]:
    n = len(rows)
    out: dict[str, Any] = {
        "clock": "source",
        "sampling": {"mode": "keyframes" if keyframes_only else "interval", "interval_sec": round(interval, 3), "samples": n, "thumb_width": THUMB_W},
        "decode_errors": (decode_errors or [])[:5],
    }
    if n == 0:
        out.update({"unavailable_intervals": [{"start_sec": 0.0, "end_sec": round(duration, 3), "reason": "no_decodable_video"}], "samples": {}, "scene_cuts_sec": [], "summary": {}})
        return out
    t = np.array(times)
    luma = np.array([r["luma"] for r in rows])
    p95 = np.array([r["luma_p95"] for r in rows])
    mot = np.array([r["motion"] if r["motion"] is not None else np.nan for r in rows])
    live_level = float(np.nanmedian(mot)) if np.isfinite(mot).any() else 0.0
    black = (p95 < 0.08) & (luma < 0.05)
    frozen = np.zeros(n, dtype=bool)
    if live_level > 0.004:
        frozen = np.nan_to_num(mot, nan=1.0) < max(0.0015, 0.1 * live_level)
    step = float(np.median(np.diff(t))) if n > 1 else interval
    intervals: list[dict[str, Any]] = []
    for flag, reason, min_len in ((black, "black", 0.5), (frozen, "frozen", 2.0)):
        i = 0
        while i < n:
            if flag[i]:
                j = i
                while j + 1 < n and flag[j + 1]:
                    j += 1
                a = t[i - 1] if (reason == "frozen" and i > 0) else t[i]
                b = t[j] + (step if reason == "black" else 0.0)
                if b - a >= min_len:
                    intervals.append({"start_sec": round(float(a), 3), "end_sec": round(float(b), 3), "reason": reason})
                i = j + 1
            else:
                i += 1
    gaps = np.diff(t)
    for k in np.where(gaps > max(3.0 * step, 2.0))[0]:
        intervals.append({"start_sec": round(float(t[k]) + step, 3), "end_sec": round(float(t[k + 1]), 3), "reason": "missing_frames"})
    if duration - (t[-1] + step) > max(1.0, 2.0 * step):
        intervals.append({"start_sec": round(float(t[-1]) + step, 3), "end_sec": round(duration, 3), "reason": "picture_ends_early"})
    hc = np.array([r["hist_change"] if r["hist_change"] is not None else 0.0 for r in rows])
    cuts = [round(float(t[i]), 3) for i in range(1, n) if hc[i] > 0.45 and not black[i] and not black[i - 1]]
    samples = {
        "t": [round(x, 3) for x in times],
        "luma": [round(x, 4) for x in luma.tolist()],
        "luma_p5": [round(r["luma_p5"], 4) for r in rows],
        "luma_p95": [round(x, 4) for x in p95.tolist()],
        "rgb": [[round(c, 4) for c in r["rgb"]] for r in rows],
        "neutral_rgb": [[round(c, 4) for c in r["neutral_rgb"]] for r in rows],
        "neutral_share": [round(r["neutral_share"], 4) for r in rows],
        "skin_share": [round(r["skin_share"], 4) for r in rows],
        "skin_rgb": [[round(c, 4) for c in r["skin_rgb"]] if r["skin_rgb"] else None for r in rows],
        "motion": [None if r["motion"] is None else round(r["motion"], 5) for r in rows],
        "box": [r["box"] for r in rows],
        "box_source": [r["box_source"] for r in rows],
    }
    out.update(
        {
            "samples": samples,
            "unavailable_intervals": sorted(intervals, key=lambda x: x["start_sec"]),
            "scene_cuts_sec": cuts,
            "static_video": bool(live_level <= 0.004),
            "summary": {
                "luma_mean": round(float(luma.mean()), 4),
                "luma_std": round(float(luma.std()), 4),
                "motion_median": round(live_level, 5),
                "mean_rgb": [round(float(x), 4) for x in np.mean([r["rgb"] for r in rows], axis=0)],
                "neutral_share_mean": round(float(np.mean([r["neutral_share"] for r in rows])), 4),
                "skin_share_mean": round(float(np.mean([r["skin_share"] for r in rows])), 4),
                "subject_box_share": round(float(np.mean([r["box"] is not None for r in rows])), 4),
            },
        }
    )
    return out


def decimate(va: dict[str, Any], cap: int = MAX_METADATA_SAMPLES) -> dict[str, Any]:
    s = va.get("samples") or {}
    n = len(s.get("t", []))
    if n <= cap:
        return va
    idx = np.linspace(0, n - 1, cap).round().astype(int)
    va = dict(va)
    va["samples"] = {k: [v[i] for i in idx] for k, v in s.items()}
    va["sampling"] = {**va.get("sampling", {}), "decimated_from": n}
    return va


def to_master_clock(va_src: dict[str, Any], sync_result: Any, camera_id: str, gaps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Map source-clock times to the master clock; add sync gaps (device not recording) as unavailable."""
    from bangonpro.ai_engine.department.media_sync import master_time

    def _m(x: float) -> float | None:
        v = master_time(sync_result, camera_id, x)
        return None if v is None else round(v, 3)

    out = {k: v for k, v in va_src.items() if k not in ("samples", "unavailable_intervals", "scene_cuts_sec")}
    s = va_src.get("samples") or {}
    ts = [_m(x) for x in s.get("t", [])]
    keep = [i for i, x in enumerate(ts) if x is not None]
    out["samples"] = {k: [v[i] for i in keep] for k, v in s.items()}
    out["samples"]["t"] = [ts[i] for i in keep]
    out["samples"]["t_source"] = [s["t"][i] for i in keep] if s.get("t") else []
    un = []
    for iv in va_src.get("unavailable_intervals", []):
        a, b = _m(iv["start_sec"]), _m(iv["end_sec"])
        if a is not None and b is not None and b > a:
            un.append({**iv, "start_sec": a, "end_sec": b, "source_start_sec": iv["start_sec"], "source_end_sec": iv["end_sec"]})
    for g in gaps or []:
        if g.get("master_end_sec", 0) > g.get("master_start_sec", 0):
            un.append({"start_sec": g["master_start_sec"], "end_sec": g["master_end_sec"], "reason": "recording_gap"})
    if sync_result is not None:
        off = float(sync_result.offsets_sec.get(camera_id, 0.0))
        if off < 0:  # device started after the master: nothing before -offset on the master clock
            un.append({"start_sec": 0.0, "end_sec": round(-off, 3), "reason": "not_yet_recording"})
    out["unavailable_intervals"] = sorted(un, key=lambda x: x["start_sec"])
    out["scene_cuts_sec"] = [c for c in (_m(x) for x in va_src.get("scene_cuts_sec", [])) if c is not None]
    out["clock"] = "master"
    return out


def sample_index_range(samples: dict[str, Any], t0: float, t1: float, *, key: str = "t") -> list[int]:
    ts = samples.get(key) or []
    idx = [i for i, x in enumerate(ts) if t0 <= x <= t1]
    if not idx and ts:
        mid = 0.5 * (t0 + t1)
        idx = [int(np.argmin([abs(x - mid) for x in ts]))]
    return idx


__all__ = ["VISUAL_VERSION", "analyse_camera", "decimate", "frame_stats", "neutral_mask", "sample_index_range", "skin_mask_ycbcr", "summarise", "to_master_clock"]
