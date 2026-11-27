"""V1.7 project isolation, cooperative pause/discard, checkpoint integrity, and output options.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from talking_head import generate_talking_head

from bangonpro.ai_engine.department.agents import JobOptions
from bangonpro.ai_engine.department.checkpoint import (
    CHECKPOINT_HASH_KEY,
    compute_checkpoint_sha256,
    verify_checkpoint_integrity,
    write_checkpoint_atomic,
    write_json_atomic,
)
from bangonpro.ai_engine.department.jobs import PAUSE_FILENAME, ProductionJob
from bangonpro.ai_engine.department.queue import JobQueue, QueueError, job_history
from bangonpro.ai_engine.department.worker import JobBusyError, worker_alive, worker_session

ROOT = Path(__file__).resolve().parents[2]
HAVE_MEDIA = all(shutil.which(t) for t in ("ffmpeg", "ffprobe"))
needs_media = pytest.mark.skipif(not HAVE_MEDIA, reason="ffmpeg/ffprobe required")


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    base = tmp_path_factory.mktemp("v17_isolation")
    e = {"base": base, "jobs": base / "jobs"}
    if HAVE_MEDIA:
        e["clip"] = generate_talking_head(base / "media")
    return e


def _env(base: Path, **extra: str) -> dict[str, str]:
    out = dict(os.environ)
    out.update(
        {
            "BANGONPRO_CACHE_DIR": str(base / "cache"),
            "BANGONPRO_PREFS_DIR": str(base / "prefs"),
            "BANGONPRO_CONFIG_DIR": str(base / "config"),
            "BANGONPRO_INTEL_DIR": str(base / "intel"),
            "PYTHONPATH": str(ROOT),
        },
        **extra,
    )
    return out


def _cli(base: Path, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "bangonpro.ai_engine.department", *args],
        capture_output=True,
        text=True,
        env=_env(base, **extra),
        timeout=300,
        check=False,
    )


def _start(base: Path, *args: str, **extra: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "bangonpro.ai_engine.department", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_env(base, **extra),
    )


def _doc(job_dir: Path) -> dict:
    return json.loads((job_dir / "job.json").read_text(encoding="utf-8"))


def _wait_for(job_dir: Path, predicate, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            d = _doc(job_dir)
            if predicate(d):
                return d
        except (OSError, ValueError):
            pass
        time.sleep(0.15)
    raise AssertionError(f"condition not reached for {job_dir}")


def _ops(job_dir: Path) -> dict[str, int]:
    cmds = json.loads((job_dir / "draft" / "timeline_commands.json").read_text(encoding="utf-8"))
    out: dict[str, int] = {}
    for b in cmds["batches"]:
        for op in b["operations"]:
            out[op["type"]] = out.get(op["type"], 0) + 1
    return out


# ----------------------------------------------------------------------------
# 1. Project & Sequence Isolation (job.json, history, queue)
# ----------------------------------------------------------------------------
def test_project_isolation_in_jobs_history_and_queue(tmp_path: Path) -> None:
    job_root = tmp_path / "jobs"
    queue_root = tmp_path / "queue"
    media_a = tmp_path / "a.mp4"
    media_b = tmp_path / "b.mp4"
    media_a.write_bytes(b"fake-a")
    media_b.write_bytes(b"fake-b")

    job_a = ProductionJob.create(
        job_root=job_root,
        brief="Project A edit",
        inputs=[media_a],
        mode="autonomous_first_cut",
        project_id="proj_a",
        project_uuid="uuid-proj-a",
        project_name="Show Alpha",
        project_path="/projects/alpha.kdenlive",
        sequence_id="seq_a1",
        sequence_uuid="uuid-seq-a1",
        sequence_name="Main Cut A",
        job_id="job_proj_a",
    )
    job_b = ProductionJob.create(
        job_root=job_root,
        brief="Project B edit",
        inputs=[media_b],
        mode="autonomous_first_cut",
        project_id="proj_b",
        project_uuid="uuid-proj-b",
        project_name="Show Beta",
        project_path="/projects/beta.kdenlive",
        sequence_id="seq_b1",
        sequence_uuid="uuid-seq-b1",
        sequence_name="Main Cut B",
        job_id="job_proj_b",
    )

    # Verify full project & sequence identity persisted in job.json
    pa = job_a.doc["project"]
    assert pa["project_uuid"] == "uuid-proj-a"
    assert pa["project_id"] == "proj_a"
    assert pa["project_name"] == "Show Alpha"
    assert pa["project_path"] == "/projects/alpha.kdenlive"
    assert pa["sequence_uuid"] == "uuid-seq-a1"
    assert pa["sequence_id"] == "seq_a1"
    assert pa["sequence_name"] == "Main Cut A"

    # Strict project_uuid filtering in job_history()
    all_hist = job_history(job_root)
    assert len(all_hist) == 2
    for row in all_hist:
        for k in ("project_uuid", "project_id", "project_name", "sequence_uuid", "sequence_id"):
            assert k in row

    hist_a = job_history(job_root, project_uuid="uuid-proj-a")
    hist_b = job_history(job_root, project_uuid="uuid-proj-b")
    assert [r["job_id"] for r in hist_a] == ["job_proj_a"]
    assert [r["job_id"] for r in hist_b] == ["job_proj_b"]
    assert all(r["project_uuid"] == "uuid-proj-a" for r in hist_a)
    assert all(r["project_uuid"] == "uuid-proj-b" for r in hist_b)
    assert job_history(job_root, project_uuid="uuid-proj-c") == []

    # Queue isolation by project_uuid
    q = JobQueue(queue_root, job_root=job_root)
    ea = q.add(
        {
            "brief": "Queue A",
            "inputs": [str(media_a)],
            "project_uuid": "uuid-proj-a",
            "project_name": "Show Alpha",
            "sequence_uuid": "uuid-seq-a1",
            "aspect_ratio": "16:9",
            "target_duration_sec": 300.0,
            "media_source": "sequence",
        }
    )
    eb = q.add(
        {
            "brief": "Queue B",
            "inputs": [str(media_b)],
            "project_uuid": "uuid-proj-b",
            "project_name": "Show Beta",
            "sequence_uuid": "uuid-seq-b1",
        }
    )
    assert ea["project_uuid"] == "uuid-proj-a"
    assert ea["sequence_uuid"] == "uuid-seq-a1"
    assert eb["project_uuid"] == "uuid-proj-b"

    assert [e["queue_id"] for e in q.entries(project_uuid="uuid-proj-a")] == [ea["queue_id"]]
    assert [e["queue_id"] for e in q.entries(project_uuid="uuid-proj-b")] == [eb["queue_id"]]
    assert len(q.status(project_uuid="uuid-proj-a")["entries"]) == 1

    # CLI history & queue list filtering
    cli_hist = _cli(tmp_path, "history", "--job-root", str(job_root), "--queue-root", str(queue_root), "--project-uuid", "uuid-proj-b")
    assert cli_hist.returncode == 0, cli_hist.stdout + cli_hist.stderr
    parsed_hist = json.loads(cli_hist.stdout)
    assert [j["job_id"] for j in parsed_hist["jobs"]] == ["job_proj_b"]
    assert [e["queue_id"] for e in parsed_hist["queue"]] == [eb["queue_id"]]

    cli_q = _cli(tmp_path, "queue", "list", "--queue-root", str(queue_root), "--project-uuid", "uuid-proj-a")
    assert cli_q.returncode == 0, cli_q.stdout + cli_q.stderr
    assert [e["queue_id"] for e in json.loads(cli_q.stdout)["entries"]] == [ea["queue_id"]]

    # Verify default project_uuid falls back to project_id for backward compatibility
    job_legacy = ProductionJob.create(job_root=job_root, brief="Legacy", inputs=[media_a], project_id="legacy_proj", job_id="job_legacy")
    assert job_legacy.doc["project"]["project_uuid"] == "legacy_proj"
    assert job_b.doc["project"]["project_uuid"] == "uuid-proj-b"


# ----------------------------------------------------------------------------
# 2. Cooperative Pause & Resume + Discard
# ----------------------------------------------------------------------------
@needs_media
def test_cooperative_pause_and_resume_completes_without_duplicate_edits(env: dict[str, Path]) -> None:
    clean_dir = env["jobs"] / "v17_clean"
    r_clean = _cli(
        env["base"],
        "run",
        "Tighten the pauses and clean up the audio",
        "--mode",
        "full_production_draft",
        "--inputs",
        str(env["clip"]),
        "--job-root",
        str(env["jobs"]),
        "--job-id",
        "v17_clean",
    )
    assert r_clean.returncode == 0, r_clean.stdout + r_clean.stderr
    clean_ops = _ops(clean_dir)

    paused_dir = env["jobs"] / "v17_paused"
    proc = _start(
        env["base"],
        "run",
        "Tighten the pauses and clean up the audio",
        "--mode",
        "full_production_draft",
        "--inputs",
        str(env["clip"]),
        "--job-root",
        str(env["jobs"]),
        "--job-id",
        "v17_paused",
        BANGONPRO_TEST_AGENT_DELAY_SEC="2",
    )
    try:
        _wait_for(paused_dir, lambda d: d["status"] == "running" and d["agents"]["media_ingest"]["status"] == "completed")
        assert worker_alive(paused_dir)

        # Request cooperative pause via CLI
        p_res = _cli(env["base"], "pause", str(paused_dir))
        assert p_res.returncode == 0, p_res.stdout + p_res.stderr
        p_payload = json.loads(p_res.stdout)
        assert p_payload["pause_requested"] is True
        assert (paused_dir / PAUSE_FILENAME).exists()

        # Worker finishes its current stage cleanly and exits with status "paused"
        rc = proc.wait(timeout=45)
        assert rc == 0, proc.stderr.read() if proc.stderr else ""
    finally:
        if proc.poll() is None:
            proc.kill()

    assert not worker_alive(paused_dir)
    d_paused = _doc(paused_dir)
    assert d_paused["status"] == "paused"
    assert d_paused.get("paused_at_utc")
    # Completed checkpoints remain intact and valid
    assert d_paused["agents"]["media_ingest"]["status"] == "completed"

    # Resume cleanly completes with identical edit operations
    r_res = _cli(env["base"], "resume", str(paused_dir))
    assert r_res.returncode == 0, r_res.stdout + r_res.stderr
    assert not (paused_dir / PAUSE_FILENAME).exists()
    d_done = _doc(paused_dir)
    assert d_done["status"] == "draft_ready"
    assert _ops(paused_dir) == clean_ops


def test_pause_reconciles_dead_worker_immediately(tmp_path: Path) -> None:
    job_root = tmp_path / "jobs"
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"fake")
    job = ProductionJob.create(job_root=job_root, brief="Test", inputs=[clip], job_id="job_dead_pause")
    job.doc["status"] = "running"
    job.doc["agents"]["media_ingest"]["status"] = "running"
    job._save(force=True)

    res = ProductionJob.pause(job.dir)
    assert res["status"] == "paused"
    assert res["worker_alive"] is False
    reloaded = _doc(job.dir)
    assert reloaded["status"] == "paused"
    assert reloaded["agents"]["media_ingest"]["status"] == "paused"


def test_discard_refuses_live_worker_and_preserves_source_footage(tmp_path: Path) -> None:
    job_root = tmp_path / "jobs"
    media_dir = tmp_path / "source_footage"
    media_dir.mkdir()
    src_file = media_dir / "original_cam1.mp4"
    src_file.write_bytes(b"precious-camera-footage")

    job = ProductionJob.create(job_root=job_root, brief="Test discard", inputs=[src_file], job_id="job_discard")
    assert job.dir.is_dir()

    # Hold worker.lock in a subprocess to simulate a live worker
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys, time; sys.path.insert(0, %r)\n"
                "from bangonpro.ai_engine.department.worker import worker_session\n"
                "with worker_session(%r):\n    print('held', flush=True); time.sleep(60)\n"
            )
            % (str(ROOT), str(job.dir)),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        assert worker_alive(job.dir)
        with pytest.raises(JobBusyError):
            ProductionJob.discard(job.dir)
        cli_busy = _cli(tmp_path, "discard", str(job.dir))
        assert cli_busy.returncode == 2
        assert job.dir.is_dir()
    finally:
        holder.kill()
        holder.wait(10)

    assert not worker_alive(job.dir)

    # Discard via CLI succeeds once stopped
    cli_ok = _cli(tmp_path, "discard", str(job.dir))
    assert cli_ok.returncode == 0, cli_ok.stdout + cli_ok.stderr
    payload = json.loads(cli_ok.stdout)
    assert payload["status"] == "discarded"
    assert not job.dir.exists()
    # Original source footage is completely untouched
    assert src_file.is_file() and src_file.read_bytes() == b"precious-camera-footage"

    # Non-job directory without job.json is rejected
    plain_dir = tmp_path / "not_a_job"
    plain_dir.mkdir()
    with pytest.raises(ValueError, match="missing job.json"):
        ProductionJob.discard(plain_dir)


# ----------------------------------------------------------------------------
# 3. Transactional Checkpoints, SHA-256 Verification, and 0o600 Locks
# ----------------------------------------------------------------------------
def test_atomic_json_and_checkpoint_sha256_integrity_and_lock_perms(tmp_path: Path) -> None:
    # 0o600 lock permissions on POSIX
    lock_job = tmp_path / "lock_job"
    lock_job.mkdir()
    with worker_session(lock_job):
        mode = stat.S_IMODE((lock_job / "worker.lock").stat().st_mode)
        assert mode == 0o600

    # Unique temp + atomic replace leaves no stray .tmp files
    out_file = tmp_path / "atomic.json"
    write_json_atomic(out_file, {"hello": "world"})
    assert json.loads(out_file.read_text(encoding="utf-8")) == {"hello": "world"}
    assert list(tmp_path.glob("*.tmp")) == []

    # Checkpoint SHA-256 verification (V1.7 hashed + V1.2-V1.6 backward compatibility)
    sample_ckpt = {
        "agent_index": 0,
        "agent_id": "executive",
        "saved_at_utc": "2026-11-27T00:00:00+00:00",
        "state": {"project_id": "p1", "sequence_id": "s1", "profile": {}, "output_dir": "/tmp/out"},
    }
    assert verify_checkpoint_integrity(sample_ckpt) is True  # legacy checkpoint without hash

    ckpt_path = tmp_path / "01_executive.json"
    write_checkpoint_atomic(ckpt_path, sample_ckpt)
    written = json.loads(ckpt_path.read_text(encoding="utf-8"))
    assert CHECKPOINT_HASH_KEY in written
    assert written[CHECKPOINT_HASH_KEY] == compute_checkpoint_sha256(written)
    assert verify_checkpoint_integrity(written) is True

    # Tampering invalidates SHA-256
    tampered = dict(written)
    tampered["state"] = dict(written["state"], project_id="hacked")
    assert verify_checkpoint_integrity(tampered) is False

    # Missing structural keys rejected even without hash
    assert verify_checkpoint_integrity({"agent_id": "executive", "agent_index": 0, "state": {}}) is False
    assert verify_checkpoint_integrity({"agent_id": "", "agent_index": 0, "state": sample_ckpt["state"]}) is False


@needs_media
def test_corrupted_checkpoint_is_skipped_and_cleanly_rerun_on_resume(env: dict[str, Path]) -> None:
    job_dir = env["jobs"] / "v17_corrupt_ckpt"
    r = _cli(
        env["base"],
        "run",
        "Tighten the pauses",
        "--mode",
        "autonomous_first_cut",
        "--inputs",
        str(env["clip"]),
        "--job-root",
        str(env["jobs"]),
        "--job-id",
        "v17_corrupt_ckpt",
    )
    assert r.returncode == 0, r.stdout + r.stderr

    # Verify all checkpoints carry valid SHA-256
    ckpts = sorted((job_dir / "checkpoints").glob("*.json"))
    assert len(ckpts) >= 3
    for cp in ckpts:
        data = json.loads(cp.read_text(encoding="utf-8"))
        assert CHECKPOINT_HASH_KEY in data
        assert verify_checkpoint_integrity(data) is True

    # Corrupt the last two checkpoints: one with bad SHA-256, one truncated JSON
    last_ckpt = ckpts[-1]
    prev_ckpt = ckpts[-2]
    bad_data = json.loads(prev_ckpt.read_text(encoding="utf-8"))
    bad_data["checkpoint_sha256"] = "0" * 64
    prev_ckpt.write_text(json.dumps(bad_data), encoding="utf-8")
    last_ckpt.write_text("{truncated json", encoding="utf-8")

    # Mark job as interrupted and resume -> corrupted checkpoints are rejected and re-executed cleanly
    job = ProductionJob(job_dir)
    valid_paths = [p for _, p in job._valid_checkpoints()]
    assert last_ckpt not in valid_paths
    assert prev_ckpt not in valid_paths

    job.doc["status"] = "interrupted"
    job._save(force=True)
    resumed_doc = job.resume()
    assert resumed_doc["status"] == "draft_ready"
    # Rewritten checkpoints now pass SHA-256 verification again
    for cp in (prev_ckpt, last_ckpt):
        assert verify_checkpoint_integrity(json.loads(cp.read_text(encoding="utf-8"))) is True


# ----------------------------------------------------------------------------
# 4. Hardened promote/relink, corrupt queue backup, and cancel path containment
# ----------------------------------------------------------------------------
@needs_media
def test_promote_and_relink_atomic_and_reject_symlinks_and_sources(env: dict[str, Path], tmp_path: Path) -> None:
    clip = tmp_path / "media" / "cam_main.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(env["clip"], clip)

    job_root = tmp_path / "jobs"
    job = ProductionJob.create(
        job_root=job_root,
        brief="Clean rough cut",
        inputs=[clip],
        mode="autonomous_first_cut",
        job_id="v17_promote_relink",
    )
    job.run()
    assert job.doc["status"] == "draft_ready"

    # Symlink target rejected by promote()
    real_dst = tmp_path / "real_target.kdenlive"
    real_dst.write_text("existing", encoding="utf-8")
    sym_dst = tmp_path / "sym_target.kdenlive"
    sym_dst.symlink_to(real_dst)
    with pytest.raises(PermissionError, match="symlink"):
        job.promote(sym_dst, approve=True)

    # Source footage target rejected by promote()
    with pytest.raises(PermissionError, match="source media"):
        job.promote(clip, approve=True)

    # Normal atomic promote succeeds without leftover .tmp files
    good_dst = tmp_path / "promoted.kdenlive"
    rec = job.promote(good_dst)
    assert Path(rec["target"]).is_file()
    assert list(tmp_path.glob("*.tmp")) == []

    # Move clip and test relink() atomicity + symlink rejection + fresh checkpoint SHA-256
    moved_dir = tmp_path / "moved_drive"
    moved_dir.mkdir()
    moved_clip = moved_dir / "cam_main_renamed.mp4"
    clip.rename(moved_clip)

    sym_proj = tmp_path / "sym_proj.kdenlive"
    sym_proj.symlink_to(good_dst)
    with pytest.raises(PermissionError, match="symlink"):
        job.relink([moved_dir], project_files=[sym_proj])

    relink_res = job.relink([moved_dir], project_files=[good_dst])
    assert relink_res["relinked"]
    assert relink_res["projects_updated"]
    assert str(moved_clip.resolve()) in good_dst.read_text(encoding="utf-8")
    # Every relinked checkpoint must still pass SHA-256 integrity verification!
    for cp in (job.dir / "checkpoints").glob("*.json"):
        assert verify_checkpoint_integrity(json.loads(cp.read_text(encoding="utf-8"))) is True


def test_corrupt_queue_backup_and_cancel_path_containment(tmp_path: Path) -> None:
    qroot = tmp_path / "queue"
    qroot.mkdir()
    qfile = qroot / "queue.json"
    qfile.write_text("{corrupt queue json!!!", encoding="utf-8")

    q = JobQueue(qroot)
    assert q.entries() == []
    backups = list(qroot.glob("queue.json.corrupt.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{corrupt queue json!!!"

    # Path containment check in cancel(): tamper with a running entry's job_dir
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fake")
    entry = q.add({"brief": "Test", "inputs": [str(media)]})
    outside_dir = tmp_path / "outside_secret"
    outside_dir.mkdir()
    doc = json.loads(qfile.read_text(encoding="utf-8"))
    doc["entries"][0]["status"] = "running"
    doc["entries"][0]["job_dir"] = str(outside_dir)
    qfile.write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(QueueError, match="outside allowed job root"):
        q.cancel(entry["queue_id"])
    assert not (outside_dir / "CANCEL").exists()


# ----------------------------------------------------------------------------
# 5. Output Options Propagation & Resource Usage Telemetry
# ----------------------------------------------------------------------------
@needs_media
def test_output_options_propagation_and_resource_telemetry(env: dict[str, Path]) -> None:
    job_dir = env["jobs"] / "v17_opts"
    res = _cli(
        env["base"],
        "run",
        "Tighten pauses for a short vertical cut",
        "--mode",
        "autonomous_first_cut",
        "--inputs",
        str(env["clip"]),
        "--job-root",
        str(env["jobs"]),
        "--job-id",
        "v17_opts",
        "--project-uuid",
        "uuid-opts-proj",
        "--project-name",
        "Vertical Show",
        "--sequence-uuid",
        "uuid-opts-seq",
        "--sequence-name",
        "Reel 1",
        "--aspect-ratio",
        "9:16",
        "--target-duration-sec",
        "45",
        "--media-source",
        "selected_bin",
    )
    assert res.returncode == 0, res.stdout + res.stderr
    d = _doc(job_dir)
    assert d["status"] == "draft_ready"
    assert d["options"]["aspect_ratio"] == "9:16"
    assert d["options"]["target_duration_sec"] == 45.0
    assert d["options"]["media_source"] == "selected_bin"
    assert d["project"]["project_uuid"] == "uuid-opts-proj"
    assert d["project"]["sequence_uuid"] == "uuid-opts-seq"

    # Resource usage telemetry recorded in job.json
    ru = d.get("resource_usage")
    assert isinstance(ru, dict)
    assert isinstance(ru["rss_mb"], (int, float)) and ru["rss_mb"] > 0
    assert isinstance(ru["cpu_time_sec"], (int, float)) and ru["cpu_time_sec"] >= 0
    assert ru.get("updated_at_utc")

    # Executive plan and delivery manifest honour aspect_ratio & target_duration_sec
    eplan = json.loads((job_dir / "draft" / "executive_plan.json").read_text(encoding="utf-8"))
    directives = eplan["interpretation"]["directives"]
    assert directives["aspect_ratio"] == "9:16"
    assert directives["output_dimensions"] == [1080, 1920]
    assert directives["target_duration_sec"] == 45.0
    assert directives["media_source"] == "selected_bin"

    manifest = json.loads((job_dir / "draft" / "delivery_manifest.json").read_text(encoding="utf-8"))
    assert manifest["aspect_ratio"] == "9:16"
    assert manifest["output_dimensions"] == [1080, 1920]
    assert manifest["target_duration_sec"] == 45.0

    # JobOptions defaults preserve 100% backward compatibility
    default_opts = JobOptions()
    assert default_opts.aspect_ratio is None
    assert default_opts.target_duration_sec is None
    assert default_opts.media_source is None


def test_half_frame_source_times_round_the_same_way_for_picture_and_dialogue() -> None:
    """Long-form journey regression: a cut at master 49.74 s on a 25 fps sequence mapped the wide
    camera (offset 0 s, source frame 1243.5) and the host microphone (offset 0.6 s, 1258.5) through
    round-half-to-even, rounding picture up and dialogue down: one frame (40 ms) out of lip sync."""
    from bangonpro.ai_engine.state import ProjectAnalysisState

    st = ProjectAnalysisState("p", "s", {"video_spec": {"fps_num": 25, "fps_den": 1}}, ".")
    assert st.sec_to_frame(49.74) == 1244 and st.sec_to_frame(50.34) == 1259
    for master in (49.74, 10.02, 3.5 / 25, 7.817, 123.46):
        snapped = st.sec_to_frame(master) / st.fps
        picture = st.sec_to_frame(snapped + 0.0)
        dialogue = st.sec_to_frame(snapped + 0.6)
        assert dialogue - picture == 15, (master, picture, dialogue)  # 0.6 s offset is exactly 15 frames
    assert st.sec_to_frame(-0.3) == 0


def test_project_index_keeps_protected_pause_kind() -> None:
    """Long-form journey regression: with a neural transcript the editorial context classifies protected
    pauses with a "kind"; build_project_index passed it as a keyword that collided with its own helper's
    first parameter (TypeError), aborting a finished 10-minute production in its final draft rebuild."""
    from types import SimpleNamespace

    from bangonpro.ai_engine.department.project_index import build_project_index

    st = SimpleNamespace(
        metadata={"editorial_context": {"protected_pauses": [{"start_sec": 9.0, "end_sec": 10.2, "kind": "reflective"}]}},
        asr_backend_report=SimpleNamespace(is_neural_inference=True), transcript_segments=[], interruption_intervals=[],
        media_assets={}, profile={}, project_id="p", rough_cut_shots=[], captions=[], artifacts={},
    )
    try:
        idx = build_project_index(st)
    except AttributeError as exc:  # stub lacks an attribute the index reads later; the pause event is built first
        raise AssertionError(f"stub state incomplete: {exc}") from exc
    pauses = [e for e in idx["events"] if e["type"] == "protected_pause"]
    assert pauses == [{"type": "protected_pause", "start_sec": 9.0, "end_sec": 10.2, "evidence": "pause classification", "kind": "reflective"}]
