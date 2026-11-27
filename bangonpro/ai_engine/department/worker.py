"""Production worker ownership, liveness and graceful termination (V1.6).

Why this exists: V1.5 recorded ``status: running`` in ``job.json`` and trusted it.
A worker that died (the app quit, a crash, an unexpected exception) left that
state behind for ever, so the Agent Control Centre showed a production that was
"running" with no process behind it - frozen on "Resuming job from checkpoint",
with Cancel doing nothing and Resume disabled.

Now every process that executes agents for a job holds ``<job>/worker.lock``
with an OS advisory lock (``flock``). The kernel releases it when the process
ends for any reason, including ``SIGKILL`` and crashes, so "is a worker alive?"
has a definitive answer that cannot go stale and is immune to PID reuse. The C++
app probes the same lock (``AgentControlCentre::jobWorkerAlive``).

``SIGTERM`` (sent by the app when the editor quits or when a cancellation does
not stop the worker in time) raises :class:`WorkerTerminated` in the main
thread, so the job records a truthful ``interrupted`` / ``cancelled`` state
before exiting; a running ``subprocess.run`` child (ffmpeg, melt) is killed by
the standard library on the way out.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import os
import signal
import socket
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:  # POSIX (macOS, Linux). Windows is not a Bang On Pro target.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

LOCK_NAME = "worker.lock"

# Process-wide registry so nested sessions (revise -> rerun -> run) re-enter instead of deadlocking.
_HELD: dict[str, list[Any]] = {}
_HELD_GUARD = threading.Lock()


class JobBusyError(RuntimeError):
    """Another live process is already working on this production."""


class WorkerTerminated(BaseException):  # noqa: N818 - BaseException so agent isolation (except Exception) cannot swallow it
    """The worker was asked to stop (SIGTERM/SIGINT): record a truthful state, then exit."""


def _utc() -> str:
    return datetime.now(UTC).isoformat()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def worker_alive(job_dir: str | Path) -> bool:
    """True while some process holds the job's worker lock.

    Probing takes the lock non-blockingly and releases it at once. Where the file
    system does not support ``flock`` (some network shares), the PID recorded in
    the lock file is checked instead (same host only).
    """
    path = Path(job_dir) / LOCK_NAME
    if not path.exists():
        return False
    key = str(path.resolve())
    with _HELD_GUARD:
        if key in _HELD:
            return True  # this very process owns it
    if fcntl is not None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            pass  # unsupported here: fall back to the recorded PID
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)
    try:
        info = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return False
    return info.get("host") == socket.gethostname() and _pid_alive(int(info.get("pid") or 0))


def lock_owner(job_dir: str | Path) -> dict[str, Any]:
    try:
        return json.loads((Path(job_dir) / LOCK_NAME).read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}


@contextmanager
def worker_session(job_dir: str | Path, *, command: str = "run"):
    """Own the job for the duration of the block; re-entrant within one process.

    Raises :class:`JobBusyError` when another live process owns it.
    """
    path = (Path(job_dir) / LOCK_NAME).resolve()
    key = str(path)
    with _HELD_GUARD:
        held = _HELD.get(key)
        if held is not None:
            held[1] += 1
    if held is not None:
        try:
            yield held[2]
        finally:
            with _HELD_GUARD:
                held[1] -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except (AttributeError, OSError):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            owner = lock_owner(path.parent)
            raise JobBusyError(
                f"This production is already being processed (process {owner.get('pid', '?')}, {owner.get('command', 'run')}). "
                "Wait for it to finish or cancel it first."
            ) from None
        except OSError:
            if worker_alive(path.parent):
                os.close(fd)
                raise JobBusyError("This production is already being processed by another process.") from None
    info = {"pid": os.getpid(), "host": socket.gethostname(), "command": command, "started_at_utc": _utc()}
    os.ftruncate(fd, 0)
    os.write(fd, json.dumps(info).encode("utf-8"))
    os.fsync(fd)
    entry = [fd, 1, info]
    with _HELD_GUARD:
        _HELD[key] = entry
    try:
        yield info
    finally:
        with _HELD_GUARD:
            entry[1] -= 1
            release = entry[1] <= 0
            if release:
                _HELD.pop(key, None)
        if release:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(fd)


_installed = False


def install_termination_handlers() -> None:
    """Turn SIGTERM/SIGINT into :class:`WorkerTerminated` in the main thread (idempotent)."""
    global _installed
    if _installed or threading.current_thread() is not threading.main_thread():
        return

    def _handler(signum: int, _frame: Any) -> None:
        raise WorkerTerminated(f"stopped by signal {signal.Signals(signum).name}")

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
    _installed = True


__all__ = ["LOCK_NAME", "JobBusyError", "WorkerTerminated", "install_termination_handlers", "lock_owner", "worker_alive", "worker_session"]
