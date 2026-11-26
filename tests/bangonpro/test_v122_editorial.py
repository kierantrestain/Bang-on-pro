"""V1.2.2 editorial intelligence: metric validity and V1.2.1 comparison on identical scenarios.

Evidence class: unit/synthetic (constructed transcript states with hand-labelled ground truth,
department/editorial_eval.py). No real footage, no neural inference.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bangonpro.ai_engine.department import editorial_eval as ev

REPO = Path(__file__).resolve().parents[2]
EVIDENCE = REPO / "docs" / "evidence" / "v1.2.2" / "editorial_metrics.json"
# Ground-truth defects an editor would have to fix (lower is better).
DEFECT_KEYS = ("content_words_lost", "cuts_inside_words", "cuts_inside_segments")


def _fixes(gt: dict, shot: dict) -> float:
    pauses_lost = (gt["meaningful_pauses"] or 0) - (gt["meaningful_pauses_preserved"] or 0)
    disfl_left = (gt["disfluent_words"] or 0) - (gt["disfluent_words_removed"] or 0)
    dead = 1 if (gt["dead_air_left_sec"] or 0) > 1.0 else 0
    return pauses_lost + disfl_left + dead + sum(gt[k] or 0 for k in DEFECT_KEYS) + (shot.get("unnecessary_cuts") or 0) + (shot.get("sub_minimum_shots") or 0)


def test_assessors_rank_graded_variants_like_ground_truth() -> None:
    v = ev.assessor_validity()
    for agent in ("rough_cut", "multicam_director"):
        assert v[agent]["kendall_tau"] >= 0.8, (agent, v[agent])


@pytest.fixture(scope="module")
def comparison() -> dict:
    v121 = ev.load_v121_modules(str(REPO))
    if v121 is None:
        pytest.skip("git tag v1.2.1 not available in this checkout")
    rows = {}
    for name, spec in ev.scenarios().items():
        out = {}
        for ver in ("v1.2.1", "v1.2.2"):
            st = ev.build(name)
            r = ev.run_v122(st) if ver == "v1.2.2" else ev.run_v121(st, v121)
            gt = ev.ground_truth_metrics(st, r["kept"], spec["truth"])
            shot = ev.shot_truth_metrics(st, r["shots"], r["kept"], spec["truth"])
            out[ver] = {"ground_truth": gt, "shots": shot, "corrective_fixes_needed": _fixes(gt, shot)}
        rows[name] = out
    validity = ev.assessor_validity()
    doc = {
        "evidence_class": "unit/synthetic (constructed transcripts, hand-labelled ground truth; no footage, no inference)",
        "assessor_validity_kendall_tau": {k: v["kendall_tau"] for k, v in validity.items()},
        "corrective_fixes_total": {ver: sum(r[ver]["corrective_fixes_needed"] for r in rows.values()) for ver in ("v1.2.1", "v1.2.2")},
        "scenarios": rows,
    }
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(doc, indent=2, default=str) + "\n", encoding="utf-8")
    return doc


def test_v122_needs_fewer_editor_fixes_than_v121(comparison: dict) -> None:
    tot = comparison["corrective_fixes_total"]
    assert tot["v1.2.2"] < tot["v1.2.1"], tot


def test_v122_never_cuts_inside_words_or_loses_content(comparison: dict) -> None:
    for name, r in comparison["scenarios"].items():
        gt = r["v1.2.2"]["ground_truth"]
        assert gt["cuts_inside_words"] == 0 and gt["content_words_lost"] == 0, (name, gt)


def test_v122_preserves_meaningful_pauses_at_least_as_well(comparison: dict) -> None:
    for name, r in comparison["scenarios"].items():
        a, b = r["v1.2.2"]["ground_truth"], r["v1.2.1"]["ground_truth"]
        if a["meaningful_pauses"]:
            assert a["meaningful_pause_preservation"] >= b["meaningful_pause_preservation"], name
