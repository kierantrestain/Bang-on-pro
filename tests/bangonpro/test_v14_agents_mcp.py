"""V1.4: the 14 production agents operate Bang On Pro's native editing through the MCP adapter.

Evidence classes, kept apart on purpose:
* **real protocol**: the stdio MCP server in a subprocess (and the official ``mcp`` SDK client when it is
  installed) - no reasoning involved;
* **real draft**: every assertion about an edit is made on the replayed draft and on the written
  ``.kdenlive`` project (and, where marked, on a melt render), never on a tool's own claim;
* **mocked reasoning**: :class:`ScriptedReasoner` stands in for Claude Code / Codex; it proves the cycle,
  not the model. Live authenticated-client runs are recorded separately in the release report.
Footage is generated (synthetic multicamera podcast), so editorial quality numbers here are synthetic.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
REPO = Path(__file__).resolve().parents[2]

from bangonpro.ai_engine.commands import TimelineCommandBuilder  # noqa: E402
from bangonpro.ai_engine.project_builder import compile_command_batches, emit_kdenlive_xml  # noqa: E402

needs_media = pytest.mark.skipif(not all(shutil.which(t) for t in ("ffmpeg", "ffprobe", "melt")), reason="ffmpeg/ffprobe/melt required")
needs_render = pytest.mark.skipif(not (shutil.which("melt") and (os.environ.get("DISPLAY") or sys.platform == "darwin")), reason="melt with a display (xvfb-run) required")


# ======================================================================= unit: draft replay
def _batch(ops: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    b = TimelineCommandBuilder(project_id="p", sequence_id="s", expected_revision=0, agent_id="t", agent_role="t")
    for t, kw in ops:
        b.add_operation(t, **kw)
    return b.build()


def test_structural_ops_replay_into_the_draft(tmp_path: Path) -> None:
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"\0" * 64)
    img = tmp_path / "logo.png"
    img.write_bytes(b"\0" * 64)
    b = _batch([
        ("import_media", {"media_ref": "m", "params": {"file_path": str(clip), "duration_sec": 40, "video_codec": "h264"}}),
        ("import_media", {"media_ref": "logo", "params": {"file_path": str(img), "duration_sec": 3600, "is_image": True, "video_codec": "image"}}),
        ("place_clip", {"media_ref": "m", "clip_id": "c1", "track_id": "V1", "in_frame": 0, "out_frame": 99, "timeline_start": 0}),
        ("place_clip", {"media_ref": "m", "clip_id": "c2", "track_id": "V1", "in_frame": 200, "out_frame": 299, "timeline_start": 100}),
        ("place_clip", {"media_ref": "m", "clip_id": "a1", "track_id": "A1", "in_frame": 0, "out_frame": 199, "timeline_start": 0}),
        ("split_clip", {"clip_id": "c1", "params": {"split_frame": 50, "new_clip_id": "c1r"}}),
        ("trim_clip", {"clip_id": "c1r", "params": {"in_frame": 50, "out_frame": 79}}),
        ("move_clip", {"clip_id": "c2", "params": {"timeline_start": 80}}),
        ("add_transition", {"clip_id": "c2", "params": {"kind": "dissolve", "duration_frames": 10}}),
        ("set_audio_level", {"clip_id": "a1", "params": {"keyframes": [{"frame": 0, "gain_db": -60}, {"frame": 25, "gain_db": 0}]}}),
        ("add_effect", {"clip_id": "c1", "params": {"mlt_service": "lift_gamma_gain", "kdenlive_id": "lift_gamma_gain", "properties": {"gain_r": 1.0}}}),
        ("set_effect_param", {"clip_id": "c1", "params": {"kdenlive_id": "lift_gamma_gain", "property": "gain_r", "value": 1.1}}),
        ("place_clip", {"media_ref": "logo", "clip_id": "g1", "track_id": "V2", "in_frame": 0, "out_frame": 49, "timeline_start": 10}),
        ("add_effect", {"clip_id": "g1", "params": {"mlt_service": "qtblend", "kdenlive_id": "qtblend", "properties": {"rect": "0 0 100 100 1"}}}),
        ("set_keyframe", {"clip_id": "g1", "params": {"kdenlive_id": "qtblend", "property": "rect", "keyframes": [{"frame": 0, "value": "0 0 100 100 0"}, {"frame": 10, "value": "0 0 100 100 1", "interpolation": "smooth"}]}}),
        ("move_clip", {"clip_id": "c1", "params": {"timeline_start": 60}}),  # overlaps c1r: rejected
        ("delete_clip", {"clip_id": "missing"}),  # rejected
    ])
    res: list[dict[str, Any]] = []
    c = compile_command_batches([b], res)
    by = {r["op_id"]: r for r in res}
    assert [r["applied"] for r in res][-2:] == [False, False]
    assert "overlaps" in res[-2]["message"] and "does not exist" in res[-1]["message"]
    assert [(x.clip_id, x.timeline_start, x.in_frame, x.out_frame) for x in c.v1_clips] == [("c1", 0, 0, 49), ("c1r", 50, 50, 79)]
    assert [(x.clip_id, x.timeline_start, x.in_frame) for x in c.v1b_clips] == [("c2", 70, 190)] and c.transitions[0]["in_frame"] == 70
    assert c.find("c1")[1].filters[0]["properties"]["gain_r"] == 1.1
    assert c.find("g1")[1].filters[0]["properties"]["rect"] == "0=0 0 100 100 0;10~=0 0 100 100 1"
    assert c.find("a1")[1].filters[0]["properties"]["level"] == "0=-60.0;25=0.0"
    assert all(by[k]["applied"] for k in list(by)[:-2])
    # The written project carries all of it as native, editable Kdenlive/MLT elements.
    out = emit_kdenlive_xml(c, tmp_path / "p.kdenlive")
    xml = out.read_text()
    assert 'mlt_service">luma<' in xml and "kdenlive:mixcut" in xml
    assert 'mlt_service">qimage<' in xml and "10~=0 0 100 100 1" in xml
    import defusedxml.ElementTree as DET

    root = DET.parse(str(out)).getroot()
    pls = {pl.get("id"): pl for pl in root.iter("playlist")}
    assert len(pls["playlist_v1_b"].findall("entry")) == 1 and len(pls["playlist_v2"].findall("entry")) == 1


def test_fourteen_agents_have_roles_and_enforced_permissions() -> None:
    from bangonpro.ai_engine.department.agent_roles import ROLES, permission_violations
    from bangonpro.ai_engine.department.registry import AGENT_ORDER

    assert tuple(ROLES) == AGENT_ORDER and len(ROLES) == 14
    assert all(r["brief"] and r["done_when"] for r in ROLES.values())
    tracks = {"v": "V1", "a": "A1", "g": "V2"}
    assert permission_violations("multicam_director", [{"type": "split_clip", "clip_id": "v"}], tracks) == []
    assert permission_violations("multicam_director", [{"type": "split_clip", "clip_id": "g"}], tracks)  # V2 is not its track
    assert permission_violations("colourist", [{"type": "add_effect", "clip_id": "v", "params": {"kdenlive_id": "lift_gamma_gain"}}], tracks) == []
    assert permission_violations("colourist", [{"type": "add_effect", "clip_id": "v", "params": {"kdenlive_id": "avfilter.gblur"}}], tracks)
    assert permission_violations("quality_control", [{"type": "delete_clip", "clip_id": "v"}], tracks)
    assert permission_violations("executive", [{"type": "move_clip", "clip_id": "v"}], tracks)


# ======================================================================= real jobs
@pytest.fixture(scope="module")
def footage(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    import bench_v122_compare as bc

    base = tmp_path_factory.mktemp("v14")
    cams = bc.generate_podcast_conversation(base)
    assets = base / "assets"
    assets.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25", "-t", "6", "-pix_fmt", "yuv420p", str(assets / "broll_city.mp4")], check=True)
    from PIL import Image

    Image.new("RGBA", (600, 200), (20, 20, 120, 230)).save(assets / "score_card.png")
    return {"base": base, "cams": cams, "assets": assets}


@pytest.fixture
def job(footage: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from bangonpro.ai_engine.department import JobOptions, ProductionJob

    monkeypatch.setenv("BANGONPRO_CACHE_DIR", str(footage["base"] / "cache"))
    monkeypatch.setenv("BANGONPRO_PREFS_DIR", str(tmp_path / "prefs"))
    monkeypatch.setenv("BANGONPRO_CONFIG_DIR", str(tmp_path / "config"))
    j = ProductionJob.create(job_root=tmp_path / "jobs", brief="Edit this podcast into a complete YouTube episode", inputs=[f"cam_{r}={p}" for r, p in footage["cams"].items()],
                             profile_ref="podcast_multicam", mode="full_production_draft", options=JobOptions(), job_id="ep")
    j.run()
    assert j.doc["status"] == "draft_ready"
    return j


class Client:
    """In-process MCP calls through the same validation and result path as the stdio server."""

    def __init__(self, job_dir: Path, allow: list[Path]) -> None:
        from bangonpro.ai_engine.mcp.__main__ import make_server
        from bangonpro.ai_engine.mcp.protocol import CallContext

        self.server, self.bt = make_server(str(job_dir), [str(a) for a in allow])
        self.server.protocol_version = "2025-06-18"
        self.ctx = CallContext(request_id="t", client_name="pytest")

    def __call__(self, name: str, **args: Any) -> dict[str, Any]:
        errs = list(self.server._validators[name].iter_errors(args))
        assert not errs, [e.message for e in errs]
        res = self.server.call_tool(self.server.tools[name], args, self.ctx)
        data = json.loads(res["content"][0]["text"])
        data["_images"] = sum(1 for c in res["content"] if c["type"] == "image")
        data["_error"] = res["isError"]
        return data

    @property
    def rev(self) -> int:
        return self.bt.ws.revision


def _sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


@needs_media
def test_stdio_protocol_conventions(job: Any) -> None:
    """Real subprocess, real stdio framing: handshake, annotations, strict args, bad lines survive."""
    proc = subprocess.Popen([sys.executable, "-m", "bangonpro.ai_engine.mcp", "serve", "--job", str(job.dir)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=REPO,
                            env={**os.environ, "PYTHONPATH": str(REPO)})
    try:
        def send(msg: Any, raw: str | None = None) -> dict[str, Any] | None:
            proc.stdin.write((raw if raw is not None else json.dumps(msg)) + "\n")
            proc.stdin.flush()
            if isinstance(msg, dict) and "id" not in msg and raw is None:
                return None
            return json.loads(proc.stdout.readline())

        init = send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01", "clientInfo": {"name": "Claude Code"}}})
        assert init["result"]["protocolVersion"] == "2025-06-18" and "instructions" in init["result"]
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        assert len(tools) >= 20
        for t in tools:
            a = t["annotations"]
            assert t["title"] and {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"} <= set(a), t["name"]
            assert t["inputSchema"]["additionalProperties"] is False
        ro = {t["name"] for t in tools if t["annotations"]["readOnlyHint"]}
        assert {"bop_status", "bop_project", "bop_frames", "bop_qc"} <= ro and "bop_edit" not in ro
        assert send(None, raw="this is not json")["error"]["code"] == -32700
        assert send({"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"] == -32601
        bad = send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "bop_project", "arguments": {"strat": 1}}})
        assert bad["error"]["code"] == -32602 and "start" in bad["error"]["data"]["accepted"]
        ok = send({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "bop_status", "arguments": {}}})
        st = ok["result"]["structuredContent"]
        assert st["connected"] and st["backend"] == "draft" and st["client"] == "Claude Code"
        err = send({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "bop_switch_camera", "arguments": {"expected_revision": 0, "start": 1, "end": 2, "camera": "cam_guest"}}})
        body = json.loads(err["result"]["content"][0]["text"])
        assert err["result"]["isError"] and body["error"]["code"] == "STALE_REVISION" and body["error"]["retryable"]
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
    presence = json.loads((job.draft_dir / "agent_presence.json").read_text())
    assert presence["client"] == "Claude Code" and presence["status"] == "disconnected"


@needs_media
def test_official_mcp_sdk_client_can_connect(job: Any) -> None:
    """Independent conformance check with the reference MCP client implementation (dev-only dependency)."""
    mcp = pytest.importorskip("mcp")
    import anyio
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def run() -> tuple[int, dict[str, Any]]:
        params = StdioServerParameters(command=sys.executable, args=["-m", "bangonpro.ai_engine.mcp", "serve", "--job", str(job.dir)], env={**os.environ, "PYTHONPATH": str(REPO)}, cwd=str(REPO))
        async with stdio_client(params) as (r, w), mcp.ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            res = await s.call_tool("bop_project", {"tracks": ["V1"], "limit": 3})
            return len(tools.tools), json.loads(res.content[0].text)

    n, proj = anyio.run(run)
    assert n >= 20 and len(proj["clips"]) == 3 and proj["partial"] is True


@needs_media
def test_switch_camera_edit_is_real_verified_and_exactly_undoable(job: Any, footage: dict[str, Any]) -> None:
    c = Client(job.dir, [footage["assets"]])
    proj = Path(c("bop_status")["project"])
    before = _sha(proj)
    shots = c("bop_project", tracks=["V1"])["clips"]
    host = next(s for s in shots if s["camera"] == "cam_host" and s["end"] - s["start"] > 4)
    a, b = host["start"] + 1.0, host["start"] + 3.0
    out = c("bop_switch_camera", expected_revision=c.rev, start=a, end=b, camera="cam_guest", reason="reaction", agent="multicam_director")
    assert not out["_error"] and out["verified"]["ok"], out
    v = c.bt.ws.view()
    assert v.camera_at(v.f(a + 1.0)) == "cam_guest" and v.camera_at(v.f(a - 0.5)) == "cam_host" and v.camera_at(v.f(b + 0.5)) == "cam_host"
    # Same moment in time: picture and dialogue stay in sync across the switch; the grade travelled with the camera.
    pm, _ = v.picture_master_at(v.f(a + 1.0))
    assert abs(pm - v.audio_master_at(v.f(a + 1.0))) < 1.0 / v.fps + 1e-6
    assert not [d for d in c("bop_qc")["defects"] if d["code"] in ("AV_DESYNC", "INCONSISTENT_COLOUR", "PICTURE_GAP")]
    assert _sha(proj) != before
    c("bop_history", action="undo")
    assert _sha(proj) == before  # the draft is byte-identical to before the edit
    c("bop_history", action="redo")
    assert c.bt.ws.view().camera_at(v.f(a + 1.0)) == "cam_guest"
    # Session recovery: a new server process for the same job resumes at the same revision and history
    c2 = Client(job.dir, [footage["assets"]])
    assert c2.rev == c.rev and [e["entry_id"] for e in c2("bop_history")["edits"]] == [e["entry_id"] for e in c("bop_history")["edits"]]
    assert c2.bt.ws.view().camera_at(v.f(a + 1.0)) == "cam_guest"


@needs_media
def test_rejections_stale_permission_locks_conflicts_and_manual_edits(job: Any, footage: dict[str, Any]) -> None:
    c = Client(job.dir, [footage["assets"]])
    r0 = c.rev
    assert c("bop_switch_camera", expected_revision=r0 - 1, start=3, end=4, camera="cam_guest")["error"]["code"] == "STALE_REVISION"
    assert c("bop_edit", agent="colourist", expected_revision=r0, ops=[{"type": "delete_clip", "clip_id": "shot_0002_v"}])["error"]["code"] == "PERMISSION_DENIED"
    assert c("bop_edit", agent="rough_cut", expected_revision=r0, ops=[{"type": "trim_clip", "clip_id": "nope", "source_in": 0, "source_out": 1}])["error"]["code"] == "OPERATION_FAILED"
    # Protected range (agent-requested) and a range the editor locked in V1.3 (master clock)
    c("bop_lock", start=3.0, end=5.0)
    assert c("bop_switch_camera", expected_revision=r0, start=3.5, end=4.5, camera="cam_guest")["error"]["code"] == "LOCKED_REGION"
    job.lock_range(20.0, 22.0, timeline=True)
    assert c("bop_switch_camera", expected_revision=r0, start=20.5, end=21.5, camera="cam_wide")["error"]["code"] == "LOCKED_REGION"
    c("bop_lock", track="A1")
    a1 = c("bop_project", tracks=["A1"])["clips"][0]["id"]
    assert c("bop_edit", agent="audio_engineer", expected_revision=r0, ops=[{"type": "set_audio_level", "clip_id": a1, "params": {"gain_db": -3}}])["error"]["code"] == "TRACK_LOCKED"
    # Two agents in one task never change the same track range
    t = c("bop_task", action="begin", instruction="graphics pass", agents=["graphics", "vfx"])
    c("bop_task", action="lease", task_id=t["task_id"], agent="graphics", tracks=["V2"], start=0, end=15)
    placed = c("bop_place_asset", expected_revision=r0, file=str(footage["assets"] / "score_card.png"), start=8, duration=3, agent="graphics")
    assert placed["verified"]["ok"]
    card = placed["diff"]["added"][0]
    out = c("bop_edit", agent="vfx", expected_revision=c.rev, ops=[{"type": "add_effect", "clip_id": card, "params": {"mlt_service": "avfilter.gblur", "kdenlive_id": "avfilter.gblur"}}])
    assert out["error"]["code"] == "CONFLICT" and "graphics" in out["error"]["message"]
    c("bop_task", action="complete", task_id=t["task_id"])  # releases the lease
    assert c("bop_edit", agent="vfx", expected_revision=c.rev, ops=[{"type": "add_effect", "clip_id": card, "params": {"mlt_service": "avfilter.gblur", "kdenlive_id": "avfilter.gblur"}}])["verified"]["ok"]
    # A completion check with a wrong key is refused up front (seen in the live Claude Code run), and a
    # retried instruction supersedes the earlier attempt instead of leaving stale issues for the editor
    errs = list(c.server._validators["bop_task"].iter_errors({"action": "begin", "instruction": "x", "expect": [{"type": "camera_at", "time": 1}]}))
    assert errs
    t1 = c("bop_task", action="begin", instruction="same brief")
    c("bop_task", action="begin", instruction="same brief")
    assert c.bt.ws.layer.doc["tasks"][t1["task_id"]]["status"] == "superseded"
    # Supplied files outside the approved folders are refused
    assert c("bop_place_asset", expected_revision=c.rev, file="/etc/hostname", start=1, duration=1, agent="graphics")["error"]["code"] in ("PATH_NOT_ALLOWED", "NOT_FOUND")
    # The editor changed the draft by hand: agents stop and say so; a later rebuild keeps the manual version
    proj = Path(c("bop_status")["project"])
    proj.write_text(proj.read_text().replace("Multicam Program V1", "Multicam Program V1 (edited by hand)"))
    assert c("bop_status")["manual_edit_drift"] is True
    assert c("bop_switch_camera", expected_revision=c.rev, start=30, end=31, camera="cam_wide")["error"]["code"] == "MANUAL_EDIT_DRIFT"
    job.rebuild_draft()
    assert list(job.draft_dir.glob("seq_main_draft_manual_*.kdenlive"))


@needs_media
def test_rerun_of_an_agent_never_retargets_agent_edits(job: Any, footage: dict[str, Any]) -> None:
    c = Client(job.dir, [footage["assets"]])
    cap_free = c("bop_add_title", expected_revision=c.rev, text="Bang On", start=30.0, duration=3.0)
    assert cap_free["verified"]["ok"]
    host = next(s for s in c("bop_project", tracks=["V1"])["clips"] if s["camera"] == "cam_host")
    c("bop_switch_camera", expected_revision=c.rev, start=host["start"] + 0.5, end=host["start"] + 2.0, camera="cam_wide", agent="multicam_director")
    rev = c.rev
    out = c("bop_run_agent", agent="multicam_director", overrides={"min_shot_duration_sec": 4.0})
    assert out["revision"] > rev  # the department's own rerun is a new revision too
    entries = {e["entry_id"]: e for e in c.bt.ws.layer.doc["entries"]}
    statuses = sorted(e.get("replay_status") for e in entries.values())
    # The title (unaffected V2) is still applied; the switch targeted a shot the rerun rebuilt: stale, not re-aimed.
    assert "applied" in statuses and set(statuses) <= {"applied", "stale"}
    switch = next(e for e in entries.values() if e["agent_id"] == "multicam_director")
    if switch["replay_status"] == "stale":
        assert switch["entry_id"] in c("bop_status")["stale_agent_edits"]


@needs_media
def test_podcast_workflow_by_specialists_with_visual_check_and_verified_correction(job: Any, footage: dict[str, Any]) -> None:
    """Footage inspection, editorial decision, multicam, graphics, audio, captions, vertical clip, QC, correction."""
    c = Client(job.dir, [footage["assets"]])
    t = c("bop_task", action="begin", instruction="Podcast pass: reaction on the host's question, score card and B-roll, title, level, quote caption, a Short",
          agents=["editorial_intelligence", "multicam_director", "graphics", "audio_engineer", "captions", "distribution"],
          expect=[{"check": "has_clip", "track": "V2", "start": 28, "end": 30}, {"check": "title_at", "time": 1.0, "text": "Episode 12"}, {"check": "caption_contains", "text": "best show"}])
    tid = t["task_id"]
    assert t["context"]["agents"]["multicam_director"]["brief"] and t["context"]["transcript_evidence"]
    # Inspect: who speaks where, what it looks like (sparse, cached)
    speech = c("bop_transcript")["segments"]
    v0 = c.bt.ws.view()
    host_turn = next(s for s in speech if s["speaker_id"] == "Host" and s["end"] - s["start"] > 4 and not s["overlap"] and v0.camera_at(v0.f(s["start"] + 1)) == "cam_host")
    f1 = c("bop_frames", times=[host_turn["start"] + 1, host_turn["start"] + 2], note={"text": "host close-up, centred", "agent": "multicam_director"})
    assert f1["_images"] == 1 and f1["frames"][0]["camera"] == "cam_host"
    f2 = c("bop_frames", times=[host_turn["start"] + 1, host_turn["start"] + 2])
    assert f2["cache"]["hits"] >= 2 and f2["frames"][0]["note"]["text"] == "host close-up, centred"
    c("bop_task", action="note", task_id=tid, agent="editorial_intelligence", finding={"kind": "reaction_opportunity", "start": host_turn["start"] + 1, "evidence": "speaker turn"})
    # Multicam: the guest's reaction while the host asks (kept short, inside the turn)
    assert c("bop_switch_camera", expected_revision=c.rev, start=host_turn["start"] + 1.0, end=host_turn["start"] + 2.5, camera="cam_guest", reason="reaction", agent="multicam_director", task_id=tid)["verified"]["ok"]
    # Graphics: B-roll with fades, score card top right, episode title
    assert c("bop_place_asset", expected_revision=c.rev, file=str(footage["assets"] / "broll_city.mp4"), start=28.0, duration=3.0, fade=0.4, agent="graphics", task_id=tid)["verified"]["ok"]
    assert c("bop_place_asset", expected_revision=c.rev, file=str(footage["assets"] / "score_card.png"), start=20.0, duration=4.0, position="top_right", agent="graphics", task_id=tid)["verified"]["ok"]
    # Deliberately too short: QC must find it and Graphics must fix it within the free space
    assert c("bop_add_title", expected_revision=c.rev, text="Episode 12", subtitle="The Bang On Show", start=0.0, duration=1.5, agent="graphics", task_id=tid)["verified"]["ok"]
    # Audio: duck the dialogue clip under the B-roll moment
    a1 = next(x for x in c("bop_project", tracks=["A1"], start=28, end=29)["clips"])
    assert c("bop_edit", agent="audio_engineer", expected_revision=c.rev, task_id=tid, ops=[{"type": "set_audio_level", "clip_id": a1["id"], "params": {"gain_db": -2.0}}])["verified"]["ok"]
    # Captions: supplied quote text (no ASR words exist for generated footage)
    assert c("bop_edit", agent="captions", expected_revision=c.rev, task_id=tid, ops=[{"type": "add_caption", "clip_id": "quote_1", "start": 12.0, "end": 15.0, "params": {"text": "This is the best show", "speaker_id": "Guest"}}])["verified"]["ok"]
    done = c("bop_task", action="complete", task_id=tid)
    assert all(ch["ok"] for ch in done["checks"]), done["checks"]
    fixed = [x for x in done["corrections"] if x["defect"]["code"] == "GRAPHIC_TOO_SHORT"]
    assert fixed and fixed[0]["result"].startswith("accepted"), done["corrections"]  # verified corrective edit
    assert done["status"] == "verified", done["unresolved"]
    v = c.bt.ws.view()
    title = next(g for g in v.c.v2_clips if g.params.get("title") == "Episode 12")
    assert title.duration_frames / v.fps >= 2.0
    assert (job.draft_dir / "captions_agent_edit.ass").read_text().count("best show") == 1
    hist = c("bop_history")["edits"]
    assert {e["agent_id"] for e in hist} >= {"multicam_director", "graphics", "audio_engineer", "captions"}


@needs_media
@needs_render
def test_rendered_output_quality_control_preview_vertical_and_cancel(job: Any, footage: dict[str, Any]) -> None:
    c = Client(job.dir, [footage["assets"]])
    c("bop_place_asset", expected_revision=c.rev, file=str(footage["assets"] / "score_card.png"), start=2.0, duration=3.0, position="top_right", agent="graphics")
    q = c("bop_qc", rendered=True, start=0, end=8)
    r = q["rendered"]
    assert abs(r["duration_sec"] - 8.0) < 0.3 and r["shots_compared"] >= 2 and r["loudness"]["measured"]
    assert not [i for i in r["issues"] if i["code"] in ("UNEXPECTED_BLACK", "RENDER_DURATION")], r["issues"]
    fr = c("bop_frames", times=[3.0, 6.0], rendered=True)
    assert fr["_images"] == 1 and "rendered draft" in fr["evidence"]
    vert = c("bop_vertical", start=11.5, end=17.0, title="Guest story")
    assert Path(vert["project"]).is_file() and vert["render_check"]["width"] == 360 and vert["render_check"]["height"] == 640
    # Cancellation: a long render stops, nothing partial is kept and the session goes on
    from bangonpro.ai_engine.mcp.protocol import CallContext

    ctx = CallContext(request_id="x", client_name="pytest")
    threading.Timer(0.5, ctx._cancel.set).start()
    res = c.server.call_tool(c.server.tools["bop_preview"], {"start": 0.0, "end": 38.0}, ctx)
    assert res["isError"] and json.loads(res["content"][0]["text"])["error"]["code"] == "CANCELLED"
    assert not list((job.draft_dir / "previews").glob("*.part.mp4"))
    assert not c("bop_status")["_error"]


@needs_media
def test_unfamiliar_instruction_through_the_executive_cycle_mocked_reasoning(job: Any, footage: dict[str, Any]) -> None:
    """Mocked reasoning (ScriptedReasoner): rules do not understand it, the planner's first plan is wrong,
    the cycle verifies on the actual draft, feeds back what failed, the second plan fixes it."""
    from bangonpro.ai_engine.mcp.__main__ import instruct
    from bangonpro.ai_engine.mcp.reasoning import ScriptedReasoner

    st = job._latest_state()
    guest_shot = [s for s in st.rough_cut_shots if s.camera_id == "cam_guest"][1]  # the second guest answer (V2 is free there)
    a = round(guest_shot.timeline_in_sec + 1.0, 2)
    instruction = "When the guest tells the story, cut to the city footage like a cold open, and give the host a name strap when he first appears"
    plan1 = {"understanding": "B-roll over the guest story; host strap", "agents": ["graphics"],
             "steps": [{"tool": "bop_place_asset", "args": {"agent": "graphics", "file": str(footage["assets"] / "broll_city.mp4"), "start": a, "duration": 3.0, "fade": 0.3}},
                       {"tool": "bop_add_title", "args": {"agent": "graphics", "text": "Host", "start": a + 3.5, "duration": 4.0}}],  # wrong: the host is not on screen there
             "expect": [{"check": "has_clip", "track": "V2", "start": a, "end": a + 2}, {"check": "camera_share", "start": 2.0, "end": 6.0, "camera": "cam_host", "min": 0.8},
                        {"check": "title_at", "time": a + 4.0, "text": "Host"}]}
    def plan2(prompt: str) -> dict[str, Any]:
        # Like a model reading the feedback: where did Quality Control move the strap?
        fb = json.loads(prompt.split("PREVIOUS ATTEMPT (fix only what failed): ", 1)[1])
        now = next(n for c in fb["qc_corrections"] if c["defect"]["code"] == "GRAPHIC_WRONG_SPEAKER" for n in c["now"])
        return {"understanding": "QC moved the strap onto the host's close-up; check it there", "steps": [], "drop_checks": [2], "drop_reason": "Quality Control moved the strap",
                "expect": [{"check": "title_at", "time": now["start"] + 0.5, "text": "Host"}]}

    rs = ScriptedReasoner([plan1, plan2])
    out = instruct(str(job.dir), instruction, allow=[str(footage["assets"])], reasoner=rs)
    assert out["path"] == "reasoning" and out["reasoner"] == "scripted_mock"
    assert "INSTRUCTION: " + instruction in rs.prompts[0] and len(rs.prompts[0]) < 30000
    res = out["result"]
    # QC found the strap over the wrong speaker and Graphics moved it; QC re-verified the correction
    moved = [x for x in res["corrections"] if x["defect"]["code"] == "GRAPHIC_WRONG_SPEAKER"]
    assert moved and moved[0]["result"].startswith("accepted"), json.dumps(out["trace"], default=str)[:3000]
    # Round 1 failed its own check (the strap moved); the planner saw exactly that and revised; round 2 verified
    assert "PREVIOUS ATTEMPT" in rs.prompts[1] and "title_at" in rs.prompts[1]
    assert out["status"] == "verified", res
    assert res["superseded_checks"] and res["superseded_checks"][0]["reason"]
    from bangonpro.ai_engine.department import ProductionJob

    assert "verified by Quality Control" in ProductionJob(job.dir).doc["last_revision"]["message"]


@needs_media
def test_offline_behaviour_and_rules_path_are_unchanged(job: Any) -> None:
    from bangonpro.ai_engine.mcp.__main__ import instruct
    from bangonpro.ai_engine.mcp.reasoning import reasoning_status

    assert reasoning_status()["app_driven"] == "off"  # nothing is invoked without explicit authorisation
    out = instruct(str(job.dir), "make it pop, you know")
    assert out["path"] == "rules" and out["revision"]["status"] == "not_understood" and "unavailable" in out["reasoning"]
    rules = instruct(str(job.dir), "Fewer camera cuts please")
    assert rules["path"] == "rules" and rules["revision"]["status"] in ("draft_ready", "completed", "needs_review"), rules["revision"]["status"]


def test_reasoning_authorisation_and_client_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bangonpro.ai_engine.mcp import reasoning
    from bangonpro.ai_engine.mcp.__main__ import apply_config, client_config

    monkeypatch.setenv("BANGONPRO_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(reasoning.ReasonerError):
        reasoning.CliReasoner("claude")  # not authorised
    reasoning.authorise("claude", max_calls_per_task=2)
    assert reasoning.load_config()["authorised"] is True
    reasoning.revoke()
    assert reasoning.reasoning_status()["app_driven"] == "off"
    cl = client_config("claude", str(tmp_path / "job"), [])
    assert cl["command"].startswith("claude mcp add --scope user bangonpro") and "bangonpro.ai_engine.mcp serve --job" in cl["command"]
    cfg_file = tmp_path / ".codex" / "config.toml"
    cfg_file.parent.mkdir()
    cfg_file.write_text('model = "x"\n\n[mcp_servers.other]\ncommand = "o"\n\n[mcp_servers.bangonpro]\ncommand = "old"\n')
    apply_config(client_config("codex", str(tmp_path / "job"), []))
    text = cfg_file.read_text()
    assert text.count("[mcp_servers.bangonpro]") == 1 and '[mcp_servers.other]\ncommand = "o"' in text and 'model = "x"' in text and "old" not in text
    assert reasoning.extract_json('Sure:\n```json\n{"steps": []}\n```') == {"steps": []}


def test_live_app_connection_failure_and_simulated_native_server(tmp_path: Path) -> None:
    """Simulated: a Python stand-in speaking TimelineCommandServer's NDJSON protocol (the C++ app is not run here)."""
    from bangonpro.ai_engine.mcp.live import LiveClient

    assert LiveClient(tmp_path / "missing.sock").status() == {"running": False, "reason": f"Bang On Pro is not running (no socket at {tmp_path / 'missing.sock'})"}
    path = tmp_path / "s.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(4)
    seen: list[dict[str, Any]] = []

    def serve() -> None:
        for _ in range(4):
            conn, _a = srv.accept()
            req = json.loads(conn.makefile().readline())
            seen.append(req)
            m = req.get("rpc_method")
            if m == "ping":
                rep = {"status": "ok", "current_revision": 7, "pong": True}
            elif m == "get_state":
                rep = {"revision": 7, "backend": "kdenlive_core", "state": {"tracks": [{"track_id": "V1"}], "clips": [{"clip_id": "12", "track_id": "V1"}]}}
            else:
                rep = {"status": "pending_approval" if req.get("actor", {}).get("requires_approval") else "ok", "current_revision": 7}
            conn.sendall((json.dumps(rep) + "\n").encode())
            conn.close()

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    cli = LiveClient(path)
    assert cli.status()["running"] and cli.status()["revision"] == 7
    batch = {"api_version": "1.0", "request_id": "r", "project_id": "p", "sequence_id": "s", "expected_revision": 7, "actor": {"agent_id": "graphics", "role": "graphics", "requires_approval": True},
             "operations": [{"op_id": "o1", "type": "add_marker", "frame_range": {"in_frame": 1, "out_frame": 1}, "params": {"label": "x"}}]}
    assert cli.execute(batch)["status"] == "pending_approval"
    assert cli.state()["clips"][0]["clip_id"] == "12"
    th.join(timeout=5)
    srv.close()
    assert seen[2]["actor"]["requires_approval"] is True  # agents propose; the editor approves in the app


def test_server_refuses_a_missing_job_with_a_structured_error(tmp_path: Path) -> None:
    from bangonpro.ai_engine.mcp.__main__ import main, make_server
    from bangonpro.ai_engine.mcp.protocol import ToolError

    with pytest.raises(ToolError) as exc:
        make_server(str(tmp_path / "nope"))
    assert exc.value.code == "NO_JOB" and exc.value.hint
    assert main(["serve", "--job", str(tmp_path / "nope")]) == 2


def test_live_tool_reports_app_not_connected(tmp_path: Path) -> None:
    from bangonpro.ai_engine.mcp.protocol import CallContext, ToolError
    from bangonpro.ai_engine.mcp.tools import BopTools

    bt = BopTools.__new__(BopTools)
    with pytest.raises(ToolError) as exc:
        BopTools.live(bt, {"action": "state", "socket": str(tmp_path / "none.sock")}, CallContext(request_id=1, client_name="t"))
    assert exc.value.code == "APP_NOT_CONNECTED" and exc.value.retryable


def test_cancellation_notification_stops_a_running_call() -> None:
    import io

    from bangonpro.ai_engine.mcp.protocol import McpServer, Tool

    started = threading.Event()

    def slow(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        started.set()
        for _ in range(200):
            ctx.check()
            ctx.progress(1, 2, "working")
            time.sleep(0.02)
        return {"done": True}

    srv = McpServer([Tool("slow", "Slow", "", {}, slow)], name="t", version="0")
    out = io.StringIO()
    srv._out = out
    srv.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "slow", "arguments": {}, "_meta": {"progressToken": "p1"}}})
    started.wait(5)
    srv.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 9}})
    for t in srv._threads:
        t.join(5)
    msgs = [json.loads(x) for x in out.getvalue().splitlines()]
    assert any(m.get("method") == "notifications/progress" and m["params"]["progressToken"] == "p1" for m in msgs)
    final = next(m for m in msgs if m.get("id") == 9)
    assert final["result"]["isError"] and "CANCELLED" in final["result"]["content"][0]["text"]
