#!/usr/bin/env python3
"""Verify the V1.2 AI Production Department inside the packaged app's bundled runtime.

Run with the bundle's Python (``Bang On Pro.app/Contents/Resources/python-env/bin/python3``):

1. Synthesises a 3-camera interview with *real spoken dialogue* (macOS ``say`` or
   ``espeak-ng``) so ASR has genuine words to transcribe.
2. Installs Whisper tiny + MobileSAM through the same ``ModelManager.install_model``
   code path as the in-app Model Manager (HTTPS, SHA-256 recorded).
3. Runs a ``full_production_draft`` job (captions, colour, VFX, export) and records
   per-agent neural vs fallback evidence, renders, loudness and source integrity.
4. Labels the evidence class (physical vs virtualised Apple Silicon, synthetic media).

Exit status is non-zero if the job fails, or if models installed but the agents did
not actually run neural inference.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bangonpro.ai_engine.agents.ingest_sync import extract_mono_pcm  # noqa: E402
from bangonpro.ai_engine.department import JobOptions, ProductionJob  # noqa: E402
from bangonpro.ai_engine.model_manager import ModelManager  # noqa: E402
from bangonpro.macos.hardware_profiles import probe_hardware_capabilities

SR = 16000
HOST_LINES = [
    "Welcome back to the show. Today we are talking about the final of the football season.",
    "So tell me, what was the moment that changed the game for you?",
    "That is incredible. Thank you so much for joining us tonight.",
]
GUEST_LINES = [
    "Thanks for having me. It was an amazing match from start to finish.",
    "Honestly the second goal changed everything. The whole stadium went wild.",
]


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603


def _tts(text: str, out: Path, voice_idx: int) -> bool:
    if shutil.which("say"):
        voices = ["Samantha", "Daniel"]
        aiff = out.with_suffix(".aiff")
        if _run(["say", "-v", voices[voice_idx % 2], "-o", str(aiff), text]).returncode != 0:
            _run(["say", "-o", str(aiff), text])
        return aiff.exists() and _run(["ffmpeg", "-y", "-v", "error", "-i", str(aiff), "-ac", "1", "-ar", str(SR), str(out)]).returncode == 0
    for tool in ("espeak-ng", "espeak"):
        if shutil.which(tool):
            return _run([tool, "-v", ["en-us", "en-gb"][voice_idx % 2], "-w", str(out), text]).returncode == 0
    return False


def _write_wav(path: Path, pcm: np.ndarray) -> None:
    pcm16 = (np.clip(pcm, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm16.tobytes())


def synthesise_interview(media_dir: Path) -> tuple[dict[str, Path], bool]:
    media_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(12)
    turns: list[tuple[str, np.ndarray]] = []
    speech_ok = True
    for i in range(max(len(HOST_LINES), len(GUEST_LINES))):
        for who, lines in (("host", HOST_LINES), ("guest", GUEST_LINES)):
            if i < len(lines):
                wav = media_dir / f"tts_{who}_{i}.wav"
                if _tts(lines[i], wav, 0 if who == "host" else 1):
                    turns.append((who, np.asarray(extract_mono_pcm(wav, sample_rate=SR), dtype=np.float32)))
                else:
                    speech_ok = False
                    t = np.arange(int(SR * 3.0)) / SR
                    turns.append((who, (0.2 * np.sin(2 * np.pi * (180 if who == "host" else 240) * t)).astype(np.float32)))
    lead, gap = int(SR * 2.0), int(SR * 0.6)
    total = lead + sum(len(p) + gap for _, p in turns) + int(SR * 1.5)
    tracks = {"host": np.zeros(total, np.float32), "guest": np.zeros(total, np.float32)}
    cur = lead
    for who, pcm in turns:
        peak = float(np.max(np.abs(pcm))) or 1.0
        tracks[who][cur: cur + len(pcm)] += 0.6 * pcm / peak
        cur += len(pcm) + gap
    floor = 0.003 * rng.standard_normal(total).astype(np.float32)
    mixes = {
        "cam_wide": 0.5 * tracks["host"] + 0.5 * tracks["guest"] + floor,
        "cam_host": tracks["host"] + 0.15 * tracks["guest"] + floor,
        "cam_guest": tracks["guest"] + 0.15 * tracks["host"] + floor,
    }
    colours = {"cam_wide": "0x1F4E79", "cam_host": "0x8B2E2E", "cam_guest": "0x2E6B3A"}
    out: dict[str, Path] = {}
    dur = total / SR
    for cam, pcm in mixes.items():
        wav = media_dir / f"{cam}.wav"
        _write_wav(wav, pcm)
        mp4 = media_dir / f"{cam}.mp4"
        # A bright central "subject" box gives segmentation and colour matching real structure.
        vf = "drawbox=x=iw*0.35:y=ih*0.2:w=iw*0.3:h=ih*0.7:color=0xE8C8A8@1:t=fill"
        res = _run([
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={colours[cam]}:s=1280x720:r=25:d={dur:.3f}",
            "-i", str(wav), "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-shortest", str(mp4),
        ])
        if res.returncode != 0:
            raise RuntimeError(f"ffmpeg failed for {cam}: {res.stderr}")
        wav.unlink(missing_ok=True)
        out[cam] = mp4
    return out, speech_ok


def evidence_class() -> dict[str, str]:
    info = {"os": platform.platform(), "machine": platform.machine(), "python": platform.python_version()}
    if sys.platform == "darwin":
        info["cpu"] = _run(["sysctl", "-n", "machdep.cpu.brand_string"]).stdout.strip()
        vmm = _run(["sysctl", "-n", "kern.hv_vmm_present"]).stdout.strip()
        info["hardware_class"] = "virtualised_apple_silicon" if vmm == "1" else "physical_apple_silicon"
    else:
        info["hardware_class"] = "linux_container_or_vm"
    info["media_class"] = "synthetic_generated (TTS speech + generated video)"
    return info


def install_models(model_dir: Path) -> dict[str, dict]:
    mgr = ModelManager(model_dir=model_dir)
    report: dict[str, dict] = {}
    for model_id in ("faster-whisper-tiny", "mobilesam-onnx"):
        t0 = time.perf_counter()
        try:
            res = mgr.install_model(model_id, timeout_sec=120.0)
            report[model_id] = {"installed": True, "seconds": round(time.perf_counter() - t0, 2), "sha256": res.get("sha256"), "status": res["status"]["status"]}
        except Exception as exc:  # noqa: BLE001 - record and continue with honest fallback
            report[model_id] = {"installed": False, "error": f"{type(exc).__name__}: {exc}"[:400]}
    return report


def verify_v122_operations(job: ProductionJob, cams: dict[str, Path], work: Path, out: Path, model_dir: Path, failures: list[str]) -> dict:
    """V1.2.2 gates inside the bundle: system check, sanitised diagnostics, queue, quality loop."""
    import zipfile

    from bangonpro.ai_engine.department import quality_loop as ql
    from bangonpro.ai_engine.department.diagnostics import export_diagnostics
    from bangonpro.ai_engine.department.queue import JobQueue
    from bangonpro.ai_engine.department.readiness import check_system

    evidence: dict = {}
    # 1. System check: the bundle must ship ffmpeg, ffprobe, melt and a complete Python runtime.
    try:
        rep = check_system(job_root=work / "jobs", model_dir=model_dir)
        by_id = {i["id"]: i for i in rep["items"]}
        evidence["readiness"] = {"overall": rep["overall"], "summary": rep["summary"], "items": {k: {"status": v["status"], "detail": v["detail"]} for k, v in by_id.items()}}
        for item_id in ("tool_ffmpeg", "tool_ffprobe", "tool_melt", "python"):
            if by_id.get(item_id, {}).get("status") != "ok":
                failures.append(f"system check: {item_id} is {by_id.get(item_id, {}).get('status')} ({by_id.get(item_id, {}).get('detail')})")
    except Exception as exc:  # noqa: BLE001 - every gate reports, none aborts the others
        failures.append(f"system check raised {type(exc).__name__}: {exc}")
    # 2. Diagnostics: a planted credential and media copy must not reach the zip.
    planted_secret = "sk-ant-BUNDLEVERIFY0000000000000000"
    planted = [job.dir / "draft" / "provider_config.json", job.dir / "draft" / "planted_copy.mp4"]
    try:
        planted[0].write_text(json.dumps({"api_key": planted_secret}), encoding="utf-8")
        planted[1].write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 1024)
        zpath = out / "diagnostics_sanitised.zip"
        res = export_diagnostics(zpath, job_dirs=[job.dir], model_dir=model_dir)
        with zipfile.ZipFile(zpath) as zf:
            names = zf.namelist()
            blob = b"".join(zf.read(n) for n in names)
        leaks = [n for n in names if n.endswith((".mp4", ".srt", ".ass", ".kdenlive")) or "provider" in n or "transcript" in n]
        evidence["diagnostics"] = {"files": len(names), "excluded": len(res["manifest"]["excluded"]), "redactions": res["manifest"]["redactions"]}
        if leaks or planted_secret.encode() in blob or str(Path.home()).encode() + b"/" in blob:
            failures.append(f"diagnostics export not sanitised (leaked entries: {leaks[:5]})")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"diagnostics export raised {type(exc).__name__}: {exc}")
    finally:
        for p in planted:
            p.unlink(missing_ok=True)
    # 3. Queue: one queued production runs to a draft through the worker (lock taken and released).
    try:
        q = JobQueue(work / "queue", job_root=work / "queue_jobs")
        entry = q.add({"brief": "First cut", "inputs": [f"{k}={v}" for k, v in cams.items()], "profile": "interview_two_camera", "mode": "autonomous_first_cut", "options": {"model_dir": str(model_dir)}})
        report = q.run_worker(max_jobs=1)
        done = q.get(entry["queue_id"])
        evidence["queue"] = {"status": done["status"], "job_status": done.get("job_status"), "ran": report["ran"], "lock_released": not q.lock_path.exists()}
        if done["status"] != "completed" or q.lock_path.exists():
            failures.append(f"queue worker: entry {done['status']} ({done.get('error')}), lock released={not q.lock_path.exists()}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"queue raised {type(exc).__name__}: {exc}")
    # 4. Quality loop: extension hooks import in the bundle and assessed agents leave a log.
    try:
        ql.ensure_extensions_loaded()
        assessed = {a: (r.get("quality_loop") or {}) for a, r in job.doc["agents"].items() if a in ql.ASSESSORS and r.get("status") == "completed"}
        evidence["quality_loop"] = {
            "assessors": sorted(ql.ASSESSORS),
            "attempts": int(job.doc.get("quality_loop_attempts", 0)),
            "per_agent": {a: {k: log.get(k) for k in ("metric", "initial_score", "final_score", "corrections_applied")} for a, log in assessed.items()},
            "final_verification": sorted((job.doc.get("final_verification") or {}).keys()),
        }
        missing = [a for a, log in assessed.items() if log and log.get("final_score") is None]
        if missing:
            failures.append(f"quality loop left no score for {', '.join(missing)}")
        if "final_verification" not in job.doc:
            failures.append("job has no final verification record")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"quality loop check raised {type(exc).__name__}: {exc}")
    return evidence


def _peak_rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return round(usage / (1024.0 * 1024.0), 2)
    return round(usage / 1024.0, 2)


def verify_bundle_self_containment(work: Path, failures: list[str]) -> dict:
    """Verify python3 runs cleanly with stripped PYTHONPATH & PYTHONNOUSERSITE=1 and probe VideoToolbox."""
    clean_env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "DYLD_LIBRARY_PATH")
    }
    clean_env["PYTHONNOUSERSITE"] = "1"
    probe_script = (
        "import json, sys; "
        "print(json.dumps({"
        "'executable': sys.executable, "
        "'prefix': sys.prefix, "
        "'base_prefix': sys.base_prefix, "
        "'version': sys.version.split()[0]"
        "}))"
    )
    proc = subprocess.run(
        [sys.executable, "-s", "-c", probe_script],
        env=clean_env,
        capture_output=True,
        text=True,
        check=False,
    )
    isolated_ok = proc.returncode == 0
    py_info: dict = {}
    if isolated_ok:
        try:
            py_info = json.loads(proc.stdout.strip())
        except json.JSONDecodeError:
            isolated_ok = False
    if not isolated_ok:
        failures.append(
            f"bundled python self-containment check failed (rc={proc.returncode}): {proc.stderr.strip()[:300]}"
        )

    caps = probe_hardware_capabilities(disk_path=work)
    return {
        "isolated_python_ok": isolated_ok,
        "python": py_info,
        "videotoolbox_available": bool(caps.videotoolbox_available),
        "ffmpeg_hwaccels": list(caps.ffmpeg_hwaccels),
        "ffmpeg_encoders": list(caps.ffmpeg_encoders),
        "host_tier": caps.host_tier,
        "is_apple_silicon": bool(caps.is_apple_silicon),
        "is_virtualized": bool(caps.is_virtualized),
        "peak_rss_mb": _peak_rss_mb(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", required=True, help="Small evidence files (JSON, summary) only")
    ap.add_argument("--work-dir", default=None, help="Media, models, jobs and renders (default: <output-dir>/../department_work)")
    ap.add_argument("--skip-model-install", action="store_true")
    a = ap.parse_args()
    out = Path(a.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    work = Path(a.work_dir).resolve() if a.work_dir else out.parent / "department_work"
    work.mkdir(parents=True, exist_ok=True)
    model_dir = work / "models"
    t0 = time.perf_counter()
    cams, speech_ok = synthesise_interview(work / "media")
    models = {} if a.skip_model_install else install_models(model_dir)
    job = ProductionJob.create(
        job_root=work / "jobs",
        brief="Full episode: match the camera colours, captions, blur the background behind the host, lower thirds and social clips",
        inputs=[f"{k}={v}" for k, v in cams.items()],
        profile_ref="interview_two_camera",
        mode="full_production_draft",
        options=JobOptions(export_video=True, episode_title="Bundle Verification", model_dir=str(model_dir)),
        job_id="bundle_verification",
    )
    job.run()
    d = job.doc
    agents = d["agents"]
    captions_path = d["artifacts"].get("captions_srt")
    caption_sample = Path(captions_path).read_text(encoding="utf-8")[:600] if captions_path and Path(captions_path).exists() else ""
    whisper_ready = bool(models.get("faster-whisper-tiny", {}).get("installed"))
    sam_ready = bool(models.get("mobilesam-onnx", {}).get("installed"))
    ei, vfx = agents["editorial_intelligence"], agents["vfx"]
    melt_ok = None
    if d["artifacts"].get("kdenlive_project") and shutil.which("melt"):
        render = work / "draft_melt_render.mp4"
        res = _run(["melt", d["artifacts"]["kdenlive_project"], "-consumer", f"avformat:{render}", "vcodec=libx264", "acodec=aac"])
        melt_ok = res.returncode == 0 and render.exists() and render.stat().st_size > 10_000
    result = {
        "evidence": evidence_class(),
        "speech_synthesis_available": speech_ok,
        "model_installs": models,
        "job_status": d["status"],
        "processing_elapsed_sec": d.get("processing_elapsed_sec"),
        "total_verification_sec": round(time.perf_counter() - t0, 2),
        "source_integrity_verified": (d.get("source_integrity") or {}).get("verified"),
        "agents": {k: {f: v.get(f) for f in ("status", "active", "neural_inference", "fallback_reason", "confidence", "operation_count", "summary", "error")} for k, v in agents.items()},
        "asr": {"neural": ei.get("neural_inference"), "fallback_reason": ei.get("fallback_reason"), "summary": ei.get("summary")},
        "captions": {"neural": agents["captions"].get("neural_inference"), "operation_count": agents["captions"].get("operation_count"), "srt_sample": caption_sample},
        "segmentation": {"neural": vfx.get("neural_inference"), "fallback_reason": vfx.get("fallback_reason")},
        "draft_melt_render_ok": melt_ok,
        "artifacts": d.get("artifacts", {}),
    }
    failures = []
    if d["status"] not in ("draft_ready", "awaiting_review"):
        failures.append(f"job status {d['status']}")
    if whisper_ready and speech_ok and not ei.get("neural_inference"):
        failures.append("Whisper installed but ASR did not run neural inference")
    if whisper_ready and speech_ok and not agents["captions"].get("operation_count"):
        failures.append("neural transcript available but no captions emitted")
    if sam_ready and vfx.get("active") and vfx.get("neural_inference") is False:
        failures.append("MobileSAM installed but VFX segmentation used a fallback")
    if melt_ok is False:
        failures.append("draft project failed to render with melt")
    result["v122"] = verify_v122_operations(job, cams, work, out, model_dir, failures)
    containment = verify_bundle_self_containment(work, failures)
    result["bundle_self_containment"] = containment
    result["videotoolbox_available"] = containment["videotoolbox_available"]
    result["peak_rss_mb"] = _peak_rss_mb()
    result["failures"] = failures
    result["passed"] = not failures
    (out / "department_verification.json").write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    shutil.copy2(job.dir / "summary.md", out / "summary.md")
    print(json.dumps({k: result[k] for k in ("passed", "failures", "job_status", "asr", "segmentation", "videotoolbox_available", "peak_rss_mb", "evidence")}, indent=2, default=str))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
