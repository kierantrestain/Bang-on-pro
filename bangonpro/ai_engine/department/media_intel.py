"""Media processing extensions (V1.2.2): assessors and technical QC checks.

Owned by the media-processing specialist. Registers hooks with
``department.quality_loop`` on import.

Design rules for every assessor here:

* it scores the **media outcome** (alignment error against independent evidence, colour distance,
  residual clipping, mask stability, verified deliverables), never the number of operations;
* it only reads analysis already in the state / ``AnalysisCache`` (no inference, decodes or renders);
* every ``Defect`` has a stable ``code`` and, where it applies, ``at_sec`` on the master clock;
  ``"error"`` is reserved for critical problems (visible lip-sync error, missing picture in a used
  shot, failed deliverable).

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from bangonpro.ai_engine.department.quality_loop import Assessment, Defect, register_assessor, register_qc_check
from bangonpro.ai_engine.state import QCIssue


def _frame_ms(st: Any) -> float:
    try:
        return 1000.0 / float(st.fps)
    except (ZeroDivisionError, KeyError, TypeError):
        return 40.0


def _audio_cam_of(st: Any, shot: Any) -> str:
    for cid, a in st.media_assets.items():
        if a.media_ref == shot.audio_media_ref:
            return cid
    return shot.camera_id


def _timeline_sec_for_master(st: Any, master_sec: float) -> float:
    for s in st.rough_cut_shots:
        if s.master_sync_in_sec <= master_sec < s.master_sync_out_sec:
            return round(s.timeline_in_sec + (master_sec - s.master_sync_in_sec), 3)
    return 0.0


# ---------------------------------------------------------------------------
# Synchronisation
# ---------------------------------------------------------------------------
def finish_synchronisation(ctx: Any, batches: list[dict[str, Any]]) -> dict[str, Any]:
    """After the V1.1 single-window sync: multi-window drift/segments + visual analysis on the master clock."""
    from bangonpro.ai_engine.department.media_sync import refine_state_sync
    from bangonpro.ai_engine.department.media_visual import to_master_clock

    st = ctx.state
    report = refine_state_sync(st, ctx.cache, ctx.progress, ctx.token) if (st.sync_result and st.intent.sync_cameras) else {}
    sr = st.sync_result
    out: dict[str, Any] = {"review": list(report.get("review", [])), "metrics": {}, "cache_hits": []}
    if sr is not None:
        for b in batches:
            for op in b.get("operations", []):
                if op.get("type") == "sync_clips":
                    op["params"].update(
                        {
                            "method": sr.method,
                            "offsets_sec": dict(sr.offsets_sec),
                            "offsets_project_frames": dict(sr.offsets_project_frames),
                            "offsets_native_frames": dict(sr.offsets_native_frames),
                            "confidences": dict(sr.confidences),
                            "drift_ppm": dict(sr.drift_ppm),
                            "segment_offsets": {k: [dict(s) for s in v] for k, v in sr.segment_offsets.items()},
                            "offset_convention": "source = master + offset + drift_ppm*1e-6*master (offset = intercept at master t=0)",
                        }
                    )
                    vals = [v for k, v in sr.confidences.items() if k != sr.reference_camera_id] or [1.0]
                    op["confidence"] = round(float(min(1.0, max(0.0, np.mean(vals)))), 4)
        cams = report.get("cameras", {})
        out["metrics"] = {
            "drift_ppm": dict(sr.drift_ppm),
            "segments": {k: len(v) for k, v in sr.segment_offsets.items()},
            "sync_residual_rms_ms": {k: v.get("residual_rms_ms") for k, v in cams.items()},
            "sync_windows_inliers": {k: f"{v.get('n_inliers')}/{v.get('n_windows')}" for k, v in cams.items()},
            "sync_analysis_sec": report.get("elapsed_sec"),
        }
        out["cache_hits"] = [f"sync_windows:{k}" for k, v in cams.items() if v.get("cache_hit")]
        drifted = [f"{k} {v:+.1f} ppm" for k, v in sr.drift_ppm.items() if abs(v) >= 1.0]
        segs = sum(1 for v in sr.segment_offsets.values() if len(v) > 1)
        out["summary_suffix"] = (f"; clock drift {', '.join(drifted)}" if drifted else "") + (f"; {segs} interrupted recording(s) re-synced per segment" if segs else "")
    src = st.metadata.get("visual_analysis_source") or {}
    if src:
        va = {}
        for cam, data in src.items():
            if cam in st.media_assets:
                gaps = ((report.get("cameras") or {}).get(cam) or {}).get("gaps", [])
                va[cam] = to_master_clock(data, sr, cam, gaps)
        st.metadata["visual_analysis"] = va
        out["metrics"]["visual_cameras"] = sorted(va)
    return out


def _sync_targets(st: Any) -> list[str]:
    sr = st.sync_result
    return [c for c, a in st.media_assets.items() if sr is not None and c != sr.reference_camera_id and a.audio_codec]


def sync_quality(st: Any) -> tuple[float, list[Defect], dict[str, Any]]:
    """Score the *applied* sync mapping against the independent per-window GCC-PHAT evidence.

    Per camera ``q = 1 / (1 + (rms_err / 10 ms)^2) * (0.6 + 0.4 * evidence_coverage)``; a camera with no
    usable evidence scores 0.3 (unverified). The score falls monotonically with alignment error.
    """
    from bangonpro.ai_engine.department.media_sync import evidence_errors_ms

    sr = st.sync_result
    rep = st.metadata.get("sync_report") or {}
    frame_ms = _frame_ms(st)
    defects: list[Defect] = []
    per: dict[str, Any] = {}
    for cam in _sync_targets(st):
        crep = (rep.get("cameras") or {}).get(cam) or {}
        errs = evidence_errors_ms(sr, rep, cam)
        overlap = sum(1 for w in crep.get("windows", []) if w.get("status") != "no_overlap")
        if not errs:
            per[cam] = {"score": 0.3, "rms_err_ms": None}
            defects.append(Defect("SYNC_UNVERIFIED", "warning", f"{cam}: no sync window agreed with confidence, so alignment is unverified. Check it by eye at a clap or plosive."))
            continue
        e = np.array([x for _, x in errs])
        rms = float(np.sqrt(np.mean(e * e)))
        worst = int(np.argmax(e))
        cov = len(errs) / max(1, overlap)
        q = (1.0 / (1.0 + (rms / 10.0) ** 2)) * (0.6 + 0.4 * cov)
        per[cam] = {"score": round(q, 4), "rms_err_ms": round(rms, 3), "max_err_ms": round(float(e[worst]), 3), "evidence_coverage": round(cov, 3)}
        at = round(errs[worst][0], 3)
        if e[worst] > frame_ms:
            defects.append(Defect("SYNC_LIPSYNC_OVER_FRAME", "error", f"{cam}: applied sync is {e[worst]:.0f} ms off the measured alignment at {at:.1f}s (> 1 frame, visible lip-sync error)", at))
        elif rms > 4.0:
            defects.append(Defect("SYNC_RESIDUAL_HIGH", "warning", f"{cam}: sync residual {rms:.1f} ms RMS; check for variable frame rate or edits inside the file", at))
        for g in crep.get("gaps", []):
            if g.get("kind") == "missing_content":
                defects.append(Defect("SYNC_RECORDING_GAP", "warning", f"{cam}: device stopped recording at {g['master_start_sec']:.2f}s for {abs(g['jump_sec']):.2f}s; re-synced per segment, avoid this angle there", round(g["master_start_sec"], 3)))
        if crep.get("n_inliers", 0) < 3 and crep:
            defects.append(Defect("SYNC_DRIFT_UNMEASURED", "info", f"{cam}: recording too short or too quiet to measure clock drift; single offset used"))
    score = float(np.mean([v["score"] for v in per.values()])) if per else 1.0
    return round(score, 4), defects, per


@register_assessor("synchronisation")
def assess_synchronisation(ctx: Any, outcome: Any) -> Assessment | None:
    st = ctx.state
    if st.sync_result is None or not st.intent.sync_cameras:
        return None
    score, defects, per = sync_quality(st)
    correction, reason = None, ""
    rules = (st.profile.get("audio_rules") or {})
    cur = float(rules.get("sync_min_window_confidence", 0.35))
    low_cov = [c for c, v in per.items() if (v.get("evidence_coverage") or 0.0) < 0.6]
    if low_cov and cur > 0.21:
        # Cheap refit from cached window measurements (no decoding): admit weaker windows, the
        # robust fit still rejects any that disagree with the clock model.
        correction = {"audio_rules": {"sync_min_window_confidence": round(max(0.2, cur - 0.1), 3)}}
        reason = f"Too few sync windows accepted for {', '.join(low_cov)}; admit weaker windows and refit"
    return Assessment(
        agent_id="synchronisation",
        score=score,
        metric="sync_alignment_vs_window_evidence",
        defects=defects,
        correction=correction,
        correction_reason=reason,
        details={"per_camera": per},
    )


@register_qc_check("media_sync_drift")
def qc_sync_drift(st: Any) -> list[QCIssue]:
    """Lip-sync error from drift, segment breaks and placement against the sync model (> 1 frame)."""
    from bangonpro.ai_engine.department.media_sync import evidence_errors_ms

    sr = st.sync_result
    rep = st.metadata.get("sync_report") or {}
    if sr is None or not rep:
        return []
    frame_ms = _frame_ms(st)
    issues: list[QCIssue] = []
    for cam in _sync_targets(st):
        errs = evidence_errors_ms(sr, rep, cam)
        if errs:
            t, e = max(errs, key=lambda x: x[1])
            if e > frame_ms:
                issues.append(QCIssue("SYNC_DRIFT_LIPSYNC", "error" if e > 3 * frame_ms else "warning", f"{cam} drifts {e:.0f} ms from the measured alignment at {t:.1f}s on the master clock (> 1 frame)", _timeline_sec_for_master(st, t), {"camera_id": cam, "master_sec": t, "error_ms": round(e, 2)}))
    cams = rep.get("cameras") or {}
    for s in st.rough_cut_shots:
        cv, ca = s.camera_id, _audio_cam_of(st, s)
        dur = max(0.0, s.master_sync_out_sec - s.master_sync_in_sec)
        for cam in {cv, ca}:
            for g in (cams.get(cam) or {}).get("gaps", []):
                if g.get("kind") == "missing_content" and g["master_start_sec"] < s.master_sync_out_sec and g["master_end_sec"] > s.master_sync_in_sec:
                    issues.append(QCIssue("SYNC_GAP_IN_SHOT", "error", f"Shot {s.shot_id} uses {cam} while it was not recording ({g['master_start_sec']:.2f}-{g['master_end_sec']:.2f}s); pick another angle or source", s.timeline_in_sec, {"camera_id": cam, "shot_id": s.shot_id, "master_sec": g["master_start_sec"]}))
        if cv == ca:
            continue
        av, aa = st.media_assets.get(cv), st.media_assets.get(ca)
        if av is None or aa is None:
            continue
        mv, ma = sr.source_time(cv, s.master_sync_in_sec), sr.source_time(ca, s.master_sync_in_sec)
        if not (0.0 < mv < av.duration_sec - 0.05 and 0.0 < ma < aa.duration_sec - 0.05):
            continue  # clamped at a file boundary: placement error is a bounds issue, not drift
        delta = abs((s.source_in_sec - mv) - (s.audio_source_in_sec - ma)) * 1000.0
        rel = abs(float(sr.drift_ppm.get(cv, 0.0)) - float(sr.drift_ppm.get(ca, 0.0))) * 1e-6 * dur * 1000.0
        res = math.hypot(float((cams.get(cv) or {}).get("residual_rms_ms") or 0.0), float((cams.get(ca) or {}).get("residual_rms_ms") or 0.0))
        total = delta + rel + res
        if total > frame_ms:
            issues.append(QCIssue("SYNC_SHOT_LIPSYNC", "error" if total > 3 * frame_ms else "warning", f"Shot {s.shot_id}: picture ({cv}) and sound ({ca}) drift {total:.0f} ms apart (> 1 frame)", s.timeline_in_sec, {"shot_id": s.shot_id, "placement_ms": round(delta, 2), "drift_over_shot_ms": round(rel, 2), "model_residual_ms": round(res, 2), "master_sec": s.master_sync_in_sec}))
    return issues


# ---------------------------------------------------------------------------
# Audio Engineer
# ---------------------------------------------------------------------------
def engineer_audio(ctx: Any) -> dict[str, Any]:
    """Measure mics and turn the plan into the track chain + per-clip operations (no rendering)."""
    from bangonpro.ai_engine.department.media_audio import compand_points_compressor, compand_points_expander, plan_audio

    st = ctx.state
    plan = plan_audio(st, ctx.cache, ctx.token, ctx.progress)
    comp = plan["compression"]
    chain = {
        "compressor_points": compand_points_compressor(comp["threshold_dbfs"], comp["ratio"]),
        "compressor_gain_db": 0.0,
        "compressor_rationale": f"{comp['ratio']:g}:1 above {comp['threshold_dbfs']:.0f} dBFS from measured speech level {plan['matching']['target_speech_dbfs']:.1f} dBFS (crest {comp['speech_crest_db']:.1f} dB); natural dynamics kept",
    }
    trims = plan["matching"]["trims_db"]
    eq = plan["matching"]["eq"]
    clip_ops: list[dict[str, Any]] = []
    for s in st.rough_cut_shots:
        sp = plan["shots"].get(s.shot_id) or {}
        m = sp.get("mic")
        cid = f"{s.shot_id}_a"
        tr = float(trims.get(m, 0.0))
        if abs(tr) >= 1.0:
            clip_ops.append({"clip_id": cid, "mlt_service": "volume", "properties": {"level": round(tr, 2)}, "rationale": f"Match {m} to {plan['matching']['target_speech_dbfs']:.1f} dBFS speech level ({tr:+.1f} dB, measured)", "confidence": 0.92})
        e = eq.get(m) or {}
        if e.get("bass_db"):
            clip_ops.append({"clip_id": cid, "mlt_service": "avfilter.bass", "properties": {"av.g": e["bass_db"], "av.f": 150}, "rationale": f"Tone-match {m}: low shelf {e['bass_db']:+.1f} dB toward the other mics", "confidence": 0.85})
        if e.get("highshelf_db"):
            clip_ops.append({"clip_id": cid, "mlt_service": "avfilter.highshelf", "properties": {"av.g": e["highshelf_db"], "av.f": 4000}, "rationale": f"Tone-match {m}: high shelf {e['highshelf_db']:+.1f} dB toward the other mics", "confidence": 0.85})
        ex = sp.get("bleed_expander")
        if ex:
            pts = compand_points_expander(ex["threshold_dbfs"], ex["reduction_db"])
            clip_ops.append({"clip_id": cid, "mlt_service": "avfilter.compand", "properties": {"av.attacks": 0.005, "av.decays": 0.3, "av.points": pts, "av.soft-knee": 6.0, "av.gain": 0.0}, "rationale": f"Reduce room bleed in {m} by {ex['reduction_db']:.0f} dB between words (no other speaker active in this shot)", "confidence": 0.8})
    st.metadata["audio_plan"] = {"chain": chain, "clip_ops": clip_ops}
    st.metadata["audio_analysis"] = {k: v for k, v in plan.items() if k != "cache_hits"}
    return plan


def audio_quality(st: Any) -> tuple[float, list[Defect], dict[str, Any]]:
    """Score the *predicted* result of the applied audio plan (no rendering).

    ``q = 0.35*level_match + 0.35*clean_peaks + 0.15*natural_dynamics + 0.15*intelligibility`` where
    level_match = exp(-spread_after_dB / 6), clean_peaks = 1 - min(1, residual_clipped_share / 0.5 %),
    natural_dynamics = 1 for a predicted speech crest >= 9 dB falling to 0 at 3 dB, and
    intelligibility = speech-band SNR mapped 6 dB -> 0, 20 dB -> 1. Operation counts never enter.
    """
    from bangonpro.ai_engine.department.media_sync import master_time

    an = st.metadata.get("audio_analysis") or {}
    if not an:
        return 1.0, [], {}
    rules = (st.profile.get("audio_rules") or {})
    min_snr = float(rules.get("min_speech_snr_db", 15.0))
    used = an.get("used_mics", [])
    mics = an.get("mics", {})
    spread = float((an.get("matching") or {}).get("level_spread_after_db", 0.0))
    level_q = math.exp(-spread / 6.0)
    residual: list[float] = []
    defects: list[Defect] = []
    for c in used:
        cl = (mics.get(c) or {}).get("clipping", {}) or {}
        share = float(cl.get("clipped_share_of_speech") or 0.0)
        dc = (an.get("declip") or {}).get(c) or {}
        after = dc.get("residual_clipped_share_of_speech") if dc.get("created") else None
        residual.append(float(after) if after is not None else share)
        if share > 0.0005:
            ev = (cl.get("events") or [{}])[0]
            at = master_time(st.sync_result, c, float(ev["start_sec"])) if (ev.get("start_sec") is not None and st.sync_result is not None) else None
            msg = f"{c}: {share:.2%} of speech samples clipped ({cl.get('event_count', 0)} events)"
            msg += "; a declipped copy is in the bin" if dc.get("created") else "; re-record or repair in an audio editor"
            defects.append(Defect("AUDIO_CLIPPING", "warning", msg, None if at is None else round(at, 3)))
    clean_q = 1.0 - min(1.0, (max(residual) if residual else 0.0) / 0.005)
    crest_after = float((an.get("compression") or {}).get("predicted_crest_after_db", 12.0))
    dyn_q = float(np.clip((crest_after - 3.0) / 6.0, 0.0, 1.0))
    snrs = [float((mics.get(c) or {})["speech_band_snr_db"]) for c in used if (mics.get(c) or {}).get("speech_band_snr_db") is not None]
    snr = min(snrs) if snrs else 30.0
    intel_q = float(np.clip((snr - 6.0) / 14.0, 0.0, 1.0))
    for c in used:
        v = (mics.get(c) or {}).get("speech_band_snr_db")
        if v is not None and v < min_snr:
            defects.append(Defect("AUDIO_LOW_SNR", "error" if v < 3.0 else "warning", f"{c}: speech-band SNR {v:.1f} dB (< {min_snr:g} dB); dialogue may be hard to follow. Enable RNNoise or re-record"))
    if spread > 3.0:
        defects.append(Defect("AUDIO_LEVEL_MISMATCH", "warning", f"Microphones still differ by {spread:.1f} dB after matching (trim limit audio_rules.max_mic_trim_db)"))
    if crest_after < 6.0:
        defects.append(Defect("AUDIO_OVERCOMPRESSED", "warning", f"Predicted speech crest {crest_after:.1f} dB: dynamics will sound squashed", subjective=True))
    score = 0.35 * level_q + 0.35 * clean_q + 0.15 * dyn_q + 0.15 * intel_q
    return round(score, 4), defects, {"level_spread_after_db": spread, "residual_clipped_share": max(residual) if residual else 0.0, "predicted_crest_after_db": crest_after, "min_speech_band_snr_db": snr}


@register_assessor("audio_engineer")
def assess_audio_engineer(ctx: Any, outcome: Any) -> Assessment | None:
    st = ctx.state
    if not st.intent.restore_audio or not st.metadata.get("audio_analysis"):
        return None
    score, defects, details = audio_quality(st)
    an = st.metadata["audio_analysis"]
    rules = (st.profile.get("audio_rules") or {})
    correction, reason = None, ""
    trims = (an.get("matching") or {}).get("trims_db", {})
    cap = float(rules.get("max_mic_trim_db", 24.0))
    if details["level_spread_after_db"] > 3.0 and trims and max(abs(v) for v in trims.values()) >= cap - 0.01 and cap < 24.0:
        correction, reason = {"audio_rules": {"max_mic_trim_db": min(24.0, cap + 6.0)}}, "Microphone trims hit the limit; allow 6 dB more"
    elif details["predicted_crest_after_db"] < 6.0 and float(rules.get("compression_ratio", 3.0)) > 2.0:
        correction, reason = {"audio_rules": {"compression_ratio": 2.0}}, "Predicted over-compression; use a gentler 2:1 ratio"
    return Assessment(agent_id="audio_engineer", score=score, metric="predicted_mix_quality", defects=defects, correction=correction, correction_reason=reason, details=details)


@register_qc_check("media_audio")
def qc_audio(st: Any) -> list[QCIssue]:
    """Clipping in used shots, off-mic speech (crosstalk), wrong mic choice, unreachable loudness, low SNR."""
    from bangonpro.ai_engine.department.media_sync import master_time

    an = st.metadata.get("audio_analysis") or {}
    if not an:
        return []
    issues: list[QCIssue] = []
    mics = an.get("mics", {})
    sr = st.sync_result
    for s in st.rough_cut_shots:
        sp = (an.get("shots") or {}).get(s.shot_id) or {}
        m = sp.get("mic") or _audio_cam_of(st, s)
        for ev in ((mics.get(m) or {}).get("clipping", {}) or {}).get("events", []):
            a = master_time(sr, m, float(ev["start_sec"])) if sr is not None else ev["start_sec"]
            if a is not None and s.master_sync_in_sec <= a < s.master_sync_out_sec:
                dc = ((an.get("declip") or {}).get(m) or {})
                issues.append(QCIssue("AUDIO_CLIPPING_IN_SHOT", "warning", f"Shot {s.shot_id}: {m} clips at {a:.1f}s" + (" (declipped copy in the bin)" if dc.get("created") else ""), round(s.timeline_in_sec + (a - s.master_sync_in_sec), 3), {"shot_id": s.shot_id, "mic": m, "master_sec": round(a, 3)}))
                break
        if sp.get("off_mic_speech_share", 0.0) > 0.3:
            issues.append(QCIssue("AUDIO_OFF_MIC_SPEECH", "warning", f"Shot {s.shot_id}: {sp['off_mic_speech_share']:.0%} of the speech is another person heard only through bleed on {m}", s.timeline_in_sec, {"shot_id": s.shot_id, "mic": m, "dominant_mic": sp.get("dominant_mic"), "master_sec": s.master_sync_in_sec}))
        elif sp and not sp.get("mic_is_dominant", True) and sp.get("own_speech_share", 0.0) < 0.2:
            issues.append(QCIssue("AUDIO_MIC_CHOICE", "warning", f"Shot {s.shot_id}: speech is mostly on {sp.get('dominant_mic')} but the shot uses {m}", s.timeline_in_sec, {"shot_id": s.shot_id, "mic": m, "master_sec": s.master_sync_in_sec}))
    target = float((an.get("matching") or {}).get("target_speech_dbfs", -20.0))
    try:
        from bangonpro.ai_engine.agents.audio_graphics_qc import _loudness_target

        lufs = float(_loudness_target(st))
    except Exception:  # noqa: BLE001 - profile without loudness settings
        lufs = -16.0
    if abs((lufs - 3.0) - target) > 15.0:  # dialogue loudness sits ~3 LU above speech RMS
        issues.append(QCIssue("AUDIO_LOUDNESS_UNREACHABLE", "warning", f"Speech at {target:.1f} dBFS needs more than the 15 dB the loudness normaliser can add to reach {lufs:g} LUFS", 0.0))
    min_snr = float((st.profile.get("audio_rules") or {}).get("min_speech_snr_db", 15.0))
    for c in an.get("used_mics", []):
        v = (mics.get(c) or {}).get("speech_band_snr_db")
        if v is not None and v < min_snr:
            issues.append(QCIssue("AUDIO_LOW_SNR", "error" if v < 3.0 else "warning", f"{c}: speech-band SNR {v:.1f} dB", 0.0, {"mic": c}))
    return issues
