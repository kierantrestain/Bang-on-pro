"""Production reliability: no job is ever left "running" when nothing runs it.

Regression suite for the editor report "the production was interrupted, froze and
stayed at 'Resuming job from checkpoint'". Root cause: ``job.json`` was the only
record of whether a production was running, and nothing reconciled it with the
process that was meant to be running it. A worker that crashed, was force-quit,
or raised an unexpected error left ``status: running`` behind forever; the
Agent Control Centre then kept showing a live job (Resume disabled, Cancel
writing a file nobody read), and two Resume presses could run the same job twice.

These tests run the real department CLI and queue worker on synthetic footage
(real ffprobe, sync, rough cut, melt). Evidence class: synthetic, automated.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ai_production import _generate_synthetic_4cam_studio

from bangonpro.ai_engine.department import JobBusyError, JobOptions, JobQueue, ProductionJob, effective_status, job_history
from bangonpro.ai_engine.department.jobs import MAX_AGENT_INTERRUPTIONS, job_owner_alive
from bangonpro.ai_engine.department.queue import MAX_QUEUE_ATTEMPTS

REPO = Path(__file__).resolve().parents[2]
HAVE_MEDIA_TOOLS = all(shutil.which(t) for t in ("ffmpeg", "ffprobe", "melt"))
pytestmark = pytest.mark.skipif(not HAVE_MEDIA_TOOLS, reason="ffmpeg/ffprobe/melt required")
BRIEF = "Make a first cut of the episode"


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> dict:
    base = tmp_path_factory.mktemp("reliability")
    mp = pytest.MonkeyPatch()
    for var, sub in (("BANGONPRO_CACHE_DIR", "cache"), ("BANGONPRO_PREFS_DIR", "prefs"), ("BANGONPRO_PROFILES_DIR", "profiles"), ("BANGONPRO_QUEUE_DIR", "queue")):
        mp.setenv(var, str(base / sub))
    cams = _generate_synthetic_4cam_studio(base)
    yield {"base": base, "inputs": [f"{k}={v}" for k, v in cams.items()]}
    mp.undo()


@pytest.fixture(scope="module")
def finished(env: dict) -> ProductionJob:
    job = ProductionJob.create(job_root=env["base"] / "jobs", brief=BRIEF, inputs=env["inputs"], profile_ref="podcast_multicam",
                               mode="autonomous_first_cut", options=JobOptions(), job_id="finished")
    job.run()
    assert job.doc["status"] == "draft_ready"
    return job


def _child_env(**extra: str) -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(REPO), **extra}


def _spawn(*args: str, **extra: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "bangonpro.ai_engine.department", *args], cwd=REPO, env=_child_env(**extra),  # noqa: S603
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _doc(job_dir: Path) -> dict:
    return json.loads((job_dir / "job.json").read_text(encoding="utf-8"))


def _wait_for(job_dir: Path, agent: str, proc: subprocess.Popen, timeout: float = 240) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"process ended early: {proc.stdout.read()[-600:]} {proc.stderr.read()[-600:]}")
        try:
            if _doc(job_dir)["agents"][agent]["status"] == "completed":
                return
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.05)
    pytest.fail(f"{agent} never completed")


def _start_cli_job(env: dict, job_id: str, delay: str = "3") -> tuple[subprocess.Popen, Path]:
    proc = _spawn("run", BRIEF, "--inputs", *env["inputs"], "--profile", "podcast_multicam", "--mode", "autonomous_first_cut",
                  "--job-root", str(env["base"] / "jobs"), "--job-id", job_id, BANGONPRO_TEST_AGENT_DELAY_SEC=delay)
    return proc, env["base"] / "jobs" / job_id


def _copy(finished: ProductionJob, name: str, **changes) -> Path:
    dst = finished.dir.parent / name
    shutil.copytree(finished.dir, dst)
    doc = _doc(dst)
    for agent, fields in changes.pop("agents", {}).items():
        doc["agents"][agent].update(fields)
    doc.update(changes)
    (dst / "job.json").write_text(json.dumps(doc), encoding="utf-8")
    return dst


# ------------------------------------------------------------------ the reported freeze
def test_killed_production_reads_as_interrupted_and_resumes_without_redoing_work(env: dict) -> None:
    proc, job_dir = _start_cli_job(env, "killed")
    _wait_for(job_dir, "synchronisation", proc)
    assert job_owner_alive(job_dir), "a running production is owned by its live process"
    proc.send_signal(signal.SIGKILL)  # force quit / crash: nothing gets the chance to record it
    proc.communicate(timeout=30)
    doc = _doc(job_dir)
    assert doc["status"] == "running", "the dead worker could not update job.json (this is what froze the app)"
    assert not job_owner_alive(job_dir)
    assert effective_status(doc, job_dir) == "interrupted"
    assert ProductionJob(job_dir).status == "interrupted"
    assert {r["job_id"]: r["status"] for r in job_history(job_dir.parent)}["killed"] == "interrupted"
    status = json.loads(subprocess.run([sys.executable, "-m", "bangonpro.ai_engine.department", "status", str(job_dir)], cwd=REPO, env=_child_env(),  # noqa: S603
                                       capture_output=True, text=True, timeout=120, check=True).stdout)
    assert status["status"] == "interrupted" and status["recorded_status"] == "running"

    out, err = _spawn("resume", str(job_dir)).communicate(timeout=600)
    doc = _doc(job_dir)
    assert doc["status"] == "draft_ready", err[-800:]
    assert doc["agents"]["media_ingest"]["attempts"] == 1 and doc["agents"]["synchronisation"]["attempts"] == 1, "finished agents are kept"
    cmds = json.loads(Path(doc["artifacts"]["timeline_commands_json"]).read_text(encoding="utf-8"))
    for agent in ("synchronisation", "rough_cut"):
        assert list(cmds["batch_agents"].values()).count(agent) == len(doc["agents"][agent]["batches"]), f"no duplicated {agent} edits"
    assert not (job_dir / "run.lock").exists()


def test_a_second_resume_never_runs_the_same_production_twice(env: dict) -> None:
    proc, job_dir = _start_cli_job(env, "twice")
    _wait_for(job_dir, "media_ingest", proc)
    proc.send_signal(signal.SIGKILL)
    proc.communicate(timeout=30)
    first = _spawn("resume", str(job_dir), BANGONPRO_TEST_AGENT_DELAY_SEC="1")
    deadline = time.monotonic() + 60
    while not job_owner_alive(job_dir) and time.monotonic() < deadline:
        time.sleep(0.05)
    second_out, _ = _spawn("resume", str(job_dir)).communicate(timeout=120)
    first.communicate(timeout=600)
    refused = json.loads(second_out)
    assert refused["busy"] is True and refused["help"]["code"] == "job_busy"
    # An in-process second writer is refused the same way (the queue worker uses this path).
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "bangonpro"])  # noqa: S603 - stands in for a live worker
    busy = ProductionJob(job_dir)
    try:
        busy.owner_path.write_text(json.dumps({"pid": owner.pid, "host": socket.gethostname(), "token": "x"}))
        with pytest.raises(JobBusyError):
            busy.resume()
    finally:
        owner.kill()
        owner.wait()
        busy.owner_path.unlink(missing_ok=True)
    doc = _doc(job_dir)
    assert doc["status"] == "draft_ready"
    events = [json.loads(line)["event"] for line in (job_dir / "events.jsonl").read_text().splitlines()]
    assert events.count("job_resumed") == 1
    assert doc["agents"]["rough_cut"]["attempts"] == 1


def test_quit_saves_the_production_as_interrupted(env: dict) -> None:
    proc, job_dir = _start_cli_job(env, "quit")
    _wait_for(job_dir, "synchronisation", proc)
    proc.send_signal(signal.SIGTERM)  # what Bang On Pro now sends when it quits
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 143 and json.loads(out)["interrupted"] is True
    doc = _doc(job_dir)
    assert doc["status"] == "interrupted" and doc["interrupted_reason"]
    assert not any(r["status"] == "running" for r in doc["agents"].values())
    assert not (job_dir / "run.lock").exists()
    _spawn("resume", str(job_dir)).communicate(timeout=600)
    assert _doc(job_dir)["status"] == "draft_ready"


def test_an_unexpected_error_ends_as_failed_with_a_fix_never_running(finished: ProductionJob) -> None:
    job_dir = _copy(finished, "torn_plan", status="running", agents={"quality_control": {"status": "running"}})
    (job_dir / "plan.json").write_text("{torn", encoding="utf-8")
    proc = _spawn("resume", str(job_dir))
    out, _ = proc.communicate(timeout=120)
    assert proc.returncode != 0 and json.loads(out)["help"]["title"]
    doc = _doc(job_dir)
    assert doc["status"] == "failed" and doc["error"] and doc["error_help"]["fix"]
    assert not (job_dir / "run.lock").exists()


def test_cancel_ends_a_production_nobody_is_running(finished: ProductionJob) -> None:
    job_dir = _copy(finished, "orphan_cancel", status="running", agents={"distribution": {"status": "running"}})
    ProductionJob(job_dir).cancel()
    doc = _doc(job_dir)
    assert doc["status"] == "cancelled" and doc["agents"]["distribution"]["status"] == "cancelled"


def test_a_reused_pid_is_not_mistaken_for_the_production(finished: ProductionJob) -> None:
    job_dir = _copy(finished, "reused_pid", status="running")
    other = subprocess.Popen(["sleep", "30"])  # noqa: S603, S607 - a live process that is not Bang On Pro
    try:
        (job_dir / "run.lock").write_text(json.dumps({"pid": other.pid, "host": socket.gethostname(), "token": "old"}))
        assert not job_owner_alive(job_dir)
        assert effective_status(_doc(job_dir), job_dir) == "interrupted"
    finally:
        other.kill()
        other.wait()


def test_an_agent_that_keeps_stopping_the_production_is_not_resumed_forever(finished: ProductionJob) -> None:
    job_dir = _copy(finished, "crash_loop", status="running",
                    agents={"distribution": {"status": "running", "interruptions": MAX_AGENT_INTERRUPTIONS - 1}})
    job = ProductionJob(job_dir)
    job.resume()
    d = job.doc
    assert d["agents"]["distribution"]["status"] == "failed" and d["agents"]["distribution"]["gave_up"]
    assert d["agents"]["distribution"]["help"]["fix_action"] == "rerun_agent"
    assert d["status"] == "completed_with_errors", "the rest of the production still finishes"
    job.resume()  # a later Resume keeps it failed instead of looping on it
    assert job.doc["agents"]["distribution"]["status"] == "failed"
    job.rerun("distribution")  # the editor can still run it deliberately
    assert job.doc["agents"]["distribution"]["status"] == "completed" and not job.doc["agents"]["distribution"].get("gave_up")
    assert job.doc["status"] == "draft_ready"


# ------------------------------------------------------------------ queue
def test_queue_stops_resuming_a_production_that_keeps_crashing(env: dict, tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "queue", job_root=tmp_path / "jobs")
    entry = q.add({"brief": BRIEF, "inputs": env["inputs"], "profile": "podcast_multicam", "mode": "autonomous_first_cut"})
    with q._locked():
        doc = q._load()
        doc["entries"][0].update({"status": "running", "crashes": MAX_QUEUE_ATTEMPTS - 1, "attempts": MAX_QUEUE_ATTEMPTS - 1})
        q._save(doc)
    report = q.run_worker()
    assert entry["queue_id"] in report["recovered_entries"] and report["ran"] == []
    e = q.get(entry["queue_id"])
    assert e["status"] == "failed" and e["help"]["code"] == "resume_loop_stopped"


def test_queue_never_reruns_a_production_finished_by_hand(finished: ProductionJob, tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "queue", job_root=finished.dir.parent)
    entry = q.add({"brief": BRIEF, "inputs": ["cam1=/nonexistent.mp4"], "profile": "podcast_multicam"})
    with q._locked():
        doc = q._load()
        doc["entries"][0].update({"status": "interrupted", "job_id": finished.doc["job_id"], "job_dir": str(finished.dir)})
        q._save(doc)
    before = (finished.dir / "events.jsonl").read_text()
    q.run_worker()
    assert q.get(entry["queue_id"])["status"] == "completed"
    assert (finished.dir / "events.jsonl").read_text() == before, "nothing ran again"


def test_queue_records_a_quit_as_interrupted_not_as_a_crash(env: dict, tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "queue", job_root=tmp_path / "jobs")
    entry = q.add({"brief": BRIEF, "inputs": env["inputs"], "profile": "podcast_multicam", "mode": "autonomous_first_cut"})
    proc = _spawn("queue", "run", "--queue-root", str(q.root), BANGONPRO_TEST_AGENT_DELAY_SEC="3")
    _wait_for(Path(entry["job_dir"]), "media_ingest", proc)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=60)
    e = q.get(entry["queue_id"])
    assert e["status"] == "interrupted" and not e.get("crashes")
    assert _doc(Path(entry["job_dir"]))["status"] == "interrupted"
    assert q.lock_state()["state"] == "free"
    q.run_worker()
    assert q.get(entry["queue_id"])["status"] == "completed"
