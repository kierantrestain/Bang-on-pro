"""V1.2.2 bounded quality feedback loop: safety, accounting and anti-gaming tests.

Real department jobs on synthetic 4-camera footage (evidence class: synthetic/unit).
Assessors here are test doubles registered for the duration of one test; the
production assessors live in department/editorial_intel.py and media_intel.py.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bangonpro.ai_engine.department import JobOptions, ProductionJob
from bangonpro.ai_engine.department import jobs as jobs_mod
from bangonpro.ai_engine.department import quality_loop as ql
from bangonpro.ai_engine.state import QCIssue

HAVE_MEDIA_TOOLS = all(shutil.which(t) for t in ("ffmpeg", "ffprobe", "melt"))
needs_media = pytest.mark.skipif(not HAVE_MEDIA_TOOLS, reason="ffmpeg/ffprobe/melt required")


# ---------------------------------------------------------------- pure acceptance rules
def _a(score: float, *defects: ql.Defect) -> ql.Assessment:
    return ql.Assessment("rough_cut", score, "m", list(defects))


def test_swapped_critical_defect_is_rejected_even_with_equal_error_count() -> None:
    before = _a(0.5, ql.Defect("CUT_IN_WORD", "error", "cut inside a word", 4.0))
    after = _a(0.9, ql.Defect("CUT_IN_WORD", "error", "cut inside a word", 9.0))
    assert before.error_count == after.error_count == 1
    ok, verdict = ql.accept_correction(before, after)
    assert not ok and "new critical defect" in verdict


def test_same_defect_within_tolerance_is_not_new() -> None:
    before = _a(0.5, ql.Defect("CUT_IN_WORD", "error", "x", 4.0))
    after = _a(0.6, ql.Defect("CUT_IN_WORD", "error", "x", 4.3))
    assert ql.new_critical_defects(before, after) == []
    assert ql.accept_correction(before, after)[0]


def test_new_warning_does_not_block_but_new_error_code_does() -> None:
    before = _a(0.5)
    assert ql.accept_correction(before, _a(0.6, ql.Defect("PACING", "warning", "slow", 2.0)))[0]
    assert not ql.accept_correction(before, _a(0.9, ql.Defect("LOST_CONTEXT", "error", "answer without question")))[0]


def test_independent_guard_blocks_score_gaming() -> None:
    g_before = [QCIssue("MISSING_CONTEXT", "warning", "w")]
    g_after = [QCIssue("MISSING_CONTEXT", "error", "e")]
    ok, verdict = ql.accept_correction(_a(0.2), _a(0.99), guard_before=g_before, guard_after=g_after)
    assert not ok and "independent QC guard" in verdict


def test_score_gain_below_threshold_is_rejected() -> None:
    assert not ql.accept_correction(_a(0.50), _a(0.51))[0]


# ---------------------------------------------------------------- job integration
@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    from test_ai_production import _generate_synthetic_4cam_studio

    base = tmp_path_factory.mktemp("ql_media")
    return {k: v for k, v in _generate_synthetic_4cam_studio(base).items() if k.startswith("cam_")}


@pytest.fixture()
def isolated_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BANGONPRO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("BANGONPRO_PREFS_DIR", str(tmp_path / "prefs"))
    ql.ensure_extensions_loaded()
    saved = (dict(ql.ASSESSORS), dict(ql.QC_CHECKS), dict(ql.FINAL_VERIFIERS))
    ql.ASSESSORS.clear()
    yield tmp_path
    ql.ASSESSORS.clear()
    ql.ASSESSORS.update(saved[0])
    ql.QC_CHECKS.clear()
    ql.QC_CHECKS.update(saved[1])
    ql.FINAL_VERIFIERS.clear()
    ql.FINAL_VERIFIERS.update(saved[2])


def _job(base: Path, media: dict[str, Path], job_id: str, budget: float = 0.0) -> ProductionJob:
    return ProductionJob.create(
        job_root=base / "jobs", brief="Tight cut", inputs=[f"{k}={v}" for k, v in media.items()],
        profile_ref="podcast_multicam", mode="autonomous_first_cut", options=JobOptions(budget_usd=budget), job_id=job_id,
    )


def _with_cost(monkeypatch: pytest.MonkeyPatch, agent_id: str, cost: float) -> None:
    orig = jobs_mod.HANDLERS[agent_id]

    def wrapped(ctx):  # type: ignore[no-untyped-def]
        out = orig(ctx)
        out.est_external_cost_usd = cost
        return out

    monkeypatch.setitem(jobs_mod.HANDLERS, agent_id, wrapped)


@needs_media
def test_rejected_attempt_time_and_cost_are_charged(isolated_hooks: Path, media: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    @ql.register_assessor("rough_cut")
    def _assess(ctx, out):  # type: ignore[no-untyped-def]
        longer = float(ctx.state.profile.get("min_shot_duration_sec", 0)) >= 3.0
        # The "correction" scores higher but introduces a critical defect at a new place.
        d = [ql.Defect("CUT_IN_WORD", "error", "cut inside a word", 9.0 if longer else 4.0)]
        return ql.Assessment("rough_cut", 0.95 if longer else 0.4, "test", d, correction=None if longer else {"min_shot_duration_sec": 3.0})

    _with_cost(monkeypatch, "rough_cut", 0.05)
    job = _job(isolated_hooks, media, "charged", budget=1.0)
    doc = job.run()
    rec = doc["agents"]["rough_cut"]
    attempt = rec["quality_loop"]["attempts"][0]
    assert attempt["accepted"] is False and "new critical defect" in attempt["verdict"]
    assert job.state.profile.get("min_shot_duration_sec") != 3.0  # original output kept
    assert rec["quality_score"] == pytest.approx(0.4)
    assert doc["budget"]["spent_usd"] == pytest.approx(0.10)  # first run + rejected attempt
    assert doc["quality_loop_cost_usd"] == pytest.approx(0.05)
    assert doc["quality_loop_processing_sec"] > 0
    assert attempt["duration_sec"] > 0 and rec["duration_sec"] >= attempt["duration_sec"]
    assert doc["processing_elapsed_sec"] >= doc["quality_loop_processing_sec"]


@needs_media
def test_attempt_skipped_when_it_would_exceed_budget(isolated_hooks: Path, media: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    @ql.register_assessor("rough_cut")
    def _assess(ctx, out):  # type: ignore[no-untyped-def]
        return ql.Assessment("rough_cut", 0.4, "test", [], correction={"min_shot_duration_sec": 3.0})

    _with_cost(monkeypatch, "rough_cut", 0.05)
    doc = _job(isolated_hooks, media, "budget", budget=0.06).run()
    att = doc["agents"]["rough_cut"]["quality_loop"]["attempts"]
    assert len(att) == 1 and att[0]["verdict"].startswith("skipped: projected external cost")
    assert doc["budget"]["spent_usd"] == pytest.approx(0.05)


@needs_media
def test_guard_rejects_correction_that_games_the_score(isolated_hooks: Path, media: dict[str, Path]) -> None:
    @ql.register_assessor("rough_cut")
    def _assess(ctx, out):  # type: ignore[no-untyped-def]
        longer = float(ctx.state.profile.get("min_shot_duration_sec", 0)) >= 3.0
        return ql.Assessment("rough_cut", 0.99 if longer else 0.3, "test", [], correction=None if longer else {"min_shot_duration_sec": 3.0})

    @ql.register_qc_check("test_guard")
    def _guard(st):  # type: ignore[no-untyped-def]
        bad = float(st.profile.get("min_shot_duration_sec", 0)) >= 3.0
        return [QCIssue("MISSING_CONTEXT", "error", "answer kept without its question")] if bad else []

    job = _job(isolated_hooks, media, "guard")
    doc = job.run()
    att = doc["agents"]["rough_cut"]["quality_loop"]["attempts"][0]
    assert att["accepted"] is False and "independent QC guard" in att["verdict"]
    assert job.state.profile.get("min_shot_duration_sec") != 3.0


@needs_media
def test_failed_correction_keeps_original_output(isolated_hooks: Path, media: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    orig = jobs_mod.HANDLERS["rough_cut"]

    def flaky(ctx):  # type: ignore[no-untyped-def]
        if float(ctx.state.profile.get("min_shot_duration_sec", 0)) >= 3.0:
            raise RuntimeError("synthetic failure")
        return orig(ctx)

    monkeypatch.setitem(jobs_mod.HANDLERS, "rough_cut", flaky)

    @ql.register_assessor("rough_cut")
    def _assess(ctx, out):  # type: ignore[no-untyped-def]
        return ql.Assessment("rough_cut", 0.4, "test", [], correction={"min_shot_duration_sec": 3.0})

    doc = _job(isolated_hooks, media, "flaky").run()
    rec = doc["agents"]["rough_cut"]
    assert rec["status"] == "completed" and rec["operation_count"] > 0
    assert "correction run failed" in rec["quality_loop"]["attempts"][0]["verdict"]


@needs_media
def test_accepted_correction_is_bounded_and_audited(isolated_hooks: Path, media: dict[str, Path]) -> None:
    @ql.register_assessor("rough_cut")
    def _assess(ctx, out):  # type: ignore[no-untyped-def]
        mn = float(ctx.state.profile.get("min_shot_duration_sec", 0))
        # Always proposes another correction: the loop must stop at MAX_ATTEMPTS_PER_AGENT.
        return ql.Assessment("rough_cut", min(1.0, 0.2 + mn / 10), "test", [], correction={"min_shot_duration_sec": mn + 1.0})

    job = _job(isolated_hooks, media, "bounded")
    doc = job.run()
    loop = doc["agents"]["rough_cut"]["quality_loop"]
    assert len(loop["attempts"]) == ql.MAX_ATTEMPTS_PER_AGENT
    assert loop["corrections_applied"] == ql.MAX_ATTEMPTS_PER_AGENT
    events = (job.dir / "events.jsonl").read_text(encoding="utf-8")
    assert events.count("quality_correction_accepted") == ql.MAX_ATTEMPTS_PER_AGENT
    assert Path(doc["artifacts"]["kdenlive_project"]).is_file()
