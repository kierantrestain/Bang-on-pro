#!/usr/bin/env python3
"""V1.3 vs V1.4 on identical inputs (synthetic footage), same machine.

1. The department job (full_production_draft) with each version's code: processing time, peak RSS,
   review items, and whether V1.4 emits the *same* draft project for the same batches (preserved
   behaviour).
2. One creative instruction that is not a predefined command: V1.3 (rule interpreter only) vs V1.4
   agents through the MCP tools (scripted tool calls = mocked reasoning; the live authenticated run is
   recorded separately in docs/evidence/v1.4/live_claude_code_run.json): edits made, checks verified,
   corrections, unresolved items for the editor, time per tool call, context size handed to a model.

usage: bench_v14_compare.py --v13 <worktree of c2d5c3e> --out <dir>

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import argparse
import json
import os
import re
import resource
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

RUN_JOB = r"""
import json, sys, time, resource
from pathlib import Path
from bangonpro.ai_engine.department import JobOptions, ProductionJob
base, cams = Path(sys.argv[1]), json.loads(sys.argv[2])
t0 = time.perf_counter()
j = ProductionJob.create(job_root=base, brief="Edit this podcast into a complete YouTube episode", inputs=[f"cam_{r}={p}" for r, p in cams.items()],
                         profile_ref="podcast_multicam", mode="full_production_draft", options=JobOptions(), job_id="ep")
j.run()
el = time.perf_counter() - t0
rev = j.revise("Make the guest's longest answer feel more dynamic with a listener cutaway, city B-roll near its end and a studio title")
plan = rev.get("plan") or {}
items = sum(len(a.get("review_items") or []) for a in j.doc["agents"].values())
print(json.dumps({"status": j.doc["status"], "processing_sec": round(el, 2), "review_items": items, "project": j.doc["artifacts"].get("kdenlive_project"),
                  "creative_instruction": rev.get("status"), "creative_message": rev.get("message"), "creative_actions": plan.get("actions"), "creative_start_agent": plan.get("start_agent"),
                  "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)}))
"""


def run_version(code_root: Path, base: Path, cams: dict[str, str], cache: Path) -> dict[str, Any]:
    env = {**os.environ, "PYTHONPATH": str(code_root), "BANGONPRO_CACHE_DIR": str(cache), "BANGONPRO_PREFS_DIR": str(base / "prefs")}
    res = subprocess.run([sys.executable, "-c", RUN_JOB, str(base), json.dumps(cams)], capture_output=True, text=True, env=env, cwd=str(code_root), check=True)
    out = json.loads(res.stdout.strip().splitlines()[-1])
    out["peak_child_rss_mb"] = round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024, 1)
    return out


def normalised(path: str) -> str:
    """The project minus what is unique per job (its random project id and the folder it lives in)."""
    t = re.sub(r'root="[^"]*"', 'root=""', Path(path).read_text())
    t = re.sub(r"\{[0-9a-f-]{36}\}", "{uuid}", t)
    t = re.sub(r"bop_[0-9a-f]{10}", "bop_id", t)
    return re.sub(r"/v1\.[34]/", "/vX/", t)


def agent_pass(job_dir: Path, assets: Path) -> dict[str, Any]:
    from bangonpro.ai_engine.mcp.__main__ import make_server
    from bangonpro.ai_engine.mcp.execution import context_pack
    from bangonpro.ai_engine.mcp.protocol import CallContext

    server, bt = make_server(str(job_dir), [str(assets)])
    server.protocol_version = "2025-06-18"
    ctx = CallContext(request_id="b", client_name="bench (scripted tool calls)")
    timings: list[tuple[str, float]] = []

    def call(name: str, **args: Any) -> dict[str, Any]:
        t0 = time.perf_counter()
        res = server.call_tool(server.tools[name], args, ctx)
        timings.append((name, time.perf_counter() - t0))
        return json.loads(res["content"][0]["text"])

    pack = context_pack(bt, ["multicam_director", "graphics"])
    v = bt.ws.view()
    guest = max((s for s in v.speech() if s["speaker_id"] == "Guest" and not s["overlap"]), key=lambda s: s["end"] - s["start"])
    mid = (guest["start"] + guest["end"]) / 2
    t = call("bop_task", action="begin", instruction="Guest answer: listener cutaway, B-roll near its end, studio title", agents=["multicam_director", "graphics"],
             expect=[{"check": "camera_at", "time": mid, "camera": "cam_host"}, {"check": "has_clip", "track": "V2", "start": guest["end"] - 0.6, "end": guest["end"] - 0.2},
                     {"check": "title_at", "time": 1.0, "text": "Live from the studio"}])
    call("bop_transcript")
    call("bop_frames", times=[guest["start"] + 1, mid])
    call("bop_frames", times=[guest["start"] + 1, mid])  # repeated look: served from the cache
    call("bop_switch_camera", expected_revision=bt.ws.revision, start=mid - 1.0, end=mid + 1.0, camera="cam_host", reason="reaction", agent="multicam_director", task_id=t["task_id"])
    clash = call("bop_place_asset", expected_revision=bt.ws.revision, file=str(assets / "broll_city.mp4"), start=round(guest["end"] - 2.2, 2), duration=2.2, fade=0.4, agent="graphics", task_id=t["task_id"])
    rejected_overlap = clash.get("error", {}).get("code") == "OPERATION_FAILED"
    if rejected_overlap:  # the guest's lower third is still up there: start the B-roll after it, as an editor would
        busy = max((c.timeline_end_inclusive + 1 for c in bt.ws.view().c.v2_clips if c.timeline_start < bt.ws.view().f(guest["end"])), default=0) / v.fps
        call("bop_place_asset", expected_revision=bt.ws.revision, file=str(assets / "broll_city.mp4"), start=round(busy, 2), duration=round(guest["end"] - busy, 2), fade=0.4, agent="graphics", task_id=t["task_id"])
    call("bop_add_title", expected_revision=bt.ws.revision, text="Live from the studio", start=0.0, duration=1.2, agent="graphics", task_id=t["task_id"])  # too short on purpose
    done = call("bop_task", action="complete", task_id=t["task_id"])
    per_tool: dict[str, list[float]] = {}
    for n, s in timings:
        per_tool.setdefault(n, []).append(s)
    return {"status": done["status"], "checks_passed": sum(c["ok"] for c in done["checks"]), "checks": len(done["checks"]), "edits": done["edits"],
            "corrections": [{"code": c["defect"]["code"], "result": c["result"]} for c in done["corrections"]], "unresolved_for_editor": len(done["unresolved"]),
            "tool_calls": len(timings), "tool_sec": {k: round(sum(val) / len(val), 3) for k, val in per_tool.items()}, "total_tool_sec": round(sum(s for _, s in timings), 2),
            "context_pack_bytes": len(json.dumps(pack, separators=(",", ":"))), "frame_cache": {"hits": bt.cache.hits, "misses": bt.cache.misses},
            "native_ops_applied": sum(len(e["batch"]["operations"]) for e in bt.ws.layer.active_entries()), "overlapping_placement_rejected": rejected_overlap}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--v13", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    import bench_v122_compare as bc
    from PIL import Image

    work = a.out / "work"
    work.mkdir(parents=True, exist_ok=True)
    cams = {r: str(p) for r, p in bc.generate_podcast_conversation(work).items()}
    assets = work / "assets"
    assets.mkdir(exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25", "-t", "6", "-pix_fmt", "yuv420p", str(assets / "broll_city.mp4")], check=True)
    Image.new("RGBA", (600, 200), (20, 20, 120, 230)).save(assets / "score_card.png")
    rows: dict[str, Any] = {"evidence": "synthetic generated footage; scripted tool calls (mocked reasoning) for the agent pass", "machine": os.uname().machine, "cpus": os.cpu_count()}
    for name, root in (("v1.3", a.v13.resolve()), ("v1.4", REPO)):
        rows[name] = run_version(root, work / name, cams, work / f"cache_{name}")
    rows["draft_identical"] = normalised(rows["v1.3"]["project"]) == normalised(rows["v1.4"]["project"])
    os.environ["BANGONPRO_CACHE_DIR"] = str(work / "cache_v1.4")
    os.environ["BANGONPRO_PREFS_DIR"] = str(work / "v1.4" / "prefs")
    rows["v1.4_agent_pass"] = agent_pass(work / "v1.4" / "ep", assets)
    for k in ("v1.3", "v1.4"):
        rows[k]["project"] = Path(rows[k]["project"]).name
    (a.out / "bench_v13_vs_v14.json").write_text(json.dumps(rows, indent=1))
    print(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
