"""Long-form multicamera synchronisation: multi-window GCC-PHAT, clock drift and interrupted recordings.

V1.2.1 measured one GCC-PHAT offset per source from a single bounded window at the start of the
recording. Over a long programme, independent device clocks drift apart (tens to hundreds of ppm:
100 ppm is 360 ms per hour), and a camera that stopped and restarted inside one file shifts every
later frame. This module measures alignment *across the whole recording* in bounded memory:

1. **Stream once per file** (16 kHz mono through ``ffmpeg``; O(chunk) memory). While streaming we
   keep a 100 Hz log-energy envelope of the speech band plus the short raw-PCM windows we need.
2. **Coarse per-window alignment** of the reference envelope against the *whole* target envelope
   (normalised cross-correlation), so large offsets and restarts are re-acquired without a prior.
3. **Fine per-window alignment** with GCC-PHAT (sub-sample peak) inside a narrow lag range around
   the coarse estimate, on raw PCM windows collected in a second streaming pass of the target.
4. **Robust joint regression** ``offset_i = a_segment + drift * t_i``: one clock-drift slope per
   device shared by every segment, per-segment intercepts, Huber IRLS with outlier rejection and
   discontinuity (segment) detection; breakpoints are refined on the envelopes.

Convention (``SyncResult``): ``source = master + offset + drift_ppm * 1e-6 * master`` with ``offset``
the intercept at master t=0; ``segment_offsets`` overrides it piecewise.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from scipy.fft import irfft, next_fast_len, rfft
from scipy.signal import butter, sosfilt, sosfilt_zi

SYNC_ANALYSIS_VERSION = "1.2.2-sync-3"
MAX_PLAUSIBLE_DRIFT_PPM = 1000.0  # consumer recorders are typically < 100 ppm; reject nonsense fits


@dataclass(frozen=True)
class SyncParams:
    """Tunable multi-window sync parameters (read from ``audio_rules.sync_*`` with safe defaults)."""

    sample_rate: int = 16000
    env_rate_hz: float = 100.0
    fine_window_sec: float = 6.0
    fine_search_sec: float = 0.35
    coarse_window_sec: float = 30.0
    target_windows: int = 32
    min_spacing_sec: float = 15.0
    max_windows: int = 96
    min_window_confidence: float = 0.35
    min_coarse_ncc: float = 0.30
    jump_tol_sec: float = 0.012
    max_drift_ppm: float = MAX_PLAUSIBLE_DRIFT_PPM

    @classmethod
    def from_profile(cls, profile: dict[str, Any], sample_rate: int = 16000) -> SyncParams:
        rules = (profile or {}).get("audio_rules", {}) or {}

        def _f(key: str, default: float, lo: float, hi: float) -> float:
            try:
                return float(min(hi, max(lo, float(rules.get(key, default)))))
            except (TypeError, ValueError):
                return default

        return cls(
            sample_rate=int(sample_rate),
            fine_window_sec=_f("sync_fine_window_sec", 6.0, 2.0, 20.0),
            target_windows=int(_f("sync_windows", 32, 6, 96)),
            min_spacing_sec=_f("sync_min_spacing_sec", 15.0, 4.0, 600.0),
            min_window_confidence=_f("sync_min_window_confidence", 0.35, 0.05, 0.95),
        )

    def key(self) -> dict[str, Any]:
        return asdict(self)

    def for_duration(self, ref_duration: float) -> SyncParams:
        """Shrink the analysis windows for short recordings (a 14 s clip still gets two windows)."""
        from dataclasses import replace

        cw = min(self.coarse_window_sec, max(4.0, 0.5 * ref_duration))
        fw = min(self.fine_window_sec, max(2.0, 0.4 * ref_duration))
        return replace(self, coarse_window_sec=round(cw, 3), fine_window_sec=round(fw, 3))


@dataclass
class WindowMeasurement:
    master_sec: float
    offset_sec: float
    confidence: float
    snr: float
    coarse_offset_sec: float
    coarse_ncc: float
    status: str = "ok"  # ok | low_confidence | no_overlap | outlier
    residual_ms: float | None = None
    segment: int | None = None


@dataclass
class DriftFit:
    camera_id: str
    offset_sec: float
    drift_ppm: float
    drift_ppm_stderr: float
    segments: list[dict[str, float]] = field(default_factory=list)
    gaps: list[dict[str, float]] = field(default_factory=list)
    residual_rms_ms: float = 0.0
    residual_max_ms: float = 0.0
    n_windows: int = 0
    n_inliers: int = 0
    n_rejected: int = 0
    confidence: float = 0.0
    mean_window_confidence: float = 0.0
    method: str = "multiwindow_gcc_phat_drift"
    review: list[str] = field(default_factory=list)
    windows: list[dict[str, Any]] = field(default_factory=list)
    span_sec: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Streaming analysis (one ffmpeg decode per pass, bounded memory)
# ---------------------------------------------------------------------------
def _band_sos(sr: int) -> np.ndarray:
    nyq = 0.5 * sr
    return butter(4, [150.0 / nyq, min(0.95, 4000.0 / nyq)], btype="bandpass", output="sos")


class _Collector:
    """Accumulates the speech-band envelope and requested raw windows from sequential PCM chunks."""

    def __init__(self, sr: int, env_rate: float, windows: list[tuple[float, float]] | None, want_env: bool) -> None:
        self.sr = sr
        self.hop = max(1, round(sr / env_rate))
        self.want_env = want_env
        self.sos = _band_sos(sr)
        self.zi: np.ndarray | None = None
        self.env_parts: list[np.ndarray] = []
        self.carry = np.zeros(0, dtype=np.float64)
        self.windows = [(round(max(0.0, a) * sr), round(max(0.0, b) * sr)) for a, b in (windows or [])]
        self.win_parts: list[list[np.ndarray]] = [[] for _ in self.windows]
        self.pos = 0

    def feed(self, chunk: np.ndarray) -> None:
        n = int(chunk.size)
        if n == 0:
            return
        start, end = self.pos, self.pos + n
        for wi, (a, b) in enumerate(self.windows):
            lo, hi = max(a, start), min(b, end)
            if hi > lo:
                self.win_parts[wi].append(chunk[lo - start : hi - start].astype(np.float32, copy=True))
        self.pos = end
        if not self.want_env:
            return
        x = chunk.astype(np.float64)
        if self.zi is None:
            self.zi = sosfilt_zi(self.sos) * (x[0] if x.size else 0.0)
        y, self.zi = sosfilt(self.sos, x, zi=self.zi)
        buf = np.concatenate((self.carry, y))
        k = buf.size // self.hop
        if k:
            frames = buf[: k * self.hop].reshape(k, self.hop)
            self.env_parts.append(np.log10(np.mean(frames * frames, axis=1) + 1e-10).astype(np.float32))
        self.carry = buf[k * self.hop :]

    def result(self) -> tuple[np.ndarray, list[np.ndarray]]:
        env = np.concatenate(self.env_parts) if self.env_parts else np.zeros(0, dtype=np.float32)
        wins = [np.concatenate(p) if p else np.zeros(0, dtype=np.float32) for p in self.win_parts]
        return env, wins


def stream_envelope_and_windows(
    source: str | Path | np.ndarray,
    *,
    sample_rate: int,
    env_rate_hz: float,
    windows: list[tuple[float, float]] | None = None,
    want_env: bool = True,
) -> tuple[np.ndarray, list[np.ndarray]]:
    """One sequential pass over a file (or an in-memory PCM array): envelope + raw windows."""
    col = _Collector(sample_rate, env_rate_hz, windows, want_env)
    if isinstance(source, np.ndarray):
        step = sample_rate * 30
        for i in range(0, source.size, step):
            col.feed(source[i : i + step])
    else:
        from bangonpro.ai_engine.agents.ingest_sync import iter_mono_pcm_chunks

        for _, chunk in iter_mono_pcm_chunks(source, sample_rate=sample_rate, chunk_duration_sec=30.0):
            col.feed(chunk)
    return col.result()


def _modulation(env: np.ndarray, env_rate: float) -> np.ndarray:
    """Log-energy minus its 1 s moving average: syllabic modulation, robust to mic gain and room tone."""
    if env.size == 0:
        return env.astype(np.float64)
    k = max(3, round(env_rate))
    kern = np.ones(k) / k
    base = np.convolve(env.astype(np.float64), kern, mode="same")
    return env.astype(np.float64) - base


def _ncc_all_lags(q: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Normalised cross-correlation of template ``q`` against every full-overlap position in ``y``."""
    m, n = q.size, y.size
    if m < 8 or n < m:
        return np.zeros(0)
    qz = q - q.mean()
    qn = float(np.sqrt(np.sum(qz * qz)))
    if qn < 1e-9:
        return np.zeros(n - m + 1)
    nfft = int(next_fast_len(n + m - 1))
    corr = irfft(rfft(y, nfft) * np.conj(rfft(qz, nfft)), nfft)[: n - m + 1]
    c1 = np.concatenate(([0.0], np.cumsum(y)))
    c2 = np.concatenate(([0.0], np.cumsum(y * y)))
    s1 = c1[m:] - c1[:-m]
    s2 = c2[m:] - c2[:-m]
    var = np.maximum(s2 - s1 * s1 / m, 1e-12)
    return corr / (qn * np.sqrt(var))


def _parabolic(vals: np.ndarray, i: int) -> float:
    if 0 < i < vals.size - 1:
        y0, y1, y2 = float(vals[i - 1]), float(vals[i]), float(vals[i + 1])
        d = y0 - 2.0 * y1 + y2
        if abs(d) > 1e-12:
            return float(np.clip(0.5 * (y0 - y2) / d, -0.5, 0.5))
    return 0.0


def gcc_phat_in_range(ref: np.ndarray, tgt: np.ndarray, sr: int, lag_lo: int, lag_hi: int) -> tuple[float, float, float]:
    """GCC-PHAT lag (samples, sub-sample) with ``tgt[n + lag] ~= ref[n]`` searched in [lag_lo, lag_hi].

    Returns ``(lag_samples, confidence, peak_snr)``.
    """
    from bangonpro.ai_engine.agents.ingest_sync import _bandpass_speech

    if ref.size < 256 or tgt.size < 256 or lag_hi <= lag_lo:
        return 0.0, 0.0, 0.0
    x = _bandpass_speech(ref, sr)
    y = _bandpass_speech(tgt, sr)
    if float(np.std(x)) < 1e-7 or float(np.std(y)) < 1e-7:
        return 0.0, 0.0, 0.0
    nfft = int(next_fast_len(x.size + y.size - 1))
    cross = rfft(y, nfft) * np.conj(rfft(x, nfft))
    mag = np.abs(cross)
    cc = irfft(cross / (mag + max(1e-10, 1e-4 * float(mag.max()))), nfft)
    lags = np.arange(int(lag_lo), int(lag_hi) + 1)
    vals = cc[np.mod(lags, nfft)]
    i = int(np.argmax(vals))
    peak = float(vals[i])
    guard = max(4, round(0.02 * sr))
    mask = np.ones(vals.size, dtype=bool)
    mask[max(0, i - guard) : i + guard + 1] = False
    side = vals[mask]
    snr = (peak - float(side.mean())) / (float(side.std()) + 1e-12) if side.size > 8 else 0.0
    conf = float(np.clip(1.0 - np.exp(-max(0.0, snr - 3.0) / 10.0), 0.0, 0.999))
    return float(lags[i]) + _parabolic(vals, i), conf, float(snr)


# ---------------------------------------------------------------------------
# Window planning and measurement
# ---------------------------------------------------------------------------
def plan_window_centres(ref_duration: float, p: SyncParams) -> list[float]:
    half = max(p.fine_window_sec, p.coarse_window_sec) / 2.0
    lo, hi = half, ref_duration - half
    if hi <= lo:
        return [ref_duration / 2.0] if ref_duration > p.fine_window_sec + 1.0 else []
    span = hi - lo
    n = int(min(p.max_windows, max(2, min(p.target_windows, math.floor(span / p.min_spacing_sec) + 1))))
    return [float(x) for x in np.linspace(lo, hi, n)]


def measure_windows(
    ref_env: np.ndarray,
    ref_windows: list[np.ndarray],
    centres: list[float],
    target_source: str | Path | np.ndarray,
    target_duration: float,
    p: SyncParams,
    *,
    target_env: np.ndarray | None = None,
) -> tuple[list[WindowMeasurement], np.ndarray]:
    """Coarse envelope alignment against the whole target, then fine GCC-PHAT per window."""
    er = p.env_rate_hz
    sr = p.sample_rate
    if target_env is None:
        target_env, _ = stream_envelope_and_windows(target_source, sample_rate=sr, env_rate_hz=er)
    mr = _modulation(ref_env, er)
    mt = _modulation(target_env, er)
    cw = round(p.coarse_window_sec * er)
    coarse: list[tuple[float, float]] = []
    def _coarse(t: float, width: int) -> tuple[float, float]:
        a = max(0, round(t * er) - width // 2)
        ncc = _ncc_all_lags(mr[a : a + width], mt)
        if ncc.size == 0:
            return float("nan"), 0.0
        k = int(np.argmax(ncc))
        return (k + _parabolic(ncc, k)) / er - a / er, float(ncc[k])

    for t in centres:
        c, v = _coarse(t, cw)
        if v < p.min_coarse_ncc + 0.2:
            # The long template may overhang the end of the target: retry with a third of it
            c2, v2 = _coarse(t, max(round(4 * er), cw // 3))
            if v2 > v + 0.1 or not math.isfinite(c):
                c, v = c2, v2
        coarse.append((c, v))
    # Fine pass: one more streaming decode of the target collects only the windows we need
    half_w = p.fine_window_sec / 2.0
    s = p.fine_search_sec
    req: list[tuple[float, float]] = []
    for t, (c, _) in zip(centres, coarse, strict=True):
        if not math.isfinite(c):
            req.append((0.0, 0.0))
            continue
        req.append((t + c - half_w - s, t + c + half_w + s))
    _, tgt_wins = stream_envelope_and_windows(target_source, sample_rate=sr, env_rate_hz=er, windows=req, want_env=False)
    out: list[WindowMeasurement] = []
    for t, (c, ncc_v), (a, b), rw, tw in zip(centres, coarse, req, ref_windows, tgt_wins, strict=True):
        if not math.isfinite(c) or b <= 0.0 or a < -s or b > target_duration + s:
            out.append(WindowMeasurement(t, float("nan"), 0.0, 0.0, c, ncc_v, status="no_overlap"))
            continue
        start = max(0.0, a)
        # tgt[n + lag] ~= ref[n] with lag = (offset - (tgt_start - ref_start)) * sr; centre on the coarse offset
        lead = round((start - (t - half_w)) * sr)
        centre = round(c * sr) - lead
        lag, conf, snr = gcc_phat_in_range(rw, tw, sr, centre - round(s * sr), centre + round(s * sr))
        out.append(WindowMeasurement(t, (lag + lead) / sr, conf, snr, c, ncc_v, status="ok"))
    return classify_windows(out, p), target_env


def classify_windows(meas: list[WindowMeasurement], p: SyncParams) -> list[WindowMeasurement]:
    """Apply the (cheap, tunable) acceptance thresholds to raw window measurements."""
    for m in meas:
        if m.status == "no_overlap" or not math.isfinite(m.offset_sec):
            m.status = "no_overlap"
        elif m.confidence < p.min_window_confidence or m.coarse_ncc < p.min_coarse_ncc:
            m.status = "low_confidence"
        else:
            m.status = "ok"
        m.residual_ms, m.segment = None, None
    return meas


# ---------------------------------------------------------------------------
# Robust joint regression with discontinuity detection
# ---------------------------------------------------------------------------
def _joint_fit(t: np.ndarray, o: np.ndarray, w: np.ndarray, seg: np.ndarray, iters: int = 8) -> tuple[np.ndarray, float, np.ndarray, float]:
    """Weighted Huber-IRLS fit of ``o = a[seg] + b t``; returns (intercepts, slope, residuals, slope_stderr)."""
    k = int(seg.max()) + 1
    hub = np.ones_like(w)
    a = np.zeros(k)
    b = 0.0
    cov_b = 0.0
    for _ in range(iters):
        ww = w * hub
        x = np.zeros((t.size, k + 1))
        x[np.arange(t.size), seg] = 1.0
        x[:, k] = t
        sw = np.sqrt(ww)
        try:
            beta, *_ = np.linalg.lstsq(x * sw[:, None], o * sw, rcond=None)
        except np.linalg.LinAlgError:
            break
        a, b = beta[:k], float(beta[k])
        r = o - (a[seg] + b * t)
        mad = float(np.median(np.abs(r - np.median(r)))) * 1.4826
        c = max(1.345 * max(mad, 5e-5), 1e-4)
        hub = np.where(np.abs(r) <= c, 1.0, c / np.maximum(np.abs(r), 1e-12))
    r = o - (a[seg] + b * t)
    dof = max(1, t.size - (k + 1))
    sigma2 = float(np.sum(w * hub * r * r) / max(1e-12, np.sum(w * hub))) * t.size / dof
    try:
        xtx = (x * (w * hub)[:, None]).T @ x
        cov = np.linalg.pinv(xtx) * sigma2 * float(np.sum(w * hub)) / t.size
        cov_b = float(max(0.0, cov[k, k]))
    except (np.linalg.LinAlgError, UnboundLocalError):
        cov_b = 0.0
    return a, b, r, math.sqrt(cov_b)


def _segment_labels(t: np.ndarray, o: np.ndarray, slope: float, tol: float, ppm_allow: float) -> np.ndarray:
    """Split time-ordered windows wherever the drift-corrected offset jumps beyond tolerance."""
    lab = np.zeros(t.size, dtype=int)
    for i in range(1, t.size):
        allow = tol + ppm_allow * 1e-6 * (t[i] - t[i - 1])
        jumped = abs((o[i] - slope * t[i]) - (o[i - 1] - slope * t[i - 1])) > allow
        lab[i] = lab[i - 1] + (1 if jumped else 0)
    return lab


def _plain_fit(t: np.ndarray, o: np.ndarray, w: np.ndarray, lab: np.ndarray, p: SyncParams) -> tuple[np.ndarray, float, np.ndarray, float]:
    a, b, r, b_se = _joint_fit(t, o, w, lab)
    if abs(b) * 1e6 > p.max_drift_ppm or t.size < 3 or (t.max() - t.min()) < 30.0:
        # Too short or implausible to separate drift from noise: one offset per segment, no drift
        a = np.array([float(np.median(o[lab == s])) for s in range(int(lab.max()) + 1)])
        return a, 0.0, o - a[lab], 0.0
    return a, b, r, b_se


def fit_drift_and_segments(windows: list[WindowMeasurement], p: SyncParams) -> tuple[np.ndarray, float, np.ndarray, float, np.ndarray]:
    """Return (intercepts, slope, labels, slope_stderr, inlier index) after outlier rejection.

    A segment must be supported by >= 2 consistent windows; a lone deviating window between (or
    beside) consistent segments is an outlier, not a restart. Iterates segmentation <-> joint fit
    (one drift slope per device clock, one intercept per segment) until the slope converges.
    """
    good = [i for i, m in enumerate(windows) if m.status == "ok" and math.isfinite(m.offset_sec)]
    if not good:
        return np.zeros(0), 0.0, np.zeros(0, dtype=int), 0.0, np.zeros(0, dtype=int)
    idx = np.array(good)
    t = np.array([windows[i].master_sec for i in good])
    o = np.array([windows[i].offset_sec for i in good])
    w = np.array([max(0.05, windows[i].confidence) for i in good])
    keep = np.ones(t.size, dtype=bool)
    slope, slope_known = 0.0, False
    tol = p.jump_tol_sec
    for _ in range(8):
        tk, ok_, wk = t[keep], o[keep], w[keep]
        kidx = np.where(keep)[0]
        lab = _segment_labels(tk, ok_, slope, tol, 25.0 if slope_known else 0.25 * p.max_drift_ppm)
        counts = np.bincount(lab)
        lone = counts[lab] < 2
        if tk.size >= 3 and lone.any() and (~lone).sum() >= 2:
            keep[kidx[lone]] = False
            continue
        _, lab = np.unique(lab, return_inverse=True)
        a, b, r, b_se = _plain_fit(tk, ok_, wk, lab, p)
        mad = float(np.median(np.abs(r - np.median(r)))) * 1.4826
        bad = np.abs(r) > max(0.002, 4.0 * mad)
        if bad.any() and (~bad).sum() >= 2:
            keep[kidx[bad]] = False
            slope, slope_known = b, True
            continue
        if slope_known and abs(b - slope) < 1e-8:
            return a, b, lab, b_se, idx[keep]
        slope, slope_known = b, True
    tk, ok_, wk = t[keep], o[keep], w[keep]
    lab = _segment_labels(tk, ok_, slope, tol, 25.0)
    _, lab = np.unique(lab, return_inverse=True)
    a, b, _, b_se = _plain_fit(tk, ok_, wk, lab, p)
    return a, b, lab, b_se, idx[keep]


def refine_breakpoint(
    ref_env: np.ndarray,
    tgt_env: np.ndarray,
    env_rate: float,
    t_lo: float,
    t_hi: float,
    model_a: tuple[float, float],
    model_b: tuple[float, float],
    *,
    win_sec: float = 1.0,
    step_sec: float = 0.05,
) -> tuple[float, float]:
    """Locate where model A stops matching and model B starts (master seconds) between two windows."""
    mr = _modulation(ref_env, env_rate)
    mt = _modulation(tgt_env, env_rate)
    win = max(4, round(win_sec * env_rate))
    step = max(1, round(step_sec * env_rate))
    ts = np.arange(max(0.0, t_lo), max(t_lo, t_hi), step / env_rate)
    if ts.size < 4:
        mid = 0.5 * (t_lo + t_hi)
        return mid, mid

    def _score(t0: float, model: tuple[float, float]) -> float:
        a, b = model
        i0 = round(t0 * env_rate)
        j0 = round((t0 + a + b * t0) * env_rate)
        if i0 < 0 or j0 < 0 or i0 + win > mr.size or j0 + win > mt.size:
            return 0.0
        x = mr[i0 : i0 + win]
        y = mt[j0 : j0 + win]
        xs, ys = x - x.mean(), y - y.mean()
        den = float(np.sqrt(np.sum(xs * xs) * np.sum(ys * ys)))
        return float(np.sum(xs * ys) / den) if den > 1e-12 else 0.0

    ca = np.array([_score(t0, model_a) for t0 in ts])
    cb = np.array([_score(t0, model_b) for t0 in ts])
    hi_a = float(np.median(ca[: max(2, ca.size // 6)]))
    hi_b = float(np.median(cb[-max(2, cb.size // 6) :]))
    lo_a = float(np.median(ca[-max(2, ca.size // 6) :]))
    lo_b = float(np.median(cb[: max(2, cb.size // 6)]))
    tau_a = 0.5 * (hi_a + lo_a)
    tau_b = 0.5 * (hi_b + lo_b)
    end_a = float(ts[int(np.argmax(np.cumsum(ca - tau_a)))]) + win / env_rate * 0.5
    sb = np.cumsum((cb - tau_b)[::-1])[::-1]
    start_b = float(ts[int(np.argmax(sb))]) + win / env_rate * 0.5
    end_a = float(np.clip(end_a, t_lo, t_hi))
    start_b = float(np.clip(start_b, end_a, t_hi))
    return end_a, start_b


def estimate_drift_for_target(
    camera_id: str,
    ref_env: np.ndarray,
    ref_windows: list[np.ndarray],
    centres: list[float],
    target_source: str | Path | np.ndarray,
    target_duration: float,
    p: SyncParams,
) -> tuple[DriftFit, np.ndarray]:
    """Full multi-window estimate for one target (offset intercept, drift ppm, segments, residuals)."""
    meas, tgt_env = measure_windows(ref_env, ref_windows, centres, target_source, target_duration, p)
    return fit_target(camera_id, meas, ref_env, tgt_env, p), tgt_env


def fit_target(camera_id: str, meas: list[WindowMeasurement], ref_env: np.ndarray, tgt_env: np.ndarray, p: SyncParams) -> DriftFit:
    """Cheap stage: thresholds, robust regression, segments and breakpoints from cached measurements."""
    meas = classify_windows(meas, p)
    a, b, lab, b_se, inl = fit_drift_and_segments(meas, p)
    fit = DriftFit(camera_id=camera_id, offset_sec=0.0, drift_ppm=0.0, drift_ppm_stderr=0.0, n_windows=len(meas))
    if inl.size == 0:
        fit.review.append(f"{camera_id}: no window aligned with confidence; sync could not be measured across the recording")
        fit.windows = [_wdict(m) for m in meas]
        return fit
    inl_set = {int(i): int(s) for i, s in zip(inl, lab, strict=True)}
    for i, m in enumerate(meas):
        if i in inl_set:
            m.segment = inl_set[i]
            m.residual_ms = round((m.offset_sec - (a[m.segment] + b * m.master_sec)) * 1000.0, 4)
        elif m.status == "ok":
            m.status = "outlier"
    res = np.array([meas[i].residual_ms for i in inl_set], dtype=float)
    fit.drift_ppm = round(b * 1e6, 3)
    fit.drift_ppm_stderr = round(b_se * 1e6, 3)
    fit.n_inliers = int(inl.size)
    fit.n_rejected = len(meas) - fit.n_inliers
    fit.residual_rms_ms = round(float(np.sqrt(np.mean(res * res))), 4) if res.size else 0.0
    fit.residual_max_ms = round(float(np.max(np.abs(res))), 4) if res.size else 0.0
    ts_inl = np.array([meas[i].master_sec for i in inl_set])
    fit.span_sec = round(float(ts_inl.max() - ts_inl.min()), 3) if ts_inl.size else 0.0
    fit.mean_window_confidence = round(float(np.mean([meas[i].confidence for i in inl_set])), 4)
    n_seg = int(lab.max()) + 1
    seg_rows: list[dict[str, float]] = []
    bounds: list[tuple[float, float]] = []
    for s in range(n_seg):
        ts = sorted(meas[i].master_sec for i, ss in inl_set.items() if ss == s)
        bounds.append((ts[0], ts[-1]))
    edges = [0.0]
    gaps: list[dict[str, float]] = []
    for s in range(n_seg - 1):
        end_a, start_b = refine_breakpoint(ref_env, tgt_env, p.env_rate_hz, bounds[s][1], bounds[s + 1][0], (a[s], b), (a[s + 1], b))
        # Second, finer pass (0.4 s windows, 10 ms steps) around the first estimate
        end_a, _ = refine_breakpoint(ref_env, tgt_env, p.env_rate_hz, max(bounds[s][1], end_a - 2.0), min(bounds[s + 1][0], end_a + 2.0), (a[s], b), (a[s + 1], b), win_sec=0.4, step_sec=0.01)
        jump = float(a[s + 1] - a[s])
        brk = end_a
        edges.append(brk)
        if jump < 0:
            # The device stopped recording: master content in [brk, brk + |jump|) never reached this file.
            # A break inside a pause cannot be localised more precisely than the pause, so the
            # unavailable span runs from the last confirmed match to the first confirmed restart.
            gaps.append({"master_start_sec": round(brk, 3), "master_end_sec": round(max(brk - jump, start_b), 3), "jump_sec": round(jump, 4), "kind": "missing_content", "restart_estimate_sec": round(start_b, 3)})
        else:
            gaps.append({"master_start_sec": round(brk, 3), "master_end_sec": round(brk, 3), "jump_sec": round(jump, 4), "kind": "inserted_content", "restart_estimate_sec": round(start_b, 3)})
    edges.append(float("inf"))
    for s in range(n_seg):
        row = {"master_start_sec": round(edges[s], 4), "offset_sec": round(float(a[s]), 6), "drift_ppm": fit.drift_ppm}
        if not math.isinf(edges[s + 1]):  # open-ended last segment: no key (strict JSON has no Infinity)
            row["master_end_sec"] = round(edges[s + 1], 4)
        seg_rows.append(row)
    fit.offset_sec = round(float(a[0]), 6)
    fit.segments = seg_rows if n_seg > 1 else []
    fit.gaps = gaps
    # Confidence: GCC quality x inlier share x residual quality (1 ms rms ~ -0.15)
    share = fit.n_inliers / max(1, fit.n_windows)
    res_q = math.exp(-fit.residual_rms_ms / 6.0)
    fit.confidence = round(float(np.clip(fit.mean_window_confidence * (0.5 + 0.5 * share) * (0.6 + 0.4 * res_q), 0.0, 0.999)), 4)
    if fit.n_inliers < 3 and fit.n_windows >= 3:  # short recordings: drift is irrelevant, not a review item
        fit.review.append(f"{camera_id}: only {fit.n_inliers} window(s) aligned; drift not measurable, single offset used")
    if share < 0.6:
        fit.review.append(f"{camera_id}: {fit.n_rejected} of {fit.n_windows} sync windows rejected (silence, noise or different content); verify alignment")
    if fit.residual_rms_ms > 4.0:
        fit.review.append(f"{camera_id}: sync residual {fit.residual_rms_ms:.1f} ms RMS is high; check for variable frame rate or edits inside the file")
    for g in gaps:
        if g["kind"] != "missing_content":
            fit.review.append(f"{camera_id}: clock jump {g['jump_sec'] * 1000:+.0f} ms at {g['master_start_sec']:.2f}s (extra content inside the file); re-synced per segment")
    fit.windows = [_wdict(m) for m in meas]
    return fit


def _wdict(m: WindowMeasurement) -> dict[str, Any]:
    d = asdict(m)
    d["master_sec"] = round(m.master_sec, 3)
    d["offset_ms"] = round(m.offset_sec * 1000.0, 4) if math.isfinite(m.offset_sec) else None
    d.pop("offset_sec", None)
    d["coarse_offset_ms"] = round(m.coarse_offset_sec * 1000.0, 2) if math.isfinite(m.coarse_offset_sec) else None
    d.pop("coarse_offset_sec", None)
    d["confidence"] = round(m.confidence, 4)
    d["snr"] = round(m.snr, 2)
    d["coarse_ncc"] = round(m.coarse_ncc, 4)
    return d


# ---------------------------------------------------------------------------
# Inverse mapping and state integration
# ---------------------------------------------------------------------------
def master_time(sync_result: Any, camera_id: str, source_sec: float) -> float | None:
    """Inverse of ``SyncResult.source_time``: the master-clock time of a source-file time."""
    if sync_result is None:
        return source_sec
    segs = (sync_result.segment_offsets or {}).get(camera_id) or []
    for seg in segs:
        k = 1.0 + float(seg.get("drift_ppm", 0.0)) * 1e-6
        m = (source_sec - float(seg.get("offset_sec", 0.0))) / k
        if float(seg.get("master_start_sec", 0.0)) - 1e-6 <= m < float(seg.get("master_end_sec", float("inf"))):
            return m
    if segs:
        return None
    k = 1.0 + float((sync_result.drift_ppm or {}).get(camera_id, 0.0)) * 1e-6
    return (source_sec - float(sync_result.offsets_sec.get(camera_id, 0.0))) / k


def refine_state_sync(st: Any, cache: Any = None, progress: Any = None, token: Any = None) -> dict[str, Any]:
    """Refine ``st.sync_result`` in place with multi-window drift/segment measurements.

    Writes ``st.metadata["sync_report"]`` and returns it. Results are cached per (reference, target)
    content hash and parameters, so re-runs and quality-loop attempts only recompute what changed.
    """
    sr_obj = st.sync_result
    if sr_obj is None or not st.intent.sync_cameras:
        return {}
    p = SyncParams.from_profile(st.profile, int(st.audio_sample_rate or 16000))
    ref_id = sr_obj.reference_camera_id
    ref = st.media_assets.get(ref_id)
    report: dict[str, Any] = {"version": SYNC_ANALYSIS_VERSION, "reference_camera_id": ref_id, "params": p.key(), "cameras": {}, "review": []}
    if ref is None or not ref.audio_codec:
        report["review"].append("Reference has no audio: drift could not be measured")
        st.metadata["sync_report"] = report
        return report
    t0 = time.perf_counter()
    p = p.for_duration(ref.duration_sec)
    report["params"] = p.key()
    centres = plan_window_centres(ref.duration_sec, p)
    half = p.fine_window_sec / 2.0
    ref_req = [(c - half, c + half) for c in centres]
    ref_env: np.ndarray | None = None
    ref_wins: list[np.ndarray] = []
    plan = plan_key(p)
    ref_env_key = {"sha": ref.sha256, "sr": p.sample_rate, "env": p.env_rate_hz}

    def _ref_env() -> np.ndarray:
        nonlocal ref_env
        if ref_env is None:
            hit = cache.get("sync_envelope", SYNC_ANALYSIS_VERSION, ref_env_key) if cache is not None else None
            if hit:
                ref_env = _b64_to_env(hit)
            else:
                _ref_full()
        return ref_env  # type: ignore[return-value]

    def _ref_full() -> tuple[np.ndarray, list[np.ndarray]]:
        nonlocal ref_env, ref_wins
        if not ref_wins:
            src = st.audio_pcm_cache.get(ref_id)
            src = src if isinstance(src, np.ndarray) and src.size else ref.file_path
            ref_env, ref_wins = stream_envelope_and_windows(src, sample_rate=p.sample_rate, env_rate_hz=p.env_rate_hz, windows=ref_req)
            if cache is not None:
                cache.put("sync_envelope", SYNC_ANALYSIS_VERSION, ref_env_key, _env_to_b64(ref_env))
        return ref_env, ref_wins  # type: ignore[return-value]

    targets = [c for c, a in st.media_assets.items() if c != ref_id and a.audio_codec]
    fps = st.fps
    measured_fresh = 0
    for n, cam in enumerate(targets):
        if token is not None:
            token.check(f"drift sync {cam}")
        if progress:
            progress(0.9 + 0.08 * n / max(1, len(targets)), f"Measuring clock drift of {cam} across the recording")
        asset = st.media_assets[cam]
        # Expensive stage (decodes, GCC-PHAT windows, envelopes) is cached without the cheap fit
        # thresholds, so a quality-loop correction of those thresholds never decodes again.
        key = {"ref": ref.sha256, "tgt": asset.sha256, "plan": plan, "ref_dur": round(ref.duration_sec, 3), "tgt_dur": round(asset.duration_sec, 3)}
        cached = cache.get("sync_windows", SYNC_ANALYSIS_VERSION, key) if cache is not None else None
        if cached:
            meas = [WindowMeasurement(**m) for m in cached["windows"]]
            tgt_env = _b64_to_env(cached["target_env"])
            env = _ref_env()
        else:
            env, wins = _ref_full()
            src = st.audio_pcm_cache.get(cam)
            src = src if isinstance(src, np.ndarray) and src.size else asset.file_path
            meas, tgt_env = measure_windows(env, wins, centres, src, asset.duration_sec, p)
            measured_fresh += 1
            if cache is not None:
                cache.put("sync_windows", SYNC_ANALYSIS_VERSION, key, {"windows": [asdict(m) for m in meas], "target_env": _env_to_b64(tgt_env)})
        fit = fit_target(cam, meas, env, tgt_env, p)
        report["cameras"][cam] = {**fit.to_dict(), "cache_hit": bool(cached)}
        _apply_fit(st, cam, fit, fps)
    sr_obj.method = "multiwindow_gcc_phat_drift" if targets else sr_obj.method
    report["review"] = [r for c in report["cameras"].values() for r in c.get("review", [])]
    report["elapsed_sec"] = round(time.perf_counter() - t0, 3)
    report["windows_planned"] = len(centres)
    report["targets_measured_fresh"] = measured_fresh
    report["frame_sec"] = round(1.0 / fps, 6)
    st.metadata["sync_report"] = report
    return report


_PLAN_FIELDS = ("sample_rate", "env_rate_hz", "fine_window_sec", "fine_search_sec", "coarse_window_sec", "target_windows", "min_spacing_sec", "max_windows")


def plan_key(p: SyncParams) -> dict[str, Any]:
    return {k: getattr(p, k) for k in _PLAN_FIELDS}


def _env_to_b64(env: np.ndarray) -> str:
    import base64

    return base64.b64encode(np.asarray(env, dtype=np.float16).tobytes()).decode("ascii")


def _b64_to_env(s: str) -> np.ndarray:
    import base64

    return np.frombuffer(base64.b64decode(s), dtype=np.float16).astype(np.float32)


def evidence_errors_ms(sync_result: Any, report: dict[str, Any], cam: str) -> list[tuple[float, float]]:
    """Per-window |applied mapping - independent GCC-PHAT measurement| in ms, as (master_sec, err_ms).

    Windows inside a recording gap are skipped (no content there to agree with).
    """
    rep = (report.get("cameras") or {}).get(cam) or {}
    gaps = [(g["master_start_sec"], g["master_end_sec"]) for g in rep.get("gaps", []) if g.get("kind") == "missing_content"]
    out = []
    for w in rep.get("windows", []):
        if w.get("status") not in ("ok", "outlier") or w.get("offset_ms") is None or w.get("status") == "outlier":
            continue
        t = float(w["master_sec"])
        if any(a - 1.0 <= t <= b + 1.0 for a, b in gaps):
            continue
        measured = t + float(w["offset_ms"]) / 1000.0
        out.append((t, abs(sync_result.source_time(cam, t) - measured) * 1000.0))
    return out


def _apply_fit(st: Any, cam: str, fit: DriftFit, fps: float) -> None:
    sr_obj = st.sync_result
    if fit.n_inliers == 0:
        sr_obj.warnings.append(f"Multi-window sync failed for '{cam}': kept the single-window offset")
        sr_obj.confidences[cam] = min(float(sr_obj.confidences.get(cam, 0.5)), 0.3)
        return
    old = float(sr_obj.offsets_sec.get(cam, 0.0))
    sr_obj.offsets_sec[cam] = fit.offset_sec
    sr_obj.offsets_project_frames[cam] = round(fit.offset_sec * fps)
    asset = st.media_assets[cam]
    sr_obj.offsets_native_frames[cam] = round(fit.offset_sec * asset.fps) if asset.has_video else sr_obj.offsets_project_frames[cam]
    sr_obj.drift_ppm[cam] = fit.drift_ppm
    if fit.segments:
        sr_obj.segment_offsets[cam] = [dict(s) for s in fit.segments]
    else:
        sr_obj.segment_offsets.pop(cam, None)
    sr_obj.confidences[cam] = fit.confidence
    if abs(old - fit.offset_sec) > 0.5:
        sr_obj.warnings.append(f"'{cam}': single-window offset {old:+.3f}s replaced by multi-window intercept {fit.offset_sec:+.3f}s")



__all__ = [
    "SYNC_ANALYSIS_VERSION",
    "DriftFit",
    "SyncParams",
    "estimate_drift_for_target",
    "fit_drift_and_segments",
    "gcc_phat_in_range",
    "master_time",
    "measure_windows",
    "plan_window_centres",
    "refine_state_sync",
    "stream_envelope_and_windows",
]
