"""V1.2.2 production-readiness verification.

Operational tests on synthetic, legally unencumbered 4-camera footage (real
ffprobe, sync, rough cut, melt and FFmpeg): agent activation and progress,
cancellation, resumption, reruns, isolated drafts, per-operation approvals,
rollback, source-media protection, save / reopen / relink, a worker killed
mid-job, low storage, an alternate volume, failed exports, the production
queue, the system check, sanitised diagnostics, measurement, learning transfer
and custom profiles. Evidence class: synthetic.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import csv
import errno
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ai_production import _generate_synthetic_4cam_studio

from bangonpro.ai_engine.department import agents as dept_agents
from bangonpro.ai_engine.department import (
    AGENT_ORDER,
    ApprovalRequiredError,
    JobOptions,
    JobQueue,
    ProductionJob,
    ProfilePreferenceStore,
)
from bangonpro.ai_engine.department.agents import InsufficientStorageError
from bangonpro.ai_engine.department.cache import AnalysisCache
from bangonpro.ai_engine.department.diagnostics import export_diagnostics
from bangonpro.ai_engine.department.errors import explain
from bangonpro.ai_engine.department.learning import (
    PreferenceError,
    detect_recurring_preferences,
    diff_job_against_final,
    export_preference_bundle,
    import_preference_bundle,
)
from bangonpro.ai_engine.department.measurement import (
    CSV_COLUMNS,
    MeasurementLog,
    active_seconds,
    savings_report,
)
from bangonpro.ai_engine.department.queue import WorkerBusyError, job_history
from bangonpro.ai_engine.department.readiness import check_storage, check_system
from bangonpro.ai_engine.department.version import APP_VERSION
from bangonpro.show_profiles.loader import list_show_profiles, load_show_profile

REPO = Path(__file__).resolve().parents[2]
HAVE_MEDIA_TOOLS = all(shutil.which(t) for t in ("ffmpeg", "ffprobe", "melt"))
needs_media = pytest.mark.skipif(not HAVE_MEDIA_TOOLS, reason="ffmpeg/ffprobe/melt required")
BRIEF = "Make a first cut of the episode"


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _cli(*args: str, env: dict[str, str] | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "bangonpro.ai_engine.department", *args], capture_output=True, text=True, cwd=REPO, env=env, check=check, timeout=600)


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    base = tmp_path_factory.mktemp("v122_readiness")
    mp = pytest.MonkeyPatch()
    for var, sub in (("BANGONPRO_CACHE_DIR", "cache"), ("BANGONPRO_PREFS_DIR", "prefs"), ("BANGONPRO_PROFILES_DIR", "profiles"), ("BANGONPRO_QUEUE_DIR", "queue")):
        mp.setenv(var, str(base / sub))
    cams = _generate_synthetic_4cam_studio(base) if HAVE_MEDIA_TOOLS else {}
    yield {"base": base, "cams": cams, "hashes": {k: _sha(v) for k, v in cams.items()}}
    mp.undo()


def _inputs(cams: dict[str, Path]) -> list[str]:
    return [f"{k}={v}" for k, v in cams.items()]


def _job(env: dict, job_id: str, *, mode: str = "autonomous_first_cut", cams: dict | None = None, root: Path | None = None, **opts) -> ProductionJob:
    return ProductionJob.create(
        job_root=root or env["base"] / "jobs", brief=opts.pop("brief", BRIEF), inputs=_inputs(cams or env["cams"]), profile_ref=opts.pop("profile", "podcast_multicam"),
        mode=mode, options=JobOptions(**opts), job_id=job_id,
    )


def _sources_unchanged(env: dict) -> bool:
    return all(_sha(p) == env["hashes"][k] for k, p in env["cams"].items())


@pytest.fixture(scope="module")
def base_job(env: dict) -> ProductionJob:
    job = _job(env, "base")
    job.run()
    return job


# ------------------------------------------------------------------ activation, progress, drafts
@needs_media
def test_activation_progress_and_isolated_draft(env: dict) -> None:
    seen: list[tuple[str, float]] = []
    job = _job(env, "progress")
    job.run(progress_callback=lambda agent, frac, msg: seen.append((agent, frac)))
    d = job.doc
    assert d["status"] == "draft_ready" and d["progress"] == 1.0
    active = [a for a in AGENT_ORDER if d["agents"][a]["active"]]
    assert {"media_ingest", "synchronisation", "rough_cut", "quality_control"} <= set(active)
    assert all(d["agents"][a]["status"] == "completed" for a in active)
    assert all(d["agents"][a]["status"] == "skipped" for a in AGENT_ORDER if a not in active)
    overall = [f for _, f in seen]
    assert overall == sorted(overall), "overall progress must never go backwards"
    assert {a for a, _ in seen} >= {"media_ingest", "synchronisation", "rough_cut"}
    # The draft lives inside the job folder; nothing is written next to the footage.
    for key in ("kdenlive_project", "timeline_commands_json", "otio_timeline"):
        assert Path(d["artifacts"][key]).resolve().is_relative_to(job.dir)
    media_dir = next(iter(env["cams"].values())).parent
    assert sorted(p.name for p in media_dir.iterdir()) == sorted(p.name for p in env["cams"].values())
    assert d["source_integrity"]["verified"] is True and _sources_unchanged(env)
    assert d["processing_elapsed_sec"] > 0
    assert all(d["agents"][a]["duration_sec"] is not None for a in active)


# ------------------------------------------------------------------ cancel, resume, rerun
@needs_media
def test_cancel_resume_and_rerun_keep_finished_work(env: dict) -> None:
    job = _job(env, "cancel_resume")

    def _cb(agent: str, frac: float, msg: str) -> None:
        if agent == "rough_cut":
            job.cancel("editor pressed Cancel")

    job.run(progress_callback=_cb)
    assert job.doc["status"] == "cancelled"
    assert job.doc["agents"]["rough_cut"]["status"] == "cancelled"
    first_elapsed = job.doc["processing_elapsed_sec"]
    assert first_elapsed and first_elapsed > 0, "machine time of a cancelled run is kept"
    reopened = ProductionJob(job.dir)  # what the Control Centre does after a restart
    reopened.resume()
    d = reopened.doc
    assert d["status"] == "draft_ready"
    assert d["agents"]["media_ingest"]["attempts"] == 1 and d["agents"]["synchronisation"]["attempts"] == 1
    assert d["processing_elapsed_sec"] > first_elapsed
    cmds = json.loads(Path(d["artifacts"]["timeline_commands_json"]).read_text(encoding="utf-8"))
    per_agent = [cmds["batch_agents"][b["request_id"]] for b in cmds["batches"]]
    assert per_agent.count("rough_cut") == len(d["agents"]["rough_cut"]["batches"]), "no duplicated rough-cut batches after resume"
    before = d["agents"]["rough_cut"]["attempts"]  # cancelled attempt + resumed attempt
    assert before == 2
    reopened.rerun("rough_cut")
    d = reopened.doc
    assert d["agents"]["rough_cut"]["attempts"] == before + 1 and d["agents"]["media_ingest"]["attempts"] == 1
    revs = [b["expected_revision"] for b in json.loads(Path(d["artifacts"]["timeline_commands_json"]).read_text())["batches"]]
    assert revs == list(range(revs[0], revs[0] + len(revs)))
    assert _sources_unchanged(env)


@needs_media
def test_checkpoint_written_but_not_recorded_is_ignored(env: dict, base_job: ProductionJob) -> None:
    """Regression: a worker killed after an agent's checkpoint but before job.json recorded it
    made resume re-run the agent on top of its own output (duplicated batches)."""
    copy_dir = env["base"] / "jobs" / "torn"
    shutil.copytree(base_job.dir, copy_dir)
    doc = json.loads((copy_dir / "job.json").read_text())
    doc["agents"]["distribution"]["status"] = "running"
    doc["status"] = "running"
    (copy_dir / "job.json").write_text(json.dumps(doc))
    job = ProductionJob(copy_dir)
    latest = job._latest_state()
    dist_batches = set(base_job.doc["agents"]["distribution"]["batches"])
    assert not dist_batches & {b["request_id"] for b in latest.command_batches}
    job.resume()
    assert job.doc["status"] == "draft_ready"
    cmds = json.loads(Path(job.doc["artifacts"]["timeline_commands_json"]).read_text())
    assert sum(1 for a in cmds["batch_agents"].values() if a == "distribution") == len(job.doc["agents"]["distribution"]["batches"])


# ------------------------------------------------------------------ approvals and rollback
@needs_media
def test_per_operation_approval_and_rollback_survive_reopen(env: dict) -> None:
    job = _job(env, "approvals")
    job.run()
    rc_batches = job.doc["agents"]["rough_cut"]["batches"]
    ops = [op for b in job.state.command_batches if b["request_id"] in rc_batches for op in b["operations"]]
    assert len(ops) >= 2
    drop = [ops[0]["op_id"], ops[1]["op_id"]]
    job.decide("rough_cut", "accept", rejected_op_ids=drop, note="keep these pauses")
    reopened = ProductionJob(job.dir)
    reopened.rebuild_draft()
    kept = {op["op_id"] for b in json.loads(Path(reopened.doc["artifacts"]["timeline_commands_json"]).read_text())["batches"] for op in b["operations"]}
    assert not set(drop) & kept, "rejected operations stay out of the draft after reopening"
    assert {op["op_id"] for op in ops[2:]} <= kept, "the rest of the agent's work is kept"
    corr = ProfilePreferenceStore("podcast_multicam").corrections()
    assert any(c.get("job_id") == "approvals" and len(c.get("rejected_ops", [])) == 2 for c in corr)
    # Rollback to the director, then a *new process* rebuilds: the rolled-back edits must stay out.
    reopened.rollback("multicam_director")
    fresh = ProductionJob(job.dir)
    fresh.rebuild_draft()
    agents_in_draft = set(json.loads(Path(fresh.doc["artifacts"]["timeline_commands_json"]).read_text())["batch_agents"].values())
    assert "rough_cut" not in agents_in_draft and "distribution" not in agents_in_draft
    assert fresh.doc["agents"]["rough_cut"]["status"] == "rolled_back"
    fresh.resume()
    assert fresh.doc["status"] == "draft_ready" and fresh.doc["agents"]["rough_cut"]["status"] == "completed"
    assert _sources_unchanged(env)


@needs_media
def test_source_media_is_protected(env: dict, base_job: ProductionJob, tmp_path: Path) -> None:
    src = next(iter(env["cams"].values()))
    with pytest.raises(PermissionError):
        base_job.promote(src, approve=True)
    link = tmp_path / "hardlink_to_source.kdenlive"
    os.link(src, link)
    with pytest.raises(PermissionError):
        base_job.promote(link, approve=True)
    target = tmp_path / "episode.kdenlive"
    assert not base_job.promote(target)["replaced_existing"]
    with pytest.raises(ApprovalRequiredError):
        base_job.promote(target)
    assert Path(base_job.promote(target, approve=True)["backup"]).exists()
    assert explain(PermissionError("Refusing to overwrite original source media"))["code"] == "source_protected"
    assert _sources_unchanged(env)


# ------------------------------------------------------------------ save, reopen, relink
@needs_media
def test_save_reopen_and_relink_moved_media(env: dict, tmp_path: Path) -> None:
    media = tmp_path / "card_A"
    shutil.copytree(next(iter(env["cams"].values())).parent, media)
    cams = {k: media / v.name for k, v in env["cams"].items()}
    job = _job(env, "relink", cams=cams)
    job.run()
    saved = tmp_path / "projects" / "episode.kdenlive"
    job.promote(saved)
    # The drive is renamed and the files moved into a new folder structure.
    moved = tmp_path / "SSD_2" / "Show" / "Episode 12"
    moved.parent.mkdir(parents=True)
    shutil.move(str(media), str(moved))
    decoy_dir = tmp_path / "decoys"
    decoy_dir.mkdir()
    # Same name and size as a real take, different content: must never be linked.
    (decoy_dir / "cam_wide.mp4").write_bytes(b"\x5a" * (moved / "cam_wide.mp4").stat().st_size)
    reopened = ProductionJob(job.dir)
    preview = reopened.relink([decoy_dir, tmp_path / "SSD_2"], dry_run=True)
    assert set(preview["relinked"]) == set(cams) and preview["dry_run"]
    res = reopened.relink([decoy_dir, tmp_path / "SSD_2"], project_files=[saved])
    assert set(res["relinked"]) == set(cams) and not res["still_missing"]
    assert all(Path(r["to"]).is_relative_to(moved) for r in res["relinked"].values())
    assert res["source_integrity"]["verified"] is True
    assert res["projects_updated"] and Path(res["projects_updated"][0]["backup"]).exists()
    for project in (saved, Path(reopened.doc["artifacts"]["kdenlive_project"])):
        text = project.read_text(encoding="utf-8")
        assert str(media) + "/" not in text and str(moved) in text
    from bangonpro.ai_engine.planner import ingest_kdenlive_project

    clips = ingest_kdenlive_project(saved).clips
    assert clips and all(Path(c.file_path).exists() for c in clips if c.file_path)
    again = ProductionJob(job.dir)
    assert all(Path(p).is_relative_to(moved) for p in again._latest_state().input_camera_files.values())
    assert again.relink([tmp_path / "SSD_2"])["relinked"] == {}, "nothing left to relink"
    assert not any(str(r["to"]).startswith(str(decoy_dir)) for r in res["relinked"].values())


# ------------------------------------------------------------------ queue, killed worker, locks
@needs_media
def test_killed_worker_is_recovered_without_redoing_finished_agents(env: dict, tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "queue", job_root=tmp_path / "jobs")
    entry = q.add({"brief": BRIEF, "inputs": _inputs(env["cams"]), "profile": "podcast_multicam", "mode": "autonomous_first_cut", "options": {"episode_title": "Episode 3"}})
    child_env = {**os.environ, "BANGONPRO_TEST_AGENT_DELAY_SEC": "3", "PYTHONPATH": str(REPO)}
    proc = subprocess.Popen([sys.executable, "-m", "bangonpro.ai_engine.department", "queue", "run", "--queue-root", str(q.root)], cwd=REPO, env=child_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
    job_dir = Path(entry["job_dir"])
    deadline = time.monotonic() + 180
    try:
        while time.monotonic() < deadline:
            try:
                doc = json.loads((job_dir / "job.json").read_text())
                if doc["agents"]["media_ingest"]["status"] == "completed":
                    break
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.05)
        else:
            pytest.fail("worker never finished media ingest")
        assert q.lock_state()["state"] == "held"
        proc.send_signal(signal.SIGKILL)  # crash mid-job, during the pause after an agent
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert q.get(entry["queue_id"])["status"] == "running" and q.lock_path.exists()
    st = q.lock_state()
    assert st["state"] == "stale" and "not running" in st["reason"]
    report = q.run_worker()
    assert report["recovered_stale_lock"] and entry["queue_id"] in report["recovered_entries"]
    done = q.get(entry["queue_id"])
    assert done["status"] == "completed" and done["recovered"] and done["attempts"] == 2
    job = ProductionJob(job_dir)
    assert job.doc["status"] == "draft_ready"
    assert job.doc["agents"]["media_ingest"]["attempts"] == 1, "finished agents are not redone"
    events = [json.loads(line)["event"] for line in (job_dir / "events.jsonl").read_text().splitlines()]
    assert "job_resumed" in events
    assert not q.lock_path.exists()


def test_worker_lock_detects_live_and_stale_workers(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "q")
    q.root.mkdir(parents=True)
    fresh = {"pid": os.getpid(), "host": socket.gethostname(), "heartbeat_utc": datetime.now(UTC).isoformat(), "token": "live"}
    q.lock_path.write_text(json.dumps(fresh))
    with pytest.raises(WorkerBusyError):
        q.acquire_worker_lock()
    old = {**fresh, "heartbeat_utc": (datetime.now(UTC) - timedelta(minutes=10)).isoformat()}
    q.lock_path.write_text(json.dumps(old))
    lock = q.acquire_worker_lock(stale_after_sec=60)
    assert "no heartbeat" in lock["recovered_stale_lock"]["reason"]
    assert q.heartbeat(lock["token"]) and not q.heartbeat("someone-else")
    q.release_worker_lock(lock["token"])
    dead = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
    dead.wait()
    q.lock_path.write_text(json.dumps({**fresh, "pid": dead.pid}))
    assert q.lock_state()["state"] == "stale"
    q.lock_path.write_text("{torn")
    assert q.acquire_worker_lock()["recovered_stale_lock"]


@needs_media
def test_queue_priority_cancel_remove_and_series_batch(env: dict, tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "queue", job_root=tmp_path / "jobs")
    spec = {"brief": BRIEF, "inputs": _inputs(env["cams"]), "profile": "podcast_multicam", "mode": "autonomous_first_cut"}
    low = q.add(spec, priority=0, label="low")
    high = q.add(spec, priority=5, label="high")
    gone = q.add(spec, priority=1, label="to cancel")
    assert q.next_entry()["queue_id"] == high["queue_id"]
    assert q.cancel(gone["queue_id"])["status"] == "cancelled"
    q.remove(gone["queue_id"])
    with pytest.raises(Exception):
        q.get(gone["queue_id"])
    store = ProfilePreferenceStore("podcast_multicam", "weekly_show")
    store.commit_overlay({"reaction_shot_frequency": 0.3}, reason="series house style")
    series = q.add_series(profile="podcast_multicam", series_id="weekly_show", episodes=[{"title": "Ep 1", "inputs": _inputs(env["cams"])}, {"title": "Ep 2", "inputs": _inputs(env["cams"])}], priority=3)
    assert len({e["series_batch_id"] for e in series}) == 1
    assert all(e["spec"]["options"]["series_id"] == "weekly_show" and e["spec"]["profile"] == "podcast_multicam" for e in series)
    report = q.run_worker()
    assert report["ran"] == [high["queue_id"], series[0]["queue_id"], series[1]["queue_id"], low["queue_id"]]
    assert all(q.get(e)["status"] == "completed" for e in report["ran"])
    for e in series:
        jd = ProductionJob(e["job_dir"]).doc
        assert jd["series_id"] == "weekly_show" and "preferences v1 applied" in jd["agents"]["learning"]["summary"]
    hist = job_history(tmp_path / "jobs")
    assert len(hist) == 4 and all(h["status"] == "draft_ready" and h["has_draft"] for h in hist)
    with pytest.raises(Exception):
        q.add({"brief": "no media"})


# ------------------------------------------------------------------ storage and volumes
@needs_media
def test_low_storage_blocks_before_processing(env: dict) -> None:
    job = _job(env, "nospace", min_free_bytes=1 << 62)
    job.run()
    d = job.doc
    assert d["status"] == "blocked_insufficient_storage"
    assert all(r.get("attempts", 0) == 0 for r in d["agents"].values()), "blocked before any agent ran"
    assert d["error_help"]["code"] == "no_space" and d["error_help"]["fix_action"] == "choose_job_folder"
    assert "choose another job folder" in d["error"]
    item = check_storage(job.dir, purpose="productions", item_id="storage_jobs", required_bytes=1 << 62)
    assert item["status"] == "error" and item["fix"] and item["fix_action"] == "choose_job_folder"
    # Space is freed: Resume continues normally.
    d["options"]["min_free_bytes"] = 512 * 1024 * 1024
    job._save(force=True)
    ProductionJob(job.dir).resume()
    assert ProductionJob(job.dir).doc["status"] == "draft_ready"
    assert explain(OSError(errno.ENOSPC, "No space left on device"))["code"] == "no_space"
    assert explain(InsufficientStorageError("x"))["code"] == "no_space"


@needs_media
def test_job_root_on_another_volume(env: dict) -> None:
    other = Path("/dev/shm")
    if not other.is_dir() or not os.access(other, os.W_OK) or other.stat().st_dev == env["base"].stat().st_dev:
        pytest.skip("no second writable volume to stand in for an external SSD")
    root = other / f"bangonpro_ext_ssd_{os.getpid()}"
    try:
        job = _job(env, "external", root=root)
        job.run()
        assert job.doc["status"] == "draft_ready"
        assert Path(job.doc["artifacts"]["kdenlive_project"]).resolve().is_relative_to(root)
        item = check_storage(root, purpose="productions", item_id="storage_jobs")
        assert item["data"]["external"] is True and item["status"] in ("ok", "warning")
        assert ProductionJob(job.dir).rebuild_draft()["kdenlive_project"]
        assert _sources_unchanged(env)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------------------ failed exports
@needs_media
def test_failed_exports_are_reported_with_fixes(env: dict, base_job: ProductionJob, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a, **_k):
        raise RuntimeError("melt render failed with exit code 1")

    monkeypatch.setattr(dept_agents, "run_project_emit_export_stage", _boom)
    base_job.rerun("distribution")
    rec = base_job.doc["agents"]["distribution"]
    assert rec["status"] == "failed" and rec["help"]["code"] == "export_failed"
    assert "Distribution" in rec["help"]["fix"] and base_job.doc["status"] == "completed_with_errors"
    monkeypatch.undo()
    base_job.rerun("distribution")
    assert base_job.doc["status"] == "draft_ready" and "help" not in base_job.doc["agents"]["distribution"]


@needs_media
def test_failed_social_render_has_actionable_review_item(env: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dept_agents, "_ffmpeg", lambda *_a, **_k: False)
    job = _job(env, "social_fail", brief="First cut and social clips", export_video=True)
    job.run()
    rec = job.doc["agents"]["distribution"]
    items = [r for r in rec["review_items"] if r.startswith("Render failed")]
    assert items and all("rerun the Distribution agent" in r for r in items)
    assert rec["help"]["code"] == "export_failed"
    summary = json.loads((job.dir / "summary.json").read_text())
    assert any("Render failed" in r for r in summary["review_items"])


def test_error_explanations_cover_common_failures() -> None:
    cases = {
        "missing_tool": FileNotFoundError(2, "No such file or directory", "ffmpeg"),
        "missing_media": FileNotFoundError("All source media are missing: cam_wide"),
        "permission_denied": PermissionError(13, "Permission denied", "/Volumes/Show/jobs"),
        "model_not_installed": RuntimeError("Whisper model faster-whisper-base not installed"),
        "export_failed": "RuntimeError: ffmpeg encoder failed while rendering the master",
    }
    for code, err in cases.items():
        info = explain(err)
        assert info["code"] == code, (code, info)
        assert info["fix"] and info["title"] and info["message"]


# ------------------------------------------------------------------ readiness
def test_readiness_report_is_structured_and_actionable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rep = check_system(job_root=tmp_path / "jobs", cache_dir=tmp_path / "cache", model_dir=tmp_path / "models", probe_hardware=False)
    assert rep["app_version"] == APP_VERSION == "1.2.2" and rep["overall"] in ("ready", "attention", "blocked")
    ids = {i["id"] for i in rep["items"]}
    assert {"hardware", "storage_jobs", "storage_cache", "tool_ffmpeg", "tool_ffprobe", "tool_melt", "python", "model_speech", "cache"} <= ids
    for item in rep["items"]:
        assert item["status"] in ("ok", "warning", "error", "info") and item["detail"]
        if item["status"] in ("warning", "error"):
            assert item["fix"], item
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    blocked = check_system(job_root=tmp_path / "jobs", cache_dir=tmp_path / "cache", model_dir=tmp_path / "models", probe_hardware=False, include_models=False)
    ff = next(i for i in blocked["items"] if i["id"] == "tool_ffmpeg")
    assert ff["status"] == "error" and ff["fix_action"] == "run_system_check" and blocked["overall"] == "blocked"
    assert blocked["items"][0]["status"] == "error", "problems are listed first"


def test_readiness_cli_and_unwritable_folder(tmp_path: Path) -> None:
    out = _cli("readiness", "--quick", "--job-root", str(tmp_path / "jobs"), "--cache-dir", str(tmp_path / "c"), "--model-dir", str(tmp_path / "m"), check=True)
    assert json.loads(out.stdout)["items"]
    if os.geteuid() != 0:  # root can write anywhere
        ro = tmp_path / "ro"
        ro.mkdir()
        ro.chmod(0o500)
        assert check_storage(ro, purpose="productions", item_id="s")["status"] == "error"


# ------------------------------------------------------------------ diagnostics
@needs_media
def test_diagnostics_export_is_sanitised(env: dict, tmp_path: Path) -> None:
    secret = "sk-ant-TESTSECRET0123456789abcdefABCDEF"
    home_path = str(Path.home() / "Movies" / "Show" / "cam_host.mp4")
    job = _job(env, "diag", brief=f"First cut. api_key={secret} footage at {home_path}")
    job.run()
    draft = job.dir / "draft"
    (draft / "provider_config.json").write_text(json.dumps({"provider": "anthropic", "api_key": secret}))
    (draft / "copied_take.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x01" * 2048)
    (draft / "transcript.json").write_text(json.dumps({"segments": [{"text": "a private conversation"}]}))
    doc = json.loads((job.dir / "job.json").read_text())
    doc["options"]["prefs_root"] = str(Path.home() / ".local/share/bangonpro/preferences")
    doc["options"]["provider_token"] = secret
    (job.dir / "job.json").write_text(json.dumps(doc))
    out = tmp_path / "diag.zip"
    res = export_diagnostics(out, job_dirs=[job.dir], cache_dir=env["base"] / "cache", model_dir=tmp_path / "m", probe_hardware=False)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        blob = b"\n".join(zf.read(n) for n in names)
        manifest = json.loads(zf.read("manifest.json"))
    assert {"manifest.json", "app.json", "readiness.json", "models.json", "cache.json", "errors.json", "jobs/diag/job.json", "jobs/diag/plan.json", "jobs/diag/events.jsonl", "jobs/diag/summary.json"} <= set(names)
    assert secret.encode() not in blob, "planted secret leaked"
    assert b"TESTSECRET" not in blob
    assert str(Path.home()).encode() + b"/" not in blob, "home path not redacted"
    assert not any(n.endswith((".mp4", ".srt", ".kdenlive")) or "provider" in n or "transcript" in n or "checkpoints" in n for n in names)
    assert b"a private conversation" not in blob
    reasons = {e["item"]: e["reason"] for e in manifest["excluded"]}
    assert any("provider_config.json" in k and "never" in v for k, v in reasons.items())
    assert any("copied_take.mp4" in k for k in reasons) and any("transcript.json" in k for k in reasons)
    assert manifest["sanitised"] and not manifest["include_media"] and manifest["redactions"]["secret_values"] >= 1
    assert sorted(i["path"] for i in manifest["included"]) == sorted(n for n in names if n != "manifest.json")
    assert not any(Path(n).name in {src.name for src in env["cams"].values()} for n in names), "no source media in the zip"
    opt = export_diagnostics(tmp_path / "diag_media.zip", job_dirs=[job.dir], cache_dir=env["base"] / "cache", model_dir=tmp_path / "m", include_media=True, probe_hardware=False)
    with zipfile.ZipFile(opt["zip_path"]) as zf:
        names2 = zf.namelist()
        blob2 = b"\n".join(zf.read(n) for n in names2)
    assert "jobs/diag/draft/transcript.json" in names2 and "jobs/diag/draft/copied_take.mp4" in names2
    assert not any("provider" in n for n in names2) and secret.encode() not in blob2
    assert res["manifest"]["app_version"] == "1.2.2"


# ------------------------------------------------------------------ measurement
@needs_media
def test_measurement_separates_machine_time_and_marks_unpaired(env: dict, base_job: ProductionJob, tmp_path: Path) -> None:
    log = MeasurementLog(tmp_path / "m")
    t0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)

    def _at(minutes: float) -> str:
        return (t0 + timedelta(minutes=minutes)).isoformat()

    def _session(kind: str, deliverable: str, minutes: int, *, pause_for: int = 0) -> str:
        assisted = kind == "agent_assisted"
        sid = log.start(deliverable_id=deliverable, kind=kind, editor="ed", evidence_class="synthetic_test", job_id=base_job.doc["job_id"] if assisted else None, job_dir=str(base_job.dir) if assisted else None)
        d = json.loads(log.path.read_text())
        d["sessions"][-1]["started_at_utc"] = t0.isoformat()
        log.path.write_text(json.dumps(d))
        for m in range(1, minutes + 1):
            log.activity(sid, "correction" if assisted and m % 5 == 0 else "review", at_utc=_at(m))
        end = minutes
        if pause_for:  # the editor pauses the timer for a meeting, then resumes
            log.pause(sid, at_utc=_at(minutes + 0.1))
            log.resume(sid, at_utc=_at(minutes + pause_for))
            end = minutes + pause_for
        log.finish(sid, quality_rating=4.0, at_utc=_at(end + 0.5))
        return sid

    _session("manual_baseline", "ep1", 60)
    sid = _session("agent_assisted", "ep1", 20, pause_for=30)
    _session("agent_assisted", "ep2", 10)
    ses = next(s for s in log.sessions() if s["session_id"] == sid)
    parts = active_seconds(ses)
    assert parts["paused"] >= 29 * 60 and parts["total"] < 22 * 60
    with pytest.raises(ValueError):
        log.finish(sid, quality_rating=6)
    jobs = {base_job.doc["job_id"]: base_job.doc}
    rep = savings_report(log.sessions(), jobs)
    rows = {r["deliverable_id"]: r for r in rep["rows"]}
    ep1, ep2 = rows["ep1"], rows["ep2"]
    assert ep1["status"] == "measured" and isinstance(ep1["human_time_saved_pct"], float)
    assert ep1["agent_assisted_human_sec"] < 25 * 60, "paused time is not human time"
    assert ep1["machine"]["processing_elapsed_sec"] == base_job.doc["processing_elapsed_sec"]
    assert ep1["machine"]["quality_loop_processing_sec"] == float(base_job.doc.get("quality_loop_processing_sec") or 0.0)
    assert ep1["corrective_work"]["correction_events"] >= 3 and ep1["quality_assisted_mean"] == 4.0
    assert ep2["status"] == "not_measured" and ep2["human_time_saved_pct"] == "not measured" and ep2["missing"] == ["manual_baseline"]
    assert rep["measured_pairs"] == 1 and rep["not_measured_deliverables"] == 1
    assert "ops_applied" not in CSV_COLUMNS and "agents_rejected" in CSV_COLUMNS and "review_items_total" in CSV_COLUMNS
    out = _cli("measure", "report", "--root", str(tmp_path / "m"), "--out-dir", str(tmp_path / "reports"), check=True)
    assert "Saved %" in out.stdout and "not measured" in out.stdout and "Corrective work" in out.stdout
    csv_path = next((tmp_path / "reports").glob("*.csv"))
    rows_csv = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert {r["deliverable_id"] for r in rows_csv} == {"ep1", "ep2"}
    assert next(r for r in rows_csv if r["deliverable_id"] == "ep2")["human_time_saved_pct"] == "not measured"
    assert any(p.suffix == ".json" for p in (tmp_path / "reports").iterdir())


# ------------------------------------------------------------------ learning
@needs_media
def test_recurring_preferences_and_safe_bundle_transfer(env: dict, base_job: ProductionJob, tmp_path: Path) -> None:
    prefs_a = tmp_path / "editor_a"
    store = ProfilePreferenceStore("podcast_multicam", "weekly_show", prefs_a)
    profile = load_show_profile("podcast_multicam")
    for job_id in ("ep1", "ep2", "ep3"):
        store.record_correction({"kind": "parameter_override", "job_id": job_id, "agent_id": "rough_cut", "overrides": {"editing_rules": {"min_silence_sec": 1.4}}})
    store.record_correction({"kind": "parameter_override", "job_id": "ep4", "agent_id": "colourist", "overrides": {"colour_rules": {"target_luma": 0.5}}})
    found = detect_recurring_preferences(profile, store.corrections())
    keys = {json.dumps(s["overlay"], sort_keys=True) for s in found}
    assert json.dumps({"editing_rules": {"min_silence_sec": 1.4}}, sort_keys=True) in keys
    sug = next(s for s in found if "min_silence_sec" in s["reason"])
    assert sug["evidence"]["jobs"] == 3 and sug["evidence"]["observations"] == 3
    assert not any("target_luma" in s["reason"] for s in found), "a single job is not a recurring preference"
    pending = store.propose(found)
    assert store.active_version() == 0, "suggestions are never applied automatically"
    store.decide_suggestion(pending[0]["suggestion_id"], approve=True)
    bundle = tmp_path / "weekly_show_prefs.json"
    export_preference_bundle(store, bundle, editor="Editor A", app_version=APP_VERSION)
    store_b = ProfilePreferenceStore("podcast_multicam", "weekly_show", tmp_path / "editor_b")
    added = import_preference_bundle(store_b, bundle)
    assert added and all(s["status"] == "pending" and s["category"] == "imported" for s in added)
    assert store_b.active_version() == 0 and store_b.active_overlay() == {}
    doc = json.loads(bundle.read_text())
    doc["items"][0]["overlay"] = {"editing_rules": {"min_silence_sec": 9.0}}
    (tmp_path / "tampered.json").write_text(json.dumps(doc))
    with pytest.raises(PreferenceError, match="checksum"):
        import_preference_bundle(store_b, tmp_path / "tampered.json")

    def _signed(items: list[dict]) -> Path:
        from bangonpro.ai_engine.department.learning import _canonical

        body = {k: v for k, v in json.loads(bundle.read_text()).items() if k != "manifest"}
        body["items"] = items
        body["manifest"] = {"algorithm": "sha256", "payload_sha256": hashlib.sha256(_canonical(body)).hexdigest()}
        p = tmp_path / f"crafted_{len(list(tmp_path.glob('crafted_*')))}.json"
        p.write_text(json.dumps(body))
        return p

    for evil in (
        {"editing_rules": {"approval_threshold": 0}},
        {"provider": {"name": "x"}},
        {"model_weights": "/tmp/evil.onnx"},
        {"caption_style": {"font_family": "__import__('os').system('x')"}},
        {"graphics_templates": {"lower_third_template": "payload.py"}},
    ):
        with pytest.raises(PreferenceError):
            import_preference_bundle(store_b, _signed([{"category": "x", "reason": "x", "overlay": evil}]))
    clamped = import_preference_bundle(store_b, _signed([{"category": "x", "reason": "x", "overlay": {"loudness_target_lufs": -99.0}}]))
    assert clamped[0]["overlay"]["loudness_target_lufs"] == -31.0
    with pytest.raises(PreferenceError, match="profile"):
        import_preference_bundle(ProfilePreferenceStore("daily_news", "x", tmp_path / "c"), bundle)
    # A real rerun with an override is recorded against its job for recurrence detection.
    base_job.rerun("rough_cut", profile_overrides={"editing_rules": {"min_silence_sec": 1.1}})
    recorded = ProfilePreferenceStore("podcast_multicam", "default").corrections()
    assert any(c.get("kind") == "parameter_override" and c.get("job_id") == "base" for c in recorded)


@needs_media
def test_learn_diff_from_job_and_final_project(env: dict, base_job: ProductionJob, tmp_path: Path) -> None:
    final = tmp_path / "final.kdenlive"
    base_job.promote(final)
    res = diff_job_against_final(base_job.dir, final, prefs_root=tmp_path / "p")
    assert res["ai_shots"] == res["final_shots"] > 0 and res["suggestions"] == []
    corr = ProfilePreferenceStore("podcast_multicam", "default", tmp_path / "p").corrections()
    assert corr[-1]["kind"] == "draft_final_diff" and corr[-1]["job_id"] == "base"
    out = _cli("learn", "diff", "--job", str(base_job.dir), "--final-project", str(final), "--prefs-root", str(tmp_path / "p"), check=True)
    assert json.loads(out.stdout)["job_id"] == "base"


# ------------------------------------------------------------------ profiles
def test_genre_profiles_carry_broadcast_targets() -> None:
    expected_lufs = {"podcast_multicam": -16.0, "interview_two_camera": -16.0, "social_short": -14.0, "gameshow_multicam": -23.0, "sports_discussion": -23.0, "daily_news": -23.0}
    for pid, lufs in expected_lufs.items():
        prof = load_show_profile(pid)
        audio = prof["audio_rules"]
        assert prof["loudness_target_lufs"] == audio["target_lufs"] == audio["loudness_target_lufs"] == prof["delivery_spec"]["target_lufs"] == lufs, pid
        assert audio["true_peak_dbtp"] == prof["delivery_spec"]["true_peak_dbtp"] <= -1.0, pid
        assert audio.get("loudness_standard"), pid
        rules = prof["editing_rules"]
        assert rules["min_shot_sec"] == prof["min_shot_duration_sec"] and rules["max_shot_sec"] == prof["max_shot_duration_sec"], pid
        assert rules["wide_shot_interval_sec"] == prof["wide_shot_interval_sec"] and rules["reaction_shot_frequency"] == prof["reaction_shot_frequency"], pid
        assert prof["caption_style"]["max_chars_per_line"] <= 42 and prof["colour_rules"]["treatment"]
        assert set(prof["speaker_camera_map"].values()) <= set(prof["camera_roles"]), pid
    news = load_show_profile("daily_news")
    assert news["audio_rules"]["true_peak_dbtp"] == -1.0 and news["audio_rules"].get("loudness_standard") == "EBU R128"
    assert load_show_profile("social_short")["video_spec"]["height"] > load_show_profile("social_short")["video_spec"]["width"]
    assert load_show_profile("daily_news")["max_shot_duration_sec"] < load_show_profile("podcast_multicam")["max_shot_duration_sec"]


@needs_media
def test_custom_profile_create_validate_and_use(env: dict, tmp_path: Path) -> None:
    pdir = tmp_path / "profiles"
    run_env = {**os.environ, "BANGONPRO_PROFILES_DIR": str(pdir)}
    out = _cli("profile", "create", "--base", "podcast_multicam", "--id", "weekly_chat", "--name", "Weekly chat", "--set", "loudness_target_lufs=-18", "reaction_shot_frequency=0.3", "editing_rules.min_silence_sec=1.2", env=run_env, check=True)
    path = Path(json.loads(out.stdout)["path"])
    assert path.parent == pdir.resolve() and json.loads(path.read_text())["reaction_shot_frequency"] == 0.3
    assert json.loads(_cli("profile", "validate", str(path), env=run_env, check=True).stdout)["valid"]
    assert "weekly_chat" in json.loads(_cli("profile", "list", env=run_env, check=True).stdout)["custom"]
    bad = _cli("profile", "create", "--base", "podcast_multicam", "--id", "broken", "--set", "reaction_shot_frequency=2.5", env=run_env)
    assert bad.returncode == 2 and "reaction_shot_frequency" in json.loads(bad.stdout)["error"] and json.loads(bad.stdout)["help"]["fix"]
    os.environ["BANGONPRO_PROFILES_DIR"] = str(pdir)
    try:
        job = _job(env, "custom_profile", profile="weekly_chat")
        assert job.doc["profile_id"] == "weekly_chat" and job.state.profile["editing_rules"]["min_silence_sec"] == 1.2
        job.run()
        assert job.doc["status"] == "draft_ready"
    finally:
        os.environ["BANGONPRO_PROFILES_DIR"] = str(env["base"] / "profiles")
    assert set(list_show_profiles()) >= {"podcast_multicam", "daily_news"}


# ------------------------------------------------------------------ CLI plumbing
@needs_media
def test_cli_queue_history_cache_models(env: dict, base_job: ProductionJob, tmp_path: Path) -> None:
    qroot, jroot = str(tmp_path / "q"), str(tmp_path / "j")
    added = json.loads(_cli("queue", "add", BRIEF, "--mode", "autonomous_first_cut", "--profile", "podcast_multicam", "--inputs", *_inputs(env["cams"]), "--episode-title", "Ep 9", "--queue-root", qroot, "--job-root", jroot, check=True).stdout)["queued"]
    assert added["status"] == "queued" and added["label"] == "Ep 9"
    listed = json.loads(_cli("queue", "list", "--queue-root", qroot, check=True).stdout)
    assert listed["counts"]["queued"] == 1 and listed["worker"]["state"] == "free"
    assert json.loads(_cli("queue", "cancel", added["queue_id"], "--queue-root", qroot, check=True).stdout)["cancelled"]["status"] == "cancelled"
    bad = _cli("queue", "cancel", added["queue_id"], "--queue-root", qroot)
    assert bad.returncode == 2 and json.loads(bad.stdout)["help"]
    assert json.loads(_cli("queue", "remove", added["queue_id"], "--queue-root", qroot, check=True).stdout)["removed"]
    hist = json.loads(_cli("history", "--job-root", str(base_job.dir.parent), "--queue-root", qroot, check=True).stdout)
    assert any(h["job_id"] == "base" for h in hist["jobs"]) and hist["queue"] == []
    stats = json.loads(_cli("cache", "stats", check=True).stdout)
    assert stats["entries"] > 0 and {n["namespace"] for n in stats["namespaces"]} >= {"sha256"}
    cleared = json.loads(_cli("cache", "clear", "--namespace", "sha256", check=True).stdout)
    assert cleared["removed"] >= 1
    after = {n["namespace"]: n["entries"] for n in json.loads(_cli("cache", "stats", check=True).stdout)["namespaces"]}
    assert after.get("sha256", 0) == 0 and sum(after.values()) > 0, "other namespaces are kept"
    models = json.loads(_cli("models", "--model-dir", str(tmp_path / "models"), check=True).stdout)
    assert "speech_ready" in models and models["installed_count"] == 0
    diag = json.loads(_cli("diagnostics", "export", "--out", str(tmp_path / "d.zip"), "--job", str(base_job.dir), "--quick", check=True).stdout)
    assert Path(diag["zip_path"]).is_file() and diag["include_media"] is False
    missing = _cli("status", str(tmp_path / "nope"))
    assert missing.returncode == 2 and json.loads(missing.stdout)["help"]["code"]
    assert _sources_unchanged(env)
    # The cache is still usable after a namespace clear.
    assert AnalysisCache().stats()["size_bytes"] > 0
