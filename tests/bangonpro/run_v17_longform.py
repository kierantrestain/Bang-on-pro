#!/usr/bin/env python3
"""V1.7 long-form YouTube production journey (A10-A12 hardening).

The 9.92 s exports in ``run_v17_acceptance.py`` are smoke evidence. This journey runs a
representative ~10 minute, three-camera podcast through the real AI production department
and the real MLT export engine, and checks the result against ground truth that this script
owns (not against any agent's self-assessment):

* editorial decisions - speech retained, dead air removed, protected pauses/laughter kept, no
  cuts inside speech, correct camera on speech, shot minimums (``bench_v122_compare.evaluate``);
* multicamera synchronisation - recovered per-camera offsets versus the generated offsets;
* export A/V synchronisation - every source carries frame-accurate sync markers (a one-frame
  white flash plus a 5.5 kHz beep at the same master time). In the melt export each surviving
  marker must appear at the timeline position the edit decision implies, and its flash and beep
  must coincide within one frame;
* captions - cue structure, timing inside programme speech and (with lexical TTS speech and a
  Whisper model) recognised words versus the script;
* revision behaviour - a scoped "tighten the opening" revision changes nothing outside its range,
  re-exports cleanly, and undo restores the previous cut exactly;
* memory - peak and post-revision RSS of the department process, peak RSS of child renders;
* export integrity - container/stream durations, codecs and timeline duration agreement.

Evidence class: synthetic generated footage (licence-free). Physical-device playback and human
editing time are out of scope and reported as such.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import resource
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (REPO_ROOT, REPO_ROOT / "tests" / "bangonpro"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import bench_v122_compare as bc  # noqa: E402

SR = bc.SR
FPS = bc.FPS
FRAME = 1.0 / FPS
# Picture/sound may differ by at most half a frame (a camera whose offset is a fractional number of
# frames cannot do better) plus detector resolution; a whole-frame lip-sync error always fails.
AV_TOLERANCE_MS = 1000.0 * FRAME / 2 + 5.0
BEEP_HZ = 5500.0
PROFILE = "podcast_multicam"
BRIEF = "Edit this podcast into a complete YouTube episode: keep the natural rhythm, match the cameras, captions"
ROLE_CAMS = {"wide": "cam_wide", "host": "cam_host", "guest": "cam_guest"}

HOST_SCRIPT = [
    "Welcome back to the studio, today we are looking at how small creative teams publish every single week.",
    "The first thing I noticed is that consistency matters far more than any single viral moment.",
    "So walk me through the system you use when a new episode lands on your desk.",
    "That makes sense, and how do you decide which moments become short clips for social media?",
    "Let us talk about the business side, because revenue is where most channels struggle.",
    "Before we wrap up, what would you tell someone starting their very first channel tomorrow?",
]
GUEST_SCRIPT = [
    "Thanks for having me, it is great to finally sit down and talk about the process properly.",
    "We record everything on three cameras, then the edit starts with the strongest story beats.",
    "Honestly the clips choose themselves when a guest laughs or says something surprising.",
    "We built a simple calendar, a weekly review meeting and a checklist that never changes.",
    "Start before you feel ready, publish on a schedule and learn from every single upload.",
]


# ----------------------------------------------------------------------------- media
def _tts(text: str, out: Path, voice: int) -> bool:
    run = lambda c: subprocess.run(c, capture_output=True, text=True, check=False).returncode == 0  # noqa: E731,S603
    if shutil.which("say"):
        aiff = out.with_suffix(".aiff")
        if not run(["say", "-v", ["Samantha", "Daniel"][voice % 2], "-o", str(aiff), text]) and not run(["say", "-o", str(aiff), text]):
            return False
        return run(["ffmpeg", "-y", "-v", "error", "-i", str(aiff), "-ac", "1", "-ar", str(SR), str(out)])
    for tool in ("espeak-ng", "espeak"):
        if shutil.which(tool):
            raw = out.with_suffix(".raw.wav")
            if run([tool, "-v", ["en-us", "en-gb"][voice % 2], "-s", "165", "-w", str(raw), text]):
                return run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-ac", "1", "-ar", str(SR), str(out)])
    return False


def _tts_bank(work: Path) -> dict[str, list[np.ndarray]] | None:
    from bangonpro.ai_engine.agents.ingest_sync import extract_mono_pcm

    bank: dict[str, list[np.ndarray]] = {"host": [], "guest": []}
    for role, lines, voice in (("host", HOST_SCRIPT, 0), ("guest", GUEST_SCRIPT, 1)):
        for i, line in enumerate(lines):
            wav = work / f"tts_{role}_{i}.wav"
            if not _tts(line, wav, voice):
                return None
            pcm = np.asarray(extract_mono_pcm(wav, sample_rate=SR), dtype=np.float64)
            bank[role].append(pcm / max(1e-6, float(np.max(np.abs(pcm)))))
    return bank


def _fill(dur: float, clips: list[np.ndarray], cursor: list[int]) -> np.ndarray:
    """Fill ``dur`` seconds with consecutive TTS sentences (0.12 s apart), truncating the last one."""
    n = int(round(dur * SR))
    out = np.zeros(n)
    i = 0
    while i < n:
        clip = clips[cursor[0] % len(clips)]
        cursor[0] += 1
        k = min(n - i, clip.size)
        seg = clip[:k].copy()
        fade = min(k, int(0.03 * SR))
        if fade:
            seg[-fade:] *= np.linspace(1.0, 0.0, fade)
        out[i:i + k] += seg
        i += k + int(0.12 * SR)
    return out


def synthesise(base: Path, gt: dict[str, Any], *, lexical: bool) -> tuple[dict[str, Path], list[float], bool]:
    """Three-camera conversation per ``gt`` with sync markers; returns camera paths, marker master times, lexical flag."""
    from test_ai_production import _synthesize_speech_like_signal, _write_wav_16bit

    n = int(round(gt["duration"] * SR))
    host, guest, slate = np.zeros(n), np.zeros(n), np.zeros(n)

    def place(buf: np.ndarray, start: float, sig: np.ndarray, gain: float) -> None:
        i = int(round(start * SR))
        j = min(n, i + sig.size)
        buf[i:j] += gain * sig[: j - i]

    bank = _tts_bank(base) if lexical else None
    lexical = bank is not None
    cursors = {"host": [0], "guest": [0]}
    click = np.random.default_rng(1).standard_normal(int(0.02 * SR)) * np.hanning(int(0.02 * SR))
    place(slate, 1.0, click / np.max(np.abs(click)), 0.45)
    seed = 600
    for role, s, e in gt["speech"]:
        seed += 7
        for who, buf, f0, syl in (("host", host, 140.0, 3.9), ("guest", guest, 215.0, 4.4)):
            if role in (who, "both"):
                sig = _fill(e - s, bank[who], cursors[who]) if bank else _synthesize_speech_like_signal(e - s, SR, f0_hz=f0, syllable_hz=syl, seed=seed + (who == "guest"))
                place(buf, s, sig, 0.55 if role == who else 0.24)
    for role, s, e in gt["interjections"]:
        place(host if role == "host" else guest, s, _synthesize_speech_like_signal(e - s, SR, f0_hz=150.0, syllable_hz=4.0, seed=999), 0.30)
    for k, (lab, ls, le, _keep) in enumerate(gt["protected"]):
        if lab == "laughter":
            place(host, ls, bc._laughter(le - ls, 11 + k), 0.40)
            place(guest, ls, bc._laughter(le - ls, 12 + k), 0.45)

    # Sync markers: inside a host turn and a guest turn of every block, on exact frame boundaries.
    period = 47.0
    blocks = int(round(gt["duration"] / period))
    markers = [round(b * period + off, 2) for b in range(blocks) for off in (5.0, 19.6)]
    t = np.arange(int(0.04 * SR)) / SR
    beep = np.sin(2 * np.pi * BEEP_HZ * t) * np.hanning(t.size) ** 0.25
    for m in markers:
        place(slate, m, beep, 0.5)

    mixes = {"wide": (0.45, 0.45, 0.6), "host": (1.0, 0.14, 0.55), "guest": (0.14, 1.0, 0.55)}
    colors = {"wide": "#1B3B6F", "host": "#9B2226", "guest": "#2D6A4F"}
    out_dir = base / "media"
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for idx, (role, (hg, gg, sg)) in enumerate(mixes.items()):
        off = gt["offsets"][role]
        lead = int(round(off * SR))
        audio = 0.0004 * np.random.default_rng(80 + idx).standard_normal(lead + n)
        audio[lead:] += hg * host + gg * guest + sg * slate
        wav = out_dir / f"{role}.wav"
        _write_wav_16bit(wav, audio, SR)
        dur = (lead + n) / SR
        # One white frame at each marker (local time = master + camera offset).
        enable = "+".join(f"between(t,{m + off - 0.001:.3f},{m + off + FRAME - 0.002:.3f})" for m in markers)
        vf = f"drawbox=x=0:y=0:w=iw:h=ih:color=white@1:t=fill:enable='{enable}'"
        mp4 = out_dir / f"{role}.mp4"
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={colors[role]}:s=640x360:r={FPS}:d={dur:.4f}",
               "-i", str(wav), "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", str(FPS),
               "-c:a", "aac", "-b:a", "160k", "-ar", str(SR), "-shortest", str(mp4)]
        subprocess.run(cmd, check=True, capture_output=True, timeout=900)  # noqa: S603
        wav.unlink()
        paths[role] = mp4
    return paths, markers, lexical


# ----------------------------------------------------------------------------- measurement
class RssSampler:
    """Samples this process's resident set size while the department runs."""

    def __init__(self) -> None:
        self.samples: list[float] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    @staticmethod
    def now_mb() -> float:
        try:
            for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
        except OSError:
            pass
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True, check=False).stdout.strip()  # noqa: S603,S607
        return round(int(out or 0) / 1024.0, 1)

    def _loop(self) -> None:
        while not self._stop.wait(0.5):
            self.samples.append(self.now_mb())

    def __enter__(self) -> RssSampler:
        self._t.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._t.join(timeout=2)


def _maxrss_mb(who: int) -> float:
    v = resource.getrusage(who).ru_maxrss
    return round(v / (1024.0 * 1024.0) if sys.platform == "darwin" else v / 1024.0, 1)


def melt_render(project: Path, out: Path) -> dict[str, Any]:
    env = dict(os.environ, SDL_AUDIODRIVER="dummy", QT_QPA_PLATFORM="offscreen")
    cmd = ["melt", str(project), "-consumer", f"avformat:{out}", "vcodec=libx264", "preset=ultrafast", "crf=24",
           "acodec=aac", "ab=160k", "ar=48000", "pix_fmt=yuv420p", "real_time=-1"]
    if shutil.which("xvfb-run") and sys.platform.startswith("linux"):
        cmd = ["xvfb-run", "-a", *cmd]
    t0 = time.perf_counter()
    res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3600, check=False)  # noqa: S603
    secs = round(time.perf_counter() - t0, 2)
    if res.returncode != 0 or not out.exists() or out.stat().st_size < 100_000:
        raise RuntimeError(f"melt render failed (rc={res.returncode}): {res.stderr[-1500:]}")
    return {"render_sec": secs, **probe(out)}


def probe(path: Path) -> dict[str, Any]:
    res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration,size:stream=codec_type,codec_name,width,height,duration,sample_rate,avg_frame_rate",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)  # noqa: S603,S607
    info = json.loads(res.stdout)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), {})
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), {})
    return {"path": str(path), "size_bytes": path.stat().st_size, "duration_sec": round(float(info["format"]["duration"]), 3),
            "video": {k: v.get(k) for k in ("codec_name", "width", "height", "avg_frame_rate", "duration")},
            "audio": {k: a.get(k) for k in ("codec_name", "sample_rate", "duration")}}


def detect_flashes(path: Path) -> list[float]:
    res = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
                          "-an", "-f", "null", "-"], capture_output=True, text=True, timeout=1800, check=True)  # noqa: S603,S607
    times, cur = [], None
    for line in res.stdout.splitlines():
        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            cur = float(m.group(1))
        m = re.search(r"YAVG=([0-9.]+)", line)
        if m and cur is not None and float(m.group(1)) > 190.0:
            times.append(cur)
    merged: list[float] = []
    for t in times:  # consecutive lit frames are one marker; keep its first frame
        if not merged or t - merged[-1] > 0.5:
            merged.append(t)
    return merged


def detect_beeps(path: Path) -> list[float]:
    """Onsets of the 40 ms 5.5 kHz sync beeps.

    A beep is a pure tone: nearly all of the 3-8 kHz energy sits in a 200 Hz band around 5.5 kHz.
    Sibilants in real speech carry 5.5 kHz energy too but spread it across the whole band, so the
    detector requires narrowband dominance as well as level (energy alone missed beeps masked by
    adjacent "s" sounds in lexical TTS speech).
    """
    from scipy.signal import butter, sosfiltfilt

    from bangonpro.ai_engine.agents.ingest_sync import extract_mono_pcm

    pcm = np.asarray(extract_mono_pcm(path, sample_rate=SR), dtype=np.float64)
    narrow = sosfiltfilt(butter(6, [5400 / (SR / 2), 5600 / (SR / 2)], btype="bandpass", output="sos"), pcm)
    wide = sosfiltfilt(butter(6, [3000 / (SR / 2), 8000 / (SR / 2)], btype="bandpass", output="sos"), pcm)
    hop, win = int(0.002 * SR), int(0.010 * SR)
    kern = np.ones(win) / win
    en = np.convolve(narrow ** 2, kern, mode="same")[::hop]
    ew = np.convolve(wide ** 2, kern, mode="same")[::hop] + 1e-12
    tonal = en / ew
    level = np.sqrt(en)
    floor = float(np.median(level)) + 1e-9
    # Beeps measure ~2500x the median band level for 40-50 ms; filter ringing on speech peaks for
    # single 2 ms hops at ~20x. Require both a sustained run and a clear level margin.
    above = (tonal > 0.6) & (level > 200.0 * floor)
    min_run = int(0.020 / 0.002)
    onsets: list[float] = []
    i = 0
    while i < above.size:
        if above[i]:
            j = i
            while j + 1 < above.size and above[j + 1]:
                j += 1
            if j - i + 1 >= min_run:
                run = en[i:j + 1]
                k = i + int(np.argmax(run >= 0.5 * float(run.max())))  # centred window: half energy at the beep's start
                t0 = k * hop / SR
                if not onsets or t0 - onsets[-1] > 0.5:
                    onsets.append(round(float(t0), 4))
            i = j + 1
        else:
            i += 1
    return onsets


def expected_marker_times(shots: list[dict[str, Any]], markers: list[float]) -> dict[float, float]:
    exp = {}
    for m in markers:
        for sh in shots:
            if sh["master_sync_in_sec"] - FRAME / 2 <= m < sh["master_sync_out_sec"] - FRAME / 2:
                exp[m] = sh["timeline_in_sec"] + (m - sh["master_sync_in_sec"])
                break
    return exp


def verify_markers(render: Path, shots: list[dict[str, Any]], markers: list[float]) -> dict[str, Any]:
    exp = expected_marker_times(shots, markers)
    flashes, beeps = detect_flashes(render), detect_beeps(render)
    rows, worst_av, worst_pos, missing = [], 0.0, 0.0, 0
    for m, te in sorted(exp.items(), key=lambda kv: kv[1]):
        f = min(flashes, key=lambda x: abs(x - te), default=None)
        b = min(beeps, key=lambda x: abs(x - te), default=None)
        ok_f = f is not None and abs(f - te) <= FRAME + 0.005
        ok_b = b is not None and abs(b - te) <= FRAME + 0.005
        if not (ok_f and ok_b):
            missing += 1
        else:
            worst_av = max(worst_av, abs(f - float(b)))
            worst_pos = max(worst_pos, abs(f - te), abs(float(b) - te))
        rows.append({"master": m, "expected": round(te, 3), "flash": f, "beep": b})
    unexpected = [f for f in flashes if all(abs(f - te) > 0.5 for te in exp.values())]
    return {"markers_in_source": len(markers), "markers_expected_in_export": len(exp), "flashes_detected": len(flashes), "beeps_detected": len(beeps),
            "missing_or_misplaced": missing, "unexpected_flashes": len(unexpected), "max_av_offset_ms": round(float(worst_av) * 1000, 1), "av_tolerance_ms": AV_TOLERANCE_MS,
            "max_position_error_ms": round(worst_pos * 1000, 1), "detail": rows}


def program_speech(shots: list[dict[str, Any]], gt: dict[str, Any]) -> list[tuple[float, float]]:
    out = []
    for sh in shots:
        for _, s, e in gt["speech"]:
            a, b = max(s, sh["master_sync_in_sec"]), min(e, sh["master_sync_out_sec"])
            if b > a:
                o = sh["timeline_in_sec"] - sh["master_sync_in_sec"]
                out.append((a + o, b + o))
    return bc._union(out)


def parse_srt(path: Path) -> list[dict[str, Any]]:
    def ts(x: str) -> float:
        h, m, rest = x.strip().split(":")
        s, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

    cues = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip()):
        lines = block.strip().splitlines()
        if len(lines) >= 2 and "-->" in lines[1]:
            a, b = lines[1].split("-->")
            cues.append({"start": ts(a), "end": ts(b), "text": " ".join(lines[2:])})
    return cues


def verify_captions(srt: Path | None, shots: list[dict[str, Any]], gt: dict[str, Any], program_sec: float, lexical: bool) -> dict[str, Any]:
    if srt is None or not srt.is_file():
        return {"present": False}
    cues = parse_srt(srt)
    speech = program_speech(shots, gt)
    dur = sum(c["end"] - c["start"] for c in cues)
    in_speech = sum(bc._overlap(speech, c["start"], c["end"]) for c in cues)
    covered = sum(bc._overlap([(c["start"], c["end"]) for c in cues], s, e) for s, e in speech)
    speech_total = sum(e - s for s, e in speech)
    order_ok = all(c["start"] < c["end"] for c in cues) and all(a["end"] <= b["start"] + 1e-3 for a, b in zip(cues, cues[1:]))
    vocab = {w for line in HOST_SCRIPT + GUEST_SCRIPT for w in re.findall(r"[a-z']{3,}", line.lower())}
    words = [w for c in cues for w in re.findall(r"[a-z']{3,}", c["text"].lower())]
    return {"present": True, "cues": len(cues), "ordered_non_overlapping": order_ok,
            "within_programme": all(0 <= c["start"] and c["end"] <= program_sec + 0.5 for c in cues),
            "cue_time_in_speech_pct": round(100 * in_speech / dur, 1) if dur else 0.0,
            "speech_captioned_pct": round(100 * covered / speech_total, 1) if speech_total else 0.0,
            "lexical_speech": lexical, "script_word_match_pct": round(100 * sum(w in vocab for w in words) / len(words), 1) if words else 0.0,
            "sample": [c["text"] for c in cues[:4]]}


def shot_rows(job: Any) -> list[dict[str, Any]]:
    st = job._latest_state()
    return sorted(({"camera_id": s.camera_id, "master_sync_in_sec": round(s.master_sync_in_sec, 3), "master_sync_out_sec": round(s.master_sync_out_sec, 3),
                    "timeline_in_sec": round(s.timeline_in_sec, 3), "timeline_out_sec": round(s.timeline_out_sec, 3)} for s in st.rough_cut_shots),
                  key=lambda r: r["timeline_in_sec"])


def _outside(shots: list[dict[str, Any]], a: float, b: float) -> list[tuple[float, float, str]]:
    return [(r["master_sync_in_sec"], r["master_sync_out_sec"], r["camera_id"]) for r in shots if r["master_sync_out_sec"] <= a or r["master_sync_in_sec"] >= b]


# ----------------------------------------------------------------------------- journey
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--output", default=str(REPO_ROOT / "docs" / "evidence" / "v1.7" / "longform_journey.json"))
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--blocks", type=int, default=13, help="47 s scenario blocks (13 = 10.2 min)")
    ap.add_argument("--model-dir", default=None, help="Model directory with an installed Whisper model (captions)")
    ap.add_argument("--install-asr", action="store_true", help="Install faster-whisper-tiny through ModelManager first")
    ap.add_argument("--allow-missing-asr", action="store_true", help="Record captions as not verifiable instead of failing when no Whisper model is available")
    a = ap.parse_args(argv)

    import tempfile

    work = Path(a.work_dir or tempfile.mkdtemp(prefix="bop_v17_longform_")).resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("BANGONPRO_CACHE_DIR", str(work / "cache"))
    os.environ.setdefault("BANGONPRO_PREFS_DIR", str(work / "prefs"))
    from bangonpro.ai_engine.department import JobOptions, ProductionJob

    failures: list[str] = []
    result: dict[str, Any] = {"journey": "v1.7_longform_youtube_podcast", "evidence_class": "synthetic generated 3-camera footage; ground truth owned by this script",
                              "host": {"platform": platform.platform(), "machine": platform.machine(), "cpu_count": os.cpu_count(), "python": platform.python_version()}}
    model_dir = Path(a.model_dir).resolve() if a.model_dir else work / "models"
    if a.install_asr:
        from bangonpro.ai_engine.model_manager import ModelManager

        try:
            ModelManager(model_dir=model_dir).install_model("faster-whisper-tiny", timeout_sec=300.0)
            result["asr_install"] = "installed"
        except Exception as exc:  # noqa: BLE001 - reported honestly below
            result["asr_install"] = f"failed: {type(exc).__name__}: {exc}"[:300]

    gt = bc._tile(bc.S2_GT, a.blocks, "podcast_youtube_longform")
    t0 = time.perf_counter()
    cams, markers, lexical = synthesise(work, gt, lexical=True)
    result["scenario"] = {"description": gt["description"], "duration_sec": gt["duration"], "cameras": len(cams), "offsets_sec": gt["offsets"],
                          "sync_markers": len(markers), "speech": "lexical TTS" if lexical else "speech-like synthetic (no TTS available)",
                          "generation_sec": round(time.perf_counter() - t0, 2)}
    path_roles = {str(p.resolve()): r for r, p in cams.items()}

    # 1. Full production draft through the real department.
    rss_before = RssSampler.now_mb()
    with RssSampler() as rs:
        t0 = time.perf_counter()
        job = ProductionJob.create(job_root=work / "jobs", brief=BRIEF, inputs=[f"{ROLE_CAMS[r]}={p}" for r, p in cams.items()], profile_ref=PROFILE,
                                   mode="full_production_draft", options=JobOptions(export_video=True, episode_title="Creator Systems, Episode 7", model_dir=str(model_dir)),
                                   job_id="longform")
        doc = job.run()
        production_sec = round(time.perf_counter() - t0, 2)
    rss_after_job = RssSampler.now_mb()
    result["production"] = {"status": doc["status"], "wall_sec": production_sec, "processing_elapsed_sec": doc.get("processing_elapsed_sec"),
                            "agents": {k: {f: v.get(f) for f in ("status", "neural_inference", "duration_sec", "fallback_reason")} for k, v in doc["agents"].items()}}
    if doc["status"] not in ("draft_ready", "completed"):
        failures.append(f"production ended {doc['status']}")
    min_shot = float(job._latest_state().profile.get("min_shot_duration_sec", 2.0))
    ev = bc.evaluate(job.dir, gt, path_roles, min_shot, PROFILE)
    result["editorial"] = ev
    if not ev["source_integrity_verified"]:
        failures.append("source footage integrity not verified")
    if any(v > 1000.0 / FPS for v in ev["sync_error_ms"].values()) or len(ev["sync_error_ms"]) < 2:
        failures.append(f"multicamera sync error above one frame or unmeasured: {ev['sync_error_ms']}")
    if ev["speech_retained_pct"] is None or ev["speech_retained_pct"] < 95.0:
        failures.append(f"speech retained {ev['speech_retained_pct']}% < 95%")
    if ev["fixes_by_type"].get("dead_air_left", 0):
        failures.append(f"dead air left in {ev['fixes_by_type']['dead_air_left']} places")
    if ev["speech_on_correct_camera_pct"] is None or ev["speech_on_correct_camera_pct"] < 80.0:
        failures.append(f"speech on correct camera {ev['speech_on_correct_camera_pct']}% < 80%")
    if ev["qc_errors"]:
        failures.append(f"{ev['qc_errors']} QC errors in the draft")

    shots = shot_rows(job)
    program_sec = shots[-1]["timeline_out_sec"] if shots else 0.0
    project = Path(job.doc["artifacts"]["kdenlive_project"])

    # 2. Export through the real MLT engine; integrity + frame-accurate A/V sync.
    render = melt_render(project, work / "longform_export.mp4")
    sync = verify_markers(Path(render["path"]), shots, markers)
    result["export"] = {**render, "timeline_duration_sec": round(program_sec, 3), "markers": {k: v for k, v in sync.items() if k != "detail"}}
    if abs(render["duration_sec"] - program_sec) > 2 * FRAME + 0.05:
        failures.append(f"export duration {render['duration_sec']} s != timeline {program_sec:.3f} s")
    vd, ad = float(render["video"].get("duration") or 0), float(render["audio"].get("duration") or 0)
    if render["video"].get("codec_name") != "h264" or render["audio"].get("codec_name") != "aac" or abs(vd - ad) > 0.1:
        failures.append(f"export streams invalid: video {render['video']}, audio {render['audio']}")
    if sync["markers_expected_in_export"] < max(4, len(markers) // 2) or sync["missing_or_misplaced"] or sync["unexpected_flashes"] or sync["max_av_offset_ms"] > AV_TOLERANCE_MS:
        failures.append(f"export A/V sync markers failed: { {k: v for k, v in sync.items() if k != 'detail'} }")
    dept_video = job.doc["artifacts"].get("rendered_video")
    result["export"]["department_render"] = probe(Path(dept_video)) if dept_video and Path(dept_video).is_file() else None

    # 3. Captions.
    srt = job.doc["artifacts"].get("captions_srt")
    caps = verify_captions(Path(srt) if srt else None, shots, gt, program_sec, lexical)
    caps["asr_neural"] = bool(doc["agents"].get("captions", {}).get("neural_inference"))
    result["captions"] = caps
    if caps.get("present"):
        if caps["cues"] < 20 or not caps["ordered_non_overlapping"] or not caps["within_programme"]:
            failures.append(f"caption structure invalid: {caps}")
        if caps["cue_time_in_speech_pct"] < 85.0 or caps["speech_captioned_pct"] < 70.0:
            failures.append(f"caption timing does not follow programme speech: {caps['cue_time_in_speech_pct']}% in speech, {caps['speech_captioned_pct']}% of speech captioned")
        if lexical and caps["asr_neural"] and caps["script_word_match_pct"] < 50.0:
            failures.append(f"caption words match the script only {caps['script_word_match_pct']}%")
    elif a.allow_missing_asr:
        caps["not_verified_reason"] = doc["agents"].get("captions", {}).get("fallback_reason") or "no Whisper model available"
    else:
        failures.append("no captions produced (install a Whisper model or pass --allow-missing-asr)")

    # 4. Revision: scoped tighten of the opening; nothing outside changes; re-export; undo restores.
    with RssSampler() as rs2:
        t0 = time.perf_counter()
        rec = job.revise("Tighten the opening but keep the natural conversation")
        rev_sec = round(time.perf_counter() - t0, 2)
    shots_rev = shot_rows(job)
    rng = (rec.get("plan") or {}).get("range_sec") or [0.0, 90.0]
    outside_same = _outside(shots_rev, *rng) == _outside(shots, *rng)
    render_rev = melt_render(Path(job.doc["artifacts"]["kdenlive_project"]), work / "longform_revision_export.mp4")
    sync_rev = verify_markers(Path(render_rev["path"]), shots_rev, markers)
    undo = job.undo_revision()
    restored = shot_rows(job) == shots
    ev_rev = bc.evaluate(job.dir, gt, path_roles, min_shot, PROFILE) if restored else None
    result["revision"] = {"instruction": rec.get("instruction"), "status": rec.get("status"), "rerun_from": rec.get("rerun_from"), "range_sec": rng, "wall_sec": rev_sec,
                          "shots_before": len(shots), "shots_after": len(shots_rev), "outside_range_unchanged": outside_same,
                          "programme_sec_before": round(program_sec, 2), "programme_sec_after": shots_rev[-1]["timeline_out_sec"] if shots_rev else None,
                          "export": {**render_rev, "markers": {k: v for k, v in sync_rev.items() if k != "detail"}},
                          "undo_status": undo.get("status") if isinstance(undo, dict) else None, "undo_restored_exactly": restored,
                          "editorial_after_undo_fixes": ev_rev["corrective_fixes_needed"] if ev_rev else None}
    if rec.get("status") != "draft_ready" or not outside_same:
        failures.append(f"revision {rec.get('status')} / outside range unchanged={outside_same}")
    if sync_rev["missing_or_misplaced"] or sync_rev["unexpected_flashes"] or sync_rev["max_av_offset_ms"] > AV_TOLERANCE_MS:
        failures.append(f"post-revision export A/V sync failed: { {k: v for k, v in sync_rev.items() if k != 'detail'} }")
    if not restored:
        failures.append("undo did not restore the pre-revision cut exactly")

    # 5. Memory.
    peak = max(rs.samples + rs2.samples + [rss_after_job])
    result["memory"] = {"rss_before_mb": rss_before, "rss_after_production_mb": rss_after_job, "rss_after_revision_mb": RssSampler.now_mb(),
                        "peak_sampled_rss_mb": peak, "maxrss_self_mb": _maxrss_mb(resource.RUSAGE_SELF), "maxrss_children_mb": _maxrss_mb(resource.RUSAGE_CHILDREN)}
    if peak > 3072.0:
        failures.append(f"department peak RSS {peak} MB > 3072 MB")
    if result["memory"]["rss_after_revision_mb"] - rss_after_job > 512.0:
        failures.append("RSS grew by more than 512 MB across a revision")

    result["failures"] = failures
    result["ok"] = not failures
    result["sync_marker_detail"] = sync["detail"]
    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    summary = {k: result[k] for k in ("ok", "failures")} | {"production_sec": result["production"]["wall_sec"], "export": {k: result["export"][k] for k in ("duration_sec", "render_sec", "markers")},
                                                         "editorial_fixes": ev["corrective_fixes_needed"], "fixes_by_type": ev["fixes_by_type"], "captions": {k: caps.get(k) for k in ("present", "cues", "cue_time_in_speech_pct", "script_word_match_pct", "asr_neural")},
                                                         "memory": result["memory"]}
    print(json.dumps(summary, indent=2, default=str))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
