"""V1.3 Executive creative director: conversational revisions on real department jobs.

Generated 3-camera podcast footage (evidence class: synthetic); every assertion is made on the
draft the job actually emits (shots, kept intervals, Kdenlive project), not on agent responses.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bangonpro.ai_engine.department import JobOptions, ProductionJob
from bangonpro.ai_engine.department.revisions import interpret_revision

needs_media = pytest.mark.skipif(not all(shutil.which(t) for t in ("ffmpeg", "ffprobe", "melt")), reason="ffmpeg/ffprobe/melt required")
PROFILE = {"profile_id": "podcast_multicam", "format_type": "podcast", "reaction_shot_frequency": 0.2, "min_shot_duration_sec": 2.0}


# ---------------------------------------------------------------- interpretation
@pytest.mark.parametrize(
    ("text", "agent", "key"),
    [
        ("Tighten the opening but keep the natural conversation", "rough_cut", "editing_rules"),
        ("Use more reaction shots during this debate", "multicam_director", "reaction_shot_frequency"),
        ("Preserve the suspense before each gameshow reveal", "rough_cut", "editing_rules"),
        ("Add our existing graphics and lower thirds where appropriate", "graphics", "graphics_rules"),
        ("Create five strong standalone vertical clips from this episode", "editorial_intelligence", "distribution_rules"),
        ("Fewer camera cuts please", "multicam_director", "min_shot_duration_sec"),
    ],
)
def test_instructions_map_to_validated_plans(text: str, agent: str, key: str) -> None:
    from bangonpro.ai_engine.department.learning import validate_overlay

    plan = interpret_revision(text, PROFILE, duration_sec=600.0, selected_range=(100.0, 160.0) if "this debate" in text else None)
    assert plan["start_agent"] == agent and key in plan["overlay"] and not plan["not_understood"], plan
    validate_overlay(plan["overlay"])  # learnable keys only, bounded


def test_opening_range_gentle_tighten_and_clip_count() -> None:
    p = interpret_revision("Tighten the opening but keep the natural conversation", PROFILE, duration_sec=600.0)
    assert p["range_sec"] == [0.0, 90.0]
    assert 0.8 < p["overlay"]["editing_rules"]["natural_pause_sec"] / 0.6 < 0.9  # gentle, not the full tighten
    assert interpret_revision("Create five strong standalone vertical clips", PROFILE)["overlay"]["distribution_rules"]["social_clip_count"] == 5


def test_unclear_or_unselected_requests_are_not_guessed() -> None:
    assert interpret_revision("make it pop, you know", PROFILE)["not_understood"]
    p = interpret_revision("Improve the pacing of this selected section", PROFILE)
    assert p["needs_selection"] and p["range_sec"] is None


# ---------------------------------------------------------------- real jobs
@pytest.fixture(scope="module")
def job(tmp_path_factory: pytest.TempPathFactory) -> ProductionJob:
    import bench_v122_compare as bc

    base = tmp_path_factory.mktemp("v13_director")
    mp = pytest.MonkeyPatch()
    mp.setenv("BANGONPRO_CACHE_DIR", str(base / "cache"))
    mp.setenv("BANGONPRO_PREFS_DIR", str(base / "prefs"))
    cams = bc.generate_podcast_conversation(base)
    j = ProductionJob.create(job_root=base / "jobs", brief="Edit this podcast into a complete YouTube episode", inputs=[f"cam_{r}={p}" for r, p in cams.items()],
                             profile_ref="podcast_multicam", mode="full_production_draft", options=JobOptions(), job_id="ep")
    j.run()
    yield j
    mp.undo()


def _spans(j: ProductionJob) -> list[tuple[float, float, str]]:
    st = j._latest_state()
    return [(round(s.master_sync_in_sec, 2), round(s.master_sync_out_sec, 2), s.camera_id) for s in st.rough_cut_shots]


def _on_screen(spans: list[tuple[float, float, str]], a: float, b: float) -> list[str | None]:
    """Camera on screen (or None when cut) every 0.1 s of the master clock in [a, b)."""
    out = []
    for k in range(int(round((b - a) * 10))):
        t = a + k / 10 + 0.05
        out.append(next((c for s, e, c in spans if s <= t < e), None))
    return out


def _outside(spans: list[tuple[float, float, str]], a: float, b: float) -> list[tuple[float, float, str]]:
    return [s for s in spans if s[1] <= a or s[0] >= b]


@needs_media
def test_range_revision_changes_only_the_range_and_reuses_analysis(job: ProductionJob) -> None:
    before = _spans(job)
    attempts_before = {a: r.get("attempts", 0) for a, r in job.doc["agents"].items()}
    rec = job.revise("Tighten this section", range_sec=(20.0, 32.0))
    assert rec["status"] == "draft_ready" and rec["rerun_from"] == "rough_cut", rec
    after = _spans(job)
    assert _outside(after, 20.0, 32.0) == _outside(before, 20.0, 32.0)  # untouched outside the range
    for a in ("media_ingest", "synchronisation", "editorial_intelligence", "multicam_director"):
        assert job.doc["agents"][a].get("attempts", 0) == attempts_before[a], a  # no unrelated rerun
    assert Path(job.doc["artifacts"]["kdenlive_project"]).is_file()
    assert job.doc["source_integrity"]["verified"] is True


@needs_media
def test_locked_range_survives_a_whole_project_revision_and_undo_restores(job: ProductionJob) -> None:
    job.lock_range(3.0, 15.0)
    before = _spans(job)
    rec = job.revise("Fewer camera cuts please")
    assert rec["status"] == "draft_ready" and rec["rerun_from"] == "multicam_director"
    after = _spans(job)
    assert _on_screen(after, 3.0, 15.0) == _on_screen(before, 3.0, 15.0)  # same picture, same kept time
    job.undo_revision()
    assert _spans(job) == before


@needs_media
def test_unclear_revision_changes_nothing(job: ProductionJob) -> None:
    before = _spans(job)
    rec = job.revise("make it pop, you know")
    assert rec["status"] == "not_understood" and _spans(job) == before
    assert job.revise("Improve the pacing of this selected section")["status"] == "needs_selection"


# ---------------------------------------------------------------- vertical clips
def test_reframe_holds_stable_subject_tracks_moving_one_and_reports_missing_evidence() -> None:
    from types import SimpleNamespace

    from bangonpro.ai_engine.department.reframe import MAX_SPEED, plan_vertical_clip

    shot = SimpleNamespace(shot_id="s1", camera_id="cam_wide", speaker_id="Host", master_sync_in_sec=0.0, master_sync_out_sec=10.0,
                           audio_media_ref="media_cam_host", audio_source_in_sec=0.0)
    assets = {c: SimpleNamespace(has_video=True, camera_role=r, width=1920, height=1080) for c, r in (("cam_wide", "wide"), ("cam_host", "host"))}
    def st(samples):  # noqa: ANN001, ANN202
        return SimpleNamespace(rough_cut_shots=[shot], media_assets=assets, profile={"speaker_camera_map": {"Host": "cam_host"}, "camera_roles": {"cam_wide": "wide", "cam_host": "host"}},
                               metadata={"visual_analysis": {"cam_host": {"samples": samples}}})
    stable = st({"t": [0, 2, 4, 6, 8], "box": [[0.30, 0.2, 0.40, 0.8]] * 5})
    p = plan_vertical_clip(stable, {"start_sec": 0.0, "end_sec": 10.0})["pieces"][0]
    assert p["camera_id"] == "cam_host" and p["swapped_to_close_up"]  # close-up instead of the wide
    assert p["framing"] == "held on subject" and len(p["keyframes"]) == 1 and abs(p["keyframes"][0][1] - 0.35) < 1e-6
    moving = st({"t": [0, 2, 4, 6, 8], "box": [[x, 0.2, x + 0.1, 0.8] for x in (0.1, 0.3, 0.5, 0.7, 0.8)]})
    kf = plan_vertical_clip(moving, {"start_sec": 0.0, "end_sec": 10.0})["pieces"][0]["keyframes"]
    assert len(kf) >= 3 and all(abs(b[1] - a[1]) <= MAX_SPEED * (b[0] - a[0]) + 1e-6 for a, b in zip(kf, kf[1:], strict=False))
    none = plan_vertical_clip(st({"t": [], "box": []}), {"start_sec": 0.0, "end_sec": 10.0})
    assert none["uncertain_sec"] == 10.0 and none["pieces"][0]["keyframes"] == [(0.0, 0.5)]


@needs_media
def test_vertical_clips_revision_emits_editable_projects_and_checked_renders(job: ProductionJob) -> None:
    import json

    rec = job.revise("Create three strong standalone vertical clips from this episode")
    assert rec["status"] == "draft_ready" and rec["rerun_from"] == "editorial_intelligence", rec
    manifest = json.loads(Path(job.doc["artifacts"]["delivery_manifest_json"]).read_text(encoding="utf-8"))
    clips = [d for d in manifest["deliverables"] if d["kind"] == "vertical_clip"]
    assert 1 <= len(clips) <= 3
    for c in clips:
        xml = Path(c["project"]).read_text(encoding="utf-8")
        assert 'width="1080"' in xml and 'height="1920"' in xml and "qtblend" in xml  # editable native vertical timeline
        r = c["render"]
        assert (r["width"], r["height"]) == ((360, 640) if r["proxy"] else (1080, 1920)) and r["audio"] and abs(r["duration_sec"] - c["duration_sec"]) <= 0.5, r
        assert r["black_sec"] <= 0.3
    assert job.doc["source_integrity"]["verified"] is True
