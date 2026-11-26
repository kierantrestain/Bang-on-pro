"""Synthetic single-camera talking-head footage for editor acceptance tests (V1.6).

One camera, one speaker, natural pauses and a long dead-air gap, a visible
"presenter" (a skin-toned head shape that moves slightly when speaking) on a
graded background. Deterministic and legally unencumbered. Evidence class:
synthetic.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import numpy as np
from test_ai_production import _synthesize_speech_like_signal

# (start_sec, duration_sec) of each spoken phrase; gaps between them are pauses.
DEFAULT_PHRASES = ((0.6, 3.2), (4.4, 2.8), (7.8, 3.4), (14.2, 3.0), (17.8, 2.6))


def generate_talking_head(out_dir: Path, *, duration_sec: float = 21.0, phrases=DEFAULT_PHRASES, name: str = "talking_head.mp4",
                          fps: int = 25, size: tuple[int, int] = (640, 360), hue_shift: float = 0.0, seed: int = 7) -> Path:
    """Render a talking-head MP4 (H.264 + AAC) and return its path. ``hue_shift`` varies the look between references."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / name
    if out.exists():
        return out
    sr = 48000
    n = int(round(duration_sec * sr))
    audio = np.zeros(n, dtype=np.float64)
    rng = np.random.default_rng(seed)
    for i, (start, dur) in enumerate(phrases):
        sig = _synthesize_speech_like_signal(dur, sr, f0_hz=140.0 + 8 * i, syllable_hz=4.0 + 0.2 * i, seed=seed * 100 + i)
        a = int(round(start * sr))
        b = min(n, a + sig.size)
        audio[a:b] += 0.5 * sig[: b - a]
    audio += 0.002 * rng.standard_normal(n)  # room tone
    audio = np.clip(audio, -1.0, 1.0)
    wav = out_dir / (out.stem + ".wav")
    with wave.open(str(wav), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(sr)
        fh.writeframes((audio * 32767).astype("<i2").tobytes())
    w, h = size
    # Background gradient + a head (ellipse via geq) that bobs while speaking (enable expressions).
    speaking = "+".join(f"between(T,{s:.2f},{s + d:.2f})" for s, d in phrases)
    bob = f"(4*sin(2*PI*3*T)*({speaking}))"
    head = (
        f"geq=lum='if(lt(pow((X-{w}/2)/({h}*0.16),2)+pow((Y-{h}*0.46-{bob})/({h}*0.22),2),1),170,60+40*Y/{h})':"
        f"cb='if(lt(pow((X-{w}/2)/({h}*0.16),2)+pow((Y-{h}*0.46-{bob})/({h}*0.22),2),1),110,{128 + int(30 * hue_shift)})':"
        f"cr='if(lt(pow((X-{w}/2)/({h}*0.16),2)+pow((Y-{h}*0.46-{bob})/({h}*0.22),2),1),155,{128 - int(20 * hue_shift)})'"
    )
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=gray:s={w}x{h}:r={fps}:d={duration_sec}",
        "-i", str(wav),
        "-vf", f"format=yuv420p,{head}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", str(fps),
        "-c:a", "aac", "-b:a", "128k", "-shortest", str(out),
    ]
    subprocess.run(cmd, check=True, timeout=180)
    wav.unlink(missing_ok=True)
    return out


__all__ = ["DEFAULT_PHRASES", "generate_talking_head"]
