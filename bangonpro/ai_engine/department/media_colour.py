"""Colour matching for the Colourist (V1.2.2): white balance, exposure, skin, shot-level, creative intent.

Works only from the sampled per-camera statistics in ``visual_analysis`` (no extra decoding):

* **white balance** from near-neutral pixels (grey-world fallback when a camera sees < 2 % neutral);
* **exposure** from mid-tone luma;
* **skin-tone consistency**: after WB+exposure, a small bounded nudge so skin chroma matches the
  reference angle (YCbCr skin-likelihood pixels);
* **shot-level** correction when lighting changes inside a camera: per-shot statistics (sample
  medians, 3-sample temporal smoothing) replace the camera-level gains only when the measured
  difference exceeds ``colour_rules.shot_threshold_de``;
* **creative intent**: the reference angle keeps its look (``colour_rules.preserve_look``), gains are
  clamped (``max_channel_gain``) and an approved LUT is applied after the match.

Distances are CIE76 Delta E in CIELAB (sRGB/D65).

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

from typing import Any

import numpy as np

COLOUR_VERSION = "1.2.2-colour-1"
_W = np.array([0.2126, 0.7152, 0.0722])


def srgb_to_lab(rgb: Any) -> np.ndarray:
    c = np.clip(np.asarray(rgb, dtype=np.float64), 0.0, 1.0)
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
    xyz = lin @ m.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16.0 / 116.0)
    return np.stack([116.0 * f[..., 1] - 16.0, 500.0 * (f[..., 0] - f[..., 1]), 200.0 * (f[..., 1] - f[..., 2])], axis=-1)


def delta_e(a: Any, b: Any) -> float:
    return float(np.linalg.norm(srgb_to_lab(a) - srgb_to_lab(b), axis=-1).mean())


def _rules(st: Any) -> dict[str, Any]:
    r = dict(st.profile.get("colour_rules", {}) or {})

    def f(k: str, d: float, lo: float, hi: float) -> float:
        try:
            return float(min(hi, max(lo, float(r.get(k, d)))))
        except (TypeError, ValueError):
            return d

    return {
        "max_channel_gain": f("max_channel_gain", 1.33, 1.0, 2.0),
        "preserve_look": bool(r.get("preserve_look", True)),
        "skin_weight": f("skin_weight", 0.5, 0.0, 1.0),
        "skin_max_nudge": f("skin_max_nudge", 0.04, 0.0, 0.15),
        "shot_threshold_de": f("shot_threshold_de", 3.0, 0.5, 20.0),
        "shot_level": bool(r.get("shot_level", True)),
        "min_neutral_share": f("min_neutral_share", 0.02, 0.0, 0.5),
        "cut_jump_de": f("cut_jump_de", 6.0, 1.0, 30.0),
        "correct_exposure": bool(r.get("correct_exposure", True)),
        "target_luma": f("target_luma", 0.45, 0.05, 0.95),
        "min_luma": f("min_luma", 0.30, 0.0, 1.0),
        "max_luma": f("max_luma", 0.68, 0.0, 1.0),
    }


def _smooth(rows: list[Any]) -> np.ndarray:
    a = np.asarray(rows, dtype=np.float64)
    if a.shape[0] < 3:
        return a
    pad = np.vstack([a[:1], a, a[-1:]])
    return np.median(np.stack([pad[:-2], pad[1:-1], pad[2:]]), axis=0)


def stats_for(samples: dict[str, Any], idx: list[int], rules: dict[str, Any]) -> dict[str, Any] | None:
    if not idx:
        return None
    rgb = _smooth([samples["rgb"][i] for i in idx])
    neu = _smooth([samples["neutral_rgb"][i] for i in idx])
    share = float(np.median([samples["neutral_share"][i] for i in idx]))
    skins = [samples["skin_rgb"][i] for i in idx if samples.get("skin_rgb") and samples["skin_rgb"][i]]
    skin_share = float(np.median([samples["skin_share"][i] for i in idx])) if samples.get("skin_share") else 0.0
    luma = float(np.median([samples["luma"][i] for i in idx]))
    use_neutral = share >= rules["min_neutral_share"]
    return {
        "wb_rgb": [float(x) for x in np.median(neu if use_neutral else rgb, axis=0)],
        "wb_source": "near_neutral_pixels" if use_neutral else "grey_world",
        "mean_rgb": [float(x) for x in np.median(rgb, axis=0)],
        "skin_rgb": [float(x) for x in np.median(np.asarray(skins), axis=0)] if skins and skin_share >= 0.005 else None,
        "luma": luma,
        "luma_p95": float(np.median([samples["luma_p95"][i] for i in idx])) if samples.get("luma_p95") else min(1.0, luma * 1.8),
        "n": len(idx),
    }


def match_gains(cam: dict[str, Any], ref: dict[str, Any], rules: dict[str, Any], exposure: float = 1.0) -> tuple[np.ndarray, dict[str, Any]]:
    """Channel gains mapping ``cam`` onto ``ref``: WB from neutrals, exposure from mid-tones, skin nudge."""
    cw, rw = np.asarray(cam["wb_rgb"]), np.asarray(ref["wb_rgb"])
    wb = rw / np.maximum(cw, 1e-3)
    wb = wb / max(1e-6, float(_W @ (wb * cw)) / max(1e-6, float(_W @ cw)))  # pure chroma correction
    expo = (ref["luma"] / max(1e-3, cam["luma"])) if rules["correct_exposure"] else 1.0
    g = wb * expo
    nudge = np.ones(3)
    if cam.get("skin_rgb") and ref.get("skin_rgb") and rules["skin_weight"] > 0:
        corr = np.asarray(cam["skin_rgb"]) * g
        target = np.asarray(ref["skin_rgb"])
        ratio = (target / max(1e-6, float(_W @ target))) / np.maximum(corr / max(1e-6, float(_W @ corr)), 1e-6)
        nudge = 1.0 + np.clip((ratio - 1.0) * rules["skin_weight"], -rules["skin_max_nudge"], rules["skin_max_nudge"])
        g = g * nudge
    g = g * exposure
    mg = rules["max_channel_gain"]
    clamped = bool(np.any(g > mg) or np.any(g < 1.0 / mg))
    g = np.clip(g, 1.0 / mg, mg)
    return g, {"wb": [round(float(x), 4) for x in wb], "exposure_match": round(float(expo), 4), "skin_nudge": [round(float(x), 4) for x in nudge], "clamped": clamped}


def corrected_distance(cam: dict[str, Any], ref: dict[str, Any], g: np.ndarray, ref_gain: np.ndarray | None = None) -> dict[str, Any]:
    """Predicted inter-camera Delta E after gains (neutral, skin, mean) and highlight/skin clipping share."""
    rg = np.ones(3) if ref_gain is None else ref_gain
    out: dict[str, Any] = {"neutral_de": delta_e(np.asarray(cam["wb_rgb"]) * g, np.asarray(ref["wb_rgb"]) * rg), "mean_de": delta_e(np.asarray(cam["mean_rgb"]) * g, np.asarray(ref["mean_rgb"]) * rg)}
    if cam.get("skin_rgb") and ref.get("skin_rgb"):
        cs = np.asarray(cam["skin_rgb"]) * g
        out["skin_de"] = delta_e(cs, np.asarray(ref["skin_rgb"]) * rg)
        out["skin_clipped_channels"] = int(np.sum(cs > 0.98))
    out["highlight_clip"] = float(max(0.0, cam["luma_p95"] * float(np.max(g)) - 1.0))
    return out


def plan_colour(st: Any, video: dict[str, Any]) -> dict[str, Any]:
    """Camera-level and shot-level gains from ``visual_analysis`` (cheap: statistics only)."""
    from bangonpro.ai_engine.department.media_visual import sample_index_range

    rules = _rules(st)
    va = st.metadata.get("visual_analysis") or {}
    cams: dict[str, Any] = {}
    for cid in video:
        s = (va.get(cid) or {}).get("samples") or {}
        if s.get("t"):
            cams[cid] = stats_for(s, list(range(len(s["t"]))), rules)
    if not cams:
        return {}
    cr = st.profile.get("colour_rules", {}) or {}
    roles = st.profile.get("camera_roles", {}) or {}
    ref = cr.get("reference_camera")
    if ref not in cams:
        ref = next((c for c in cams if roles.get(c) == "wide"), None) or min(cams, key=lambda c: video[c].priority)
    exposure = 1.0
    rl = cams[ref]["luma"]
    match_ref_look = bool(cr.get("match_reference_look", False))
    if rules["correct_exposure"] and rl > 0:
        if match_ref_look and abs(rl - rules["target_luma"]) > 0.03:
            exposure = min(1.35, max(0.75, rules["target_luma"] / rl))
        elif rl < rules["min_luma"]:
            exposure = min(1.5, rules["target_luma"] / rl)
        elif rl > rules["max_luma"]:
            exposure = max(0.7, rules["target_luma"] / rl)
    style_tint = np.ones(3)
    raw_tint = cr.get("style_tint_rgb")
    if match_ref_look and isinstance(raw_tint, (list, tuple)) and len(raw_tint) == 3:
        style_tint = np.clip(np.asarray([float(x) for x in raw_tint], dtype=np.float64), 0.85, 1.15)
    ref_target_gain = np.clip(np.ones(3) * exposure * style_tint, 1.0 / rules["max_channel_gain"], rules["max_channel_gain"])
    cam_gain: dict[str, Any] = {}
    for cid, cs in cams.items():
        if cid == ref:
            g = ref_target_gain.copy()
            info = {"wb": [1.0, 1.0, 1.0], "exposure_match": 1.0, "skin_nudge": [1.0, 1.0, 1.0], "clamped": False}
        else:
            g, info = match_gains(cs, cams[ref], rules, exposure)
            if match_ref_look:
                g = np.clip(g * style_tint, 1.0 / rules["max_channel_gain"], rules["max_channel_gain"])
        before = corrected_distance(cs, cams[ref], np.ones(3))
        after = corrected_distance(cs, cams[ref], g, ref_target_gain)
        cam_gain[cid] = {"gains": [round(float(x), 4) for x in g], **info, "de_before": {k: round(v, 3) for k, v in before.items()}, "de_after": {k: round(v, 3) for k, v in after.items()}, "wb_source": cs["wb_source"]}
    shots: dict[str, Any] = {}
    for sh in st.rough_cut_shots:
        cid = sh.camera_id
        if cid not in cams:
            continue
        smp = (va.get(cid) or {}).get("samples") or {}
        idx = sample_index_range(smp, sh.master_sync_in_sec, sh.master_sync_out_sec)
        ss = stats_for(smp, idx, rules)
        g = np.asarray(cam_gain[cid]["gains"])
        mode = "camera"
        if ss and rules["shot_level"]:
            # Does this shot look different from the camera average (lighting change)?
            diff = delta_e(np.asarray(ss["wb_rgb"]), np.asarray(cams[cid]["wb_rgb"]))
            ldiff = abs(ss["luma"] - cams[cid]["luma"])
            if diff > rules["shot_threshold_de"] or ldiff > 0.03:
                # Map this shot onto the reference look; on the reference angle itself this only undoes
                # the lighting change relative to its own average (its creative look is preserved).
                r2 = {**rules, "skin_weight": 0.0} if (cid == ref and rules["preserve_look"]) else rules
                g, _ = match_gains(ss, cams[ref], r2, exposure)
                mode = "shot"
        de_after = corrected_distance(ss, cams[ref], g, np.ones(3) * exposure) if ss else None
        shots[sh.shot_id] = {"camera_id": cid, "mode": mode, "gains": [round(float(x), 4) for x in g], "samples": len(idx), "de_after": None if de_after is None else {k: round(v, 3) for k, v in de_after.items()}, "stats": ss, "master_in_sec": round(sh.master_sync_in_sec, 3)}
    return {"version": COLOUR_VERSION, "reference_camera": ref, "exposure_gain": round(exposure, 4), "rules": rules, "camera_stats": cams, "cameras": cam_gain, "shots": shots}


__all__ = ["COLOUR_VERSION", "corrected_distance", "delta_e", "match_gains", "plan_colour", "srgb_to_lab", "stats_for"]
