"""V1.2.2 department benchmark: the four priority genres on synthetic multicamera footage.

Runs a full production draft per genre profile (podcast, gameshow, sports discussion,
daily news) and records machine processing time, per-agent time, quality-loop activity,
review load, QC findings and cache reuse on a second (re-edit) job. Human editing time
is NOT measured here (it requires paired editor sessions, see
docs/INTERNAL_TESTING_PROGRAMME.md); the modelled estimate is labelled as such.

Usage: python3 tests/bangonpro/bench_v122_department.py [--out docs/evidence/v1.2.2/department_benchmark.json]

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

GENRES = {
    "podcast_multicam": "Tight conversational cut, match the cameras, captions and two social clips",
    "gameshow_multicam": "Build the episode with reactions and applause, lower thirds and highlights",
    "sports_discussion": "Fast-paced panel cut, keep the banter, highlights for social",
    "daily_news": "Tight news package, remove dead air, captions and lower thirds",
}


def _run(base: Path, profile: str, brief: str, cams: dict[str, Path], job_id: str, export: bool) -> dict:
    from bangonpro.ai_engine.department import JobOptions, ProductionJob
    from bangonpro.ai_engine.department.measurement import modelled_estimate

    t0 = time.perf_counter()
    job = ProductionJob.create(
        job_root=base / "jobs", brief=brief, inputs=[f"{k}={v}" for k, v in cams.items()],
        profile_ref=profile, mode="full_production_draft",
        options=JobOptions(export_video=export, episode_title=f"{profile} pilot"), job_id=job_id,
    )
    doc = job.run()
    wall = time.perf_counter() - t0
    agents = {}
    for a, r in doc["agents"].items():
        if not r.get("active"):
            continue
        loop = r.get("quality_loop") or {}
        agents[a] = {
            "status": r.get("status"),
            "duration_sec": r.get("duration_sec"),
            "confidence": r.get("confidence"),
            "operations": r.get("operation_count"),
            "neural_inference": r.get("neural_inference"),
            "quality_score": r.get("quality_score"),
            "quality_initial_score": loop.get("initial_score"),
            "corrections_tried": len(loop.get("attempts", [])),
            "corrections_applied": loop.get("corrections_applied", 0),
            "review_items": len(r.get("review_items") or []),
            "cache_hits": r.get("cache_hits") or [],
        }
    summary = json.loads((job.dir / "summary.json").read_text(encoding="utf-8"))
    return {
        "profile": profile,
        "status": doc["status"],
        "wall_sec": round(wall, 2),
        "processing_elapsed_sec": doc.get("processing_elapsed_sec"),
        "quality_loop_attempts": doc.get("quality_loop_attempts", 0),
        "qc_passed": summary.get("qc_passed"),
        "qc_issue_count": summary.get("qc_issue_count"),
        "review_items_total": len(summary.get("review_items", [])),
        "operations": summary.get("operations"),
        "source_integrity_verified": (doc.get("source_integrity") or {}).get("verified"),
        "final_verification": doc.get("final_verification", {}),
        "agents": agents,
        "modelled_estimate": modelled_estimate(doc),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "docs" / "evidence" / "v1.2.2" / "department_benchmark.json"))
    ap.add_argument("--export", action="store_true", help="also render masters and derivatives")
    ap.add_argument("--profiles", nargs="*", default=list(GENRES))
    args = ap.parse_args(argv)

    base = Path(tempfile.mkdtemp(prefix="bop_bench_v122_"))
    os.environ["BANGONPRO_CACHE_DIR"] = str(base / "cache")
    os.environ["BANGONPRO_PREFS_DIR"] = str(base / "prefs")
    from test_ai_production import _generate_synthetic_4cam_studio

    cams = {k: v for k, v in _generate_synthetic_4cam_studio(base).items() if k.startswith("cam_")}
    rows = []
    for profile in args.profiles:
        rows.append(_run(base, profile, GENRES[profile], cams, f"{profile}_cold", args.export))
        reedit = _run(base, profile, GENRES[profile] + ", slightly looser pacing", cams, f"{profile}_reedit", False)
        rows[-1]["reedit"] = {
            "processing_elapsed_sec": reedit["processing_elapsed_sec"],
            "editorial_cache_hits": reedit["agents"].get("editorial_intelligence", {}).get("cache_hits", []),
            "editorial_duration_sec": reedit["agents"].get("editorial_intelligence", {}).get("duration_sec"),
        }
    out = {
        "evidence_class": "synthetic (generated 4-camera, 14 s programme); machine time only",
        "host": {"platform": platform.platform(), "machine": platform.machine(), "cpu_count": os.cpu_count(), "python": platform.python_version()},
        "human_editing_time": "NOT MEASURED - requires paired editor sessions (EXT-003)",
        "rows": rows,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2, default=str) + "\n", encoding="utf-8")
    for r in rows:
        print(f"{r['profile']:<20} {r['status']:<22} proc={r['processing_elapsed_sec']}s loop={r['quality_loop_attempts']} "
              f"qc_issues={r['qc_issue_count']} review={r['review_items_total']} reedit={r['reedit']['processing_elapsed_sec']}s")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
