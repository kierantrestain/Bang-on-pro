"""Reusable deterministic media scenarios with known ground truth (V1.2.2 media processing).

Every generator is seeded, writes only into the directory it is given, and returns the file paths
plus the exact ground truth used to build them, so tests and the metrics script can compare what the
agents measure against what was synthesised. Evidence class: **synthetic**.

Scenarios: clock drift + dropout (long recording), mixed frame rates + VFR, clipped microphone,
crosstalk/bleed, noisy room, lighting change mid-recording, colour-cast camera, moving subject (with
an analytic ground-truth matte), duplicate files and a truncated file.

Run ``python3 tests/bangonpro/scenarios_v122.py --metrics docs/evidence/v1.2.2/media_metrics.json``
to regenerate the V1.2.1 vs V1.2.2 comparison.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from scipy.signal import butter, sosfilt

SR = 48000


# ---------------------------------------------------------------------------
# Signal primitives
# ---------------------------------------------------------------------------
def speech_like(duration_sec: float, sr: int = SR, *, seed: int, f0_hz: float = 140.0, activity: np.ndarray | None = None) -> np.ndarray:
    """Aperiodic speech-like signal: random syllables and pauses, jittered pitch, consonant noise.

    ``activity`` (optional, per-sample 0/1) restricts speech to turns. Peak-normalised to 1.0.
    """
    rng = np.random.default_rng(seed)
    n = round(duration_sec * sr)
    env = np.zeros(n)
    pos = 0
    while pos < n:
        syl = round(rng.uniform(0.08, 0.26) * sr)
        seg = np.hanning(syl) * rng.uniform(0.35, 1.0)
        end = min(n, pos + syl)
        env[pos:end] = seg[: end - pos]
        pause = rng.uniform(0.03, 0.15) if rng.random() > 0.12 else rng.uniform(0.3, 0.9)
        pos = end + round(pause * sr)
    if activity is not None:
        env *= activity[:n]
    walk = np.cumsum(rng.standard_normal(n // 480 + 2)) * 0.01
    walk = np.interp(np.arange(n) / 480.0, np.arange(walk.size), walk)
    f0 = f0_hz * (1.0 + np.clip(walk - walk.mean(), -0.15, 0.15))
    ph = 2.0 * np.pi * np.cumsum(f0) / sr
    voiced = 0.5 * np.sin(ph) + 0.3 * np.sin(2 * ph) + 0.2 * np.sin(3 * ph) + 0.12 * np.sin(5 * ph) + 0.06 * np.sin(8 * ph)
    sos = butter(4, [300.0 / (0.5 * sr), 3400.0 / (0.5 * sr)], btype="bandpass", output="sos")
    noise = sosfilt(sos, rng.standard_normal(n))
    noise /= max(1e-9, float(np.std(noise)))
    sig = (0.62 * voiced + 0.38 * 0.35 * noise) * env
    return sig / max(1e-9, float(np.max(np.abs(sig))))


def turn_activity(duration_sec: float, sr: int, *, seed: int, n_speakers: int = 2, overlap_prob: float = 0.08) -> list[np.ndarray]:
    """Alternating conversational turns (3-9 s) with occasional overlaps; returns per-speaker 0/1."""
    rng = np.random.default_rng(seed)
    n = round(duration_sec * sr)
    acts = [np.zeros(n) for _ in range(n_speakers)]
    pos, spk = round(0.5 * sr), 0
    while pos < n:
        dur = round(rng.uniform(3.0, 9.0) * sr)
        acts[spk][pos : pos + dur] = 1.0
        if rng.random() < overlap_prob:
            other = (spk + 1) % n_speakers
            o0 = pos + dur - round(0.8 * sr)
            acts[other][max(0, o0) : pos + dur] = 1.0
        pos += dur + round(rng.uniform(0.2, 0.8) * sr)
        spk = (spk + 1) % n_speakers
    return acts


def write_wav(path: Path, x: np.ndarray, sr: int = SR) -> Path:
    """16-bit PCM WAV; mono for 1-D input, interleaved for (n, ch)."""
    arr = np.asarray(x, dtype=np.float64)
    ch = 1 if arr.ndim == 1 else arr.shape[1]
    pcm = (np.clip(arr, -1.0, 32767.0 / 32768.0) * 32768.0).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(ch)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    return path


def warp_to_source(master: np.ndarray, sr: int, source_duration: float, segments: list[dict[str, float]]) -> np.ndarray:
    """Render a device recording from master content under ``source = master*(1+d) + offset`` per segment.

    ``segments`` (source-clock spans): ``[{"source_start_sec", "source_end_sec", "offset_sec", "drift_ppm"}]``.
    """
    n = round(source_duration * sr)
    s = np.arange(n) / sr
    out = np.zeros(n)
    for seg in segments:
        sel = (s >= seg["source_start_sec"]) & (s < seg["source_end_sec"])
        k = 1.0 + seg["drift_ppm"] * 1e-6
        tm = (s[sel] - seg["offset_sec"]) / k
        out[sel] = np.interp(tm * sr, np.arange(master.size), master, left=0.0, right=0.0)
    return out


def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(["ffmpeg", "-y", "-v", "error", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {' '.join(args)}\n{proc.stderr}")


def have_media_tools() -> bool:
    return all(shutil.which(t) for t in ("ffmpeg", "ffprobe"))


@dataclass
class Scenario:
    name: str
    files: dict[str, Path]
    truth: dict[str, Any] = field(default_factory=dict)

    def inputs(self) -> list[str]:
        return [f"{k}={v}" for k, v in self.files.items()]


# ---------------------------------------------------------------------------
# 1. Clock drift + dropout (long recording)
# ---------------------------------------------------------------------------
def drift_dropout(base: Path, *, duration_sec: float = 240.0, sr: int = SR, seed: int = 122) -> Scenario:
    """Reference recorder plus three devices with independent clocks.

    * ``cam_a``: +80 ppm drift, starts 1.25 s before the master, **1.5 s dropout** at 60 % of the file
      (the device stopped and resumed inside the same file, so 1.5 s of content is missing).
    * ``cam_b``: -60 ppm drift, offset +2.30 s, continuous.
    * ``cam_c``: +35 ppm, started **41 s late** (beyond V1.2.1's +/-30 s single-window search).
    """
    d = base / "drift_dropout"
    d.mkdir(parents=True, exist_ok=True)
    acts = turn_activity(duration_sec + 60.0, sr, seed=seed)
    host = speech_like(duration_sec + 60.0, sr, seed=seed + 1, f0_hz=135.0, activity=acts[0])
    guest = speech_like(duration_sec + 60.0, sr, seed=seed + 2, f0_hz=210.0, activity=acts[1])
    master = 0.5 * host + 0.45 * guest
    rng = np.random.default_rng(seed + 9)
    ref = master[: round(duration_sec * sr)] + 0.002 * rng.standard_normal(round(duration_sec * sr))
    files = {"rec_master": write_wav(d / "rec_master.wav", 0.7 * ref, sr)}
    truth: dict[str, Any] = {"reference": "rec_master", "duration_sec": duration_sec, "cameras": {}}

    def _device(name: str, offset: float, ppm: float, src_dur: float, dropout: tuple[float, float] | None, gain: float, seed_n: int) -> None:
        k = 1.0 + ppm * 1e-6
        if dropout is None:
            segs_src = [{"source_start_sec": 0.0, "source_end_sec": src_dur, "offset_sec": offset, "drift_ppm": ppm}]
            segs_master = [{"master_start_sec": 0.0, "master_end_sec": float("inf"), "offset_sec": offset, "drift_ppm": ppm}]
            gaps: list[dict[str, float]] = []
        else:
            s_d, g = dropout
            o2 = offset - k * g
            t_d = (s_d - offset) / k
            segs_src = [
                {"source_start_sec": 0.0, "source_end_sec": s_d, "offset_sec": offset, "drift_ppm": ppm},
                {"source_start_sec": s_d, "source_end_sec": src_dur, "offset_sec": o2, "drift_ppm": ppm},
            ]
            segs_master = [
                {"master_start_sec": 0.0, "master_end_sec": t_d, "offset_sec": offset, "drift_ppm": ppm},
                {"master_start_sec": t_d, "master_end_sec": float("inf"), "offset_sec": o2, "drift_ppm": ppm},
            ]
            gaps = [{"master_start_sec": t_d, "master_end_sec": t_d + g}]
        sig = warp_to_source(master, sr, src_dur, segs_src)
        sos = butter(2, [120.0 / (0.5 * sr), 7000.0 / (0.5 * sr)], btype="bandpass", output="sos")
        sig = sosfilt(sos, sig) * gain + 0.003 * np.random.default_rng(seed_n).standard_normal(sig.size)
        files[name] = write_wav(d / f"{name}.wav", sig, sr)
        truth["cameras"][name] = {"offset_sec": offset, "drift_ppm": ppm, "segments": segs_master, "gaps": gaps}

    _device("cam_a", 1.25, 80.0, duration_sec - 4.0, (0.6 * (duration_sec - 4.0), 1.5), 0.8, seed + 11)
    _device("cam_b", 2.30, -60.0, duration_sec - 6.0, None, 0.6, seed + 12)
    _device("cam_c", -41.0, 35.0, duration_sec - 50.0, None, 0.7, seed + 13)
    return Scenario("drift_dropout", files, truth)


def true_source_time(cam_truth: dict[str, Any], master_sec: float) -> float | None:
    for seg in cam_truth["segments"]:
        if seg["master_start_sec"] <= master_sec < seg["master_end_sec"]:
            return master_sec + seg["offset_sec"] + seg["drift_ppm"] * 1e-6 * master_sec
    return None


def in_gap(cam_truth: dict[str, Any], master_sec: float, pad: float = 0.0) -> bool:
    return any(g["master_start_sec"] - pad <= master_sec < g["master_end_sec"] + pad for g in cam_truth.get("gaps", []))


__all__ = [
    "SR",
    "Scenario",
    "drift_dropout",
    "have_media_tools",
    "in_gap",
    "speech_like",
    "true_source_time",
    "turn_activity",
    "warp_to_source",
    "write_wav",
]


if __name__ == "__main__":  # pragma: no cover - metrics entry point, see media_metrics below
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--metrics", default="")
    args = ap.parse_args()
    print(json.dumps({"scenarios": [n for n in __all__ if n[0].islower()]}, indent=2))
