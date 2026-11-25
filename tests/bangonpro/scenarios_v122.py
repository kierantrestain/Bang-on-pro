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
from fractions import Fraction
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


W, H = 320, 180


def write_av(path: Path, frame_fn: Any, n_frames: int, fps: str | float, audio: np.ndarray | None = None, sr: int = SR, *, vcodec: list[str] | None = None, acodec: list[str] | None = None, extra: list[str] | None = None, setpts: str | None = None) -> Path:
    """Encode frames from ``frame_fn(i) -> uint8 HxWx3`` (+ optional mono audio) with ffmpeg."""
    import tempfile

    wav = None
    args = ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "pipe:0"]
    if audio is not None:
        wav = Path(tempfile.mkstemp(suffix=".wav", dir=str(path.parent))[1])
        write_wav(wav, audio, sr)
        args += ["-i", str(wav)]
    args += ["-map", "0:v"] + (["-map", "1:a"] if audio is not None else [])
    if setpts:
        args += ["-vf", f"settb=1/30000,setpts={setpts}", "-fps_mode", "passthrough", "-video_track_timescale", "30000"]
    args += vcodec or ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "25"]
    if audio is not None:
        args += acodec or ["-c:a", "aac", "-b:a", "160k"]
    args += (extra or []) + [str(path)]
    proc = subprocess.Popen(["ffmpeg", "-y", "-v", "error", *args], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    for i in range(n_frames):
        proc.stdin.write(np.ascontiguousarray(frame_fn(i), dtype=np.uint8).tobytes())
    proc.stdin.close()
    err = proc.stderr.read().decode() if proc.stderr else ""
    rc = proc.wait()
    if wav is not None:
        wav.unlink(missing_ok=True)
    if rc != 0:
        raise RuntimeError(f"ffmpeg failed for {path}: {err}")
    return path


def _scene(seed: int = 5) -> dict[str, np.ndarray]:
    """Studio-like test scene in linear-ish display RGB 0..1: textured backdrop, grey card, face, patches."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:H, 0:W]
    back = np.stack([0.36 + 0.10 * (xx / W), 0.38 + 0.05 * (yy / H), 0.42 - 0.06 * (xx / W)], axis=-1)
    back += 0.02 * rng.standard_normal((H, W, 1))
    img = back.copy()
    img[20:70, 20:80] = (0.55, 0.55, 0.55)  # neutral grey card
    img[20:70, 240:300] = (0.85, 0.85, 0.84)  # white card
    face = ((xx - 160) / 34.0) ** 2 + ((yy - 85) / 44.0) ** 2 <= 1.0
    img[face] = (0.80, 0.56, 0.44)  # skin tone
    body = (yy > 125) & (np.abs(xx - 160) < 60)
    img[body] = (0.18, 0.22, 0.35)
    for k, c in enumerate(((0.7, 0.2, 0.2), (0.2, 0.6, 0.25), (0.2, 0.3, 0.7))):
        img[140:170, 20 + 30 * k : 45 + 30 * k] = c
    return {"img": np.clip(img, 0, 1), "face": face, "grey": np.s_[20:70, 20:80], "white": np.s_[20:70, 240:300]}


def _to8(img: np.ndarray) -> np.ndarray:
    return (np.clip(img, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


# ---------------------------------------------------------------------------
# 2. Mixed frame rates + VFR
# ---------------------------------------------------------------------------
MIXED_FPS = {"cam_25": ("25", 0.0), "cam_2997": ("30000/1001", 0.36), "cam_30": ("30", 0.72), "cam_50": ("50", 1.04), "cam_5994": ("60000/1001", 1.48)}


def mixed_fps(base: Path, *, duration_sec: float = 10.0, sr: int = SR, seed: int = 31) -> Scenario:
    d = base / "mixed_fps"
    d.mkdir(parents=True, exist_ok=True)
    acts = turn_activity(duration_sec + 3, sr, seed=seed)
    speech = 0.5 * speech_like(duration_sec + 3, sr, seed=seed + 1, activity=acts[0]) + 0.4 * speech_like(duration_sec + 3, sr, seed=seed + 2, f0_hz=205, activity=acts[1])
    sc = _scene(seed)["img"]
    files: dict[str, Path] = {}
    truth: dict[str, Any] = {"offsets_sec": {}, "fps": {}}
    for name, (fps, off) in MIXED_FPS.items():
        f = float(Fraction(fps))
        n = round((duration_sec + off) * f)
        audio = np.concatenate([0.0005 * np.random.default_rng(seed).standard_normal(round(off * sr)), speech[: round(duration_sec * sr)]])

        def frame(i: int, _f: float = f) -> np.ndarray:
            img = sc.copy()
            x = int(40 + (i / _f) * 20) % (W - 30)
            img[100:120, x : x + 20] = (0.9, 0.9, 0.2)
            return _to8(img)

        files[name] = write_av(d / f"{name}.mp4", frame, n, fps, audio, sr)
        truth["offsets_sec"][name] = off
        truth["fps"][name] = f
    # Variable frame rate phone clip (timestamps irregular: 1.4/30, 1.4/30, 0.2/30 s ...)
    files["phone_vfr"] = write_av(d / "phone_vfr.mp4", lambda i: _to8(sc), 240, 30, speech[: 8 * sr], sr, setpts="(N+0.4*mod(N\\,3))/(30*TB)")
    truth["vfr"] = ["phone_vfr"]
    return Scenario("mixed_fps", files, truth)


# ---------------------------------------------------------------------------
# 3. Duplicates and a truncated file
# ---------------------------------------------------------------------------
def duplicates_and_corrupt(base: Path, *, duration_sec: float = 8.0, sr: int = SR, seed: int = 41) -> Scenario:
    d = base / "duplicates"
    d.mkdir(parents=True, exist_ok=True)
    acts = turn_activity(duration_sec, sr, seed=seed)
    host = speech_like(duration_sec, sr, seed=seed + 1, activity=acts[0])
    guest = speech_like(duration_sec, sr, seed=seed + 2, f0_hz=210, activity=acts[1])
    sc = _scene(seed)["img"]
    n = round(duration_sec * 25)

    def frame(i: int) -> np.ndarray:
        img = sc.copy()
        img[90:110, (10 + 2 * i) % 300 : (10 + 2 * i) % 300 + 16] = (0.95, 0.9, 0.1)
        return _to8(img)

    files = {"cam_host": write_av(d / "cam_host.mp4", frame, n, 25, 0.6 * host + 0.15 * guest, sr)}
    files["cam_guest"] = write_av(d / "cam_guest.mp4", lambda i: _to8(sc[:, ::-1]), n, 25, 0.15 * host + 0.6 * guest, sr)
    shutil.copyfile(files["cam_host"], d / "cam_host_COPY.mp4")
    files["cam_host_copy"] = d / "cam_host_COPY.mp4"
    _ffmpeg(["-i", str(files["cam_host"]), "-c:v", "mpeg4", "-q:v", "5", "-c:a", "pcm_s16le", str(d / "A001_C003_proxy.mov")])
    files["cam_host_proxy"] = d / "A001_C003_proxy.mov"
    # Truncated without faststart: the index (moov) is at the end and gets cut off
    raw = files["cam_host"].read_bytes()
    (d / "cam_wide_truncated.mp4").write_bytes(raw[: int(len(raw) * 0.6)])
    files["cam_wide_truncated"] = d / "cam_wide_truncated.mp4"
    truth = {"duplicates": [{"cam_host", "cam_host_copy"}, {"cam_host", "cam_host_proxy"}, {"cam_host_copy", "cam_host_proxy"}], "not_duplicates": [{"cam_host", "cam_guest"}], "unreadable": ["cam_wide_truncated"]}
    return Scenario("duplicates_and_corrupt", files, truth)


# ---------------------------------------------------------------------------
# 4-6. Audio: clipped mic, crosstalk/bleed, noisy room
# ---------------------------------------------------------------------------
def audio_mics(base: Path, *, duration_sec: float = 60.0, sr: int = SR, seed: int = 51) -> Scenario:
    """Three lavalier recorders with known level, bleed, clipping and noise floor.

    * ``mic_host``: host direct at -20 dBFS speech RMS, guest bleed at -14 dB relative, clean floor.
    * ``mic_guest``: guest direct 7 dB quieter than the host mic, host bleed -18 dB, **hard clipped**
      between 12 s and 18 s (gain x4 into a 0.98 ceiling).
    * ``mic_noisy``: third speaker in a noisy room: broadband room noise at -42 dBFS RMS.
    """
    d = base / "audio_mics"
    d.mkdir(parents=True, exist_ok=True)
    acts = turn_activity(duration_sec, sr, seed=seed, n_speakers=3)
    sp = [speech_like(duration_sec, sr, seed=seed + k, f0_hz=f, activity=acts[k]) for k, f in enumerate((130.0, 205.0, 170.0))]

    def _rms_db(x: np.ndarray, act: np.ndarray) -> float:
        sel = x[act > 0]
        return float(20 * np.log10(np.sqrt(np.mean(sel * sel)) + 1e-12))

    def _scale(x: np.ndarray, act: np.ndarray, target_db: float) -> np.ndarray:
        return x * 10 ** ((target_db - _rms_db(x, act)) / 20.0)

    rng = np.random.default_rng(seed + 7)
    floor = 10 ** (-66 / 20.0)
    own = {"mic_host": -20.0, "mic_guest": -27.0, "mic_noisy": -22.0}
    direct = {m: _scale(sp[k], acts[k], own[m]) for k, m in enumerate(own)}
    # bleed[j][i]: speaker i's level in mic j relative to speaker i on their own mic (as measured)
    bleed = {"mic_host": {"mic_guest": -7.0, "mic_noisy": -18.0}, "mic_guest": {"mic_host": -25.0, "mic_noisy": -20.0}, "mic_noisy": {"mic_host": -16.0, "mic_guest": -14.0}}
    mix = {}
    for j in own:
        x = direct[j].copy()
        for i, b_db in bleed[j].items():
            x += direct[i] * 10 ** (b_db / 20.0)
        mix[j] = x + floor * rng.standard_normal(x.size)
    # A 1.5 s shout inside the guest's first long turn drives the guest mic into a 0.98 ceiling
    on = np.where(acts[1] > 0)[0]
    a = int(on[0] + round(0.5 * sr)) if on.size else round(12 * sr)
    b = a + round(1.5 * sr)
    shout = mix["mic_guest"].copy()
    shout[a:b] += direct["mic_guest"][a:b] * 5.0
    unclipped = shout.copy()
    mix["mic_guest"] = np.clip(shout, -0.98, 0.98)
    over = np.abs(unclipped) > 0.98
    sp_samples = int(np.sum(acts[1][: unclipped.size] > 0))
    sos = butter(2, [200.0 / (0.5 * sr), 4000.0 / (0.5 * sr)], btype="bandpass", output="sos")
    room = sosfilt(sos, rng.standard_normal(unclipped.size))
    room *= 10 ** (-42 / 20.0) / float(np.sqrt(np.mean(room * room)))
    mix["mic_noisy"] = mix["mic_noisy"] + room
    band = butter(4, [300.0 / (0.5 * sr), 3400.0 / (0.5 * sr)], btype="bandpass", output="sos")
    sp_band = sosfilt(band, direct["mic_noisy"])[acts[2] > 0]
    nz_band = sosfilt(band, room)
    snr_truth = 10 * np.log10(np.mean(sp_band**2) / np.mean(nz_band**2))
    files = {m: write_wav(d / f"{m}.wav", x, sr) for m, x in mix.items()}
    write_wav(d / "reference_mic_guest_unclipped.wav", np.clip(unclipped, -1.0, 1.0) * 0.5, sr)  # metrics only (-6 dB), not an input
    truth = {
        "speech_rms_dbfs": own,
        "bleed_db": bleed,
        "clipped": {"mic_guest": {"start_sec": round(a / sr, 3), "end_sec": round(b / sr, 3), "clipped_samples": int(over.sum()), "clipped_share_of_speech": round(float(over.sum()) / max(1, sp_samples), 6)}},
        "noise_floor_dbfs": {"mic_host": -66.0, "mic_guest": -66.0, "mic_noisy": -42.0},
        "speech_band_snr_db": {"mic_noisy": round(float(snr_truth), 2)},
        "unclipped_reference_gain_db": -6.02,
    }
    return Scenario("audio_mics", files, truth)


# ---------------------------------------------------------------------------
# 7-8. Colour: colour-cast camera and lighting change mid-recording
# ---------------------------------------------------------------------------
CAST_GAINS = (1.12, 1.0, 0.86)
CAST_EXPOSURE = 0.90


def colour_cameras(base: Path, *, duration_sec: float = 8.0, seed: int = 61, change_at_sec: float = 4.0) -> Scenario:
    """Two angles of one scene: ``cam_ref`` neutral, ``cam_cast`` with a warm cast and -1.3 EV-ish exposure,
    plus ``cam_light`` whose lighting changes at ``change_at_sec`` (warmer and darker)."""
    d = base / "colour"
    d.mkdir(parents=True, exist_ok=True)
    s = _scene(seed)
    img = s["img"]
    n = round(duration_sec * 25)
    rng = np.random.default_rng(seed)
    noise = [0.006 * rng.standard_normal((H, W, 1)) for _ in range(4)]
    files = {"cam_ref": write_av(d / "cam_ref.mp4", lambda i: _to8(img + noise[i % 4]), n, 25)}
    cast = np.clip(img * np.array(CAST_GAINS) * CAST_EXPOSURE, 0, 1)
    files["cam_cast"] = write_av(d / "cam_cast.mp4", lambda i: _to8(cast[:, ::-1] + noise[(i + 1) % 4]), n, 25)
    light_after = np.clip(img * np.array((1.06, 1.0, 0.90)) * 0.85, 0, 1)
    files["cam_light"] = write_av(d / "cam_light.mp4", lambda i: _to8((img if i / 25.0 < change_at_sec else light_after) + noise[(i + 2) % 4]), n, 25)
    truth = {"cast_gains": CAST_GAINS, "cast_exposure": CAST_EXPOSURE, "light_change_at_sec": change_at_sec, "light_after_gains": (1.06, 1.0, 0.90), "light_after_exposure": 0.85, "face_rgb": (0.80, 0.56, 0.44), "grey_rgb": (0.55, 0.55, 0.55)}
    return Scenario("colour_cameras", files, truth)


# ---------------------------------------------------------------------------
# 9. Moving subject with analytic ground-truth matte
# ---------------------------------------------------------------------------
def moving_subject(base: Path, *, duration_sec: float = 6.0, fps: int = 25, seed: int = 71, static_tail_sec: float = 0.0) -> Scenario:
    """Skin-toned head-and-shoulders shape moving left to right over a textured backdrop."""
    d = base / "moving"
    d.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:H, 0:W]
    back = np.stack([0.30 + 0.25 * ((xx // 16 + yy // 16) % 2), 0.42 + 0.05 * np.sin(xx / 9.0), 0.48 - 0.1 * (yy / H)], axis=-1) + 0.015 * rng.standard_normal((H, W, 1))
    n = round(duration_sec * fps)
    move_frames = round((duration_sec - static_tail_sec) * fps)

    def centre(i: int) -> float:
        k = min(i, move_frames - 1)
        return 70.0 + 180.0 * k / max(1, move_frames - 1)

    def mask(i: int) -> np.ndarray:
        cx = centre(i)
        head = ((xx - cx) / 26.0) ** 2 + ((yy - 70) / 34.0) ** 2 <= 1.0
        torso = ((xx - cx) / 52.0) ** 2 + ((yy - 165) / 60.0) ** 2 <= 1.0
        return head | torso

    def frame(i: int) -> np.ndarray:
        img = back.copy()
        m = mask(i)
        img[m] = (0.82, 0.58, 0.46)
        return _to8(img)

    files = {"cam_host": write_av(d / "cam_host.mp4", frame, n, fps, 0.3 * speech_like(duration_sec, SR, seed=seed, f0_hz=150.0))}
    return Scenario("moving_subject", files, {"fps": fps, "n_frames": n, "mask_fn": mask, "centre_fn": centre, "static_tail_sec": static_tail_sec})


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
    "audio_mics",
    "colour_cameras",
    "drift_dropout",
    "duplicates_and_corrupt",
    "mixed_fps",
    "moving_subject",
    "write_av",
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
