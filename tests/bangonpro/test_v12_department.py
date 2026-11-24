"""V1.2 AI Production Department regression & acceptance tests.

Real execution on synthetic (generated, legally unencumbered) 4-camera footage:
ffprobe, GCC-PHAT sync, VAD diarisation, rough cut, colour matching, masking,
QC, melt render and FFmpeg derivatives all run for real. Evidence class: synthetic.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ai_production import _generate_synthetic_4cam_studio

from bangonpro.ai_engine.department import (
    AGENT_ORDER,
    ApprovalRequiredError,
    JobOptions,
    ProductionJob,
    ProfilePreferenceStore,
    capability_matrix,
    compute_activation,
    parse_brief_flags,
)
from bangonpro.ai_engine.department.learning import (
    PreferenceError,
    suggest_from_decisions,
    validate_overlay,
)
from bangonpro.ai_engine.department.measurement import (
    MeasurementLog,
    modelled_estimate,
    savings_report,
)
from bangonpro.ai_engine.department.registry import AGENTS_BY_ID, transitive_dependents
from bangonpro.ai_engine.state import ProductionIntent
from bangonpro.show_profiles.loader import list_show_profiles, load_show_profile

HAVE_MEDIA_TOOLS = all(shutil.which(t) for t in ("ffmpeg", "ffprobe", "melt"))
needs_media = pytest.mark.skipif(not HAVE_MEDIA_TOOLS, reason="ffmpeg/ffprobe/melt required")
BRIEF = "Build the full episode, match the camera colours, blur the background behind the host and make social clips"


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    base = tmp_path_factory.mktemp("v12_department")
    mp = pytest.MonkeyPatch()
    mp.setenv("BANGONPRO_CACHE_DIR", str(base / "cache"))
    mp.setenv("BANGONPRO_PREFS_DIR", str(base / "prefs"))
    cams = _generate_synthetic_4cam_studio(base) if HAVE_MEDIA_TOOLS else {}
    yield {"base": base, **cams}
    mp.undo()


def _inputs(env: dict[str, Path]) -> list[str]:
    return [f"{k}={v}" for k, v in env.items() if k.startswith("cam_")]


@pytest.fixture(scope="module")
def full_job(env: dict[str, Path]) -> ProductionJob:
    job = ProductionJob.create(
        job_root=env["base"] / "jobs", brief=BRIEF, inputs=_inputs(env), profile_ref="podcast_multicam",
        mode="full_production_draft", options=JobOptions(export_video=True, episode_title="Episode 12"), job_id="full",
    )
    job.run()
    return job


# ---------------------------------------------------------------- registry
def test_registry_has_14_agents_in_dependency_order() -> None:
    matrix = capability_matrix()
    assert len(matrix) == 14 and len(AGENT_ORDER) == 14
    seen: set[str] = set()
    for a in AGENT_ORDER:
        assert set(AGENTS_BY_ID[a].depends_on) <= seen, f"{a} runs before a dependency"
        seen.add(a)
    assert "quality_control" in transitive_dependents("rough_cut")
    assert {m["agent_id"] for m in matrix} >= {"executive", "learning", "colourist", "vfx", "distribution"}


def test_activation_respects_modes_and_profile() -> None:
    prof = load_show_profile("podcast_multicam")
    narrow = ProductionIntent(raw_instruction="normalise audio", restore_audio=True)
    manual = compute_activation(narrow, "manual", {}, prof, n_inputs=4, n_video=4)
    assert [a for a, v in manual.items() if v["active"]] == ["executive"]
    assisted = compute_activation(narrow, "assisted", {}, prof, n_inputs=4, n_video=4)
    assert assisted["audio_engineer"]["active"] and not assisted["colourist"]["active"] and not assisted["vfx"]["active"]
    flags = parse_brief_flags(BRIEF)
    assert flags["colour"] and flags["vfx"] and flags["social"]
    full = compute_activation(ProductionIntent.full_production(), "full_production_draft", flags, prof, n_inputs=4, n_video=4)
    assert all(v["active"] for v in full.values())
    prof2 = dict(prof, agent_config={"disabled_agents": ["graphics"]})
    off = compute_activation(ProductionIntent.full_production(), "full_production_draft", flags, prof2, n_inputs=4, n_video=4, overrides={"vfx": False})
    assert not off["graphics"]["active"] and not off["vfx"]["active"]


def test_all_profiles_load_with_v12_sections() -> None:
    ids = set(list_show_profiles())
    assert {"podcast_multicam", "gameshow_multicam", "sports_discussion", "daily_news", "interview_two_camera", "social_short"} <= ids
    for pid in ids:
        prof = load_show_profile(pid)
        for key in ("colour_rules", "distribution_rules", "agent_config"):
            assert key in prof, f"{pid} missing {key}"


# ---------------------------------------------------------------- end-to-end
@needs_media
def test_full_production_draft_end_to_end(full_job: ProductionJob) -> None:
    d = full_job.doc
    assert d["status"] == "draft_ready"
    assert all(r["status"] == "completed" for r in d["agents"].values()), {a: r["status"] for a, r in d["agents"].items()}
    assert d["source_integrity"]["verified"] is True
    # Honest neural/fallback reporting: VAD-only transcript -> captions withheld, not faked
    cap = d["agents"]["captions"]
    assert cap["neural_inference"] is False and cap["operation_count"] == 0
    assert d["agents"]["editorial_intelligence"]["neural_inference"] is False
    kd = Path(d["artifacts"]["kdenlive_project"]).read_text(encoding="utf-8")
    assert "lift_gamma_gain" in kd and "mask_start" in kd and "boxblur" in kd and "kdenlive:bangonpro_agent" in kd
    assert d["artifacts"]["unresolved_effects"] == "0"
    cmds = json.loads(Path(d["artifacts"]["timeline_commands_json"]).read_text(encoding="utf-8"))
    revs = [b["expected_revision"] for b in cmds["batches"]]
    assert revs == list(range(revs[0], revs[0] + len(revs)))
    assert set(cmds["batch_agents"].values()) >= {"synchronisation", "rough_cut", "colourist", "vfx", "audio_engineer", "graphics"}
    manifest = json.loads(Path(d["artifacts"]["delivery_manifest_json"]).read_text(encoding="utf-8"))
    kinds = {x["kind"]: x for x in manifest["deliverables"]}
    assert kinds["master"]["loudness_ok"] is True, kinds["master"]
    assert kinds["social_9x16"]["status"] == "rendered"
    assert manifest["publish_status"].startswith("not_published")
    assert (full_job.dir / "summary.md").read_text(encoding="utf-8").startswith("# Production summary")
    assert len(list((full_job.dir / "checkpoints").glob("*.json"))) == 15


@needs_media
def test_draft_renders_natively_with_melt(full_job: ProductionJob, tmp_path: Path) -> None:
    out = tmp_path / "draft.mp4"
    res = subprocess.run(["melt", full_job.doc["artifacts"]["kdenlive_project"], "-consumer", f"avformat:{out}", "vcodec=libx264", "acodec=aac"], capture_output=True, text=True, timeout=600, check=False)
    assert res.returncode == 0 and out.stat().st_size > 10_000


@needs_media
def test_second_job_reuses_shared_analysis(env: dict[str, Path], full_job: ProductionJob) -> None:
    # Same footage + same editorial scope -> transcript/diarisation reused; user disables colour & VFX
    job = ProductionJob.create(job_root=env["base"] / "jobs", brief=BRIEF, inputs=_inputs(env), profile_ref="podcast_multicam", mode="autonomous_first_cut", options=JobOptions(enabled_overrides={"colourist": False, "vfx": False}), job_id="reuse")
    job.run()
    hits = job.doc["agents"]["editorial_intelligence"]["cache_hits"] + job.doc["agents"]["media_ingest"]["cache_hits"]
    assert "editorial:transcript" in hits and any(h.startswith("sha256:") for h in hits)
    assert not job.doc["agents"]["colourist"]["active"] and not job.doc["agents"]["vfx"]["active"]
    assert job.doc["status"] == "draft_ready"


@needs_media
def test_reject_is_dependency_aware_and_rerun_refines(full_job: ProductionJob) -> None:
    full_job.decide("rough_cut", "reject", note="pacing wrong")
    excluded, stale = full_job.excluded_agents()
    assert {"rough_cut", "colourist", "graphics", "vfx", "audio_engineer"} <= excluded
    assert {"quality_control", "distribution"} <= stale
    cmds = json.loads(Path(full_job.doc["artifacts"]["timeline_commands_json"]).read_text(encoding="utf-8"))
    assert "rough_cut" not in cmds["batch_agents"].values()
    full_job.decide("rough_cut", "accept")
    assert not full_job.excluded_agents()[0]
    full_job.rerun("colourist", profile_overrides={"colour_rules": {"correct_exposure": False}})
    assert full_job.doc["agents"]["colourist"]["attempts"] == 2
    assert "exposure x1.00" in full_job.doc["agents"]["colourist"]["summary"]
    assert full_job.doc["status"] == "draft_ready"
    corrections = ProfilePreferenceStore("podcast_multicam").corrections()
    assert any(c["agent_id"] == "rough_cut" and c["decision"] == "rejected" for c in corrections)


@needs_media
def test_cancel_then_resume(env: dict[str, Path]) -> None:
    job = ProductionJob.create(job_root=env["base"] / "jobs", brief="first cut", inputs=_inputs(env), profile_ref="podcast_multicam", mode="autonomous_first_cut", job_id="cancel")
    calls = {"n": 0}

    def _cb(agent: str, frac: float, msg: str) -> None:
        calls["n"] += 1
        if agent == "synchronisation":
            job.cancel("test interruption")

    job.run(progress_callback=_cb)
    assert job.doc["status"] == "cancelled"
    assert job.doc["agents"]["media_ingest"]["status"] == "completed"
    job2 = ProductionJob(job.dir)
    job2.resume()
    assert job2.doc["status"] == "draft_ready"
    assert job2.doc["agents"]["media_ingest"]["attempts"] == 1  # completed work not redone


@needs_media
def test_missing_media_and_insufficient_storage(env: dict[str, Path]) -> None:
    inputs = _inputs(env)[:2] + ["cam_guest=/nonexistent/guest.mp4"]
    job = ProductionJob.create(job_root=env["base"] / "jobs", brief="first cut", inputs=inputs, profile_ref="podcast_multicam", mode="autonomous_first_cut", job_id="missing")
    job.run()
    ing = job.doc["agents"]["media_ingest"]
    assert ing["status"] == "completed" and any("Missing media" in r for r in ing["review_items"])
    qc = json.loads(Path(job.doc["artifacts"]["qc_report_json"]).read_text(encoding="utf-8"))
    assert any(i["code"] == "MISSING_MEDIA" for i in qc["issues"])
    big = ProductionJob.create(job_root=env["base"] / "jobs", brief="first cut", inputs=_inputs(env), profile_ref="podcast_multicam", mode="autonomous_first_cut", options=JobOptions(min_free_bytes=1 << 62), job_id="nospace")
    big.run()
    assert big.doc["status"] == "blocked_insufficient_storage"


@needs_media
def test_promote_requires_approval_to_replace(full_job: ProductionJob, tmp_path: Path) -> None:
    target = tmp_path / "episode.kdenlive"
    rec = full_job.promote(target)
    assert target.exists() and not rec["replaced_existing"]
    with pytest.raises(ApprovalRequiredError):
        full_job.promote(target)
    rec2 = full_job.promote(target, approve=True)
    assert rec2["replaced_existing"] and Path(rec2["backup"]).exists()
    src = next(iter(full_job.state.input_camera_files.values()))
    with pytest.raises(PermissionError):
        full_job.promote(src, approve=True)


@needs_media
def test_assisted_mode_stages_pending_proposals(env: dict[str, Path]) -> None:
    job = ProductionJob.create(job_root=env["base"] / "jobs", brief="Sync the cameras and normalise the audio to -16 LUFS", inputs=_inputs(env), profile_ref="podcast_multicam", mode="assisted", job_id="assisted")
    job.run()
    assert job.doc["status"] == "awaiting_review"
    decisions = {m["decision"] for m in job.doc["batches"].values()}
    assert decisions == {"pending"}
    assert not job.doc["agents"]["colourist"]["active"]


# ---------------------------------------------------------------- learning
def test_learning_versioning_and_guard_rails(tmp_path: Path) -> None:
    store = ProfilePreferenceStore("podcast_multicam", "weekly_show", tmp_path)
    prof = load_show_profile("podcast_multicam")
    decisions = [{"camera_overrides": [{"from_role": "reaction", "to_role": "host"}] * 4}]
    sugg = store.propose(suggest_from_decisions(prof, decisions))
    assert sugg and sugg[0]["category"] == "camera_choice"
    v1 = store.decide_suggestion(sugg[0]["suggestion_id"], approve=True)
    assert v1 == 1 and store.active_version() == 1
    merged, _overlay = store.apply_to_profile(prof)
    assert merged["reaction_shot_frequency"] < prof["reaction_shot_frequency"]
    v2 = store.commit_overlay({"loudness_target_lufs": -100.0}, reason="out of range")
    assert store.load_version(v2)["overlay"]["loudness_target_lufs"] == -31.0  # clamped
    assert store.rollback(1) == 1 and store.reset() == 0
    assert store.apply_to_profile(prof)[1] == {}
    with pytest.raises(PreferenceError):
        validate_overlay({"requires_approval": False})
    with pytest.raises(PreferenceError):
        validate_overlay({"editing_rules": {"approval_threshold": 0}})


# ---------------------------------------------------------------- measurement
def test_measurement_formula_and_honest_claims(tmp_path: Path) -> None:
    log = MeasurementLog(tmp_path)
    assert savings_report(log.sessions())["claim_status"].startswith("NOT MEASURED")
    t0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)

    def _session(kind: str, minutes: int) -> None:
        sid = log.start(deliverable_id="ep1", kind=kind, editor="ed", evidence_class="synthetic_test")
        doc = json.loads(log.path.read_text())
        doc["sessions"][-1]["started_at_utc"] = t0.isoformat()
        log.path.write_text(json.dumps(doc))
        for m in range(1, minutes + 1):
            log.activity(sid, "editing", at_utc=(t0 + timedelta(minutes=m)).isoformat())
        log.finish(sid, quality_rating=4.0, at_utc=(t0 + timedelta(minutes=minutes, seconds=30)).isoformat())

    _session("manual_baseline", 100)
    _session("agent_assisted", 30)
    rep = savings_report(log.sessions())
    row = rep["rows"][0]
    assert row["status"] == "measured"
    assert row["human_time_saved_pct"] == pytest.approx((6030 - 1830) / 6030 * 100, abs=0.1)
    assert row["evidence_class"] == ["synthetic_test"]
    est = modelled_estimate({"batches": {"b": {"decision": "auto_accepted", "operation_types": {"place_clip": 10}}}})
    assert est["label"].startswith("MODELLED ESTIMATE")


def test_cli_agents_and_plan_only(tmp_path: Path) -> None:
    out = subprocess.run([sys.executable, "-m", "bangonpro.ai_engine.department", "agents"], capture_output=True, text=True, check=True)
    assert len(json.loads(out.stdout)["agents"]) == 14
    out = subprocess.run([sys.executable, "-m", "bangonpro.ai_engine.department", "run", "make a first cut", "--mode", "manual", "--job-root", str(tmp_path), "--job-id", "m"], capture_output=True, text=True, check=True)
    payload = json.loads(out.stdout)
    assert payload["status"] == "planned_manual"
    assert [a for a, r in payload["agents"].items() if r["active"]] == ["executive"]
