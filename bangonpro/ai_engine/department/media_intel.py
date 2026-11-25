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
