"""Editorial intelligence extensions (V1.2.2): assessors, QC checks and final verification.

Owned by the editorial-intelligence specialist. Registers hooks with
``department.quality_loop`` on import.

Assessor design rules (shared with the quality loop):

* scores are **defect-based**: every penalty is something a human editor would have to
  fix (a meaningful pause or laugh removed, a clipped word, dead air left in, a flash
  frame, an avoidable sub-minimum shot, a cut that changes nothing, an unavailable angle
  on air). No term rewards the number of operations or cuts;
* they recompute from state only: no inference, no media decoding, no external calls;
* defects carry stable ``code``s and a master-clock ``at_sec``; ``error`` is reserved
  for critical problems (clipped or lost words, unavailable angle on air);
* a correction is proposed only when the defect it targets is measured, and it is a
  learnable profile overlay the job verifies by rerunning the agent.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

from typing import Any

from bangonpro.ai_engine.department import editorial_qc
from bangonpro.ai_engine.department.quality_loop import (
    Assessment,
    Defect,
    register_assessor,
    register_final_verifier,
    register_qc_check,
)

# ---------------------------------------------------------------------------
# Rough Cut: pause protection, dead air, cut safety
# ---------------------------------------------------------------------------
W_PROTECTED = 0.35  # share of meaningful pauses removed
W_DEAD_AIR = 0.25  # share of removable dead air left in
P_CRITICAL = 0.4  # per distinct word clipped or lost (critical; capped at 0.8)
P_FLASH = 0.04  # per speech sliver between two cuts (capped at 0.12)
P_CHOPPY = 0.05  # edit rate above the format reference


def rough_cut_score(pm: dict[str, Any], *, silence_removal_requested: bool = True) -> tuple[float, list[Defect]]:
    """Score ``editorial_pacing.pacing_metrics`` output; returns (score, defects)."""
    defects: list[Defect] = []
    for p in pm.get("protected_lost", []):
        defects.append(Defect("PROTECTED_PAUSE_REMOVED", "warning", f"Meaningful pause ({p['kind'].replace('_', ' ')}) cut from {p['original_sec']:.1f}s to {p['kept_sec']:.1f}s", p["start_sec"]))
    # One defect per affected word (a clipped word is not counted again as lost)
    clipped: dict[float, dict[str, Any]] = {}
    for c in pm.get("cuts_inside_words", []):
        clipped.setdefault(float(c.get("word_start", c["at_sec"])), c)
    for ws, c in sorted(clipped.items()):
        defects.append(Defect("CUT_INSIDE_WORD", "error", f"Cut splits the word '{c['word']}'", ws))
    seg_cuts = {c["segment_id"]: c for c in pm.get("cuts_inside_segments", [])}
    for sid, c in sorted(seg_cuts.items()):
        defects.append(Defect("CUT_INSIDE_SPEECH", "error", f"Cut inside speech segment {sid}", c["at_sec"]))
    lost = {float(w.get("word_start", w["at_sec"])): w for w in pm.get("words_lost", []) if float(w.get("word_start", w["at_sec"])) not in clipped}
    for ws, w in sorted(lost.items()):
        defects.append(Defect("WORD_REMOVED", "error", f"Word '{w['word']}' removed by a pause cut", ws))
    for f in pm.get("flash_fragments", []):
        defects.append(Defect("FLASH_FRAGMENT", "warning", f"{f['duration_sec']:.2f}s sliver of speech between two cuts", f["at_sec"]))
    if silence_removal_requested:
        for d in pm.get("dead_air_kept", []):
            defects.append(Defect("DEAD_AIR_KEPT", "info", f"{d['kept_sec']:.1f}s pause kept ({d['kind'].replace('_', ' ')})", d["start_sec"]))
    choppy = pm.get("edits_per_min", 0.0) > pm.get("ref_max_edits_per_min", 1e9) and pm.get("edited_sec", 0.0) >= 60.0
    if choppy:
        defects.append(Defect("EDIT_RATE_HIGH", "warning", f"{pm['edits_per_min']:.1f} pacing edits/min (reference {pm['ref_max_edits_per_min']:g}): choppy", None))
    critical = len(clipped) + len(seg_cuts) + len(lost)
    penalty = W_PROTECTED * (1.0 - float(pm.get("protected_retention", 1.0)))
    if silence_removal_requested:
        penalty += W_DEAD_AIR * (1.0 - float(pm.get("dead_air_removed_ratio", 1.0)))
    penalty += min(0.8, P_CRITICAL * critical) + min(0.12, P_FLASH * len(pm.get("flash_fragments", []))) + (P_CHOPPY if choppy else 0.0)
    return round(max(0.0, 1.0 - penalty), 4), defects


@register_assessor("rough_cut")
def assess_rough_cut(ctx: Any, outcome: Any) -> Assessment | None:
    from bangonpro.ai_engine.department.editorial_pacing import pacing_metrics, resolve_pacing_rules

    st = ctx.state
    if not st.kept_master_intervals or outcome.skipped_reason:
        return None
    decisions = st.metadata.get("rough_cut_decisions") or {}
    pm = pacing_metrics(st, st.kept_master_intervals, declared_disfluencies=decisions.get("disfluencies"))
    score, defects = rough_cut_score(pm, silence_removal_requested=bool(st.intent.remove_silence))
    rules = decisions.get("rules") or resolve_pacing_rules(st)
    er = st.profile.get("editing_rules", {}) or {}
    correction: dict[str, Any] | None = None
    reason = ""
    critical = [d for d in defects if d.severity == "error"]
    if (critical or pm["protected_retention"] < 1.0) and rules.get("pause_policy") == "remove":
        correction, reason = {"editing_rules": {"pause_policy": "compress"}}, "meaningful pauses or words were cut: compress pauses instead of removing them"
    elif pm["protected_retention"] < 1.0:
        toggles = {k: True for k in ("protect_question_pauses", "protect_punchline_pauses", "protect_reaction_pauses") if er.get(k) is False}
        if toggles:
            correction, reason = {"editing_rules": toggles}, "re-enable pause protection"
        else:
            cur = float(rules.get("protected_pause_max_sec", 1.5))
            correction, reason = {"editing_rules": {"protected_pause_max_sec": round(min(8.0, cur + 0.5), 2)}}, "protected pauses kept too short"
    elif st.intent.remove_silence and pm["dead_air_removed_ratio"] < 0.8 and rules.get("brief_pacing") != "loose":
        if rules.get("pause_policy") == "keep":
            correction, reason = {"editing_rules": {"pause_policy": "compress"}}, "dead air left in although silence removal was requested"
        else:
            correction = {
                "editing_rules": {
                    "natural_pause_sec": round(max(0.15, float(rules["natural_pause_sec"]) * 0.7), 3),
                    "turn_pause_sec": round(max(0.2, float(rules["turn_pause_sec"]) * 0.7), 3),
                    "min_pause_saving_sec": round(max(0.15, float(rules["min_pause_saving_sec"]) * 0.6), 3),
                }
            }
            reason = f"{pm['dead_air_excess_kept_sec']:.1f}s of removable dead air kept"
    elif any(d.code == "EDIT_RATE_HIGH" for d in defects):
        correction, reason = {"editing_rules": {"min_pause_saving_sec": round(min(3.0, float(rules["min_pause_saving_sec"]) + 0.2), 3)}}, "too many small pacing edits"
    details = {k: v for k, v in pm.items() if k not in ("protected_detail",)}
    return Assessment(agent_id="rough_cut", score=score, metric="rough_cut_pacing_integrity", defects=defects, correction=correction, correction_reason=reason, details=details)


# ---------------------------------------------------------------------------
# Multicam Director: cut discipline on the provisional shot plan
# ---------------------------------------------------------------------------
P_SUBMIN = 0.05
P_UNNECESSARY = 0.04
P_TOO_LONG = 0.02


def director_score(m: dict[str, Any]) -> tuple[float, list[Defect]]:
    """Score ``editorial_director.shot_plan_metrics`` output; returns (score, defects)."""
    defects: list[Defect] = []
    for s in m.get("sub_minimum_shots", []):
        defects.append(Defect("SHOT_BELOW_MINIMUM", "warning", f"{s['duration_sec']:.2f}s shot on {s['camera_id']} (minimum {m['min_shot_sec']:g}s)", s["at_sec"]))
    for s in m.get("noop_cuts", []):
        defects.append(Defect("UNNECESSARY_CUT", "warning", f"Cut from {s['camera_id']} to the same angle", s["at_sec"]))
    for s in m.get("interjection_cuts", []):
        defects.append(Defect("UNNECESSARY_CUT", "warning", f"Cut to {s['camera_id']} for a brief interjection and straight back", s["at_sec"]))
    for s in m.get("shots_too_long", []):
        defects.append(Defect("SHOT_TOO_LONG", "info", f"{s['duration_sec']:.1f}s static shot on {s['camera_id']}", s["at_sec"]))
    unavailable = float(m.get("unavailable_on_air_sec", 0.0))
    if unavailable > 0.04:
        defects.append(Defect("UNAVAILABLE_ANGLE_ON_AIR", "error", f"{unavailable:.1f}s of black/frozen/missing video on air", None))
    rate, ref = float(m.get("camera_cuts_per_min", 0.0)), float(m.get("ref_max_camera_cuts_per_min", 1e9))
    rate_pen = 0.0
    if rate > ref and m.get("edited_sec", 0.0) >= 30.0:
        rate_pen = min(0.15, 0.3 * (rate - ref) / ref)
        defects.append(Defect("CUT_RATE_HIGH", "warning", f"{rate:.1f} camera cuts/min (reference {ref:g}): choppy", None))
    cov = m.get("speaker_coverage")
    cov_pen = 0.0
    if cov is not None and cov < 0.6:
        cov_pen = 0.5 * (0.6 - float(cov))
        defects.append(Defect("SPEAKER_COVERAGE_LOW", "warning", f"Only {float(cov):.0%} of speech on the speaker, the wide or a reaction", None, subjective=True))
    penalty = (
        min(0.3, P_SUBMIN * len(m.get("sub_minimum_shots", [])))
        + min(0.2, P_UNNECESSARY * int(m.get("unnecessary_cuts", 0)))
        + min(0.1, P_TOO_LONG * len(m.get("shots_too_long", [])))
        + min(0.4, unavailable / 5.0)
        + rate_pen
        + cov_pen
    )
    return round(max(0.0, 1.0 - penalty), 4), defects


@register_assessor("multicam_director")
def assess_director(ctx: Any, outcome: Any) -> Assessment | None:
    from bangonpro.show_profiles.loader import get_effective_profile_settings

    st = ctx.state
    m = (st.metadata.get("camera_plan") or {}).get("provisional_metrics")
    if not m:
        return None
    score, defects = director_score(m)
    eff = get_effective_profile_settings(st.profile)
    correction: dict[str, Any] | None = None
    reason = ""
    if m.get("sub_minimum_shots") or any(d.code == "CUT_RATE_HIGH" for d in defects):
        correction = {"min_shot_duration_sec": round(min(30.0, float(eff["min_shot_duration_sec"]) + 0.5), 2)}
        reason = "avoidable sub-minimum shots / choppy cut rate"
    elif any(d.code == "SPEAKER_COVERAGE_LOW" for d in defects):
        correction = {"reaction_shot_frequency": round(max(0.0, float(eff["reaction_shot_frequency"]) - 0.2), 3)}
        reason = "too much speech covered by cutaways"
    return Assessment(agent_id="multicam_director", score=score, metric="director_cut_discipline", defects=defects, correction=correction, correction_reason=reason, details=m)


# ---------------------------------------------------------------------------
# QC checks and the Executive final verifier
# ---------------------------------------------------------------------------
for _name, _fn in editorial_qc.CHECKS.items():
    register_qc_check(_name)(_fn)


@register_final_verifier("brief_compliance")
def verify_brief(st: Any, job_doc: dict[str, Any]) -> dict[str, Any]:
    from bangonpro.ai_engine.department.editorial_brief import verify_draft_against_brief

    return verify_draft_against_brief(st, job_doc)


__all__ = ["assess_director", "assess_rough_cut", "director_score", "rough_cut_score", "verify_brief"]
