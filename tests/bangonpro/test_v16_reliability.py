"""V1.6 production reliability and editor-facing truthfulness (real CLI, real workers, synthetic footage).

The editor's report: a production was interrupted and froze on "Resuming job from checkpoint".
Root cause: job.json recorded "running" and was trusted; any failure outside an agent left it
there with no process. These tests run the department exactly as the app does (subprocesses,
signals, the queue) and check that every exit is truthful, nothing runs twice and a resumed
production never duplicates an edit. Evidence class: real code, synthetic footage.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from talking_head import generate_talking_head

from bangonpro.ai_engine.department.brief_coverage import brief_coverage
from bangonpro.ai_engine.department.jobs import ProductionJob
from bangonpro.ai_engine.department.queue import JobQueue
from bangonpro.ai_engine.department.stages import editor_stages
from bangonpro.ai_engine.department.worker import JobBusyError, worker_alive, worker_session

ROOT = Path(__file__).resolve().parents[2]
HAVE_MEDIA = all(shutil.which(t) for t in ("ffmpeg", "ffprobe"))
needs_media = pytest.mark.skipif(not HAVE_MEDIA, reason="ffmpeg/ffprobe required")


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    base = tmp_path_factory.mktemp("v16_reliability")
    e = {"base": base, "jobs": base / "jobs"}
    if HAVE_MEDIA:
        e["clip"] = generate_talking_head(base / "media")
    return e


def _env(base: Path, **extra: str) -> dict[str, str]:
    out = dict(os.environ)
    out.update({"BANGONPRO_CACHE_DIR": str(base / "cache"), "BANGONPRO_PREFS_DIR": str(base / "prefs"),
                "BANGONPRO_CONFIG_DIR": str(base / "config"), "BANGONPRO_INTEL_DIR": str(base / "intel"),
                "PYTHONPATH": str(ROOT)}, **extra)
    return out


def _cli(base: Path, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "bangonpro.ai_engine.department", *args], capture_output=True, text=True,
                          env=_env(base, **extra), timeout=300, check=False)


def _start(base: Path, *args: str, **extra: str) -> subprocess.Popen[str]:
    return subprocess.Popen([sys.executable, "-m", "bangonpro.ai_engine.department", *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=_env(base, **extra))


def _doc(job_dir: Path) -> dict:
    return json.loads((job_dir / "job.json").read_text(encoding="utf-8"))


def _run_args(env: dict[str, Path], job_id: str, clip: Path | None = None) -> list[str]:
    return ["run", "Tighten the pauses and clean up the audio", "--mode", "full_production_draft", "--edit-type", "full_edit",
            "--inputs", str(clip or env["clip"]), "--job-root", str(env["jobs"]), "--job-id", job_id]


def _wait_for(job_dir: Path, predicate, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            d = _doc(job_dir)
            if predicate(d):
                return d
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    raise AssertionError(f"condition not reached for {job_dir}")


def _ops(job_dir: Path) -> dict[str, int]:
    cmds = json.loads((job_dir / "draft" / "timeline_commands.json").read_text(encoding="utf-8"))
    out: dict[str, int] = {}
    for b in cmds["batches"]:
        for op in b["operations"]:
            out[op["type"]] = out.get(op["type"], 0) + 1
    return out


# ------------------------------------------------------------------------------------------- ownership
def test_worker_lock_is_exclusive_reentrant_and_released_by_the_kernel(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    assert not worker_alive(job)
    holder = subprocess.Popen([sys.executable, "-c", (
        "import sys, time; sys.path.insert(0, %r)\n"
        "from bangonpro.ai_engine.department.worker import worker_session\n"
        "with worker_session(%r):\n    print('held', flush=True); time.sleep(60)\n") % (str(ROOT), str(job))], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        assert worker_alive(job)
        with pytest.raises(JobBusyError), worker_session(job):
            pass
    finally:
        holder.kill()  # SIGKILL: the worker cannot clean up; the kernel releases the lock anyway
        holder.wait(10)
    assert not worker_alive(job)
    with worker_session(job), worker_session(job):  # nested ownership in one process re-enters
        assert worker_alive(job)
    assert not worker_alive(job)


@needs_media
def test_clean_run_reports_eight_stages_reasoning_and_the_single_camera_profile(env: dict[str, Path]) -> None:
    r = _cli(env["base"], *_run_args(env, "clean"))
    assert r.returncode == 0, r.stdout + r.stderr
    d = _doc(env["jobs"] / "clean")
    assert d["status"] == "draft_ready"
    assert d["profile_id"] == "youtube_talking_head"  # "Automatic" style with one camera
    assert [s["label"] for s in d["stages"]][0] == "Analysing footage" and len(d["stages"]) == 8
    assert all(s["state"] in ("done", "skipped") for s in d["stages"])
    assert d["reasoning"]["mode"] == "local" and d["reasoning"]["calls"] == 0
    assert "Processed on this Mac" in d["reasoning"]["message"]
    assert "worker_pid" in d and not worker_alive(env["jobs"] / "clean")


# ---------------------------------------------------------------------------------- the reported freeze
@needs_media
def test_a_dead_workers_running_state_is_reconciled_never_shown_as_live(env: dict[str, Path]) -> None:
    job = env["jobs"] / "killed"
    p = _start(env["base"], *_run_args(env, "killed"), BANGONPRO_TEST_AGENT_DELAY_SEC="2")
    try:
        _wait_for(job, lambda d: d["status"] == "running" and d["agents"]["media_ingest"]["status"] == "completed")
        assert worker_alive(job)
        p.send_signal(signal.SIGKILL)  # a crash: nothing can be recorded by the worker
        p.wait(10)
    finally:
        if p.poll() is None:
            p.kill()
    assert _doc(job)["status"] == "running"  # what V1.5 then displayed for ever
    st = json.loads(_cli(env["base"], "status", str(job)).stdout)
    assert st["status"] == "interrupted" and st["worker_alive"] is False
    assert st["error_help"]["fix_action"] == "resume_job"
    assert not any(r["status"] == "running" for r in st["agents"].values())


@needs_media
def test_failure_after_the_last_agent_ends_blocked_not_frozen_and_resume_completes(env: dict[str, Path]) -> None:
    clip = env["base"] / "drive" / "clip.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(env["clip"], clip)
    job = env["jobs"] / "moved"
    p = _start(env["base"], *_run_args(env, "moved", clip), BANGONPRO_TEST_AGENT_DELAY_SEC="1")
    _wait_for(job, lambda d: d["agents"]["synchronisation"]["status"] == "completed")
    p.kill()
    p.wait(10)
    moved = clip.with_name("clip_moved.mp4")
    clip.rename(moved)  # the editor's drive was renamed / ejected
    r1 = _cli(env["base"], "resume", str(job))
    d1 = _doc(job)
    assert r1.returncode == 2 and json.loads(r1.stdout)["help"]["code"] == "missing_media"
    assert d1["status"] == "blocked_missing_media" and d1["error_help"]["title"] == "Footage not found"
    assert d1.get("repeated_failure") is False
    _cli(env["base"], "resume", str(job))
    assert _doc(job)["repeated_failure"] is True  # the panel says: fix the cause before resuming again
    moved.rename(clip)
    r3 = _cli(env["base"], "resume", str(job))
    d3 = _doc(job)
    assert r3.returncode == 0 and d3["status"] == "draft_ready"
    assert not d3.get("error") and d3["source_integrity"]["verified"] is True


@needs_media
def test_second_process_is_refused_sigterm_records_interrupted_and_resume_never_duplicates(env: dict[str, Path]) -> None:
    job = env["jobs"] / "term"
    p = _start(env["base"], *_run_args(env, "term"), BANGONPRO_TEST_AGENT_DELAY_SEC="2")
    _wait_for(job, lambda d: d["status"] == "running" and d["agents"]["media_ingest"]["status"] == "completed")
    busy = _cli(env["base"], "resume", str(job))
    assert busy.returncode == 2 and json.loads(busy.stdout)["help"]["code"] == "busy"
    assert _doc(job)["status"] == "running"  # the live worker's state was not touched
    p.send_signal(signal.SIGTERM)  # what quitting Bang On Pro sends first
    assert p.wait(30) == 143
    d = _doc(job)
    assert d["status"] == "interrupted" and d["interruption"]["stage"]
    r = _cli(env["base"], "resume", str(job))
    assert r.returncode == 0 and _doc(job)["status"] == "draft_ready"
    assert _doc(job)["resume_attempts"] == 1
    assert _ops(job) == _ops(env["jobs"] / "clean")  # identical edit operations to an uninterrupted run


@needs_media
def test_cancel_with_no_live_worker_is_recorded_immediately(env: dict[str, Path]) -> None:
    job = env["jobs"] / "cancel_dead"
    p = _start(env["base"], *_run_args(env, "cancel_dead"), BANGONPRO_TEST_AGENT_DELAY_SEC="2")
    _wait_for(job, lambda d: d["status"] == "running")
    p.kill()
    p.wait(10)
    out = json.loads(_cli(env["base"], "cancel", str(job)).stdout)
    assert out["worker_alive"] is False and out["status"] == "cancelled"


# -------------------------------------------------------------------------------------------- queue
def test_queue_does_not_resume_cancelled_jobs_or_crash_loop(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "queue", job_root=tmp_path / "jobs")
    cancelled_dir = tmp_path / "jobs" / "c"
    cancelled_dir.mkdir(parents=True)
    (cancelled_dir / "job.json").write_text(json.dumps({"status": "cancelled"}), encoding="utf-8")
    a = q.add({"brief": "x", "inputs": ["/none.mp4"]})
    b = q.add({"brief": "y", "inputs": ["/none.mp4"]})
    doc = json.loads((tmp_path / "queue" / "queue.json").read_text(encoding="utf-8"))
    for e in doc["entries"]:
        e["status"] = "running"
        if e["queue_id"] == a["queue_id"]:
            e["job_dir"] = str(cancelled_dir)
        else:
            e["auto_resumes"] = 2  # it already crashed twice
    (tmp_path / "queue" / "queue.json").write_text(json.dumps(doc), encoding="utf-8")
    q.recover_interrupted()
    by_id = {e["queue_id"]: e for e in q.entries()}
    assert by_id[a["queue_id"]]["status"] == "cancelled"
    assert by_id[b["queue_id"]]["status"] == "needs_attention"
    assert by_id[b["queue_id"]]["help"]["code"] == "interrupted"


# ------------------------------------------------------------------------------ editor-facing truth
def test_stage_fraction_is_reported_only_when_measured() -> None:
    agents = {a: {"agent_id": a, "active": a in ("executive", "media_ingest", "rough_cut"), "status": "pending"} for a in
              ("executive", "learning", "media_ingest", "synchronisation", "editorial_intelligence", "multicam_director", "rough_cut",
               "audio_engineer", "colourist", "captions", "graphics", "vfx", "quality_control", "distribution")}
    agents["executive"]["status"] = "completed"
    agents["media_ingest"].update(status="running", progress=0.0, message="Probing")
    stages = editor_stages({"status": "running", "agents": agents})
    analyse = next(s for s in stages if s["id"] == "analyse")
    assert analyse["state"] == "running" and analyse["fraction"] is None  # 0.0 says nothing: no invented percentage
    agents["media_ingest"]["progress"] = 0.5
    analyse = next(s for s in editor_stages({"status": "running", "agents": agents}) if s["id"] == "analyse")
    assert analyse["fraction"] == pytest.approx(0.75)  # executive done + half of ingest, of two active agents
    assert next(s for s in stages if s["id"] == "colour")["state"] == "skipped"


def test_every_clause_of_the_brief_is_handled_or_reported() -> None:
    c = brief_coverage("Tighten the pauses, add captions, open with a cold open of the funniest moment, please")
    assert c["handled"] == ["Tighten the pauses", "add captions"]
    assert [x["text"] for x in c["not_handled"]] == ["open with a cold open of the funniest moment"]
    assert c["vague"] == ["please"]


@needs_media
def test_unhandled_instructions_are_listed_with_a_reason_when_no_subscription_is_connected(env: dict[str, Path]) -> None:
    args = _run_args(env, "unhandled")
    args[1] = "Tighten the pauses and add zooms on every punchline"
    r = _cli(env["base"], *args)
    assert r.returncode == 0, r.stderr
    d = _doc(env["jobs"] / "unhandled")
    nh = d["brief_coverage"]["not_handled"]
    assert [x["text"] for x in nh] == ["add zooms on every punchline"]
    assert nh[0]["reason"].startswith("Not applied")
    assert "no connected subscription was available" in d["reasoning"]["message"]


def test_job_is_reconciled_only_when_nobody_owns_it(tmp_path: Path) -> None:
    job_dir = tmp_path / "j"
    job_dir.mkdir()
    (job_dir / "job.json").write_text(json.dumps({"job_id": "j", "status": "running", "agents": {}}), encoding="utf-8")
    with worker_session(job_dir):
        # This process owns it: a reconcile from here must not overwrite a live worker's state.
        holder_view = ProductionJob(job_dir)
        assert holder_view.worker_alive()
    assert ProductionJob(job_dir).reconcile() is True
    assert _doc(job_dir)["status"] == "interrupted"
