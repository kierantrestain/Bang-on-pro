"""Visual and audio evidence for reasoning agents: sparse frames, contact sheets, low-res renders.

Only what a decision needs is decoded. Frames are cached on disk by source identity (path, size,
mtime), time and width; frames of the *rendered* draft are cached by draft revision and time, so an
unchanged timeline is never rendered or analysed twice. Agents can attach a note to what they saw
(``observations.json``) and later calls return it, so nobody looks again without a reason.
Every result says what it shows: a source frame of the clip on screen (effects not rendered) or a
frame of the rendered draft.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw


def _identity(path: str) -> str:
    try:
        stt = os.stat(path)
        return f"{Path(path).resolve()}|{stt.st_size}|{int(stt.st_mtime)}"
    except OSError:
        return str(path)


class FrameCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.notes_path = root / "observations.json"
        self.hits = 0
        self.misses = 0

    def key(self, *parts: Any) -> str:
        return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:20]

    def frame(self, path: str, t: float, width: int = 320, *, tag: str = "") -> bytes | None:
        k = self.key(_identity(path), round(t, 2), width, tag)
        f = self.root / f"{k}.jpg"
        if f.is_file():
            self.hits += 1
            return f.read_bytes()
        self.misses += 1
        cmd = ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", path, "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if res.returncode != 0 or not res.stdout:
            return None
        f.write_bytes(res.stdout)
        return res.stdout

    def notes(self) -> dict[str, Any]:
        try:
            return json.loads(self.notes_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def add_note(self, key: str, note: dict[str, Any]) -> None:
        n = self.notes()
        n[key] = note
        self.notes_path.write_text(json.dumps(n, indent=1), encoding="utf-8")


def contact_sheet(frames: list[tuple[bytes | None, str]], cols: int = 4, cell_w: int = 320) -> bytes:
    """Frames in a grid, each labelled (time, camera); a missing frame is a grey cell saying so."""
    imgs = []
    for data, label in frames:
        im = Image.open(io.BytesIO(data)).convert("RGB") if data else Image.new("RGB", (cell_w, cell_w * 9 // 16), (60, 60, 60))
        im = im.resize((cell_w, max(1, round(im.height * cell_w / im.width))))
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, cell_w, 16], fill=(0, 0, 0))
        d.text((4, 2), (label if data else f"{label} (no frame)")[:48], fill=(255, 255, 255))
        imgs.append(im)
    if not imgs:
        imgs = [Image.new("RGB", (cell_w, 180), (60, 60, 60))]
    cols = max(1, min(cols, len(imgs)))
    rows = (len(imgs) + cols - 1) // cols
    h = max(i.height for i in imgs)
    sheet = Image.new("RGB", (cols * cell_w, rows * h), (20, 20, 20))
    for i, im in enumerate(imgs):
        sheet.paste(im, ((i % cols) * cell_w, (i // cols) * h))
    buf = io.BytesIO()
    sheet.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


def render_range(project: Path, out: Path, start_frame: int, end_frame: int, *, cancelled: Any = None) -> dict[str, Any]:
    """Render [start, end) of the draft with melt (low bitrate) and probe the file.

    Rendered at the project size: melt's consumer-side scaling crashes MLT 7.22 on these projects,
    and frames are scaled when they are extracted anyway.
    """
    from bangonpro.ai_engine.agents.ingest_sync import probe_media_file

    out.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    tmp = out.with_name(out.stem + ".part" + out.suffix)
    cmd = ["melt", str(project), f"in={max(0, start_frame)}", f"out={max(start_frame, end_frame - 1)}", "-consumer", f"avformat:{tmp}", "vcodec=libx264", "preset=ultrafast",
           "vb=800k", "pix_fmt=yuv420p", "acodec=aac", "ab=96k", "ar=48000", "ac=2", "real_time=-2"]
    import tempfile
    import time as _time

    with tempfile.TemporaryFile(mode="w+") as errf:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errf, text=True, env=env)
        t0 = _time.monotonic()
        while proc.poll() is None:
            if (cancelled is not None and cancelled()) or _time.monotonic() - t0 > 1800:
                proc.kill()
                proc.wait()
                tmp.unlink(missing_ok=True)
                raise InterruptedError("render cancelled")
            _time.sleep(0.1)
        errf.seek(0)
        err = errf.read()[-4000:]
    if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < 1024:
        tmp.unlink(missing_ok=True)
        hint = " (MLT's Qt module needs a display: run under xvfb-run on a headless Linux machine)" if "X11" in err else ""
        raise RuntimeError(f"melt render failed (rc={proc.returncode}){hint}")
    tmp.replace(out)
    return probe_media_file(out)


def black_spans(path: Path) -> list[tuple[float, float]]:
    res = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vf", "blackdetect=d=0.2:pix_th=0.08", "-an", "-f", "null", "-"], capture_output=True, text=True, timeout=600, check=False)
    return [(float(a), float(b)) for a, b in re.findall(r"black_start:(\d+(?:\.\d+)?) black_end:(\d+(?:\.\d+)?)", res.stderr)]


def loudness(path: str, start: float | None = None, duration: float | None = None) -> dict[str, Any]:
    """EBU R128 integrated loudness and true peak (ffmpeg ebur128)."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-i", path, "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
    tail = res.stderr[res.stderr.rfind("Summary:"):] if "Summary:" in res.stderr else ""
    i = re.search(r"I:\s+(-?\d+(?:\.\d+)?) LUFS", tail)
    lra = re.search(r"LRA:\s+(-?\d+(?:\.\d+)?) LU", tail)
    tp = re.search(r"Peak:\s+(-?\d+(?:\.\d+)?) dBFS", tail)
    if not i:
        return {"measured": False, "reason": "no audio measured"}
    return {"measured": True, "integrated_lufs": float(i.group(1)), "lra_lu": float(lra.group(1)) if lra else None, "true_peak_dbtp": float(tp.group(1)) if tp else None}


def thumb_similarity(a: bytes | None, b: bytes | None) -> float | None:
    """0..1 similarity of two frames at 32x18 (mean absolute difference); None if either is missing."""
    if not a or not b:
        return None
    import numpy as np

    ia = np.asarray(Image.open(io.BytesIO(a)).convert("L").resize((32, 18)), dtype=np.float64)
    ib = np.asarray(Image.open(io.BytesIO(b)).convert("L").resize((32, 18)), dtype=np.float64)
    return round(1.0 - float(np.mean(np.abs(ia - ib))) / 255.0, 4)


__all__ = ["FrameCache", "black_spans", "contact_sheet", "loudness", "render_range", "thumb_similarity"]
