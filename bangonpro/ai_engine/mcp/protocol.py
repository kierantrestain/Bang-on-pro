"""Minimal Model Context Protocol server over stdio (newline-delimited JSON-RPC 2.0).

Implemented with the standard library so the embedded runtime gains no dependency. Behaviour
follows the conventions the reference editors use for agents:

* ``initialize`` negotiates one of :data:`PROTOCOL_VERSIONS`; ``ping``; ``tools/list`` with titles and
  the four annotation hints; empty ``resources/list`` and ``prompts/list``.
* ``tools/call`` validates arguments strictly (an unknown or invalid argument is ``-32602`` naming
  the accepted ones); a failing tool is an ``isError`` result with a structured error, never a crash.
* A line that is not JSON gets ``-32700`` (id ``null``) and the server keeps serving.
* Each call runs on its own thread: ``notifications/cancelled`` stops a long call cooperatively and
  ``notifications/progress`` is sent when the request carried a ``progressToken``.
* Nothing but protocol messages is written to stdout; diagnostics go to stderr.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import IO, Any

from jsonschema import Draft202012Validator

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


class ToolError(Exception):
    """A tool could not do what was asked. Reported to the client as an ``isError`` result."""

    def __init__(self, code: str, message: str, *, hint: str = "", retryable: bool = False, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.hint, self.retryable, self.details = code, message, hint, retryable, details or {}

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.hint:
            d["hint"] = self.hint
        if self.details:
            d["details"] = self.details
        return d


class Cancelled(ToolError):
    def __init__(self, message: str = "Cancelled by the client") -> None:
        super().__init__("CANCELLED", message, retryable=True)


@dataclass
class CallContext:
    """Passed to every tool handler: cancellation, progress and the calling client."""

    request_id: Any
    client_name: str
    progress_token: Any = None
    _cancel: threading.Event = field(default_factory=threading.Event)
    _notify: Callable[[dict[str, Any]], None] | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    def progress(self, done: float, total: float | None = None, message: str = "") -> None:
        if self.progress_token is None or self._notify is None:
            return
        params: dict[str, Any] = {"progressToken": self.progress_token, "progress": done}
        if total is not None:
            params["total"] = total
        if message:
            params["message"] = message
        self._notify({"jsonrpc": "2.0", "method": "notifications/progress", "params": params})


@dataclass
class Tool:
    name: str
    title: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[dict[str, Any], CallContext], Any]
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False

    def definition(self) -> dict[str, Any]:
        schema = {"type": "object", "additionalProperties": False, **self.schema}
        schema.setdefault("properties", {})
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": schema,
            "annotations": {"title": self.title, "readOnlyHint": self.read_only, "destructiveHint": self.destructive,
                            "idempotentHint": self.idempotent, "openWorldHint": False},
        }


class McpServer:
    def __init__(self, tools: list[Tool], *, name: str, version: str, instructions: str = "",
                 on_initialize: Callable[[dict[str, Any]], None] | None = None,
                 on_call: Callable[[str, dict[str, Any], CallContext, str], None] | None = None) -> None:
        self.tools = {t.name: t for t in tools}
        self._validators = {t.name: Draft202012Validator(t.definition()["inputSchema"]) for t in tools}
        self.name, self.version, self.instructions = name, version, instructions
        self.protocol_version = PROTOCOL_VERSIONS[0]
        self.client_name = "unknown client"
        self._on_initialize, self._on_call = on_initialize, on_call
        self._out: IO[str] = sys.stdout
        self._write_lock = threading.Lock()
        self._inflight: dict[Any, CallContext] = {}
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ io
    def _send(self, msg: dict[str, Any]) -> None:
        line = json.dumps(msg, separators=(",", ":"), default=str)
        with self._write_lock:
            self._out.write(line + "\n")
            self._out.flush()

    @staticmethod
    def _error(mid: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": mid, "error": err}

    def serve(self, inp: IO[str] | None = None, out: IO[str] | None = None) -> None:
        self._out = out or sys.stdout
        for raw in inp or sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self._send(self._error(None, -32700, "Parse error: the line is not JSON"))
                continue
            if isinstance(msg, list):  # batches are not part of current MCP; answer each
                for m in msg:
                    self.handle(m)
            else:
                self.handle(msg)
        for t in self._threads:
            t.join(timeout=30)

    # ------------------------------------------------------------------ dispatch
    def handle(self, msg: Any) -> None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                return  # a reply to a request we never send
            self._send(self._error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid request"))
            return
        method, mid, params = msg["method"], msg.get("id"), msg.get("params") or {}
        is_notification = "id" not in msg
        if method == "notifications/cancelled":
            ctx = self._inflight.get(params.get("requestId"))
            if ctx is not None:
                ctx._cancel.set()
            return
        if method.startswith("notifications/"):
            return
        if method == "initialize":
            asked = str(params.get("protocolVersion") or PROTOCOL_VERSIONS[0])
            self.protocol_version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            info = params.get("clientInfo") or {}
            self.client_name = str(info.get("title") or info.get("name") or "unknown client")
            if self._on_initialize:
                self._on_initialize(params)
            result = {"protocolVersion": self.protocol_version, "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": self.name, "title": "Bang On Pro", "version": self.version}}
            if self.instructions:
                result["instructions"] = self.instructions
            self._send({"jsonrpc": "2.0", "id": mid, "result": result})
            return
        if is_notification:
            return
        if method == "ping":
            self._send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "tools/list":
            self._send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [t.definition() for t in self.tools.values()]}})
        elif method == "resources/list":
            self._send({"jsonrpc": "2.0", "id": mid, "result": {"resources": []}})
        elif method == "prompts/list":
            self._send({"jsonrpc": "2.0", "id": mid, "result": {"prompts": []}})
        elif method == "tools/call":
            self._start_call(mid, params)
        else:
            self._send(self._error(mid, -32601, f"Method not found: {method}"))

    def _start_call(self, mid: Any, params: dict[str, Any]) -> None:
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = self.tools.get(str(name))
        if tool is None:
            self._send(self._error(mid, -32602, f"Unknown tool '{name}'", {"tools": sorted(self.tools)}))
            return
        if not isinstance(args, dict):
            self._send(self._error(mid, -32602, "arguments must be an object"))
            return
        errors = sorted(self._validators[tool.name].iter_errors(args), key=lambda e: list(e.path))
        if errors:
            accepted = sorted(tool.definition()["inputSchema"]["properties"])
            msgs = []
            for e in errors[:5]:
                if e.validator == "additionalProperties":
                    msgs.append(f"{e.message} for {tool.name}; expected: {', '.join(accepted)}")
                else:
                    msgs.append(f"{'.'.join(str(p) for p in e.path) or '<arguments>'}: {e.message}")
            self._send(self._error(mid, -32602, "; ".join(msgs), {"accepted": accepted}))
            return
        meta = params.get("_meta") or {}
        ctx = CallContext(request_id=mid, client_name=self.client_name, progress_token=meta.get("progressToken"), _notify=self._send)
        self._inflight[mid] = ctx
        th = threading.Thread(target=self._run_call, args=(tool, args, ctx), daemon=True, name=f"mcp-{tool.name}")
        self._threads = [t for t in self._threads if t.is_alive()] + [th]
        th.start()

    def call_tool(self, tool: Tool, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        """Run a tool and build its MCP result (also used in-process by the execution cycle)."""
        try:
            if self._on_call:
                self._on_call(tool.name, args, ctx, "start")
            out = tool.handler(args, ctx)
            content, structured = _to_content(out)
            res: dict[str, Any] = {"content": content, "isError": False}
            if structured is not None and self.protocol_version >= "2025-06-18":
                res["structuredContent"] = structured
            return res
        except ToolError as exc:
            body = {"error": exc.to_dict()}
            return {"content": [{"type": "text", "text": json.dumps(body, default=str)}], "isError": True}
        except Exception as exc:  # noqa: BLE001 - a broken tool must never end the session
            print(traceback.format_exc(), file=sys.stderr)
            body = {"error": {"code": "INTERNAL", "message": f"internal error: {type(exc).__name__}: {exc}", "retryable": False}}
            return {"content": [{"type": "text", "text": json.dumps(body, default=str)}], "isError": True}
        finally:
            if self._on_call:
                try:
                    self._on_call(tool.name, args, ctx, "end")
                except Exception:  # noqa: BLE001, S110 - presence reporting is best effort
                    pass

    def _run_call(self, tool: Tool, args: dict[str, Any], ctx: CallContext) -> None:
        try:
            res = self.call_tool(tool, args, ctx)
            self._send({"jsonrpc": "2.0", "id": ctx.request_id, "result": res})
        finally:
            self._inflight.pop(ctx.request_id, None)


@dataclass
class ToolOutput:
    """A tool result with images (contact sheets) as well as structured data."""

    data: dict[str, Any]
    images: list[tuple[bytes, str]] = field(default_factory=list)  # (bytes, mime)


def _to_content(out: Any) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    import base64

    if isinstance(out, ToolOutput):
        content: list[dict[str, Any]] = [{"type": "text", "text": json.dumps(out.data, default=str, separators=(",", ":"))}]
        for data, mime in out.images:
            content.append({"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": mime})
        return content, out.data
    if isinstance(out, dict):
        return [{"type": "text", "text": json.dumps(out, default=str, separators=(",", ":"))}], out
    return [{"type": "text", "text": str(out)}], None


__all__ = ["PROTOCOL_VERSIONS", "CallContext", "Cancelled", "McpServer", "Tool", "ToolError", "ToolOutput"]
