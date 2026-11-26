"""V1.2.2 media processing: ground-truth sync (drift, dropout, late start), sync-metric validity,
duplicate and corrupt-media detection. Evidence class: synthetic (generated recordings with known
offsets, clock drift and dropouts; tests/bangonpro/scenarios_v122.py).

The V1.2.1 baseline is the unchanged single-window GCC-PHAT engine (``run_ingest_sync_agent``)
run on the identical state before the V1.2.2 multi-window refinement.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import copy
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scenarios_v122 as sv

from bangonpro.ai_engine.department import JobOptions, ProductionJob
from bangonpro.ai_engine.department.agents import HANDLERS, AgentContext
from bangonpro.ai_engine.department.cache import AnalysisCache
from bangonpro.ai_engine.orchestrator import CancellationToken

needs_media = pytest.mark.skipif(not sv.have_media_tools(), reason="ffmpeg/ffprobe required")
EVIDENCE = Path(__file__).resolve().parents[2] / "docs" / "evidence" / "v1.2.2" / "media_metrics.json"
FRAME_MS = 40.0


def _ctx(job: ProductionJob, agent: str) -> AgentContext:
    return AgentContext(agent, job.state, CancellationToken(), lambda f, m: None, AnalysisCache(), job.options(), job.plan)


def _kendall(a: list[float], b: list[float]) -> float:
    c = d = 0
    for i in range(len(a)):
        for j in range(i + 1, len(a)):
            s = (a[i] - a[j]) * (b[i] - b[j])
            c += s > 0
            d += s < 0
    return (c - d) / max(1, c + d)


@pytest.fixture(scope="module")
def sync_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    base = tmp_path_factory.mktemp("v122_media_sync")
    mp = pytest.MonkeyPatch()
    mp.setenv("BANGONPRO_CACHE_DIR", str(base / "cache"))
    mp.setenv("BANGONPRO_PREFS_DIR", str(base / "prefs"))
    sc = sv.drift_dropout(base, duration_sec=120.0)
    job = ProductionJob.create(job_root=base / "jobs", brief="First cut", inputs=sc.inputs(), profile_ref="podcast_multicam",
                               mode="autonomous_first_cut", options=JobOptions(), job_id="sync")
    for a in ("learning", "media_ingest"):
        HANDLERS[a](_ctx(job, a))
    from bangonpro.ai_engine.agents.ingest_sync import run_ingest_sync_agent
    from bangonpro.ai_engine.department.media_sync import refine_state_sync

    st = job.state
    t0 = time.perf_counter()
    run_ingest_sync_agent(st, CancellationToken(), lambda f, m: None)
    v121_sec = time.perf_counter() - t0
    v121 = copy.deepcopy(st.sync_result)
    t0 = time.perf_counter()
    refine_state_sync(st, AnalysisCache())
    v122_sec = time.perf_counter() - t0
    yield {"sc": sc, "st": st, "v121": v121, "v122": st.sync_result, "v121_sec": v121_sec, "refine_sec": v122_sec}
    mp.undo()


def _errors_ms(sr, truth: dict, cams: list[str], duration: float) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for cam in cams:
        ct = truth["cameras"][cam]
        per = {}
        for label, frac in (("start", 0.05), ("middle", 0.5), ("end", 0.9)):
            t = duration * frac
            if sv.in_gap(ct, t, pad=0.5):
                t += 2.0
            true = sv.true_source_time(ct, t)
            if true is not None:
                per[label] = round(abs(sr.source_time(cam, t) - true) * 1000.0, 2)
        out[cam] = per
    return out


@needs_media
def test_v122_sync_recovers_drift_dropout_and_late_start_within_one_frame(sync_run: dict) -> None:
    truth = sync_run["sc"].truth
    cams = sorted(truth["cameras"])
    e121 = _errors_ms(sync_run["v121"], truth, cams, truth["duration_sec"])
    e122 = _errors_ms(sync_run["v122"], truth, cams, truth["duration_sec"])
    worst121 = max(v for p in e121.values() for v in p.values())
    worst122 = max(v for p in e122.values() for v in p.values())
    sr = sync_run["v122"]
    doc = {
        "evidence_class": "synthetic (120 s generated recordings: +80 ppm drift with 1.5 s dropout, -60 ppm, +35 ppm started 41 s late)",
        "sync_error_ms": {"v1.2.1": e121, "v1.2.2": e122},
        "worst_sync_error_ms": {"v1.2.1": worst121, "v1.2.2": worst122},
        "drift_ppm": {"truth": {c: truth["cameras"][c]["drift_ppm"] for c in cams}, "v1.2.2": dict(sr.drift_ppm)},
        "segments_detected": {c: len(sr.segment_offsets.get(c, [])) for c in cams},
        "processing_sec": {"v1.2.1_single_window": round(sync_run["v121_sec"], 2), "v1.2.2_refinement_extra": round(sync_run["refine_sec"], 2)},
    }
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    prev = json.loads(EVIDENCE.read_text(encoding="utf-8")) if EVIDENCE.is_file() else {}
    EVIDENCE.write_text(json.dumps({**prev, "sync": doc}, indent=2) + "\n", encoding="utf-8")
    assert worst122 <= FRAME_MS, e122
    assert worst121 > worst122, (e121, e122)
    for c in cams:
        assert abs(sr.drift_ppm.get(c, 0.0) - truth["cameras"][c]["drift_ppm"]) <= 10.0, (c, sr.drift_ppm)
    assert len(sr.segment_offsets.get("cam_a", [])) == 2  # the dropout is re-synced per segment


@needs_media
def test_sync_quality_score_falls_with_alignment_error(sync_run: dict) -> None:
    from bangonpro.ai_engine.department.media_intel import sync_quality

    st = sync_run["st"]
    base = copy.deepcopy(st.sync_result)
    errors_ms = [0.0, 10.0, 25.0, 80.0]
    scores = []
    for e in errors_ms:
        st.sync_result = copy.deepcopy(base)
        st.sync_result.offsets_sec["cam_b"] += e / 1000.0
        st.sync_result.segment_offsets.pop("cam_b", None)
        scores.append(sync_quality(st)[0])
    st.sync_result = base
    tau = _kendall(errors_ms, scores)
    prev = json.loads(EVIDENCE.read_text(encoding="utf-8")) if EVIDENCE.is_file() else {}
    prev.setdefault("assessor_validity_kendall_tau", {})["synchronisation"] = {"injected_error_ms": errors_ms, "scores": scores, "kendall_tau_vs_error": tau}
    EVIDENCE.write_text(json.dumps(prev, indent=2) + "\n", encoding="utf-8")
    assert tau == -1.0, scores  # strictly lower score for larger error


@needs_media
def test_duplicate_and_corrupt_sources_are_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BANGONPRO_CACHE_DIR", str(tmp_path / "cache"))
    sc = sv.duplicates_and_corrupt(tmp_path)
    job = ProductionJob.create(job_root=tmp_path / "jobs", brief="First cut", inputs=sc.inputs(), profile_ref="podcast_multicam",
                               mode="autonomous_first_cut", options=JobOptions(), job_id="dup")
    HANDLERS["learning"](_ctx(job, "learning"))
    out = HANDLERS["media_ingest"](_ctx(job, "media_ingest"))
    inv = job.state.metadata["media_inventory"]
    groups = [set(d.get("camera_ids") or d.get("members") or []) for d in inv["duplicates"]]
    found = {frozenset(p) for p in map(frozenset, sc.truth["duplicates"]) if any(p <= g for g in groups)}
    assert len(found) == len(sc.truth["duplicates"]), inv["duplicates"]
    assert not any(set(p) <= g for p in sc.truth["not_duplicates"] for g in groups)
    assert set(sc.truth["unreadable"]) <= set(inv["unreadable_camera_ids"])
    assert out.review_items  # actionable messages for the editor
