"""Shared reasoning for the department: the editor's own authorised CLIs, never a bundled model.

Two ways in:

1. **Client-driven** (preferred): Claude Code, Codex, Grok Build or Antigravity is connected to the
   Bang On Pro MCP server and does the reasoning itself while it calls the tools. That client owns its
   own session and limits; nothing here is involved and Bang On Pro never switches it.
2. **App-driven**: Bang On Pro asks an installed, already signed-in CLI for a short structured plan
   through its documented headless mode. The subscription guard verifies the official account
   and included usage before sending the plan. Providers with unverified billing stay unavailable.

The router shares consent, authorisation, failover, cooldowns and usage across both application
entry points. Every external attempt reserves a ledger entry before execution. Account login and
project-specific MCP connections remain independent of app-driven reasoning.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Protocol

from . import subscriptions

PROVIDERS = ("claude", "codex", "grok", "agy")
CLIENTS = PROVIDERS  # V1.4 name
PROVIDER_INFO: dict[str, dict[str, Any]] = {
    "claude": {"title": "Claude Code", "binary": "claude", "login": "claude (then /login)", "api_key_env": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")},
    "codex": {"title": "OpenAI Codex", "binary": "codex", "login": "codex login", "api_key_env": ("OPENAI_API_KEY", "CODEX_API_KEY")},
    "grok": {"title": "Grok Build", "binary": "grok", "login": "grok (sign in with your X / SuperGrok account)", "api_key_env": ("XAI_API_KEY", "GROK_API_KEY")},
    "agy": {"title": "Google Antigravity", "binary": "agy", "login": "agy (sign in with your Google account)", "api_key_env": ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY")},
}
# Failure kinds. Only availability problems fail over; the others need the editor or a better request.
FAILOVER_KINDS = frozenset({"usage_exhausted", "rate_limited", "unavailable", "auth", "not_entitled", "incompatible"})
NEEDS_EDITOR = frozenset({"auth", "not_entitled", "incompatible"})  # parked until re-authorised, never retried on a timer
DEFAULT_PRIVACY = {"mode": "local_only", "consent": False, "metadata": True, "transcript": True}
DEFAULT_COOLDOWN = {"usage_exhausted": 3600.0, "rate_limited": 60.0, "unavailable": 30.0}
MAX_COOLDOWN = 7 * 86400.0


def config_dir() -> Path:
    base = os.environ.get("BANGONPRO_CONFIG_DIR") or os.path.join(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"), "bangonpro")
    return Path(base)


def config_path() -> Path:
    return config_dir() / "reasoning.json"


def state_path() -> Path:
    return config_dir() / "provider_state.json"


def ledger_path() -> Path:
    return config_dir() / "reasoning_ledger.jsonl"


def _now_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(p: Path, doc: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=p.parent, delete=False) as tmp:
        name = tmp.name
        try:
            json.dump(doc, tmp, indent=1)
            tmp.close()
            os.replace(name, p)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name)


@contextlib.contextmanager
def _storage_lock(name: str = "settings"):
    config_dir().mkdir(parents=True, exist_ok=True)
    with (config_dir() / f".{name}.lock").open("a") as lock:
        try:
            import fcntl
        except ImportError:
            import msvcrt

            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def privacy(cfg: dict[str, Any]) -> dict[str, Any]:
    saved = cfg.get("privacy")
    return {**DEFAULT_PRIVACY, **saved} if isinstance(saved, dict) else DEFAULT_PRIVACY.copy()


def enabled(cfg: dict[str, Any]) -> bool:
    policy = privacy(cfg)
    return policy["mode"] == "connected_subscriptions" and policy["consent"] is True


def configure(settings: dict[str, Any]) -> dict[str, Any]:
    """Update only routing and privacy settings in the router's existing authorisation file."""
    with _storage_lock():
        cfg = load_config()
        if "priority" in settings:
            set_priority(settings["priority"], cfg=cfg, save=False)
        for key in ("max_calls_per_task", "max_calls_per_day"):
            if key in settings:
                value = settings[key]
                if (value is not None or key == "max_calls_per_task") and (type(value) is not int or value < 1):
                    raise ValueError("Reasoning budgets must be positive whole numbers.")
                cfg[key] = value
        if "auto_failover" in settings:
            cfg["auto_failover"] = settings["auto_failover"] is True
        if "privacy" in settings:
            policy = settings["privacy"]
            if not isinstance(policy, dict):
                raise ValueError("Privacy settings must be an object.")
            cfg["privacy"] = {
                "mode": "connected_subscriptions" if enabled(settings) else "local_only",
                "consent": enabled(settings),
                "metadata": policy.get("metadata") is True,
                "transcript": policy.get("transcript") is True,
            }
        return _save_config(cfg)


def load_config() -> dict[str, Any]:
    """The authorisation file (V1.4 single-client files are read as one authorised provider)."""
    try:
        cfg = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(cfg, dict):
        return {}
    if "providers" not in cfg and cfg.get("client") in PROVIDERS:  # V1.4
        cfg = {"version": 2, "providers": {cfg["client"]: {"authorised": bool(cfg.get("authorised")), "authorised_at": cfg.get("authorised_at"), "model": cfg.get("model")}},
               "priority": [cfg["client"]], "max_calls_per_task": int(cfg.get("max_calls_per_task", 3))}
    entries = cfg.get("providers")
    provs = {k: v for k, v in entries.items() if k in PROVIDERS and isinstance(v, dict)} if isinstance(entries, dict) else {}
    cfg["providers"] = provs
    saved_priority = cfg.get("priority")
    order = list(dict.fromkeys(p for p in saved_priority if p in PROVIDERS)) if isinstance(saved_priority, list) else []
    cfg["priority"] = order + [p for p in PROVIDERS if p not in order]
    auth = [p for p in cfg["priority"] if provs.get(p, {}).get("authorised")]
    cfg["authorised"] = bool(auth)
    cfg["client"] = auth[0] if auth else None
    return cfg


def _save_config(cfg: dict[str, Any]) -> dict[str, Any]:
    doc = {k: cfg[k] for k in ("version", "providers", "priority", "max_calls_per_task", "max_calls_per_day", "privacy", "auto_failover") if k in cfg}
    doc["version"] = 3
    _write_json(config_path(), doc)
    return load_config()


def authorise(client: str, *, model: str | None = None, max_calls_per_task: int | None = None, max_calls_per_day: int | None = None,
              priority: list[str] | None = None, allow_api_billing: bool = False) -> dict[str, Any]:
    """Allow Bang On Pro to ask ``client`` for plans. Other authorised providers stay authorised."""
    if client not in PROVIDERS:
        raise ValueError(f"client must be one of {PROVIDERS}")
    if allow_api_billing:
        raise ValueError("Bang On Pro only routes included subscription usage.")
    with _storage_lock():
        cfg = load_config() or {"providers": {}, "priority": list(PROVIDERS)}
        entry = {"authorised": True, "authorised_at": _now_iso()}
        if model:
            entry["model"] = model
        cfg["providers"][client] = entry
        if priority:
            set_priority(priority, cfg=cfg, save=False)
        if max_calls_per_task is not None:
            cfg["max_calls_per_task"] = max(1, int(max_calls_per_task))
        if max_calls_per_day is not None:
            cfg["max_calls_per_day"] = max(1, int(max_calls_per_day))
        out = _save_config(cfg)
        st = _load_state()
        status = st.get("providers", {}).get(client, {})
        if status.get("kind") in NEEDS_EDITOR:
            status.pop("kind", None)
        _save_state(st)
    return out


def set_priority(order: list[str], *, cfg: dict[str, Any] | None = None, save: bool = True) -> dict[str, Any]:
    if save:
        return configure({"priority": order})
    bad = [p for p in order if p not in PROVIDERS]
    if bad:
        raise ValueError(f"unknown providers {bad}; use {PROVIDERS}")
    cfg = cfg if cfg is not None else load_config()
    rest = [p for p in cfg.get("priority") or [] if p not in order]
    cfg["priority"] = list(dict.fromkeys(order)) + rest
    return cfg


def revoke(client: str | None = None) -> None:
    """Withdraw one provider's authorisation, or all of them."""
    with _storage_lock():
        cfg = load_config() or {"providers": {}, "priority": list(PROVIDERS)}
        providers = cfg["providers"]
        for p in (PROVIDERS if client is None else (client,)):
            if p in providers:
                providers[p] = {"authorised": False, "revoked_at": _now_iso()}
        _save_config(cfg)


# ---------------------------------------------------------------------- provider state + ledger
def _load_state() -> dict[str, Any]:
    try:
        doc = json.loads(state_path().read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(doc: dict[str, Any]) -> None:
    doc["events"] = (doc.get("events") or [])[-50:]
    with contextlib.suppress(OSError):
        _write_json(state_path(), doc)


def _ledger_rows(since: float) -> list[dict[str, Any]]:
    rows = []
    try:
        with ledger_path().open(encoding="utf-8") as fh:
            for line in fh:
                with contextlib.suppress(ValueError):
                    r = json.loads(line)
                    if isinstance(r, dict) and isinstance(r.get("ts"), (int, float)) and r["ts"] >= since:
                        rows.append(r)
    except OSError:
        pass
    return rows


class ReasonerError(RuntimeError):
    pass


class BudgetExhausted(ReasonerError):
    pass


class AllProvidersUnavailable(ReasonerError):
    def __init__(self, message: str, *, retry_at: float | None, attempts: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.retry_at = retry_at
        self.attempts = attempts


@dataclass
class ProviderFailure(Exception):
    kind: str
    message: str
    retry_at: float | None = None
    provider: str = ""

    def __str__(self) -> str:
        return f"{self.provider or 'provider'} {self.kind}: {self.message[:200]}"


@dataclass
class ReasoningBudget:
    """One budget for every reasoning call of a task, across providers, agents and purposes.

    Every attempt is charged before it is made, failed ones included, and appended to the local
    ledger with whatever usage the CLI reported (``"unknown"`` when it reported none).
    """

    max_calls: int = 3
    max_calls_per_day: int | None = None
    used: int = 0
    attempts: list[dict[str, Any]] = field(default_factory=list)


    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None = None) -> ReasoningBudget:
        cfg = load_config() if cfg is None else cfg
        return cls(max_calls=int(cfg.get("max_calls_per_task", 3)), max_calls_per_day=cfg.get("max_calls_per_day"))

    def used_today(self) -> int:
        day0 = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return len(_ledger_rows(day0))

    def remaining(self) -> int:
        left = self.max_calls - self.used
        if self.max_calls_per_day is not None:
            left = min(left, int(self.max_calls_per_day) - self.used_today())
        return max(0, left)

    def charge(self, row: dict[str, Any]) -> None:
        # Reserve a durable attempt before requesting reasoning. Concurrent app processes use the same ledger.
        with _storage_lock("ledger"):
            budget_id = row.get("budget_id")
            if budget_id:
                self.used = sum(entry.get("budget_id") == budget_id for entry in _ledger_rows(0))
            if self.used >= self.max_calls:
                raise BudgetExhausted(f"Reasoning budget of {self.max_calls} calls for this task is used up")
            if self.max_calls_per_day is not None and self.used_today() >= int(self.max_calls_per_day):
                raise BudgetExhausted(f"Daily reasoning budget of {self.max_calls_per_day} calls is used up")
            fd = os.open(ledger_path(), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as ledger:
                ledger.write(json.dumps(row | {"kind": "started"}) + "\n")
                ledger.flush()
                os.fsync(ledger.fileno())
            self.used += 1

    def record(self, row: dict[str, Any]) -> None:
        self.attempts.append(row)
        with _storage_lock("ledger"):
            path = ledger_path()
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
                try:
                    with path.open(encoding="utf-8") as ledger:
                        for line in ledger:
                            try:
                                matches = json.loads(line).get("attempt_id") == row["attempt_id"]
                            except (ValueError, AttributeError):
                                matches = False
                            tmp.write(json.dumps(row, default=str) + "\n" if matches else line)
                    tmp.close()
                    os.replace(tmp.name, path)
                finally:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(tmp.name)

    def summary(self) -> dict[str, Any]:
        cost = [a["estimated_cost_usd"] for a in self.attempts if isinstance(a.get("estimated_cost_usd"), (int, float))]
        return {"calls": self.used, "max_calls": self.max_calls, "failed": sum(1 for a in self.attempts if not a.get("ok")),
                "providers": sorted({a["provider"] for a in self.attempts}), "estimated_cost_usd": round(sum(cost), 4) if cost and len(cost) == len(self.attempts) else "unknown"}


# ---------------------------------------------------------------------- failure classification
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("usage_exhausted", re.compile(r"usage limit|limit reached|hit your (?:usage )?limit|out of (?:credits|usage|quota)|quota (?:exceeded|exhausted)|exceeded your (?:current )?quota|"
                                   r"resource[_ ]exhausted|insufficient[_ ]quota|credit balance is too low|(?:weekly|daily|monthly|5-hour|session) limit|"
                                   r"used up your|no (?:remaining )?(?:credits|messages)", re.I)),
    ("auth", re.compile(r"not logged in|log ?in (?:again|required|first)|please (?:run )?(?:/login|log ?in|\S+ login)|unauthori[sz]ed|\b401\b|authentication (?:failed|required|error)|"
                        r"invalid (?:api[ _-]?key|x-api-key|token)|oauth token (?:has )?expired|credentials? (?:expired|not found|missing)|sign in to|not signed in|unauthenticated", re.I)),
    ("not_entitled", re.compile(r"not (?:available|included) (?:on|in|with) your (?:plan|subscription)|upgrade (?:your|to a) (?:plan|subscription)|subscription (?:required|does not)|"
                                r"not entitled|requires? a (?:paid|pro|plus|max|supergrok|premium)|permission[_ ]denied|\b403\b|forbidden|access denied|"
                                r"model .{0,40}(?:not available|not supported|does not exist|not found)", re.I)),
    ("rate_limited", re.compile(r"rate[ _-]?limit|too many requests|\b429\b|slow down|requests per minute", re.I)),
    ("incompatible", re.compile(r"unknown (?:option|flag|argument|command)|unrecognized (?:option|arguments?)|unexpected argument|invalid value for '--|no such option", re.I)),
    ("invalid_request", re.compile(r"\b400\b|invalid[_ ]request|invalid[_ ]argument|prompt is too long|context (?:length|window) exceeded|maximum context", re.I)),
    ("unavailable", re.compile(r"overloaded|\b(?:500|502|503|504|529)\b|service unavailable|temporarily unavailable|internal server error|bad gateway|timed? ?out|"
                               r"connection (?:refused|reset|error|closed)|network (?:error|unreachable)|econn\w+|enotfound|stream error|\bunavailable\b|try again later", re.I)),
]
_UNITS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
          "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "d": 86400, "day": 86400, "days": 86400}


def parse_reset(text: str, now: float | None = None) -> float | None:
    """When a provider said it will be available again (epoch seconds), if it said so."""
    now = time.time() if now is None else now
    t = text or ""
    if m := re.search(r"\|(\d{10})\b", t):  # 'Claude AI usage limit reached|1732600000'
        return float(m.group(1))
    if m := re.search(r"retry[-_ ]after[\"':= ]+(\d+(?:\.\d+)?)", t, re.I):
        return now + float(m.group(1))
    if m := re.search(r"(?:try again|retry|resets?|available again)\s+in\s+((?:\d+(?:\.\d+)?\s*[a-z]+[\s,and]*){1,4})", t, re.I):
        total = sum(float(n) * _UNITS[u.lower()] for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", m.group(1), re.I) if u.lower() in _UNITS)
        if total > 0:
            return now + total
    if m := re.search(r"(?:resets?|try again|available)\s+(?:at|on)?\s*(\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)", t, re.I):
        with contextlib.suppress(ValueError):
            dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
            return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()
    if m := re.search(r"(?:resets?|try again)\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t, re.I):
        h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        local = datetime.fromtimestamp(now).astimezone()
        cand = local.replace(hour=h, minute=int(m.group(2) or 0), second=0, microsecond=0)
        if cand.timestamp() <= now:
            cand += timedelta(days=1)
        return cand.timestamp()
    return None


def classify_failure(provider: str, text: str, *, returncode: int | None = None, now: float | None = None) -> ProviderFailure:
    now = time.time() if now is None else now
    kind = next((k for k, rx in _PATTERNS if rx.search(text or "")), "unknown")
    if kind == "unknown" and returncode in (None, 0):
        kind = "invalid_output"
    retry = parse_reset(text, now) if kind in DEFAULT_COOLDOWN else None
    return ProviderFailure(kind=kind, message=(text or "").strip()[-400:] or f"exit {returncode}", retry_at=retry, provider=provider)


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


# ---------------------------------------------------------------------- adapters
def _clean_env(provider: str, allow_api_billing: bool) -> dict[str, str]:
    return subscriptions.client_env(subscriptions.client_path(provider))


def _run(argv: list[str], *, cwd: str, env: dict[str, str], timeout: float, cancel: threading.Event | None, use_pty: bool = False, input_text: str | None = None) -> tuple[int, str, str]:
    """Run one headless call; stops on cancel or timeout (the process is killed, nothing is retried)."""
    master = slave = None
    if use_pty:  # Antigravity print mode may write nothing to a non-TTY stdout (upstream issue); give it a terminal
        import pty

        master, slave = pty.openpty()
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL, stdout=slave if use_pty else subprocess.PIPE, stderr=subprocess.PIPE, text=not use_pty)  # noqa: S603
    out_chunks: list[bytes] = []
    if use_pty:
        os.close(slave)  # type: ignore[arg-type]

        def pump() -> None:
            while True:
                try:
                    data = os.read(master, 65536)  # type: ignore[arg-type]
                except OSError:
                    return
                if not data:
                    return
                out_chunks.append(data)

        threading.Thread(target=pump, daemon=True).start()
    t_end = time.monotonic() + timeout
    while True:
        try:
            out, err = proc.communicate(input=input_text, timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            input_text = None
            if (cancel is not None and cancel.is_set()) or time.monotonic() > t_end:
                proc.kill()
                proc.communicate()
                raise ProviderFailure("cancelled" if cancel is not None and cancel.is_set() else "unavailable", "the call was stopped" if cancel is not None and cancel.is_set() else f"no answer within {timeout:.0f}s") from None
    if use_pty:
        time.sleep(0.05)
        with contextlib.suppress(OSError):
            os.close(master)  # type: ignore[arg-type]
        out = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", b"".join(out_chunks).decode("utf-8", "replace")).replace("\r", "")
        err = err.decode("utf-8", "replace") if isinstance(err, bytes) else err
    return proc.returncode, out or "", err or ""


def _json_obj(text: str) -> dict[str, Any] | None:
    s = text.strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        last = [ln for ln in s.splitlines() if ln.strip().startswith("{")]
        for ln in reversed(last):
            with contextlib.suppress(ValueError):
                obj = json.loads(ln)
                if isinstance(obj, dict):
                    return obj
    return None


def _usage(obj: dict[str, Any] | None) -> dict[str, Any]:
    if not obj:
        return {}
    u = {k: obj.get(k) for k in ("total_cost_usd", "duration_ms", "num_turns", "usage", "modelUsage", "stats") if obj.get(k) is not None}
    return u


class Adapter:
    """One provider's documented headless call. ``call`` returns the answer text or raises ProviderFailure."""

    name = ""

    def argv(self, prompt: str, model: str | None, workdir: str) -> list[str]:
        raise NotImplementedError

    def parse(self, rc: int, out: str, err: str, workdir: str) -> tuple[str, dict[str, Any]]:
        raise NotImplementedError

    def call(self, prompt: str, *, model: str | None, timeout: float, cancel: threading.Event | None, allow_api_billing: bool) -> tuple[str, dict[str, Any]]:
        with tempfile.TemporaryDirectory(prefix=f"bop_{self.name}_") as td:
            argv = self.argv(prompt, model, td)
            argv[0] = subscriptions.client_path(self.name) or argv[0]
            rc, out, err = _run(argv, cwd=td, env=_clean_env(self.name, allow_api_billing), timeout=timeout, cancel=cancel)
            return self.parse(rc, out, err, td)

    def fail(self, text: str, rc: int | None) -> ProviderFailure:
        return classify_failure(self.name, text, returncode=rc)


class ClaudeAdapter(Adapter):
    name = "claude"

    def argv(self, prompt: str, model: str | None, workdir: str) -> list[str]:
        return ["claude", "-p", prompt, "--output-format", "json", "--max-turns", "1", *(["--model", model] if model else [])]

    def parse(self, rc: int, out: str, err: str, workdir: str) -> tuple[str, dict[str, Any]]:
        obj = _json_obj(out)
        if obj is not None and (obj.get("is_error") or obj.get("subtype", "success") != "success"):
            raise self.fail(f"{obj.get('result', '')} {obj.get('subtype', '')} {obj.get('api_error_status') or ''} {err}", rc or 1)
        if rc != 0:
            raise self.fail(f"{err} {out}", rc)
        return (str(obj.get("result", "")) if obj is not None else out), _usage(obj)


class CodexAdapter(Adapter):
    name = "codex"

    def argv(self, prompt: str, model: str | None, workdir: str) -> list[str]:
        return ["codex", "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                "--skip-git-repo-check", "--sandbox", "read-only", *subscriptions.CODEX_OPTIONS,
                "-c", 'forced_login_method="chatgpt"', "--output-last-message", str(Path(workdir) / "last.txt"), "-"]

    def run_verified(self, prompt: str, *, binary: str, access: dict[str, Any], timeout: float,
                     cancel: threading.Event | None) -> tuple[str, dict[str, Any]]:
        with tempfile.TemporaryDirectory(prefix="bop_codex_") as td:
            argv = self.argv(prompt, None, td)
            argv[0] = binary
            argv[-1:-1] = access["auth_config"]
            rc, out, err = _run(argv, cwd=td, env=subscriptions.client_env(binary), timeout=timeout, cancel=cancel, input_text=prompt)
            return self.parse(rc, out, err, td)

    def call(self, prompt: str, *, model: str | None, timeout: float, cancel: threading.Event | None, allow_api_billing: bool) -> tuple[str, dict[str, Any]]:
        binary = subscriptions.client_path(self.name)
        access = subscriptions.subscription_access(self.name, binary) if binary else {"ready": False, "message": "Codex is missing"}
        if not access["ready"]:
            raise ProviderFailure("not_entitled", access["message"], access.get("retry_at"), self.name)
        return self.run_verified(prompt, binary=binary, access=access, timeout=timeout, cancel=cancel)

    def fail(self, text: str, rc: int | None) -> ProviderFailure:
        failure = super().fail(text, rc)
        # CLI diagnostics can echo the editor's request. Persist only the classification and reset time.
        failure.message = {
            "auth": "The Codex account session needs attention.",
            "not_entitled": "Codex denied access for this account.",
            "incompatible": "Update the official Codex client.",
            "usage_exhausted": "Codex reported an included usage limit.",
            "rate_limited": "Codex reported a rate limit.",
            "unavailable": "Codex is temporarily unavailable.",
        }.get(failure.kind, "Codex could not complete the reasoning request.")
        return failure

    def parse(self, rc: int, out: str, err: str, workdir: str) -> tuple[str, dict[str, Any]]:
        last = Path(workdir) / "last.txt"
        if rc != 0:
            raise self.fail(f"{err} {out}", rc)
        text = last.read_text(encoding="utf-8") if last.is_file() else out
        m = re.search(r"tokens used:?\s*([\d,]+)", f"{err}\n{out}", re.I)
        return text, ({"tokens_used": int(m.group(1).replace(",", ""))} if m else {})


class GrokAdapter(Adapter):
    name = "grok"

    def argv(self, prompt: str, model: str | None, workdir: str) -> list[str]:
        # Planning needs no tools: deny shell, edits, writes and web, and no subagents.
        return ["grok", "--no-auto-update", "-p", prompt, "--output-format", "json", "--max-turns", "1", "--cwd", workdir, "--disallowed-tools", "Agent",
                "--deny", "Bash", "--deny", "Edit", "--deny", "Write", "--deny", "WebFetch", *(["-m", model] if model else [])]

    def parse(self, rc: int, out: str, err: str, workdir: str) -> tuple[str, dict[str, Any]]:
        obj = _json_obj(out)
        if obj is not None and obj.get("type") == "error":
            raise self.fail(f"{obj.get('message', '')} {err}", rc or 1)
        if rc != 0:
            raise self.fail(f"{err} {out}", rc)
        if obj is None:
            raise ProviderFailure("invalid_output", "grok returned no JSON result", provider=self.name)
        return str(obj.get("text") or obj.get("result") or ""), _usage(obj)


class AntigravityAdapter(Adapter):
    name = "agy"

    def argv(self, prompt: str, model: str | None, workdir: str) -> list[str]:
        return ["agy", "-p", prompt, "--output-format", "json", *(["--model", model] if model else [])]

    def call(self, prompt: str, *, model: str | None, timeout: float, cancel: threading.Event | None, allow_api_billing: bool) -> tuple[str, dict[str, Any]]:
        with tempfile.TemporaryDirectory(prefix="bop_agy_") as td:
            env = _clean_env(self.name, allow_api_billing)
            rc, out, err = _run(self.argv(prompt, model, td), cwd=td, env=env, timeout=timeout, cancel=cancel)
            if rc == 0 and not out.strip():
                rc, out, err = _run(self.argv(prompt, model, td), cwd=td, env=env, timeout=timeout, cancel=cancel, use_pty=True)
            return self.parse(rc, out, err, td)

    def parse(self, rc: int, out: str, err: str, workdir: str) -> tuple[str, dict[str, Any]]:
        obj = _json_obj(out)
        if obj is not None and (obj.get("is_error") or obj.get("error")):
            e = obj.get("error")
            raise self.fail(f"{json.dumps(e) if isinstance(e, dict) else e or ''} {obj.get('result', '')} {err}", rc or 1)
        if rc != 0:
            raise self.fail(f"{err} {out}", rc)
        if obj is None:
            if not out.strip():
                raise ProviderFailure("invalid_output", "agy printed nothing", provider=self.name)
            return out, {}
        so = obj.get("structured_output")
        text = json.dumps(so) if isinstance(so, dict) else str(next((obj[k] for k in ("result", "response", "text", "output", "content") if obj.get(k)), ""))
        return text, _usage(obj)


ADAPTERS: dict[str, type[Adapter]] = {"claude": ClaudeAdapter, "codex": CodexAdapter, "grok": GrokAdapter, "agy": AntigravityAdapter}


# ---------------------------------------------------------------------- router
class Reasoner(Protocol):
    kind: str

    def plan(self, prompt: str) -> dict[str, Any]: ...


class ProviderRouter:
    """Use one authorised provider at a time; fail over on confirmed availability problems only."""

    kind = "authenticated_cli"

    def __init__(self, *, only: list[str] | None = None, budget: ReasoningBudget | None = None, timeout: float = 240.0, adapters: dict[str, Adapter] | None = None,
                 installed: Any = None, clock: Any = None, on_switch: Any = None, cancel: threading.Event | None = None, test: bool = False) -> None:
        self.cfg = load_config()
        self.adapters = adapters or {p: ADAPTERS[p]() for p in PROVIDERS}
        self._installed = installed or (lambda p: bool(subscriptions.client_path(p)))
        self.test = test
        self.clock = clock or time.time
        self.only = only
        self.budget = budget or ReasoningBudget.from_config(self.cfg)
        self._task_cap = budget.max_calls if budget else None
        self.timeout = timeout
        self.on_switch = on_switch  # called with the event before the next provider is asked (checkpoint hook)
        self.cancel = cancel
        self.active: str | None = None
        self.provider: str | None = None
        self.last_usage: dict[str, Any] = {}
        self.events: list[dict[str, Any]] = []
        self.task_id: str | None = None
        self.budget_id: str | None = None

    # compatibility with V1.4's CliReasoner
    @property
    def calls(self) -> int:
        return self.budget.used

    @property
    def max_calls(self) -> int:
        return self.budget.max_calls

    def authorised(self) -> list[str]:
        provs = self.cfg.get("providers") or {}
        order = [p for p in self.cfg.get("priority") or [] if provs.get(p, {}).get("authorised")]
        return [p for p in order if self.only is None or p in self.only]

    def availability(self, p: str, st: dict[str, Any] | None = None) -> tuple[bool, str]:
        st = _load_state() if st is None else st
        if not self._installed(p):
            return False, "Not installed"
        entry = (self.cfg.get("providers") or {}).get(p, {})
        if entry.get("allow_api_billing"):
            return False, "Reconnect to remove the legacy billing configuration"
        if p in subscriptions.BLOCKED:
            return False, subscriptions.BLOCKED[p]
        ps = (st.get("providers") or {}).get(p) or {}
        if not self.test and ps.get("kind") in NEEDS_EDITOR:
            return False, f"{ps['kind']}: {ps.get('message', '')[:120]}"
        if float(ps.get("until") or 0) > self.clock():
            return False, f"{ps.get('kind')} until {_now_iso(float(ps['until']))}"
        return True, "Available"

    def candidates(self) -> list[str]:
        st = _load_state()
        order = self.authorised()
        if self.active in order:  # stay on the provider that is working until a task boundary
            order = [self.active] + [p for p in order if p != self.active]
        if self.cfg.get("auto_failover") is False:
            order = order[:1]
        return [p for p in order if self.availability(p, st)[0]]

    def begin_task(self, task_id: str | None = None, *, budget_id: str | None = None) -> None:
        """Safe task boundary: forget the sticky choice so the preferred provider is used again once its cooldown has passed."""
        self.active = None
        self.task_id = task_id
        self.budget_id = budget_id
        if budget_id:
            self.budget.used = sum(row.get("budget_id") == budget_id for row in _ledger_rows(0))

    def plan(self, prompt: str | Callable[[dict[str, Any]], str], *, purpose: str = "plan", agent: str = "executive") -> dict[str, Any]:
        tried: list[dict[str, Any]] = []
        while True:
            self.cfg = load_config()
            if not enabled(self.cfg):
                raise AllProvidersUnavailable("External reasoning requires Connected Subscriptions consent", retry_at=None, attempts=tried)
            cands = [p for p in self.candidates() if p not in {t["provider"] for t in tried}]
            if not cands:
                st = (_load_state().get("providers") or {})
                waits = [float(st[p]["until"]) for p in self.authorised() if p in st and float(st[p].get("until") or 0) > self.clock() and st[p].get("kind") not in NEEDS_EDITOR]
                raise AllProvidersUnavailable("No authorised reasoning provider is available" + (f"; the earliest is back at {_now_iso(min(waits))}" if waits else ""),
                                              retry_at=min(waits) if waits else None, attempts=tried)
            p = cands[0]
            pcfg = self.cfg["providers"][p]
            t0 = time.perf_counter()
            row: dict[str, Any] = {"ts": time.time(), "at": _now_iso(), "provider": p, "purpose": purpose, "agent": agent,
                                   "task_id": self.task_id, "budget_id": self.budget_id, "attempt_id": uuid.uuid4().hex}
            reserved = False
            try:
                adapter = self.adapters[p]
                verified = isinstance(adapter, CodexAdapter)
                if verified:
                    binary = subscriptions.client_path(p)
                    if not binary:
                        raise ProviderFailure("unavailable", "Codex is missing")
                    access = subscriptions.subscription_access(p, binary)
                    if not access["ready"]:
                        self._remember_access(p, access)
                        reset = access.get("retry_at")
                        raise ProviderFailure("usage_exhausted" if reset else "not_entitled", access["message"], reset)
                with _storage_lock():
                    current = load_config()
                    if current != self.cfg:
                        continue  # sign-in, consent or routing changed during the preflight
                    if self.cancel is not None and self.cancel.is_set():
                        raise ProviderFailure("cancelled", "the call was stopped")
                    self.budget.max_calls_per_day = current.get("max_calls_per_day")
                    max_calls = int(current.get("max_calls_per_task", 3))
                    self.budget.max_calls = min(max_calls, self._task_cap) if self._task_cap is not None else max_calls
                    text_prompt = prompt(privacy(current)) if callable(prompt) else prompt
                    self.budget.charge(row)
                    reserved = True
                if verified:
                    self._remember_access(p, access)
                    text, usage = adapter.run_verified(text_prompt, binary=binary, access=access, timeout=self.timeout, cancel=self.cancel)
                else:
                    text, usage = adapter.call(text_prompt, model=pcfg.get("model"), timeout=self.timeout, cancel=self.cancel, allow_api_billing=False)
                plan = extract_json(text)
                if self.test and plan.get("ok") is not True:
                    raise ReasonerError("The provider did not return the connection-test response")
            except BudgetExhausted:
                raise
            except OSError as exc:
                if reserved:
                    self.budget.record(row | {"ok": False, "kind": "io_error", "usage": "unknown"})
                raise ReasonerError("The reasoning client or its usage ledger could not be accessed") from exc
            except ProviderFailure as f:
                f.provider = p
                row.update(ok=False, kind=f.kind, duration_ms=round((time.perf_counter() - t0) * 1000), usage="unknown")
                if reserved:
                    self.budget.record(row)
                tried.append({"provider": p, "kind": f.kind, "message": f.message[:200]})
                if f.kind == "cancelled":
                    raise ReasonerError("Reasoning was cancelled") from f
                if f.kind not in FAILOVER_KINDS:
                    raise ReasonerError(str(f)) from f
                self._park(p, f)
                nxt = next((c for c in self.candidates() if c not in {t["provider"] for t in tried}), None)
                ev = {"at": _now_iso(self.clock()), "from": p, "to": nxt, "kind": f.kind, "reason": f.message[:160], "retry_at": _now_iso(f.retry_at) if f.retry_at else None,
                      "task_id": self.task_id, "mode": "app_driven"}
                if not self.test and self.cfg.get("auto_failover") is not False:
                    self.events.append(ev)
                    with _storage_lock():
                        st = _load_state()
                        st.setdefault("events", []).append(ev)
                        _save_state(st)
                    if nxt is not None and self.on_switch is not None:
                        self.on_switch(ev)
                self.active = None
                continue
            except ReasonerError as exc:  # the answer had no usable plan: a reasoning problem, not an availability one
                row.update(ok=False, kind="invalid_output", duration_ms=round((time.perf_counter() - t0) * 1000), usage="unknown")
                if reserved:
                    self.budget.record(row)
                raise ReasonerError(f"{p}: {exc}") from exc
            row.update(ok=True, kind="ok", duration_ms=round((time.perf_counter() - t0) * 1000), usage=usage or "unknown",
                       estimated_cost_usd=usage.get("total_cost_usd") if isinstance(usage, dict) else None)
            self.budget.record(row)
            self._mark_ok(p)
            self.active = self.provider = p
            self.last_usage = {"provider": p, **(usage or {})}
            return plan

    def _remember_access(self, p: str, access: dict[str, Any]) -> None:
        with _storage_lock():
            st = _load_state()
            ps = st.setdefault("providers", {}).setdefault(p, {})
            ps["subscription"] = access["message"]
            ps["subscription_ready"] = access["ready"]
            reset = access.get("reset_at")
            ps["reset_at"] = _now_iso(reset) if reset else None
            _save_state(st)

    def _park(self, p: str, f: ProviderFailure) -> None:
        with _storage_lock():
            st = _load_state()
            ps = st.setdefault("providers", {}).setdefault(p, {})
            ps["failures"] = int(ps.get("failures", 0)) + 1
            now = self.clock()
            if f.kind in NEEDS_EDITOR:
                until = None
            elif f.retry_at:
                until = min(max(f.retry_at, now + 30.0), now + MAX_COOLDOWN)
            else:
                base = DEFAULT_COOLDOWN.get(f.kind, 60.0)
                until = now + min(base * (2 ** max(0, ps["failures"] - 1)) if f.kind == "unavailable" else base, 900.0 if f.kind == "unavailable" else MAX_COOLDOWN)
            ps.update({"kind": f.kind, "message": f.message[:300], "since": _now_iso(now), "until": until, "reported_reset": bool(f.retry_at)})
            _save_state(st)

    def _mark_ok(self, p: str) -> None:
        with _storage_lock():
            st = _load_state()
            status = st.setdefault("providers", {}).setdefault(p, {})
            status.update(kind="ok", last_ok=_now_iso(self.clock()), failures=0, until=None, reported_reset=False)
            if self.test:
                status["last_test"] = status["last_ok"]
            else:
                st["active"] = p
            _save_state(st)


class CliReasoner(ProviderRouter):
    """V1.4 entry point: the authorised provider(s), with automatic failover between them."""

    def __init__(self, client: str | None = None, *, timeout: float = 240.0, model: str | None = None, **kw: Any) -> None:
        super().__init__(only=[client] if client else None, timeout=timeout, **kw)
        if client and client not in PROVIDERS:
            raise ReasonerError(f"Unknown provider '{client}'")
        if not enabled(self.cfg):
            raise ReasonerError("External reasoning is off in AI Providers & Subscriptions")
        auth = self.authorised()
        if not auth:
            raise ReasonerError(f"{client} is not authorised for app-driven reasoning" if client else
                                "No reasoning client is authorised in AI Providers & Subscriptions")
        if not any(self._installed(p) for p in auth):
            raise ReasonerError(f"{'/'.join(auth)} is not installed or not on PATH")
        if model and client:
            self.cfg["providers"][client]["model"] = model
        self.client = auth[0]


def provider_connection(provider: str, *, launch: bool = True) -> str:
    binary = subscriptions.client_path(provider)
    if not binary:
        return "Install the official client before connecting."
    auth = subscriptions.local_auth(provider, binary)
    if launch and (auth in ("signed_out", "other") or (auth == "unverified" and provider in ("grok", "agy"))):
        subscriptions.launch_login(provider, binary)
        return "Complete sign-in in the official client, then choose Check sign-in."
    if auth != "account":
        return "The official client did not verify subscription-account sign-in."
    access = subscriptions.subscription_access(provider, binary)
    authorise(provider)
    ProviderRouter()._remember_access(provider, access)
    return access["message"]


def reasoning_status(*, check_auth: bool = False) -> dict[str, Any]:
    cfg = load_config()
    st = _load_state()
    probe = ProviderRouter()
    provs: dict[str, Any] = {}
    for p in PROVIDERS:
        entry = (cfg.get("providers") or {}).get(p) or {}
        saved = st.get("providers", {}).get(p, {})
        binary = subscriptions.client_path(p)
        authorised = entry.get("authorised") is True
        auth = subscriptions.local_auth(p, binary) if binary and check_auth else "unverified"
        available, state = probe.availability(p, st) if authorised else (False, "Bang On Pro authorisation required")
        if p in subscriptions.BLOCKED:
            available, state = False, subscriptions.BLOCKED[p]
        if not binary:
            available, state = False, "Official client missing"
        elif auth in ("signed_out", "other") or (check_auth and auth == "unverified" and p not in subscriptions.BLOCKED):
            available, state = False, "Subscription-account sign-in required"
        elif not enabled(cfg) and available:
            available, state = False, "Local Only"
        elif available and not saved.get("subscription_ready", bool(saved.get("last_ok"))):
            available, state = False, saved.get("subscription") or "Pending subscription check"
        until = saved.get("until")
        provs[p] = {"test_supported": p not in subscriptions.BLOCKED, "title": PROVIDER_INFO[p]["title"], "installed": bool(binary), "authorised": authorised, "auth": auth,
                    "available": available, "state": state, "install_url": subscriptions.INSTALL_URLS[p],
                    "last_test": saved.get("last_test"), "last_ok": saved.get("last_ok"),
                    "cooldown_until": _now_iso(until) if until and until > time.time() else None,
                    "reported_reset": _now_iso(until) if until and saved.get("reported_reset") else saved.get("reset_at"),
                    "subscription": "Unverified" if p in subscriptions.BLOCKED else saved.get("subscription", "Not checked")}
    order = cfg.get("priority") or list(PROVIDERS)
    auth = [p for p in order if provs[p]["authorised"]]
    active = st.get("active")
    out: dict[str, Any] = {
        "installed_clients": [p for p in PROVIDERS if provs[p]["installed"]],
        "app_driven": "off", "providers": provs, "priority": order, "auto_failover": cfg.get("auto_failover", True),
        "privacy": cfg.get("privacy"), "active": active if provs.get(active, {}).get("available") else None,
        "failover_events": (st.get("events") or [])[-5:],
        "budget": {"max_calls_per_task": int(cfg.get("max_calls_per_task", 3)), "max_calls_per_day": cfg.get("max_calls_per_day"),
                   "used_today": ReasoningBudget().used_today()},
    }
    if enabled(cfg) and auth:
        out["app_driven"] = "authorised" if any(provs[p]["installed"] for p in auth) else "authorised_but_not_installed"
        out["client"] = next((p for p in auth if provs[p]["available"]), auth[0])
    return out


def provider_self_test(providers: list[str] | None = None, *, timeout: float = 120.0) -> list[dict[str, Any]]:
    """One minimal authenticated call through the same guard and daily budget."""
    out = []
    for p in providers if providers is not None else load_config().get("priority", []):
        r = ProviderRouter(only=[p], budget=ReasoningBudget(max_calls=1), timeout=timeout, test=True)
        try:
            r.plan('Reply with exactly this JSON and nothing else: {"ok": true}', purpose="self_test")
            out.append({"provider": p, "ok": True})
        except ReasonerError as exc:
            attempts = getattr(exc, "attempts", [])
            reason = attempts[-1]["message"] if attempts else str(exc)
            if not attempts and (r.cfg.get("providers") or {}).get(p, {}).get("authorised") and enabled(r.cfg):
                reason = r.availability(p)[1] if not r.availability(p)[0] else reason
            out.append({"provider": p, "ok": False, "error": reason[:300]})
    return out


class ScriptedReasoner:
    """Test double: returns prepared plans in order. Results produced with it are *mocked reasoning*."""

    kind = "scripted_mock"

    def __init__(self, plans: list[Any]) -> None:
        self.plans = list(plans)
        self.prompts: list[str] = []
        self.budget = ReasoningBudget(max_calls=100)
        self.last_usage: dict[str, Any] = {}

    def begin_task(self, task_id: str | None = None) -> None:
        pass

    def plan(self, prompt: str, *, purpose: str = "plan", agent: str = "executive") -> dict[str, Any]:
        self.prompts.append(prompt)
        if self.budget.used >= self.budget.max_calls:
            raise BudgetExhausted("Scripted reasoning budget exhausted")
        self.budget.used += 1
        if not self.plans:
            raise ReasonerError("No scripted plan left")
        nxt = self.plans.pop(0)
        return nxt(prompt) if callable(nxt) else nxt


__all__ = ["ADAPTERS", "CLIENTS", "FAILOVER_KINDS", "PROVIDERS", "PROVIDER_INFO", "Adapter", "AllProvidersUnavailable", "BudgetExhausted", "CliReasoner", "ProviderFailure",
           "ProviderRouter", "Reasoner", "ReasonerError", "ReasoningBudget", "ScriptedReasoner", "authorise", "classify_failure", "config_path", "extract_json", "load_config",
           "parse_reset", "provider_self_test", "reasoning_status", "revoke", "set_priority"]
