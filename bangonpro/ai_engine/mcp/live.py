"""Client for the running app's Timeline Command server (``TimelineCommandServer``, C++).

The app listens on a user-private local socket (0600) and accepts newline-delimited Timeline
Command API v1 batches plus ``rpc_method`` control requests (``ping``, ``get_revision``,
``get_state``, ``get_activity_log``, ``undo``, ``redo``). The MCP server uses it to report whether the
editor is open, to read the live timeline (including the editor's own manual changes) and to send
validated batches to the native engine, which applies its own revision check, track locks and undo.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from pathlib import Path
from typing import Any


def socket_candidates() -> list[Path]:
    """Where ``TimelineCommandServer::defaultSocketPath`` puts the socket (Qt RuntimeLocation, else temp).

    Qt's RuntimeLocation is ``$XDG_RUNTIME_DIR`` on Linux (``/tmp/runtime-$USER`` when unset) and
    ``~/Library/Application Support`` on macOS (no application sub-folder).
    """
    env = os.environ.get("BANGONPRO_TIMELINE_SOCKET")
    if env:
        return [Path(env)]
    name = "bangonpro-timeline-v1.sock"
    out: list[Path] = []
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if os.uname().sysname == "Darwin":
        out.append(Path.home() / "Library" / "Application Support" / name)
    elif runtime:
        out.append(Path(runtime) / name)
    else:
        out.append(Path(os.environ.get("TMPDIR", "/tmp")) / f"runtime-{os.environ.get('USER') or os.environ.get('LOGNAME') or ''}" / name)
    out.append(Path(os.environ.get("TMPDIR", "/tmp")) / name)
    return out


def default_socket_path() -> Path:
    """The running app's socket: the first candidate that exists, else Qt's primary location."""
    cands = socket_candidates()
    return next((c for c in cands if c.exists()), cands[0])


class LiveError(RuntimeError):
    pass


class LiveClient:
    def __init__(self, socket_path: str | Path | None = None, timeout: float = 10.0) -> None:
        self.path = Path(socket_path) if socket_path else default_socket_path()
        self.timeout = timeout

    def _rpc(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not hasattr(socket, "AF_UNIX"):
            raise LiveError("Local sockets are not available on this platform")
        if not self.path.exists():
            raise LiveError(f"Bang On Pro is not running (no socket at {self.path})")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(str(self.path))
            s.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        except OSError as exc:
            raise LiveError(f"Bang On Pro did not answer: {exc}") from exc
        finally:
            s.close()
        line = buf.split(b"\n", 1)[0]
        try:
            return json.loads(line)
        except ValueError as exc:
            raise LiveError("Bang On Pro sent an unreadable reply") from exc

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        return self._rpc({"rpc_method": method, "request_id": f"mcp_{uuid.uuid4().hex[:8]}", **params})

    def status(self) -> dict[str, Any]:
        try:
            r = self.call("ping")
            return {"running": True, "revision": r.get("current_revision"), "socket": str(self.path)}
        except LiveError as exc:
            return {"running": False, "reason": str(exc)}

    def state(self) -> dict[str, Any]:
        """The live timeline as tracks and clips (frame ranges as the native engine reports them)."""
        r = self.call("get_state")
        st = r.get("state") or {}
        return {"revision": r.get("revision"), "backend": r.get("backend"), "can_undo": r.get("can_undo"), "can_redo": r.get("can_redo"),
                "tracks": st.get("tracks", []), "clips": st.get("clips", []), "sequences": st.get("sequences", []), "media": st.get("media", [])}

    def execute(self, batch: dict[str, Any]) -> dict[str, Any]:
        from bangonpro.ai_engine.commands import validate_timeline_command_batch

        validate_timeline_command_batch(batch)
        return self._rpc(batch)

    def undo(self, actor: str = "mcp") -> dict[str, Any]:
        return self.call("undo", actor_id=actor)

    def redo(self, actor: str = "mcp") -> dict[str, Any]:
        return self.call("redo", actor_id=actor)


__all__ = ["LiveClient", "LiveError", "default_socket_path", "socket_candidates"]
