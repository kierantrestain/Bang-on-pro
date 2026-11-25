"""Media Ingest intelligence (V1.2.2): identification, classification, duplicates and validation.

Everything here is read-only, bounded and cached by content hash:

* **Identification** from ``ffprobe`` container/stream tags (make, model, software/encoder, handler
  names, timecode, creation time, BWF originator/time reference, channel layout/count, sample rate,
  bit depth) plus filename hints.
* **Classification**: camera with audio, camera without audio, audio recorder (multitrack, lav/mic,
  stereo), screen capture, graphics/still.
* **Duplicates**: identical SHA-256, or the same audio fingerprint *and* duration under different
  names (a re-wrap/transcode), or the same video perceptual hash at start/middle/end when there is no
  audio.
* **Validation**: decode probes at start, middle and end (truncated/corrupt files), stream duration
  mismatch, variable frame rate (packet timestamps), missing or silent audio and silent channels.

Every problem comes with an actionable message for the editor.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import re
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np

INGEST_VERSION = "1.2.2-ingest-1"
_ERROR_PATTERNS = re.compile(
    r"(moov atom not found|invalid data found|error while decoding|corrupt|partial file|truncat|"
    r"invalid nal|missing picture|end of file|could not find codec|error splitting|concealing)",
    re.IGNORECASE,
)
_IMAGE_CODECS = {"png", "mjpeg", "bmp", "tiff", "gif", "webp", "jpeg2000", "targa", "dpx", "exr"}
_ALPHA_PIX = ("yuva", "rgba", "argb", "bgra", "abgr", "ya8", "ya16", "gbrap")
_SCREEN_HINTS = re.compile(r"\b(obs|screenflow|camtasia|screen ?(capture|record\w*)|replaykit|shadowplay|desktop|loom)\b", re.IGNORECASE)
_DISPLAY_SIZES = {(2560, 1440), (2560, 1600), (2880, 1800), (3024, 1964), (3456, 2234), (1440, 900), (1680, 1050), (1920, 1200), (3840, 2400)}
_RECORDER_MAKERS = re.compile(r"(zoom|tascam|sound ?devices|rode|zaxcom|deity|sennheiser|shure|roland|marantz|sony pcm)", re.IGNORECASE)
_CAMERA_MAKERS = re.compile(r"(canon|sony|panasonic|blackmagic|gopro|apple|dji|fujifilm|nikon|red|arri|z cam|insta360|samsung|google)", re.IGNORECASE)


def _run(cmd: list[str], timeout: float = 120.0, *, binary: bool = False) -> tuple[int, Any, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, b"" if binary else "", str(exc)
    err = proc.stderr.decode("utf-8", errors="replace")
    return proc.returncode, proc.stdout if binary else proc.stdout.decode("utf-8", errors="replace"), err


def ffprobe_raw(path: str | Path) -> tuple[dict[str, Any] | None, str]:
    """Full ffprobe JSON (format + streams + chapters) or ``(None, stderr)`` when unreadable."""
    rc, out, err = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], 60.0)
    if rc != 0 or not out.strip():
        return None, err.strip()
    try:
        return json.loads(out), err.strip()
    except ValueError:
        return None, "ffprobe returned invalid JSON"


def _rate(s: str | None) -> float:
    if not s or s in ("0/0", "N/A"):
        return 0.0
    try:
        f = Fraction(s)
        return float(f) if f > 0 else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def _num(v: Any) -> float | None:
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _parse_timecode(tc: str | None, fps: float) -> float | None:
    if not tc:
        return None
    m = re.match(r"^(\d{1,2})[:;.](\d{2})[:;.](\d{2})[:;.](\d{2})$", str(tc).strip())
    if not m:
        return None
    hh, mm, ss, ff = (int(x) for x in m.groups())
    base = round(fps) if fps > 0 else 25
    return hh * 3600 + mm * 60 + ss + ff / float(base)


def identify(raw: dict[str, Any], path: str | Path) -> dict[str, Any]:
    """Device identity and stream facts from probe tags (missing tags are reported as ``None``)."""
    fmt = raw.get("format", {}) or {}
    ftags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}
    streams = raw.get("streams", []) or []
    v = next((s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    a = audios[0] if audios else None

    def _tag(*names: str) -> str | None:
        for n in names:
            for tags in (ftags, *({str(k).lower(): val for k, val in (s.get("tags") or {}).items()} for s in streams)):
                if tags.get(n):
                    return str(tags[n])
        return None

    fps_avg = _rate(v.get("avg_frame_rate")) if v else 0.0
    fps_r = _rate(v.get("r_frame_rate")) if v else 0.0
    handlers = sorted({str((s.get("tags") or {}).get("handler_name")) for s in streams if (s.get("tags") or {}).get("handler_name")})
    tc = _tag("timecode")
    bwf_ref = _num(_tag("time_reference"))
    a_sr = int(a.get("sample_rate", 0) or 0) if a else 0
    return {
        "make": _tag("com.apple.quicktime.make", "make", "com.android.manufacturer", "manufacturer"),
        "model": _tag("com.apple.quicktime.model", "model", "com.android.model", "originator"),
        "software": _tag("com.apple.quicktime.software", "software", "encoder", "encoded_by"),
        "originator": _tag("originator"),
        "serial": _tag("com.apple.quicktime.camera.identifier", "serial", "camera_serial_number", "originator_reference"),
        "handler_names": handlers,
        "creation_time": _tag("creation_time", "com.apple.quicktime.creationdate", "origination_date"),
        "timecode": tc,
        "timecode_sec": _parse_timecode(tc, fps_avg or fps_r),
        "bwf_time_reference_sec": round(bwf_ref / a_sr, 6) if (bwf_ref is not None and a_sr) else None,
        "container": str(fmt.get("format_name", "")),
        "video_codec": str(v.get("codec_name")) if v else None,
        "pix_fmt": str(v.get("pix_fmt")) if v else None,
        "width": int(v.get("width", 0) or 0) if v else 0,
        "height": int(v.get("height", 0) or 0) if v else 0,
        "fps_avg": round(fps_avg, 4),
        "fps_r": round(fps_r, 4),
        "video_frames": int(v.get("nb_frames", 0) or 0) if v and str(v.get("nb_frames", "")).isdigit() else None,
        "video_duration_sec": _num(v.get("duration")) if v else None,
        "audio_streams": len(audios),
        "audio_codec": str(a.get("codec_name")) if a else None,
        "audio_sample_rate": a_sr,
        "audio_channels": int(a.get("channels", 0) or 0) if a else 0,
        "audio_channel_layout": str(a.get("channel_layout")) if a and a.get("channel_layout") else None,
        "audio_bit_depth": int(a.get("bits_per_raw_sample") or a.get("bits_per_sample") or 0) if a else 0,
        "audio_duration_sec": _num(a.get("duration")) if a else None,
        "container_duration_sec": _num(fmt.get("duration")),
        "attached_picture": any((s.get("disposition") or {}).get("attached_pic") for s in streams),
        "filename": Path(str(path)).name,
    }


def classify(ident: dict[str, Any]) -> tuple[str, list[str]]:
    """Return ``(source_kind, reasons)``."""
    name = ident["filename"].lower().replace("_", " ").replace("-", " ")
    hints = " ".join(str(ident.get(k) or "") for k in ("software", "make", "model", "originator")) + " " + " ".join(ident.get("handler_names") or [])
    reasons: list[str] = []
    vcodec = ident.get("video_codec")
    if vcodec:
        frames = ident.get("video_frames")
        dur = ident.get("video_duration_sec") or ident.get("container_duration_sec") or 0.0
        if vcodec in _IMAGE_CODECS and ((frames is not None and frames <= 1) or dur < 0.2 or "image2" in ident.get("container", "")):
            return "graphics_or_still", [f"single {vcodec} image"]
        if any(str(ident.get("pix_fmt") or "").startswith(p) for p in _ALPHA_PIX):
            return "graphics_or_still", [f"video with alpha channel ({ident.get('pix_fmt')}): graphics overlay"]
        if re.search(r"\b(gfx|graphics?|logo|bug|lower ?third|title|still)\b", name):
            reasons.append("filename suggests graphics")
            return "graphics_or_still", reasons
        if _SCREEN_HINTS.search(hints) or _SCREEN_HINTS.search(name) or re.search(r"\bscreen\b|\bcapture\b", name):
            return "screen_capture", ["screen-recording software or filename"]
        if (ident.get("width"), ident.get("height")) in _DISPLAY_SIZES and abs(ident.get("fps_avg", 0) - ident.get("fps_r", 0)) > 0.5:
            return "screen_capture", ["display resolution with variable frame rate"]
        if ident.get("audio_streams"):
            return "camera_av", [f"{vcodec} video with {ident.get('audio_codec')} audio"]
        return "camera_video_only", [f"{vcodec} video without audio"]
    ch = int(ident.get("audio_channels") or 0)
    maker = _RECORDER_MAKERS.search(hints)
    if ch >= 3:
        return "audio_recorder_multitrack", [f"{ch}-channel audio" + (f" from {maker.group(1)}" if maker else "")]
    if re.search(r"\b(lav|lavalier|lapel|wireless|tx\d?|lav\d)\b", name) or "wireless" in hints.lower():
        return "audio_recorder_lav", ["lavalier/wireless recorder hint in name or tags"]
    return "audio_recorder", [f"{ch}-channel audio-only" + (f" ({maker.group(1)})" if maker else "")]


# ---------------------------------------------------------------------------
# Decode probes, VFR and audio fingerprint
# ---------------------------------------------------------------------------
def decode_probe_frame(path: str | Path, t_sec: float, w: int = 32, h: int = 18) -> tuple[np.ndarray | None, list[str]]:
    rc, out, err = _run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t_sec):.3f}", "-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-vf", f"scale={w}:{h}:flags=area", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"], 60.0, binary=True)
    errs = [ln.strip() for ln in err.splitlines() if _ERROR_PATTERNS.search(ln)][:3]
    if rc != 0 or not out or len(out) < w * h:
        return None, errs or [err.strip()[:200] or "no frame decoded"]
    return np.frombuffer(out[: w * h], dtype=np.uint8).reshape(h, w), errs


def dhash(gray: np.ndarray) -> int:
    """64-bit difference hash of a small grey frame (perceptual, codec-independent)."""
    from PIL import Image

    small = np.asarray(Image.fromarray(gray).resize((9, 8), Image.Resampling.BILINEAR), dtype=np.int16)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return int(sum(1 << i for i, b in enumerate(bits) if b))


def packet_timing(path: str | Path, start_sec: float = 0.0, n_packets: int = 240) -> list[float]:
    spec = f"{start_sec:.3f}%+#{n_packets}" if start_sec > 0 else f"%+#{n_packets}"
    rc, out, _ = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", spec, "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path)], 60.0)
    if rc != 0:
        return []
    vals = sorted(v for v in (_num(x.strip().rstrip(",")) for x in out.splitlines()) if v is not None)
    return vals


def vfr_measure(path: str | Path, duration: float, ident: dict[str, Any]) -> dict[str, Any]:
    """Frame-interval statistics from packet timestamps at the start and middle (no decoding)."""
    pts = packet_timing(path, 0.0) + packet_timing(path, max(0.0, duration / 2.0))
    d = np.diff(np.array(pts)) if len(pts) > 3 else np.zeros(0)
    d = d[(d > 1e-5) & (d < 1.0)]
    if d.size < 10:
        return {"vfr": False, "measured": False}
    med = float(np.median(d))
    dev = np.abs(d - med) / max(med, 1e-6)
    frac_off = float(np.mean(dev > 0.2))
    rate_mismatch = ident.get("fps_r", 0) > 0 and ident.get("fps_avg", 0) > 0 and abs(ident["fps_r"] - ident["fps_avg"]) / ident["fps_r"] > 0.01
    return {
        "vfr": bool(frac_off > 0.05 or (rate_mismatch and frac_off > 0.01)),
        "measured": True,
        "median_frame_interval_ms": round(med * 1000.0, 3),
        "irregular_interval_share": round(frac_off, 4),
        "r_vs_avg_rate_mismatch": bool(rate_mismatch),
    }


def audio_window_native(path: str | Path, start: float, dur: float, sr: int = 8000) -> np.ndarray:
    """Multichannel float PCM ``(n, ch)`` for a bounded window (keeps channel separation)."""
    rc, out, _ = _run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-i", str(path), "-t", f"{dur:.3f}", "-map", "0:a:0", "-ar", str(sr), "-f", "f32le", "-c:a", "pcm_f32le", "pipe:1"], 120.0, binary=True)
    if rc != 0 or not out:
        return np.zeros((0, 1), dtype=np.float32)
    return np.frombuffer(out, dtype=np.float32).copy()


def fingerprint(mono: np.ndarray, sr: int = 8000) -> np.ndarray:
    """Haitsma-Kalker style 16-bit sub-fingerprints (100 ms frames, 50 ms hop, 17 bands 300-3400 Hz)."""
    hop, win = sr // 20, sr // 10
    if mono.size < win * 4:
        return np.zeros(0, dtype=np.uint16)
    n = 1 + (mono.size - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    frames = mono[idx] * np.hanning(win)[None, :]
    spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    freqs = np.fft.rfftfreq(win, 1.0 / sr)
    edges = np.geomspace(300.0, 3400.0, 18)
    bands = np.stack([spec[:, (freqs >= lo) & (freqs < hi)].sum(axis=1) for lo, hi in zip(edges[:-1], edges[1:], strict=True)], axis=1)
    e = np.log10(bands + 1e-10)
    dd = (e[1:, :-1] - e[1:, 1:]) - (e[:-1, :-1] - e[:-1, 1:])
    bits = (dd > 0).astype(np.uint16)
    return (bits * (1 << np.arange(16, dtype=np.uint16))[None, :]).sum(axis=1).astype(np.uint16)


def fingerprint_ber(a: np.ndarray, b: np.ndarray, max_shift: int = 20) -> tuple[float, int]:
    """Lowest bit error rate between two fingerprints over +/- ``max_shift`` frames (50 ms each)."""
    if a.size < 20 or b.size < 20:
        return 1.0, 0
    best = (1.0, 0)
    for s in range(-max_shift, max_shift + 1):
        x = a[max(0, s) :]
        y = b[max(0, -s) :]
        n = min(x.size, y.size)
        if n < 20:
            continue
        diff = np.bitwise_xor(x[:n], y[:n])
        ber = float(np.unpackbits(diff.view(np.uint8)).mean())
        if ber < best[0]:
            best = (ber, s)
    return best


# ---------------------------------------------------------------------------
# Per-source inspection
# ---------------------------------------------------------------------------
def inspect_source(path: str | Path, sha256: str, cache: Any = None) -> dict[str, Any]:
    """Identify, classify and validate one source (cached by content hash)."""
    key = {"sha256": sha256, "size": Path(path).stat().st_size}
    if cache is not None:
        hit = cache.get("ingest_inspect", INGEST_VERSION, key)
        if hit:
            return {**hit, "cache_hit": True}
    raw, perr = ffprobe_raw(path)
    if raw is None:
        msg = perr or "ffprobe could not read the file"
        hint = "The file is truncated or still being copied (container index missing). Re-copy it from the card; if the original is gone, an MP4 recovery tool can rebuild the index." if re.search(r"moov atom not found|invalid data", msg, re.I) else "Check the file opens in a player; re-export or re-copy it."
        return {"readable": False, "error": msg, "action": hint, "issues": [{"code": "UNREADABLE_MEDIA", "severity": "error", "message": f"{Path(path).name}: {msg}. {hint}"}]}
    ident = identify(raw, path)
    kind, reasons = classify(ident)
    issues: list[dict[str, Any]] = []
    dur = ident.get("container_duration_sec") or ident.get("video_duration_sec") or ident.get("audio_duration_sec") or 0.0
    probes: list[dict[str, Any]] = []
    hashes: list[int] = []
    if ident.get("video_codec") and kind != "graphics_or_still":
        for label, t in (("start", 0.0), ("middle", dur / 2.0), ("end", max(0.0, dur - 1.0))):
            fr, errs = decode_probe_frame(path, t)
            probes.append({"at": label, "t_sec": round(t, 3), "decoded": fr is not None, "errors": errs})
            if fr is not None:
                hashes.append(dhash(fr))
        failed = [p for p in probes if not p["decoded"]]
        if failed:
            where = ", ".join(f"{p['at']} ({p['t_sec']:.1f}s)" for p in failed)
            issues.append({"code": "DECODE_FAILURE", "severity": "error" if len(failed) >= 2 else "warning", "message": f"{ident['filename']}: no picture decodes at {where}. The file is probably truncated or corrupt; re-copy it from the original media before editing."})
        elif any(p["errors"] for p in probes):
            issues.append({"code": "DECODE_ERRORS", "severity": "warning", "message": f"{ident['filename']}: decoder reported errors ({'; '.join(e for p in probes for e in p['errors'])[:160]}). Check for corruption around those points."})
    vd, ad = ident.get("video_duration_sec"), ident.get("audio_duration_sec")
    if vd and ad and abs(vd - ad) > max(0.5, 0.01 * max(vd, ad)):
        issues.append({"code": "STREAM_DURATION_MISMATCH", "severity": "warning", "message": f"{ident['filename']}: video lasts {vd:.2f}s but audio {ad:.2f}s. Expect missing picture or sound at the end; sync uses the audio."})
    vfr = vfr_measure(path, dur, ident) if ident.get("video_codec") and kind not in ("graphics_or_still",) else {"vfr": False, "measured": False}
    if vfr.get("vfr"):
        issues.append({"code": "VARIABLE_FRAME_RATE", "severity": "warning", "message": f"{ident['filename']}: variable frame rate ({vfr.get('irregular_interval_share', 0):.0%} irregular frame intervals). Phones and screen recorders do this; conform it to constant frame rate before a long edit or lip sync can wander."})
    audio: dict[str, Any] = {}
    fp = np.zeros(0, dtype=np.uint16)
    if ident.get("audio_streams"):
        win = audio_window_native(path, 0.0, min(60.0, max(1.0, dur)))
        ch = max(1, int(ident.get("audio_channels") or 1))
        if win.size >= ch:
            pcm = win[: (win.size // ch) * ch].reshape(-1, ch)
            rms = np.sqrt(np.mean(pcm.astype(np.float64) ** 2, axis=0) + 1e-18)
            rms_db = [round(float(20 * np.log10(r + 1e-12)), 2) for r in rms]
            silent = [i for i, r in enumerate(rms_db) if r < -70.0]
            audio = {"channel_rms_db_first_60s": rms_db, "silent_channels": silent}
            if len(silent) == ch:
                issues.append({"code": "SILENT_AUDIO", "severity": "warning", "message": f"{ident['filename']}: the audio is silent in the first minute (mic muted, unplugged or wrong input). It cannot be used for sync or dialogue."})
            elif silent:
                issues.append({"code": "SILENT_CHANNEL", "severity": "info", "message": f"{ident['filename']}: channel(s) {', '.join(str(i + 1) for i in silent)} of {ch} are silent; map the live channel(s) only so the mix is not 6 dB low."})
            fp = fingerprint(pcm.mean(axis=1))
    elif ident.get("video_codec") and kind in ("camera_video_only",):
        issues.append({"code": "NO_AUDIO", "severity": "warning", "message": f"{ident['filename']}: camera has no audio track, so it cannot be synchronised by sound. Align it by timecode or a visual slate, or record scratch audio."})
    result = {
        "readable": True,
        "identity": ident,
        "source_kind": kind,
        "classification_reasons": reasons,
        "duration_sec": round(float(dur), 4),
        "decode_probes": probes,
        "video_dhash": [f"{h:016x}" for h in hashes],
        "vfr": vfr,
        "audio": audio,
        "fingerprint_frames": int(fp.size),
        "fingerprint_hex": fp.tobytes().hex(),
        "issues": issues,
    }
    if cache is not None:
        cache.put("ingest_inspect", INGEST_VERSION, key, result)
    return {**result, "cache_hit": False}


def find_duplicates(entries: list[dict[str, Any]], *, max_ber: float = 0.15) -> list[dict[str, Any]]:
    """Group duplicate sources: identical SHA-256, same audio fingerprint + duration, or same video hash."""
    dups: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    ok = [e for e in entries if e.get("status") == "ok"]
    for i, a in enumerate(ok):
        for b in ok[i + 1 :]:
            pair = (a["camera_id"], b["camera_id"])
            if a.get("sha256") and a.get("sha256") == b.get("sha256"):
                dups.append({"camera_ids": list(pair), "kind": "identical_sha256", "evidence": {"sha256": a["sha256"]}})
                seen_pairs.add(pair)
                continue
            da, db = float(a.get("duration_sec", 0)), float(b.get("duration_sec", 0))
            same_dur = abs(da - db) <= max(0.25, 0.005 * max(da, db))
            fa, fb = a.get("_fp"), b.get("_fp")
            if same_dur and fa is not None and fb is not None and fa.size and fb.size:
                ber, shift = fingerprint_ber(fa, fb)
                if ber <= max_ber:
                    dups.append({"camera_ids": list(pair), "kind": "same_audio_fingerprint", "evidence": {"bit_error_rate": round(ber, 4), "shift_ms": shift * 50, "duration_diff_sec": round(abs(da - db), 3)}})
                    seen_pairs.add(pair)
                    continue
            ha, hb = a.get("video_dhash") or [], b.get("video_dhash") or []
            if same_dur and len(ha) == 3 and len(hb) == 3 and not (fa is not None and fa.size and fb is not None and fb.size):
                dist = [bin(int(x, 16) ^ int(y, 16)).count("1") for x, y in zip(ha, hb, strict=True)]
                if max(dist) <= 6:
                    dups.append({"camera_ids": list(pair), "kind": "same_video_hash", "evidence": {"hamming": dist}})
    return dups


def continuation_groups(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Files from the same device whose creation times chain (a camera that stopped and restarted)."""
    from datetime import datetime

    def _ts(s: str | None) -> float | None:
        if not s:
            return None
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    by_dev: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for e in entries:
        ident = (e.get("device") or {})
        if e.get("status") != "ok" or not (ident.get("make") or ident.get("model")):
            continue
        by_dev.setdefault((str(ident.get("make")), str(ident.get("model")) + str(ident.get("serial") or "")), []).append(e)
    out = []
    for (make, model), items in by_dev.items():
        stamped = sorted((t, e) for e in items if (t := _ts((e.get("device") or {}).get("creation_time"))) is not None)
        for (t1, e1), (t2, e2) in zip(stamped, stamped[1:], strict=False):
            gap = t2 - (t1 + float(e1.get("duration_sec", 0)))
            if -5.0 <= gap <= 3600.0:
                out.append({"device": f"{make} {model}".strip(), "camera_ids": [e1["camera_id"], e2["camera_id"]], "gap_sec": round(gap, 1)})
    return out


__all__ = ["INGEST_VERSION", "classify", "continuation_groups", "find_duplicates", "fingerprint", "fingerprint_ber", "identify", "inspect_source"]
