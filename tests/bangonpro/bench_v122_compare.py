"""V1.2.1 vs V1.2.2 department benchmark on identical footage, briefs and production settings.

Both versions run the *same* generated multicamera scenarios, the *same* briefs, mode and
options, and (by default) the *same* production-profile JSON files (copied from the V1.2.1
checkout), each job in its own subprocess so peak memory can be measured. Quality is scored
against the scenarios' **ground truth** by this script, independently of any agent's internal
assessor, so a version cannot look better by optimising its own scores:

* editorial defects an editor would have to fix (``corrective_fixes_needed``): meaningful pauses
  or laughter removed, dead air left in, setup kept, cuts inside speech, a cut to an interjector,
  shots below the profile minimum, speech on the wrong camera, sync error above one frame, speech lost;
* review workload (review items and QC findings the editor must read);
* machine processing time (including quality-loop attempts), peak RSS, cache reuse on a re-edit,
  external cost.

Human editing time is NOT measured here (it needs paired editor sessions; see
docs/INTERNAL_TESTING_PROGRAMME.md). Evidence class: synthetic.

Usage::

    python3 tests/bangonpro/bench_v122_compare.py --baseline /path/to/v1.2.1/checkout \\
        [--candidate .] [--profiles-from baseline|own] [--out docs/evidence/v1.2.2/v121_vs_v122_benchmark.json]

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

SR = 48000
FPS = 25

# ----------------------------------------------------------------------------- scenarios
# Speech spans: (role, start, end) on the master clock. "both" = crosstalk.
S1_GT: dict[str, Any] = {
    "name": "studio_4cam",
    "description": "14 s, 4 cameras, mixed 25/30 fps, setup delay, 2 s dead air, crosstalk (existing V1.2 generator)",
    "duration": 14.0,
    "setup": (0.0, 2.0),
    "speech": [("host", 2.0, 5.5), ("guest", 7.5, 11.2), ("both", 11.2, 12.6), ("host", 12.6, 14.0)],
    "protected": [],
    "dead_air": [(5.5, 7.5)],
    "interjections": [],
    "offsets": {"wide": 0.0, "host": 0.40, "guest": 0.88, "reaction": 1.20},
}

S2_GT: dict[str, Any] = {
    "name": "podcast_conversation",
    "description": "47 s, 3 cameras, reflective pause inside a turn, laughter, a 0.35 s interjection, 4.5 s dead air, crosstalk",
    "duration": 47.0,
    "setup": (0.0, 3.0),
    "speech": [
        ("host", 3.0, 9.0), ("host", 10.2, 14.0), ("guest", 14.5, 21.0), ("host", 23.0, 29.0),
        ("guest", 33.5, 39.5), ("both", 39.5, 41.0), ("host", 41.0, 45.0),
    ],
    # (label, start, end, minimum seconds an editor keeps; genre-specific values in PAUSE_KEEP_SEC)
    "protected": [("reflective_pause", 9.0, 10.2, 0.7), ("laughter", 21.0, 22.6, 1.2)],
    "dead_air": [(29.0, 33.5)],
    "interjections": [("host", 17.0, 17.35)],
    "offsets": {"wide": 0.0, "host": 0.6, "guest": 1.3},
}

SCENARIOS = {"studio_4cam": S1_GT, "podcast_conversation": S2_GT}

# Scenario camera role -> camera id per profile (so each profile's speaker/camera map applies).
PROFILE_CAMERAS: dict[str, dict[str, str]] = {
    "podcast_multicam": {"wide": "cam_wide", "host": "cam_host", "guest": "cam_guest", "reaction": "cam_reaction"},
    "gameshow_multicam": {"wide": "cam_wide", "host": "cam_host", "guest": "cam_panel", "reaction": "cam_reaction"},
    "sports_discussion": {"wide": "cam_desk", "host": "cam_host", "guest": "cam_guest", "reaction": "cam_panel"},
    "daily_news": {"wide": "cam_wide", "host": "cam_desk", "guest": "cam_guest", "reaction": "cam_broll"},
}

# Editorial convention used as ground truth: a reflective pause inside a speaker's turn carries
# meaning and keeps at least this much of its beat (news is tightened hardest).
PAUSE_KEEP_SEC: dict[str, float] = {"podcast_multicam": 0.7, "gameshow_multicam": 0.6, "sports_discussion": 0.6, "daily_news": 0.4}

BRIEFS: dict[str, str] = {
    "podcast_multicam": "Conversational cut that keeps the natural rhythm, match the cameras, captions and social clips",
    "gameshow_multicam": "Build the episode with reactions and laughter, lower thirds and highlights",
    "sports_discussion": "Fast-paced panel cut, keep the banter, highlights for social",
    "daily_news": "Tight news package, remove dead air, captions and lower thirds",
}


def _laughter(duration: float, seed: int) -> np.ndarray:
    from scipy.signal import butter, sosfilt

    n = int(round(duration * SR))
    t = np.arange(n) / SR
    rng = np.random.default_rng(seed)
    sos = butter(4, [300 / (SR / 2), 4000 / (SR / 2)], btype="bandpass", output="sos")
    noise = sosfilt(sos, rng.standard_normal(n))
    noise /= max(1e-6, float(np.std(noise)))
    env = np.abs(np.sin(2 * np.pi * 2.6 * t)) ** 2 * np.exp(-t / (duration * 0.8))
    sig = noise * env
    return sig / max(1e-6, float(np.max(np.abs(sig))))


def generate_podcast_conversation(base: Path) -> dict[str, Path]:
    from test_ai_production import _synthesize_speech_like_signal, _write_wav_16bit

    gt = S2_GT
    n = int(round(gt["duration"] * SR))
    host = np.zeros(n)
    guest = np.zeros(n)
    slate = np.zeros(n)

    def place(buf: np.ndarray, start: float, sig: np.ndarray, gain: float) -> None:
        i = int(round(start * SR))
        j = min(n, i + sig.size)
        buf[i:j] += gain * sig[: j - i]

    click = np.random.default_rng(1).standard_normal(int(0.02 * SR)) * np.hanning(int(0.02 * SR))
    place(slate, 1.0, click / np.max(np.abs(click)), 0.45)
    seed = 600
    for role, s, e in gt["speech"]:
        seed += 7
        if role in ("host", "both"):
            place(host, s, _synthesize_speech_like_signal(e - s, SR, f0_hz=140.0, syllable_hz=3.9, seed=seed), 0.55 if role == "host" else 0.24)
        if role in ("guest", "both"):
            place(guest, s, _synthesize_speech_like_signal(e - s, SR, f0_hz=215.0, syllable_hz=4.4, seed=seed + 1), 0.55 if role == "guest" else 0.23)
    for role, s, e in gt["interjections"]:
        place(host if role == "host" else guest, s, _synthesize_speech_like_signal(e - s, SR, f0_hz=150.0, syllable_hz=4.0, seed=999), 0.30)
    lab, ls, le, _keep = gt["protected"][1]
    place(host, ls, _laughter(le - ls, 11), 0.40)
    place(guest, ls, _laughter(le - ls, 12), 0.45)

    mixes = {"wide": (0.45, 0.45, 0.6), "host": (1.0, 0.14, 0.55), "guest": (0.14, 1.0, 0.55)}
    colors = {"wide": "#1B3B6F", "host": "#9B2226", "guest": "#2D6A4F"}
    out_dir = base / "podcast_conversation"
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for idx, (role, (hg, gg, sg)) in enumerate(mixes.items()):
        off = gt["offsets"][role]
        lead = int(round(off * SR))
        audio = 0.0004 * np.random.default_rng(80 + idx).standard_normal(lead + n)
        audio[lead:] += hg * host + gg * guest + sg * slate
        wav = out_dir / f"{role}.wav"
        _write_wav_16bit(wav, audio, SR)
        mp4 = out_dir / f"{role}.mp4"
        dur = (lead + n) / SR
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={colors[role]}:s=640x360:r={FPS}:d={dur:.4f}",
               "-i", str(wav), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
               "-ar", str(SR), "-shortest", str(mp4)]
        subprocess.run(cmd, check=True, capture_output=True)
        wav.unlink()
        paths[role] = mp4
    return paths


def generate_studio_4cam(base: Path) -> dict[str, Path]:
    from test_ai_production import _generate_synthetic_4cam_studio

    cams = _generate_synthetic_4cam_studio(base / "studio_4cam_src")
    return {k.removeprefix("cam_"): v for k, v in cams.items() if k.startswith("cam_")}


GENERATORS = {"studio_4cam": generate_studio_4cam, "podcast_conversation": generate_podcast_conversation}

# ----------------------------------------------------------------------------- runner
_RUNNER = r"""
import json, os, resource, sys, time
args = json.loads(sys.argv[1])
os.environ["BANGONPRO_CACHE_DIR"] = args["cache"]
os.environ["BANGONPRO_PREFS_DIR"] = args["prefs"]
import bangonpro
from bangonpro.ai_engine.department import JobOptions, ProductionJob
t = time.perf_counter()
job = ProductionJob.create(job_root=args["job_root"], brief=args["brief"], inputs=args["inputs"], profile_ref=args["profile"],
                           mode=args["mode"], options=JobOptions(export_video=args["export"], episode_title="Pilot", budget_usd=args["budget"]),
                           job_id=args["job_id"])
doc = job.run()
wall = time.perf_counter() - t
print("BENCH_RESULT " + json.dumps({"job_dir": str(job.dir), "wall_sec": wall, "package": bangonpro.__file__,
      "maxrss_self": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
      "maxrss_children": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss}))
"""


def _rss_mb(v: int) -> float:
    return round(v / (1024.0 * 1024.0) if sys.platform == "darwin" else v / 1024.0, 1)


def run_job(checkout: Path, args: dict[str, Any]) -> dict[str, Any]:
    env = dict(os.environ, PYTHONPATH=str(checkout), PYTHONDONTWRITEBYTECODE="1")
    res = subprocess.run([sys.executable, "-c", _RUNNER, json.dumps(args)], cwd=str(checkout), env=env, capture_output=True, text=True, timeout=3600, check=False)
    line = next((ln for ln in res.stdout.splitlines() if ln.startswith("BENCH_RESULT ")), None)
    if res.returncode != 0 or line is None:
        raise RuntimeError(f"job {args['job_id']} failed in {checkout}:\n{res.stdout[-2000:]}\n{res.stderr[-4000:]}")
    out = json.loads(line.removeprefix("BENCH_RESULT "))
    if not Path(out["package"]).resolve().is_relative_to(checkout.resolve()):
        raise RuntimeError(f"benchmark imported {out['package']} instead of {checkout}")
    return out


# ----------------------------------------------------------------------------- ground-truth evaluation
def _union(iv: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for s, e in sorted(iv):
        if out and s <= out[-1][1] + 1e-3:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _overlap(kept: list[tuple[float, float]], s: float, e: float) -> float:
    return sum(max(0.0, min(e, b) - max(s, a)) for a, b in kept)


def evaluate(job_dir: Path, gt: dict[str, Any], path_roles: dict[str, str], min_shot: float, profile: str = "") -> dict[str, Any]:
    st = json.loads((job_dir / "draft" / "analysis_state.json").read_text(encoding="utf-8"))
    doc = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    summary = json.loads((job_dir / "summary.json").read_text(encoding="utf-8"))
    role_of = {cid: path_roles.get(str(Path(a["file_path"]).resolve()), "?") for cid, a in st["media_assets"].items()}
    shots = sorted(st["rough_cut_shots"], key=lambda s: s["timeline_in_sec"])
    kept = _union([(s["master_sync_in_sec"], s["master_sync_out_sec"]) for s in shots])
    fixes: dict[str, int] = {}

    def fix(code: str, n: int = 1) -> None:
        if n:
            fixes[code] = fixes.get(code, 0) + n

    speech_total = sum(e - s for _, s, e in gt["speech"])
    speech_kept = sum(_overlap(kept, s, e) for _, s, e in gt["speech"])
    for _, s, e in gt["speech"]:
        if (e - s) - _overlap(kept, s, e) > 0.2:
            fix("speech_lost")
    protected = []
    for label, s, e, need in gt["protected"]:
        if label == "reflective_pause":
            need = PAUSE_KEEP_SEC.get(profile, need)
        k = _overlap(kept, s, e)
        protected.append({"label": label, "kept_sec": round(k, 2), "required_sec": need, "ok": k >= need - 0.05})
        if k < need - 0.05:
            fix(f"{label}_removed")
    dead = []
    for s, e in gt["dead_air"]:
        k = _overlap(kept, s, e)
        dead.append({"span": [s, e], "remaining_sec": round(k, 2), "ok": k <= 1.0})
        if k > 1.0:
            fix("dead_air_left")
    setup_kept = _overlap(kept, *gt["setup"])
    if setup_kept > 0.5:
        fix("setup_kept")
    # Cuts inside speech: a master discontinuity that starts or ends well inside a speech span.
    inside = 0
    for a, b in zip(kept, kept[1:]):
        gap_s, gap_e = a[1], b[0]
        for _, s, e in gt["speech"]:
            if s + 0.15 < gap_s < e - 0.15 or s + 0.15 < gap_e < e - 0.15:
                inside += 1
                break
    fix("cut_inside_speech", inside)
    # Camera choice over speech.
    right = total = 0.0
    wrong_spans = 0
    for role, s, e in gt["speech"]:
        ok_roles = {"wide"} if role == "both" else {role, "wide"}
        span_right = span_total = 0.0
        for sh in shots:
            ov = max(0.0, min(e, sh["master_sync_out_sec"]) - max(s, sh["master_sync_in_sec"]))
            if ov > 0:
                span_total += ov
                if role_of.get(sh["camera_id"]) in ok_roles:
                    span_right += ov
        right += span_right
        total += span_total
        if span_total > 0 and span_right / span_total < 0.5:
            wrong_spans += 1
    fix("speech_on_wrong_camera", wrong_spans)
    interj = 0
    for role, s, e in gt["interjections"]:
        interj += sum(1 for sh in shots if role_of.get(sh["camera_id"]) == role and s - 0.3 <= sh["master_sync_in_sec"] <= e + 0.5)
    fix("cut_to_interjection", interj)
    durs = [sh["timeline_out_sec"] - sh["timeline_in_sec"] for sh in shots]
    short = sum(1 for d in durs[:-1] if d < min_shot - 0.05)
    fix("shot_below_minimum", short)
    sync_err = {}
    sr = st.get("sync_result") or {}
    for cid, off in (sr.get("offsets_sec") or {}).items():
        role = role_of.get(cid)
        if role in gt["offsets"]:
            sync_err[role] = round(abs(float(off) - gt["offsets"][role]) * 1000.0, 1)
    fix("sync_error_over_one_frame", sum(1 for v in sync_err.values() if v > 1000.0 / FPS))
    agents = doc["agents"]
    return {
        "corrective_fixes_needed": sum(fixes.values()),
        "fixes_by_type": fixes,
        "speech_retained_pct": round(100.0 * speech_kept / speech_total, 1) if speech_total else None,
        "protected": protected,
        "dead_air": dead,
        "setup_kept_sec": round(setup_kept, 2),
        "speech_on_correct_camera_pct": round(100.0 * right / total, 1) if total else None,
        "cuts": max(0, len(shots) - 1),
        "mean_shot_sec": round(float(np.mean(durs)), 2) if durs else None,
        "edited_duration_sec": round(sum(durs), 2),
        "sync_error_ms": sync_err,
        "review_items": len(summary.get("review_items", [])),
        "qc_findings": summary.get("qc_issue_count"),
        "qc_errors": sum(1 for i in st["qc_report"]["issues"] if i["severity"] == "error"),
        "processing_elapsed_sec": doc.get("processing_elapsed_sec"),
        "quality_loop_processing_sec": doc.get("quality_loop_processing_sec", 0.0),
        "quality_loop_attempts": doc.get("quality_loop_attempts", 0),
        "corrections_applied": sum(int((r.get("quality_loop") or {}).get("corrections_applied", 0)) for r in agents.values()),
        "external_cost_usd": (doc.get("budget") or {}).get("spent_usd", 0.0),
        "agent_sec": {a: r.get("duration_sec") for a, r in agents.items() if r.get("active") and r.get("duration_sec") is not None},
        "editorial_cache_hits": agents.get("editorial_intelligence", {}).get("cache_hits", []),
        "status": doc.get("status"),
        "source_integrity_verified": (doc.get("source_integrity") or {}).get("verified"),
    }


# ----------------------------------------------------------------------------- driver
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--baseline", required=True, help="checkout of v1.2.1")
    ap.add_argument("--candidate", default=str(REPO), help="checkout of v1.2.2 (default: this repo)")
    ap.add_argument("--profiles-from", choices=("baseline", "own"), default="baseline",
                    help="baseline = both versions use V1.2.1 profile files (identical settings); own = each its shipped profiles")
    ap.add_argument("--profiles", nargs="*", default=list(PROFILE_CAMERAS))
    ap.add_argument("--scenarios", nargs="*", default=list(SCENARIOS))
    ap.add_argument("--mode", default="full_production_draft")
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--out", default=str(REPO / "docs" / "evidence" / "v1.2.2" / "v121_vs_v122_benchmark.json"))
    a = ap.parse_args(argv)

    versions = {"v1.2.1": Path(a.baseline).resolve(), "v1.2.2": Path(a.candidate).resolve()}
    base = Path(tempfile.mkdtemp(prefix="bop_compare_"))
    media = {name: GENERATORS[name](base / "media") for name in a.scenarios}
    path_roles = {str(p.resolve()): role for cams in media.values() for role, p in cams.items()}
    hashes_before = {str(p): p.stat().st_mtime_ns for cams in media.values() for p in cams.values()}

    rows: list[dict[str, Any]] = []
    # Discarded warm-up per version (imports, codec and disk caches), then alternate run order.
    for ver, checkout in versions.items():
        scen0 = a.scenarios[0]
        w = base / "warmup" / ver
        run_job(checkout, {"cache": str(w / "cache"), "prefs": str(w / "prefs"), "job_root": str(w / "jobs"), "mode": a.mode, "export": False, "budget": 0.0,
                           "profile": str(versions["v1.2.1"] / "bangonpro" / "show_profiles" / "podcast_multicam.json"), "brief": BRIEFS["podcast_multicam"],
                           "inputs": [f"{PROFILE_CAMERAS['podcast_multicam'][r]}={p}" for r, p in media[scen0].items()], "job_id": "warmup"})
    combo = 0
    for profile in a.profiles:
        for scen in a.scenarios:
            gt = SCENARIOS[scen]
            inputs = [f"{PROFILE_CAMERAS[profile][role]}={p}" for role, p in media[scen].items()]
            order = list(versions.items()) if combo % 2 == 0 else list(reversed(list(versions.items())))
            combo += 1
            for ver, checkout in order:
                src = versions["v1.2.1"] if a.profiles_from == "baseline" else checkout
                prof_path = src / "bangonpro" / "show_profiles" / f"{profile}.json"
                prof = json.loads(prof_path.read_text(encoding="utf-8"))
                min_shot = float(prof.get("min_shot_duration_sec", 2.0))
                vdir = base / ver / profile / scen
                common = {"cache": str(vdir / "cache"), "prefs": str(vdir / "prefs"), "job_root": str(vdir / "jobs"),
                          "profile": str(prof_path), "mode": a.mode, "export": bool(a.export), "budget": 0.0, "inputs": inputs}
                cold = run_job(checkout, {**common, "brief": BRIEFS[profile], "job_id": "cold"})
                ev = evaluate(Path(cold["job_dir"]), gt, path_roles, min_shot, profile)
                re = run_job(checkout, {**common, "brief": BRIEFS[profile] + ", slightly looser pacing", "job_id": "reedit", "export": False})
                ev_re = json.loads((Path(re["job_dir"]) / "job.json").read_text(encoding="utf-8"))
                ei = ev_re["agents"].get("editorial_intelligence", {})
                rows.append({
                    "version": ver, "profile": profile, "scenario": scen, **ev,
                    "wall_sec": round(cold["wall_sec"], 2),
                    "peak_rss_mb": _rss_mb(cold["maxrss_self"]),
                    "peak_child_rss_mb": _rss_mb(cold["maxrss_children"]),
                    "reedit": {"processing_elapsed_sec": ev_re.get("processing_elapsed_sec"), "editorial_sec": ei.get("duration_sec"),
                               "editorial_cache_hits": ei.get("cache_hits", []), "wall_sec": round(re["wall_sec"], 2)},
                })
                r = rows[-1]
                print(f"{ver} {profile:<18} {scen:<21} fixes={r['corrective_fixes_needed']:<3} review={r['review_items']:<3} "
                      f"proc={r['processing_elapsed_sec']}s rss={r['peak_rss_mb']}MB reedit_ei={r['reedit']['editorial_sec']}s", flush=True)
    for p, mt in hashes_before.items():
        assert Path(p).stat().st_mtime_ns == mt, f"source media modified: {p}"

    def agg(ver: str, key: str) -> float:
        vals = [r[key] for r in rows if r["version"] == ver and isinstance(r[key], (int, float))]
        return round(float(np.mean(vals)), 3) if vals else float("nan")

    totals = {ver: {k: agg(ver, k) for k in ("corrective_fixes_needed", "review_items", "qc_findings", "cuts", "speech_retained_pct",
                                              "speech_on_correct_camera_pct", "processing_elapsed_sec", "quality_loop_processing_sec",
                                              "peak_rss_mb", "peak_child_rss_mb", "external_cost_usd")} for ver in versions}
    for ver in versions:
        totals[ver]["reedit_editorial_sec_mean"] = round(float(np.mean([r["reedit"]["editorial_sec"] or 0 for r in rows if r["version"] == ver])), 3)
        totals[ver]["fixes_total"] = sum(r["corrective_fixes_needed"] for r in rows if r["version"] == ver)
        by: dict[str, int] = {}
        for r in rows:
            if r["version"] == ver:
                for k, n in r["fixes_by_type"].items():
                    by[k] = by.get(k, 0) + n
        totals[ver]["fixes_by_type"] = by
    out = {
        "evidence_class": "synthetic generated multicamera scenarios; ground-truth editorial evaluation; machine time only",
        "identical_inputs": {"footage": "same generated files for both versions", "briefs": BRIEFS, "mode": a.mode, "export": a.export,
                             "profiles_from": a.profiles_from, "budget_usd": 0.0},
        "human_editing_time": "NOT MEASURED - requires paired editor sessions (EXT-003)",
        "host": {"platform": platform.platform(), "machine": platform.machine(), "cpu_count": os.cpu_count(), "python": platform.python_version()},
        "scenarios": {k: {kk: vv for kk, vv in v.items()} for k, v in SCENARIOS.items() if k in a.scenarios},
        "pause_keep_sec_ground_truth": PAUSE_KEEP_SEC,
        "totals": totals,
        "rows": rows,
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(totals, indent=1))
    shutil.rmtree(base, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
