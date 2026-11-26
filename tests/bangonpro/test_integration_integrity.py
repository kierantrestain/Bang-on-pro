# SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
# SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""Integration-integrity regressions: one provider system, one reasoning router, reachable native engine.

* The AI Assistant's instruction interpreter uses the department's shared provider router, only when
  the local rules cannot interpret the instruction, and keeps the local interpretation when the router
  is unavailable or answers badly.
* The provider registry lists only providers Bang On Pro can actually call.
* The live client finds the app's Timeline Command socket where Qt's RuntimeLocation puts it.
* Segmentation models are loaded once and reused across frames (bounded).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bangonpro.ai_engine.mcp import live, reasoning
from bangonpro.ai_engine.planner import interpret_instruction, shared_reasoning_interpreter


class _Router:
    provider_id = "shared_reasoning_router"

    def __init__(self, answer: Any) -> None:
        self.answer, self.calls = answer, []

    def __call__(self, instruction: str, ctx: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(instruction)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_rules_first_router_only_when_the_rules_need_clarification() -> None:
    router = _Router({"add_captions": True})
    understood = interpret_instruction("Add captions", llm_interpreter=router)
    assert router.calls == [] and understood.used_llm is False and understood.intent.add_captions

    vague = "Give the second half a bit more energy for the YouTube crowd"
    base = interpret_instruction(vague)
    assert base.intent.needs_clarification, "the rules are expected not to cover this instruction"
    router = _Router({"remove_silence": True, "multicam_switch": True})
    out = interpret_instruction(vague, llm_interpreter=router)
    assert router.calls == [vague] and out.used_llm is True
    assert out.interpreter_backend == "llm_provider:shared_reasoning_router"
    # The interpreted flags define the actions and the editorial scope (not the rules' empty parse).
    assert out.intent.needs_clarification is False and out.intent.scope == "full_production"
    assert out.intent.parsed_actions == ["remove_silence", "multicam_switch"]


@pytest.mark.parametrize("answer", [reasoning.AllProvidersUnavailable("none", retry_at=None, attempts=[]), {"rm -rf": True}])
def test_unavailable_or_invalid_router_answer_keeps_the_local_interpretation(answer: Any) -> None:
    vague = "Give the second half a bit more energy for the YouTube crowd"
    out = interpret_instruction(vague, llm_interpreter=_Router(answer))
    assert out.used_llm is False and out.intent.needs_clarification is True
    assert "shared reasoning unavailable" in out.interpreter_backend


def test_ai_assistant_uses_the_shared_router_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BANGONPRO_CONFIG_DIR", str(tmp_path / "cfg"))
    assert shared_reasoning_interpreter() is None  # nothing authorised: the rules stay the only interpreter
    reasoning.configure({"privacy": {"mode": "connected_subscriptions", "consent": True, "metadata": False}})
    reasoning.authorise("claude")
    monkeypatch.setattr(reasoning.subscriptions, "BLOCKED", {})
    monkeypatch.setattr(reasoning.subscriptions, "client_path", lambda p: "/usr/bin/true")

    def call(self: Any, prompt: str, **_: Any) -> Any:
        assert "PRIVATE PROJECT" not in prompt
        return '{"add_captions": true, "unexpected": 1}', {}

    monkeypatch.setattr(reasoning.ClaudeAdapter, "call", call)
    interp = shared_reasoning_interpreter()
    assert interp is not None
    assert interp("Put words on screen", {"title": "PRIVATE PROJECT"}) == {"add_captions": True}  # unknown keys never reach the planner
    rows = [json.loads(x) for x in reasoning.ledger_path().read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["provider"] == "claude" and rows[-1]["purpose"] == "interpret" and rows[-1]["ok"] is True  # one shared ledger/budget


def test_provider_registry_lists_only_callable_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bangonpro.ai_engine.model_manager import ModelManager
    from bangonpro.ai_engine.providers import AiProviderRegistry

    monkeypatch.setenv("BANGONPRO_CONFIG_DIR", str(tmp_path / "cfg"))
    reasoning.authorise("codex")
    provs = {p.provider_id: p for p in AiProviderRegistry().list_providers()}
    external = sorted(k for k, p in provs.items() if p.is_external)
    assert external == sorted(f"reasoning_{p}" for p in reasoning.PROVIDERS)
    assert provs["reasoning_codex"].consent_granted and not provs["reasoning_claude"].consent_granted
    assert not any("whisper_cpp" in k or k in ("openai_compatible", "anthropic", "deepgram", "local_ollama") for k in provs)
    # No catalogued model without an inference backend in this release (nothing to download for nothing).
    assert {e.runtime_backend for e in ModelManager(model_dir=tmp_path / "m").list_catalog()} <= {"faster_whisper", "openai_whisper", "onnxruntime", "sam2"}


def test_live_socket_matches_qt_runtime_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.delenv("BANGONPRO_TIMELINE_SOCKET", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    real = os.uname()
    monkeypatch.setattr(os, "uname", lambda: os.uname_result(("Darwin",) + tuple(real)[1:]))
    # macOS: QStandardPaths::RuntimeLocation is ~/Library/Application Support (no app sub-folder)
    assert live.default_socket_path() == tmp_path / "Library" / "Application Support" / "bangonpro-timeline-v1.sock"
    monkeypatch.setattr(os, "uname", lambda: os.uname_result(("Linux",) + tuple(real)[1:]))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    assert live.default_socket_path() == tmp_path / "run" / "bangonpro-timeline-v1.sock"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("USER", "ed")
    assert live.default_socket_path() == tmp_path / "runtime-ed" / "bangonpro-timeline-v1.sock"
    # The app hands its exact socket to every process it starts.
    monkeypatch.setenv("BANGONPRO_TIMELINE_SOCKET", str(tmp_path / "app.sock"))
    assert live.LiveClient().path == tmp_path / "app.sock"


def test_segmentation_models_are_loaded_once_and_bounded(tmp_path: Path) -> None:
    from bangonpro.roto_studio import mask_engine as me

    me.clear_model_cache()
    loads: list[str] = []
    files = []
    for i in range(me._MODEL_CACHE_MAX + 1):
        f = tmp_path / f"m{i}.onnx"
        f.write_bytes(b"x" * (i + 1))
        files.append(f)
    for _frame in range(5):  # five frames of one shot: one load
        me._cached_model("ort", files[0], ("CPU",), lambda: loads.append("m0") or object())
    assert loads == ["m0"]
    files[0].write_bytes(b"replaced weights")  # replaced weights are reloaded
    me._cached_model("ort", files[0], ("CPU",), lambda: loads.append("m0'") or object())
    for f in files[1:]:
        me._cached_model("ort", f, ("CPU",), lambda f=f: loads.append(f.stem) or object())
    assert len(me._MODEL_CACHE) == me._MODEL_CACHE_MAX
    me.clear_model_cache()
    assert not me._MODEL_CACHE


def _render_frame(tmp_path: Path, name: str, producer_filters: str, *, two_tracks: bool) -> Any:
    import os
    import subprocess

    import numpy as np
    from PIL import Image

    bg = '<producer id="bg"><property name="mlt_service">color</property><property name="resource">0x224466ff</property><property name="length">5</property></producer>'
    tracks = '<playlist id="p0"><entry producer="bg" in="0" out="4"/></playlist>' if two_tracks else ""
    xml = (f'<?xml version="1.0" encoding="utf-8"?><mlt profile="atsc_720p_25">{bg if two_tracks else ""}'
           f'<producer id="fg"><property name="mlt_service">color</property><property name="resource">0xff5a1fff</property><property name="length">5</property>{producer_filters}</producer>'
           f'{tracks}<playlist id="p1"><entry producer="fg" in="0" out="4"/></playlist><tractor id="t0">'
           + ('<track producer="p0"/>' if two_tracks else "") + '<track producer="p1"/>'
           + ('<transition mlt_service="composite" a_track="0" b_track="1"/>' if two_tracks else "") + "</tractor></mlt>")
    (tmp_path / f"{name}.mlt").write_text(xml, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("QT_QPA_PLATFORM_PLUGIN_PATH", "QT_PLUGIN_PATH")} | {"QT_QPA_PLATFORM": "offscreen"}
    out = tmp_path / f"{name}.png"
    subprocess.run(["melt", str(tmp_path / f"{name}.mlt"), "-consumer", f"avformat:{out}", "vcodec=png", "frames=1", "update=1", "real_time=0"],
                   capture_output=True, env=env, timeout=120, check=True)
    return np.asarray(Image.open(out).convert("RGB")).astype(int)


def _filters_xml(filters: list[dict[str, Any]]) -> str:
    return "".join(f'<filter mlt_service="{f["mlt_service"]}">' + "".join(f'<property name="{k}">{v}</property>' for k, v in f["properties"].items()) + "</filter>"
                   for f in filters)


@pytest.mark.skipif(not __import__("shutil").which("melt"), reason="melt required")
def test_matte_masks_put_the_effect_where_the_agent_asked_in_a_native_render(tmp_path: Path) -> None:
    """A department VFX request ("blur the background behind the host": masked effect, invert) and a Roto
    Studio subject isolation (matte only) render with the effect / opacity exactly where intended."""
    import numpy as np
    from PIL import Image

    from bangonpro.ai_engine.project_builder import _effect_op_to_filters

    subject = np.zeros((720, 1280), dtype=np.uint8)
    subject[180:540, 400:880] = 255  # white = the subject
    matte = tmp_path / "matte.png"
    Image.fromarray(subject).save(matte)
    subj = subject > 127
    # 1. Effect outside the subject (the VFX agent's background treatment): brightness 0 marks where it applies.
    outside = _effect_op_to_filters("apply_mask", {"matte_path": str(matte), "feather": 0, "invert": True,
                                                   "masked_effect": {"mlt_service": "brightness", "properties": {"level": 0.0}}})
    assert [f["mlt_service"] for f in outside] == ["mask_start", "brightness", "mask_apply"]
    dark = _render_frame(tmp_path, "outside", _filters_xml(outside), two_tracks=False).sum(axis=2) < 60
    assert dark[~subj].mean() > 0.98 and dark[subj].mean() < 0.02
    # 2. The same effect on the subject only.
    inside = _effect_op_to_filters("apply_mask", {"matte_path": str(matte), "feather": 0, "invert": False,
                                                  "masked_effect": {"mlt_service": "brightness", "properties": {"level": 0.0}}})
    dark = _render_frame(tmp_path, "inside", _filters_xml(inside), two_tracks=False).sum(axis=2) < 60
    assert dark[subj].mean() > 0.98 and dark[~subj].mean() < 0.02
    # 3. Matte only (Roto Studio commit): the subject stays opaque over the track below, the rest is transparent.
    iso = _effect_op_to_filters("apply_mask", {"matte_path": str(matte), "feather": 0, "invert": False})
    assert [f["mlt_service"] for f in iso] == ["shape"]
    im = _render_frame(tmp_path, "isolate", _filters_xml(iso), two_tracks=True)
    orange = (im[..., 0] > 180) & (im[..., 2] < 110)
    assert orange[subj].mean() > 0.98 and orange[~subj].mean() < 0.02


def test_ai_assistant_command_honours_its_privacy_setting(tmp_path: Path) -> None:
    """The exact command the AI Assistant dock runs: an instruction the rules cannot interpret goes to the
    shared router only when the editor's privacy setting allows it; Local Only never calls a provider."""
    import os
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[2]
    state = {"api_version": "timeline_command_v1", "project_id": "p", "sequence_id": "s", "revision": 2,
             "state": {"active_sequence_id": "s", "sequences": [{"sequence_id": "s", "name": "Main", "fps_num": 25, "fps_den": 1}],
                       "media_bin": [], "tracks": [{"track_id": "V1", "sequence_id": "s", "name": "V1", "kind": "video", "index": 0}],
                       "clips": [], "captions": [], "markers": []}}
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    bindir = tmp_path / ".local/bin"
    bindir.mkdir(parents=True)
    fake = bindir / "codex"
    fake.write_text("""
import json, pathlib, sys
if sys.argv[1] == "app-server":
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        result = {
            "initialize": {},
            "account/read": {"account": {"type": "chatgpt", "planType": "plus"}},
            "account/rateLimits/read": {"ordinaryUsageAllowed": True,
                "rateLimits": {"credits": {"hasCredits": False, "unlimited": False, "balance": "0"}}}
        }[request["method"]]
        print(json.dumps({"id": request["id"], "result": result}), flush=True)
elif sys.argv[1] == "exec":
    prompt = sys.stdin.read()
    assert "Project: {}" in prompt
    pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1]).write_text('{"add_captions": true}')
""", encoding="utf-8")
    fake.replace(bindir / "codex.py")
    import shlex

    fake.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(bindir / "codex.py")) + ' "$@"\n', encoding="utf-8")
    fake.chmod(0o755)
    env = dict(os.environ, HOME=str(tmp_path), BANGONPRO_CONFIG_DIR=str(tmp_path / "cfg"), PATH="/usr/bin:/bin")
    def settings(*args: str) -> None:
        subprocess.run([sys.executable, "-m", "bangonpro.ai_engine.mcp", *args], cwd=str(repo), env=env, check=True, capture_output=True)

    settings("authorise", "--client", "codex")
    ledger = tmp_path / "cfg" / "reasoning_ledger.jsonl"

    def run(*extra: str) -> dict[str, Any]:
        proc = subprocess.run([sys.executable, "-m", "bangonpro.ai_engine.cli", "Give the second half a bit more energy for the YouTube crowd",
                               "--project-state", str(tmp_path / "state.json"), "--plan-only", "--output-dir", str(tmp_path / "out"), "--json", *extra],
                              cwd=str(repo), env=env, capture_output=True, text=True, timeout=120, check=False)
        assert proc.returncode == 0, proc.stderr[-1500:]
        return json.loads(proc.stdout)

    local = run()
    assert local["needs_clarification"] is True and not ledger.exists()
    settings("providers", "--configure", json.dumps({"privacy": {"mode": "connected_subscriptions", "consent": True}}))
    shared = run()
    assert shared["needs_clarification"] is False and "add_captions" in shared["parsed_actions"]
    rows = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines()]
    assert [(r["provider"], r["purpose"], r["ok"]) for r in rows] == [("codex", "interpret", True)]
