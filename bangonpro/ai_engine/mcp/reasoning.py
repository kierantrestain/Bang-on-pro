"""Shared reasoning for the department: the user's own Claude Code or Codex, never a bundled model.

Two ways in:

1. **Client-driven** (preferred): Claude Code or Codex is connected to the Bang On Pro MCP server
   and does the reasoning itself while it calls the tools. Nothing here is involved.
2. **App-driven**: the editor types an instruction in Bang On Pro that the rule-based interpreter does
   not understand. If the editor has explicitly authorised it, the Executive asks the installed,
   already signed-in CLI (``claude -p`` or ``codex exec``, their documented headless modes) for a
   short structured plan, which is then executed and verified with the same tools.

No API keys are read or stored, no credentials are touched, no model is downloaded, and nothing
runs unless ``authorise`` was called for that client. Without authorisation the department works
exactly as before (offline, deterministic).

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Protocol

CLIENTS = ("claude", "codex")


def config_path() -> Path:
    base = os.environ.get("BANGONPRO_CONFIG_DIR") or os.path.join(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"), "bangonpro")
    return Path(base) / "reasoning.json"


def load_config() -> dict[str, Any]:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def authorise(client: str, *, model: str | None = None, max_calls_per_task: int = 3) -> dict[str, Any]:
    if client not in CLIENTS:
        raise ValueError(f"client must be one of {CLIENTS}")
    cfg = {"client": client, "authorised": True, "authorised_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "max_calls_per_task": int(max_calls_per_task)}
    if model:
        cfg["model"] = model
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    return cfg


def revoke() -> None:
    try:
        config_path().unlink()
    except OSError:
        pass


def reasoning_status() -> dict[str, Any]:
    cfg = load_config()
    found = {c: bool(shutil.which(c)) for c in CLIENTS}
    st: dict[str, Any] = {"installed_clients": [c for c, ok in found.items() if ok], "app_driven": "off"}
    if cfg.get("authorised") and cfg.get("client") in CLIENTS:
        st["app_driven"] = "authorised" if found.get(cfg["client"]) else "authorised_but_not_installed"
        st["client"] = cfg["client"]
    return st


class Reasoner(Protocol):
    kind: str

    def plan(self, prompt: str) -> dict[str, Any]: ...


class ReasonerError(RuntimeError):
    pass


def extract_json(text: str) -> dict[str, Any]:
    """The first JSON object in a model's answer (fenced or bare)."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    cands = [m.group(1)] if m else []
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    cands.append(text[start:i + 1])
                    break
    for c in cands:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            continue
    raise ReasonerError("The reasoning client did not return a JSON plan")


class CliReasoner:
    """One bounded, tool-less headless call to the user's own CLI per planning round."""

    kind = "authenticated_cli"

    def __init__(self, client: str | None = None, *, timeout: float = 240.0, model: str | None = None) -> None:
        cfg = load_config()
        self.client = client or cfg.get("client") or ""
        if self.client not in CLIENTS:
            raise ReasonerError("No reasoning client is authorised (python -m bangonpro.ai_engine.mcp authorise --client claude|codex)")
        if not cfg.get("authorised") or cfg.get("client") != self.client:
            raise ReasonerError(f"{self.client} is not authorised for app-driven reasoning")
        if not shutil.which(self.client):
            raise ReasonerError(f"{self.client} is not installed or not on PATH")
        self.timeout = timeout
        self.model = model or cfg.get("model")
        self.calls = 0
        self.max_calls = int(cfg.get("max_calls_per_task", 3))
        self.last_usage: dict[str, Any] = {}

    def plan(self, prompt: str) -> dict[str, Any]:
        if self.calls >= self.max_calls:
            raise ReasonerError(f"Reasoning budget of {self.max_calls} calls per task used up")
        self.calls += 1
        env = os.environ.copy()
        if self.client == "claude":
            cmd = ["claude", "-p", prompt, "--output-format", "json", "--max-turns", "1"]
            if self.model:
                cmd += ["--model", self.model]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, env=env, check=False)
            if res.returncode != 0:
                raise ReasonerError(f"claude exited {res.returncode}: {(res.stderr or res.stdout)[-300:]}")
            try:
                out = json.loads(res.stdout)
                text = str(out.get("result", ""))
                self.last_usage = {k: out.get(k) for k in ("total_cost_usd", "duration_ms", "num_turns", "usage", "is_error") if k in out}
                if out.get("is_error"):
                    raise ReasonerError(f"claude reported an error: {text[:200]}")
            except ValueError:
                text = res.stdout
            return extract_json(text)
        with tempfile.TemporaryDirectory() as td:
            last = Path(td) / "last.txt"
            cmd = ["codex", "exec", "--skip-git-repo-check", "--output-last-message", str(last)]
            if self.model:
                cmd += ["--model", self.model]
            res = subprocess.run([*cmd, prompt], capture_output=True, text=True, timeout=self.timeout, env=env, check=False, cwd=td)
            if res.returncode != 0:
                raise ReasonerError(f"codex exited {res.returncode}: {(res.stderr or res.stdout)[-300:]}")
            return extract_json(last.read_text(encoding="utf-8") if last.is_file() else res.stdout)


class ScriptedReasoner:
    """Test double: returns prepared plans in order. Results produced with it are *mocked reasoning*."""

    kind = "scripted_mock"

    def __init__(self, plans: list[Any]) -> None:
        self.plans = list(plans)
        self.prompts: list[str] = []

    def plan(self, prompt: str) -> dict[str, Any]:
        self.prompts.append(prompt)
        if not self.plans:
            raise ReasonerError("No scripted plan left")
        nxt = self.plans.pop(0)
        return nxt(prompt) if callable(nxt) else nxt


__all__ = ["CLIENTS", "CliReasoner", "Reasoner", "ReasonerError", "ScriptedReasoner", "authorise", "config_path", "extract_json", "load_config", "reasoning_status", "revoke"]
