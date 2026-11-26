"""``python -m bangonpro.ai_engine.mcp`` - connect Claude Code or Codex to Bang On Pro's agents.

  serve --job DIR [--allow DIR ...]     stdio MCP server (what the AI client starts)
  config --client claude|codex --job DIR [--allow DIR] [--apply]
                                        print (or apply) the client's own connection command
  authorise --client claude|codex       allow Bang On Pro to ask that CLI for plans (app-driven)
  revoke                                withdraw that permission
  instruct --job DIR "instruction"      rules first; the authorised client for anything else
  status --job DIR                      connection and draft status

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from bangonpro.ai_engine.department.version import APP_VERSION as DEPT_VERSION


def make_server(job_dir: str, allow: list[str] | None = None) -> tuple[Any, Any]:
    from bangonpro.ai_engine.mcp.protocol import McpServer
    from bangonpro.ai_engine.mcp.tools import INSTRUCTIONS, BopTools
    from bangonpro.ai_engine.mcp.workspace import Workspace

    ws = Workspace(job_dir, allowed_roots=allow or [])
    bt = BopTools(ws)
    server = McpServer(bt.tool_list(), name="bangonpro", version=DEPT_VERSION, instructions=INSTRUCTIONS, on_initialize=bt.on_initialize, on_call=bt.on_call)
    return server, bt


def serve_command(job: str, allow: list[str]) -> list[str]:
    cmd = [sys.executable, "-m", "bangonpro.ai_engine.mcp", "serve", "--job", str(Path(job).resolve())]
    for a in allow:
        cmd += ["--allow", str(Path(a).resolve())]
    return cmd


def client_config(client: str, job: str, allow: list[str]) -> dict[str, Any]:
    cmd = serve_command(job, allow)
    pkg_root = str(Path(__file__).resolve().parents[3])
    if client == "claude":
        add = ["claude", "mcp", "add", "--scope", "user", "bangonpro", "--env", f"PYTHONPATH={pkg_root}", "--", *cmd]
        return {"client": "Claude Code", "command": shlex.join(add), "argv": add,
                "note": "Claude Code uses your own signed-in account. Read-only tools are marked; Claude asks before edits unless you allow them."}
    toml = "[mcp_servers.bangonpro]\n" + f"command = {json.dumps(cmd[0])}\n" + f"args = {json.dumps(cmd[1:])}\n" + f'env = {{ PYTHONPATH = {json.dumps(pkg_root)} }}\n'
    return {"client": "Codex", "config_file": str(Path.home() / ".codex" / "config.toml"), "toml": toml, "note": "Codex uses your own signed-in ChatGPT account or its configured login."}


def apply_config(cfg: dict[str, Any]) -> str:
    if "argv" in cfg:
        if not shutil.which("claude"):
            raise SystemExit("claude is not installed; run the printed command after installing Claude Code")
        res = subprocess.run(cfg["argv"], capture_output=True, text=True, check=False)
        return (res.stdout + res.stderr).strip()
    path = Path(cfg["config_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    out, skip = [], False
    for ln in lines:  # replace our own section only; every other line stays exactly as it was
        t = ln.strip()
        if t.startswith("["):
            skip = t == "[mcp_servers.bangonpro]" or t.startswith("[mcp_servers.bangonpro.")
        if not skip:
            out.append(ln)
    path.write_text("\n".join([*out, "", cfg["toml"]]).lstrip("\n"), encoding="utf-8")
    return f"wrote [mcp_servers.bangonpro] to {path}"


def instruct(job: str, instruction: str, *, allow: list[str] | None = None, range_timeline: tuple[float, float] | None = None, reasoner: Any = None) -> dict[str, Any]:
    """Rules first (deterministic, offline); the authorised reasoning client for anything else."""
    from bangonpro.ai_engine.department import ProductionJob

    rec = ProductionJob(job).revise(instruction, range_timeline=range_timeline)
    if rec.get("status") not in ("not_understood",):
        return {"path": "rules", "revision": rec}
    return reasoning_fallback(job, instruction, rec, allow=allow, reasoner=reasoner)


def reasoning_fallback(job: str, instruction: str, rec: dict[str, Any], *, allow: list[str] | None = None, reasoner: Any = None) -> dict[str, Any]:
    """The rule interpreter did not understand: the Executive asks the authorised reasoning client."""
    from bangonpro.ai_engine.department import ProductionJob
    from bangonpro.ai_engine.mcp.execution import run_cycle
    from bangonpro.ai_engine.mcp.reasoning import CliReasoner, ReasonerError

    try:
        rsn = reasoner or CliReasoner()
    except ReasonerError as exc:
        return {"path": "rules", "revision": rec, "reasoning": f"unavailable: {exc}"}
    _, bt = make_server(job, allow)
    out = run_cycle(bt, instruction, rsn)
    job_obj = ProductionJob(job)
    res = out.get("result") or {}
    msg = {"verified": f"Done by the agents ({res.get('edits', 0)} edits, verified by Quality Control)",
           "needs_attention": f"The agents made {res.get('edits', 0)} edits; {len(res.get('unresolved') or [])} issue(s) need your review"}.get(out["status"], f"The agents could not complete this: {out['status']}")
    job_obj.doc["last_revision"] = {"revision": rec["revision"], "instruction": instruction, "status": out["status"], "message": msg, "path": out.get("reasoner")}
    job_obj._save(force=True)
    return {"path": "reasoning", "reasoner": out.get("reasoner"), **out}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bangonpro.ai_engine.mcp")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--job", required=True)
    s.add_argument("--allow", action="append", default=[])
    c = sub.add_parser("config")
    c.add_argument("--client", choices=("claude", "codex"), required=True)
    c.add_argument("--job", required=True)
    c.add_argument("--allow", action="append", default=[])
    c.add_argument("--apply", action="store_true")
    au = sub.add_parser("authorise")
    au.add_argument("--client", choices=("claude", "codex"), required=True)
    au.add_argument("--model")
    au.add_argument("--max-calls", type=int, default=3)
    sub.add_parser("revoke")
    ins = sub.add_parser("instruct")
    ins.add_argument("--job", required=True)
    ins.add_argument("--allow", action="append", default=[])
    ins.add_argument("--range-timeline", nargs=2, type=float)
    ins.add_argument("instruction")
    stt = sub.add_parser("status")
    stt.add_argument("--job", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        from bangonpro.ai_engine.mcp.protocol import ToolError

        try:
            server, bt = make_server(a.job, a.allow)
        except ToolError as exc:
            print(json.dumps({"error": exc.to_dict()}), file=sys.stderr)
            return 2
        try:
            server.serve()
        finally:
            bt.disconnect()
        return 0
    if a.cmd == "config":
        cfg = client_config(a.client, a.job, a.allow)
        if a.apply:
            cfg["applied"] = apply_config(cfg)
        cfg.pop("argv", None)
        print(json.dumps(cfg, indent=1))
        return 0
    if a.cmd == "authorise":
        from bangonpro.ai_engine.mcp.reasoning import authorise

        print(json.dumps(authorise(a.client, model=a.model, max_calls_per_task=a.max_calls), indent=1))
        return 0
    if a.cmd == "revoke":
        from bangonpro.ai_engine.mcp.reasoning import revoke

        revoke()
        print(json.dumps({"revoked": True}))
        return 0
    if a.cmd == "instruct":
        print(json.dumps(instruct(a.job, a.instruction, allow=a.allow, range_timeline=tuple(a.range_timeline) if a.range_timeline else None), indent=1, default=str))
        return 0
    if a.cmd == "status":
        from bangonpro.ai_engine.mcp.protocol import CallContext

        _, bt = make_server(a.job)
        print(json.dumps(bt.status({}, CallContext(request_id="cli", client_name="cli")), indent=1, default=str))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
