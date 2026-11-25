"""Measured microphone analysis for the Audio Engineer (V1.2.2).

One bounded streaming pass per microphone (``ffmpeg`` f32 PCM at the native rate, all channels;
windowed sampling for very long files) measures, per mic:

* peak, RMS, speech-active RMS, noise floor (5th percentile of 50 ms frame levels), crest factor;
* **clipping**: exact sample plateaus for lossless sources (PCM/FLAC/ALAC), an amplitude-density
  spike at the top level for lossy sources (AAC/MP3/Opus), with clipped-sample share and events;
* band levels (low < 300 Hz, speech band 300-3400 Hz, high > 3.4 kHz) and spectral tilt;
* a 10 Hz level envelope (and speech-band envelope) used for activity, crosstalk/bleed and the
  intelligibility proxy (speech-band SNR).

Results are cached by content hash, so the quality loop and reruns never decode again.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

AUDIO_VERSION = "1.2.2-audio-1"
LOSSLESS = {"pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_s16be", "pcm_s24be", "flac", "alac", "pcm_f64le", "wavpack"}
ENV_HZ = 10.0


def _db(x: float | np.ndarray) -> Any:
    return 20.0 * np.log10(np.maximum(np.asarray(x, dtype=np.float64), 1e-9))


def _stream_native(path: str, sr: int, ch: int, spans: list[tuple[float, float]] | None, chunk_sec: float = 10.0):
    """Yield ``(t0_sec, pcm[n, ch])`` from one ffmpeg process per span (whole file when ``spans`` is None)."""
    for a, b in spans or [(0.0, -1.0)]:
        cmd = ["ffmpeg", "-v", "error"]
        if a > 0:
            cmd += ["-ss", f"{a:.3f}"]
        cmd += ["-i", path]
        if b > a:
            cmd += ["-t", f"{b - a:.3f}"]
        cmd += ["-map", "0:a:0", "-vn", "-ar", str(sr), "-ac", str(ch), "-f", "f32le", "-c:a", "pcm_f32le", "pipe:1"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False)
        assert proc.stdout is not None
        step = int(chunk_sec * sr) * ch * 4
        pos = 0
        try:
            while True:
                buf = proc.stdout.read(step)
                if not buf:
                    break
                usable = len(buf) // (4 * ch) * 4 * ch
                arr = np.frombuffer(buf[:usable], dtype=np.float32).reshape(-1, ch)
                yield a + pos / sr, arr
                pos += arr.shape[0]
        finally:
            proc.stdout.close()
            proc.wait(timeout=30)


def analyse_mic(path: str, *, sample_rate: int, channels: int, duration: float, codec: str | None, full_max_sec: float = 2400.0, windows: int = 24, window_sec: float = 30.0) -> dict[str, Any]:
    """Measure one microphone/recorder file (source clock)."""
    sr = int(min(48000, max(8000, sample_rate or 48000)))
    ch = max(1, int(channels or 1))
    spans = None
    if duration > full_max_sec:
        starts = np.linspace(0.0, max(0.0, duration - window_sec), windows)
        spans = [(float(s), float(s) + window_sec) for s in starts]
    lossless = (codec or "") in LOSSLESS
    hop = int(sr / ENV_HZ)
    sos_sp = butter(4, [300.0 / (0.5 * sr), 3400.0 / (0.5 * sr)], btype="bandpass", output="sos")
    sos_lo = butter(4, 300.0 / (0.5 * sr), btype="lowpass", output="sos")
    sos_hi = butter(4, min(0.95, 3400.0 / (0.5 * sr)), btype="highpass", output="sos")
    zi = {k: None for k in ("sp", "lo", "hi")}
    env_t: list[np.ndarray] = []
    env_db: list[np.ndarray] = []
    env_sp: list[np.ndarray] = []
    band_pw = {"lo": [], "sp": [], "hi": []}
    peak = np.zeros(ch)
    n_total = 0
    sumsq = 0.0
    clip_plateau = np.zeros(ch, dtype=np.int64)
    top_hist = np.zeros((ch, 64), dtype=np.int64)  # |x| histogram over [0, 1.05]
    clip_frames: list[tuple[float, int]] = []
    carry = np.zeros((0, ch), dtype=np.float32)
    carry_t = None
    for t0, arr in _stream_native(path, sr, ch, spans):
        if carry.shape[0]:
            arr = np.vstack([carry, arr])
            t0 = carry_t if carry_t is not None else t0
        k = arr.shape[0] // hop
        if k == 0:
            carry, carry_t = arr, t0
            continue
        use, carry = arr[: k * hop], arr[k * hop :]
        carry_t = t0 + k * hop / sr
        x = use.astype(np.float64)
        ax = np.abs(x)
        peak = np.maximum(peak, ax.max(axis=0))
        n_total += x.shape[0]
        mono = x.mean(axis=1)
        sumsq += float(np.sum(mono * mono))
        top_hist += np.stack([np.histogram(ax[:, c], bins=64, range=(0.0, 1.05))[0] for c in range(ch)])
        # Exact plateaus: >= 3 identical consecutive samples at high level (digital clipping in PCM)
        eq = (np.abs(np.diff(x, axis=0)) < 1e-7) & (ax[1:] > 0.25)
        run3 = eq[1:] & eq[:-1]
        clip_plateau += run3.sum(axis=0)
        per_frame = np.zeros(k, dtype=np.int64)
        if run3.any():
            idx = np.where(run3.any(axis=1))[0] + 1
            np.add.at(per_frame, np.minimum(idx // hop, k - 1), 1)
        for fi in np.where(per_frame > 0)[0]:
            clip_frames.append((t0 + fi * hop / sr, int(per_frame[fi])))
        fr = mono.reshape(k, hop)
        env_t.append(t0 + (np.arange(k) + 0.5) * hop / sr)
        env_db.append(10 * np.log10(np.mean(fr * fr, axis=1) + 1e-12))
        for key, sos in (("sp", sos_sp), ("lo", sos_lo), ("hi", sos_hi)):
            if zi[key] is None:
                zi[key] = sosfilt_zi(sos) * mono[0]
            y, zi[key] = sosfilt(sos, mono, zi=zi[key])
            p = np.mean(y.reshape(k, hop) ** 2, axis=1)
            band_pw[key].append(p)
            if key == "sp":
                env_sp.append(10 * np.log10(p + 1e-12))
    if not env_db:
        return {"measured": False, "reason": "no decodable audio"}
    t = np.concatenate(env_t)
    lev = np.concatenate(env_db)
    lev_sp = np.concatenate(env_sp)
    bands = {k: np.concatenate(v) for k, v in band_pw.items()}
    floor = float(np.percentile(lev, 5))
    active = lev > max(floor + 12.0, -60.0)
    act_rms = float(10 * np.log10(np.mean(10 ** (lev[active] / 10)) + 1e-12)) if active.any() else float(lev.max())
    peak_db = [round(float(v), 2) for v in _db(peak)]
    # Clipping share
    if lossless:
        clipped = int(clip_plateau.max())
        method = "exact_sample_plateaus"
    else:
        clipped = 0
        method = "amplitude_density_spike"
        for c in range(ch):
            hist = top_hist[c]
            cum = np.cumsum(hist[::-1])[::-1]
            top_bin = int(np.searchsorted(np.cumsum(hist), hist.sum() * (1 - 1e-4)))
            if top_bin < 16:
                continue
            hi = hist[max(0, top_bin - 1) :].sum()
            ref = hist[max(0, int(top_bin * 0.86)) : max(1, int(top_bin * 0.9))].sum() + 1
            if hi > 3 * ref and hi > 50:
                clipped = max(clipped, int(cum[max(0, top_bin - 1)] - ref))
    ratio = clipped / max(1, n_total)
    active_samples = max(1, int(active.sum()) * hop)
    events: list[dict[str, float]] = []
    for ts, cnt in clip_frames:
        if events and ts - events[-1]["end_sec"] <= 0.25:
            events[-1]["end_sec"] = round(ts + 1.0 / ENV_HZ, 3)
            events[-1]["clipped_samples"] += cnt
        else:
            events.append({"start_sec": round(ts, 3), "end_sec": round(ts + 1.0 / ENV_HZ, 3), "clipped_samples": cnt})
    sp_act = float(10 * np.log10(np.mean(bands["sp"][active]) + 1e-12)) if active.any() else None
    quiet = lev <= np.percentile(lev, 20)
    sp_noise = float(10 * np.log10(np.mean(bands["sp"][quiet]) + 1e-12)) if quiet.any() else None
    lo_act = float(10 * np.log10(np.mean(bands["lo"][active]) + 1e-12)) if active.any() else None
    hi_act = float(10 * np.log10(np.mean(bands["hi"][active]) + 1e-12)) if active.any() else None
    pk_active = float(_db(peak.max()))
    return {
        "measured": True,
        "sample_rate": sr,
        "channels": ch,
        "lossless": lossless,
        "sampled_spans": spans,
        "peak_dbfs": max(peak_db),
        "peak_dbfs_per_channel": peak_db,
        "rms_dbfs": round(float(10 * np.log10(sumsq / max(1, n_total) + 1e-12)), 2),
        "speech_rms_dbfs": round(act_rms, 2),
        "noise_floor_dbfs": round(floor, 2),
        "crest_db": round(pk_active - act_rms, 2),
        "active_share": round(float(active.mean()), 4),
        "clipping": {"method": method, "clipped_samples": int(clipped), "clipped_share": round(ratio, 6), "clipped_share_of_speech": round(clipped / active_samples, 6), "events": events[:200], "event_count": len(events)},
        "bands_db": {"low_rel": None if lo_act is None or sp_act is None else round(lo_act - sp_act, 2), "high_rel": None if hi_act is None or sp_act is None else round(hi_act - sp_act, 2)},
        "spectral_tilt_db": None if lo_act is None or hi_act is None else round(hi_act - lo_act, 2),
        "speech_band_snr_db": None if sp_act is None or sp_noise is None else round(sp_act - sp_noise, 2),
        "env_t": [round(float(x), 2) for x in t],
        "env_db": [round(float(x), 1) for x in lev],
        "env_sp_db": [round(float(x), 1) for x in lev_sp],
    }


def envelope_on_master(an: dict[str, Any], sync_result: Any, cam: str, grid: np.ndarray) -> np.ndarray:
    """Resample a mic's 10 Hz level envelope onto the master-clock grid (drift/segment aware)."""
    t = np.asarray(an.get("env_t") or [], dtype=float)
    v = np.asarray(an.get("env_db") or [], dtype=float)
    if t.size < 2:
        return np.full(grid.size, -120.0)
    src = np.array([sync_result.source_time(cam, float(g)) for g in grid]) if sync_result is not None else grid
    out = np.interp(src, t, v, left=-120.0, right=-120.0)
    return out


def activity_and_bleed(env: dict[str, np.ndarray], floors: dict[str, float], speaker_activity: dict[str, np.ndarray] | None = None) -> dict[str, Any]:
    """Speaker activity per mic (speaker timeline when available, else energy dominance) and bleed matrix.

    ``bleed_db[j][i]`` = median level of mic ``j`` relative to mic ``i`` while only mic ``i``'s
    speaker talks (how loud speaker i leaks into mic j).
    """
    mics = sorted(env)
    if not mics:
        return {"active": {}, "bleed_db": {}, "method": "none"}
    stack = np.stack([env[m] for m in mics])
    if speaker_activity:
        act = {m: speaker_activity.get(m, np.zeros(stack.shape[1], dtype=bool)) for m in mics}
        method = "speaker_timeline"
    else:
        act = {}
        for i, m in enumerate(mics):
            others = np.delete(stack, i, axis=0)
            dom = stack[i] >= (others.max(axis=0) + 3.0 if others.size else stack[i])
            act[m] = dom & (stack[i] > floors.get(m, -70.0) + 10.0)
        method = "energy_dominance"
    bleed: dict[str, dict[str, float | None]] = {}
    for j, mj in enumerate(mics):
        bleed[mj] = {}
        for i, mi in enumerate(mics):
            if i == j:
                continue
            solo = act[mi] & ~np.any([act[m] for m in mics if m != mi], axis=0)
            if solo.sum() < 10:
                bleed[mj][mi] = None
                continue
            bleed[mj][mi] = round(float(np.median(stack[j][solo] - stack[i][solo])), 2)
    return {"active": act, "bleed_db": bleed, "method": method}


def declip_derivative(path: str, out_path: Path, *, sample_rate: int, max_sec: float, duration: float, timeout: float = 1800.0) -> dict[str, Any]:
    """Write a declipped copy (FFmpeg ``adeclip``) as 32-bit float WAV; never touches the source."""
    if duration > max_sec:
        return {"created": False, "reason": f"file longer than audio_rules.declip_max_sec ({max_sec:.0f}s)"}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", path, "-map", "0:a:0", "-af", "adeclip", "-c:a", "pcm_f32le", "-ar", str(sample_rate), str(out_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"created": False, "reason": f"adeclip failed: {exc}"}
    if proc.returncode != 0 or not out_path.is_file():
        return {"created": False, "reason": "adeclip failed: " + proc.stderr.decode("utf-8", "replace")[-200:]}
    return {"created": True, "path": str(out_path), "filter": "adeclip (FFmpeg, LGPL)"}


def _rules(st: Any) -> dict[str, Any]:
    r = dict(st.profile.get("audio_rules", {}) or {})

    def f(k: str, d: float, lo: float, hi: float) -> float:
        try:
            return float(min(hi, max(lo, float(r.get(k, d)))))
        except (TypeError, ValueError):
            return d

    return {
        "max_mic_trim_db": f("max_mic_trim_db", 24.0, 0.0, 24.0),
        "enable_mic_level_matching": bool(r.get("enable_mic_level_matching", True)),
        "tone_match": bool(r.get("tone_match", True)),
        "tone_match_max_db": f("tone_match_max_db", 4.0, 0.0, 8.0),
        "compression_ratio": f("compression_ratio", 3.0, 1.0, 6.0),
        "dense_speech_crest_db": f("dense_speech_crest_db", 12.0, 6.0, 20.0),
        "bleed_reduction_db": f("bleed_reduction_db", 6.0, 0.0, 15.0),
        "bleed_threshold_db": f("bleed_threshold_db", -20.0, -40.0, -3.0),
        "declip": bool(r.get("declip", True)),
        "declip_min_ratio": f("declip_min_ratio", 0.0001, 0.0, 0.1),
        "declip_max_sec": f("declip_max_sec", 1800.0, 0.0, 14400.0),
        "min_speech_snr_db": f("min_speech_snr_db", 15.0, 0.0, 40.0),
        "target_speech_dbfs": f("target_speech_dbfs", -20.0, -40.0, -10.0),
    }


def _mic_of_shot(st: Any, shot: Any) -> str:
    for cid, a in st.media_assets.items():
        if a.media_ref == shot.audio_media_ref:
            return cid
    return shot.camera_id


def plan_audio(st: Any, cache: Any, token: Any = None, progress: Any = None) -> dict[str, Any]:
    """Measure every mic (cached), then plan matching, dynamics, bleed handling and declip."""
    import time

    rules = _rules(st)
    t0 = time.perf_counter()
    mics = [c for c, a in st.media_assets.items() if a.audio_codec]
    used = sorted({_mic_of_shot(st, s) for s in st.rough_cut_shots}) or mics
    analyses: dict[str, dict[str, Any]] = {}
    hits: list[str] = []
    for n, cam in enumerate(mics):
        if token is not None:
            token.check(f"audio analysis {cam}")
        if progress:
            progress(0.05 + 0.5 * n / max(1, len(mics)), f"Measuring microphone {cam}")
        a = st.media_assets[cam]
        key = {"sha": a.sha256, "sr": a.audio_sample_rate, "ch": a.audio_channels}
        an = cache.get("mic_analysis", AUDIO_VERSION, key) if cache is not None else None
        if an:
            hits.append(f"mic_analysis:{cam}")
        else:
            an = analyse_mic(a.file_path, sample_rate=a.audio_sample_rate, channels=a.audio_channels, duration=a.duration_sec, codec=a.audio_codec)
            if cache is not None:
                cache.put("mic_analysis", AUDIO_VERSION, key, an)
        analyses[cam] = an
    sr_obj = st.sync_result
    end = max([s.master_sync_out_sec for s in st.rough_cut_shots] + [a.duration_sec for a in st.media_assets.values()] + [1.0])
    grid = np.arange(0.0, end, 1.0 / ENV_HZ)
    env = {c: envelope_on_master(an, sr_obj, c, grid) for c, an in analyses.items() if an.get("measured")}
    floors = {c: float(an.get("noise_floor_dbfs", -70.0)) for c, an in analyses.items() if an.get("measured")}
    spk_act: dict[str, np.ndarray] | None = None
    if st.transcript_segments:
        spk_act = {c: np.zeros(grid.size, dtype=bool) for c in env}
        for seg in st.transcript_segments:
            if seg.camera_id in spk_act and not seg.is_filler_or_silence:
                i0, i1 = int(seg.start_sec * ENV_HZ), int(np.ceil(seg.end_sec * ENV_HZ))
                spk_act[seg.camera_id][max(0, i0) : max(0, i1)] = True
        if not any(v.any() for v in spk_act.values()):
            spk_act = None
    ab = activity_and_bleed(env, floors, spk_act)
    act = ab["active"]
    # Speech level per mic: power mean of 100 ms frames where that mic's own speaker talks, excluding
    # frames > 10 dB above the median (a shout or laugh must not set the trim for the whole programme).
    level: dict[str, float] = {}
    for c in env:
        sel = act.get(c, np.zeros(grid.size, dtype=bool)) if act else np.zeros(grid.size, dtype=bool)
        if sel.sum() >= 10:
            v = env[c][sel]
            v = v[v <= np.median(v) + 10.0]
            level[c] = round(float(10 * np.log10(np.mean(10 ** (v / 10)) + 1e-12)), 2)
        else:
            level[c] = float(analyses[c].get("speech_rms_dbfs", -30.0))
    used_m = [c for c in used if c in level]
    target = float(np.median([level[c] for c in used_m])) if used_m else rules["target_speech_dbfs"]
    trims: dict[str, float] = {}
    for c in used_m:
        want = target - level[c]
        trims[c] = round(float(np.clip(want, -rules["max_mic_trim_db"], rules["max_mic_trim_db"])) if rules["enable_mic_level_matching"] else 0.0, 2)
    eq: dict[str, dict[str, float]] = {}
    if rules["tone_match"] and len(used_m) >= 2:
        lows = {c: analyses[c]["bands_db"].get("low_rel") for c in used_m}
        highs = {c: analyses[c]["bands_db"].get("high_rel") for c in used_m}
        ml = float(np.median([v for v in lows.values() if v is not None])) if any(v is not None for v in lows.values()) else None
        mh = float(np.median([v for v in highs.values() if v is not None])) if any(v is not None for v in highs.values()) else None
        cap = rules["tone_match_max_db"]
        for c in used_m:
            g_lo = 0.0 if ml is None or lows[c] is None else float(np.clip(ml - lows[c], -cap, cap))
            g_hi = 0.0 if mh is None or highs[c] is None else float(np.clip(mh - highs[c], -cap, cap))
            eq[c] = {"bass_db": round(g_lo, 2) if abs(g_lo) >= 1.5 else 0.0, "highshelf_db": round(g_hi, 2) if abs(g_hi) >= 1.5 else 0.0}
    crest = float(np.median([analyses[c].get("crest_db", 18.0) for c in used_m])) if used_m else 18.0
    ratio = rules["compression_ratio"] if crest >= rules["dense_speech_crest_db"] else min(rules["compression_ratio"], 2.0)
    thr = float(np.clip(target + 6.0, -30.0, -10.0))
    pred_crest = crest - (1.0 - 1.0 / ratio) * max(0.0, (target + crest) - thr)
    # Crosstalk / bleed per shot (speaker timeline when available, otherwise energy dominance)
    shots: dict[str, Any] = {}
    for s in st.rough_cut_shots:
        m = _mic_of_shot(st, s)
        i0, i1 = int(s.master_sync_in_sec * ENV_HZ), max(int(s.master_sync_in_sec * ENV_HZ) + 1, int(s.master_sync_out_sec * ENV_HZ))
        n_fr = max(1, i1 - i0)
        own = act.get(m, np.zeros(grid.size, dtype=bool))[i0:i1] if act else np.zeros(n_fr, dtype=bool)
        others = [c for c in act if c != m] if act else []
        off = np.zeros(n_fr, dtype=bool)
        for c in others:
            off |= act[c][i0:i1]
        off &= ~own
        dom_counts = {c: int(act[c][i0:i1].sum()) for c in act} if act else {}
        dominant = max(dom_counts, key=dom_counts.get) if dom_counts and max(dom_counts.values()) > 0 else m
        bleed_in = [v for k, v in (ab["bleed_db"].get(m) or {}).items() if v is not None]
        strong_bleed = max(bleed_in) if bleed_in else None
        expander = None
        if rules["bleed_reduction_db"] > 0 and strong_bleed is not None and strong_bleed > rules["bleed_threshold_db"] and off.mean() < 0.05:
            lvl = level.get(m, target) + trims.get(m, 0.0)
            t_exp = round(float(np.clip(lvl + strong_bleed + 4.0, -70.0, -20.0)), 1)
            expander = {"threshold_dbfs": t_exp, "reduction_db": rules["bleed_reduction_db"]}
        shots[s.shot_id] = {"mic": m, "off_mic_speech_share": round(float(off.mean()), 4), "own_speech_share": round(float(own.mean()), 4), "dominant_mic": dominant, "mic_is_dominant": dominant == m, "bleed_into_mic_db": strong_bleed, "bleed_expander": expander, "master_in_sec": round(s.master_sync_in_sec, 3), "master_out_sec": round(s.master_sync_out_sec, 3)}
    # Declip derivatives (job output, never the source), only where clipping is measured
    declip: dict[str, Any] = {}
    for c in used_m:
        cl = analyses[c].get("clipping", {})
        if cl.get("clipped_share_of_speech", 0.0) >= max(1e-9, rules["declip_min_ratio"]):
            entry = {"clipped_share_of_speech": cl.get("clipped_share_of_speech"), "created": False}
            if rules["declip"]:
                a = st.media_assets[c]
                outp = Path(st.output_dir) / "restored_audio" / f"{c}_declipped.wav"
                res = declip_derivative(a.file_path, outp, sample_rate=int(min(48000, a.audio_sample_rate or 48000)), max_sec=rules["declip_max_sec"], duration=a.duration_sec)
                entry.update(res)
                if res.get("created"):
                    after = analyse_mic(str(outp), sample_rate=int(min(48000, a.audio_sample_rate or 48000)), channels=a.audio_channels, duration=a.duration_sec, codec="pcm_f32le")
                    entry["residual_clipped_share_of_speech"] = (after.get("clipping") or {}).get("clipped_share_of_speech")
                    entry["peak_dbfs_after"] = after.get("peak_dbfs")
            else:
                entry["reason"] = "audio_rules.declip is false"
            declip[c] = entry
    after_levels = {c: level[c] + trims.get(c, 0.0) for c in used_m}
    spread_before = (max(level[c] for c in used_m) - min(level[c] for c in used_m)) if len(used_m) >= 2 else 0.0
    spread_after = (max(after_levels.values()) - min(after_levels.values())) if len(after_levels) >= 2 else 0.0
    mic_summary = {c: {k: v for k, v in an.items() if k not in ("env_t", "env_db", "env_sp_db")} for c, an in analyses.items()}
    for c in mic_summary:
        mic_summary[c]["speech_level_dbfs"] = level.get(c)
    plan = {
        "version": AUDIO_VERSION,
        "mics": mic_summary,
        "used_mics": used_m,
        "activity_method": ab["method"],
        "bleed_db": ab["bleed_db"],
        "matching": {"target_speech_dbfs": round(target, 2), "trims_db": trims, "eq": eq, "level_spread_before_db": round(spread_before, 2), "level_spread_after_db": round(spread_after, 2)},
        "compression": {"ratio": ratio, "threshold_dbfs": round(thr, 1), "speech_crest_db": round(crest, 2), "predicted_crest_after_db": round(pred_crest, 2)},
        "shots": shots,
        "declip": declip,
        "elapsed_sec": round(time.perf_counter() - t0, 3),
        "cache_hits": hits,
    }
    return plan


def compand_points_expander(threshold_dbfs: float, reduction_db: float) -> str:
    t = float(threshold_dbfs)
    r = float(reduction_db)
    return f"-90/{-90 - r:.1f}|{t - 1:.1f}/{t - 1 - r:.1f}|{t:.1f}/{t:.1f}|0/0"


def compand_points_compressor(threshold_dbfs: float, ratio: float) -> str:
    t = float(threshold_dbfs)
    return f"-80/-80|{t:.1f}/{t:.1f}|0/{t + (0.0 - t) / max(1.0, ratio):.1f}"


__all__ = ["AUDIO_VERSION", "ENV_HZ", "activity_and_bleed", "analyse_mic", "compand_points_compressor", "compand_points_expander", "declip_derivative", "envelope_on_master", "plan_audio"]
