# SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
# SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""Official client discovery, sign-in and subscription eligibility checks."""

from __future__ import annotations

import json
import os
import selectors
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any

INSTALL_URLS = {
    "claude": "https://code.claude.com/docs/en/setup",
    "codex": "https://learn.chatgpt.com/docs/cli",
    "grok": "https://docs.x.ai/build/overview",
    "agy": "https://antigravity.google/docs/cli-install",
}
LOGIN_ARGS = {"claude": ["auth", "login"], "codex": ["login"], "grok": ["login"], "agy": []}
BLOCKED = {
    "claude": "Claude Code does not expose a supported check that rules out usage-credit fallback for this integration.",
    "grok": "Grok Build does not expose a verified account entitlement and top-up check for this integration.",
    "agy": "Antigravity does not expose a verified subscription sign-in and billing check for this integration.",
}
APP_BINARIES = {
    "codex": (
        "ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
        "Codex.app/Contents/Resources/codex",
    ),
    "agy": (
        "Antigravity.app/Contents/Resources/app/bin/agy",
        "Antigravity.app/Contents/Resources/bin/agy",
    ),
}
PERSONAL_CODEX_PLANS = {"plus", "pro", "prolite", "promax"}
CODEX_OPTIONS = [
    "-c", 'model_provider="openai"',
    "-c", 'chatgpt_base_url="https://chatgpt.com/backend-api/"',
    "-c", 'web_search="disabled"',
    "-c", "project_doc_max_bytes=0",
    "-c", 'approval_policy="never"',
    "--disable", "shell_tool",
    "--disable", "unified_exec",
    "--disable", "multi_agent",
    "--disable", "apps",
    "--disable", "plugins",
    "--disable", "view_image",
    "--disable", "code_mode",
    "--disable", "hooks",
    "--disable", "memories",
]


def client_path(provider: str) -> str | None:
    home = Path.home()
    names = ("grok", "grok-build") if provider == "grok" else (provider,)
    dirs = (
        home / ".local/bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"),
        home / "bin", home / ".antigravity/bin", home / ".grok/bin", home / ".npm-global/bin",
    )
    for name in names:
        for directory in dirs:
            if found := shutil.which(str(directory / name)):
                return found
        for root in (Path("/Applications"), home / "Applications"):
            for relative in APP_BINARIES.get(provider, ()):
                if found := shutil.which(str(root / relative)):
                    return found
        if found := shutil.which(name):
            return os.path.abspath(found)
    return None


def client_env(binary: str | None = None) -> dict[str, str]:
    """Allow the client to use its own account store without inheriting API or gateway overrides."""
    allowed = ("HOME", "USER", "LOGNAME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "LC_CTYPE",
               "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "TERM")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    paths = [str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    if binary:
        paths.insert(0, str(Path(binary).parent))
    env["PATH"] = os.pathsep.join(paths)
    env["NO_COLOR"] = "1"
    return env


def local_auth(provider: str, binary: str) -> str:
    args = {"claude": ["auth", "status"], "codex": ["login", "status"]}.get(provider)
    if args is None:
        return "unverified"
    try:
        with tempfile.TemporaryDirectory(prefix="bop_auth_") as cwd:
            result = subprocess.run([binary, *args], cwd=cwd, env=client_env(binary), stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=8, check=False)
        if provider == "claude":
            auth = json.loads(result.stdout)
            if auth.get("loggedIn") is False:
                return "signed_out"
            if result.returncode == 0 and auth.get("loggedIn") is True:
                return "account" if auth.get("authMethod") == "claude.ai" else "other"
        else:
            output = (result.stdout + result.stderr).lower()
            if result.returncode == 0 and "logged in using chatgpt" in output:
                return "account"
            if result.returncode == 0 and "logged in using" in output:
                return "other"
            if "not logged in" in output:
                return "signed_out"
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
        pass
    return "unverified"


def launch_login(provider: str, binary: str) -> None:
    """Give the official interactive client its own terminal. It owns the entire login flow."""
    if sys.platform != "darwin":
        raise RuntimeError("Open the official client to sign in. In-app Terminal sign-in requires macOS.")
    folder = Path(tempfile.mkdtemp(prefix="bop_subscription_"))
    script = folder / "Sign in.command"
    env_args = [f"{key}={value}" for key, value in client_env(binary).items()]
    script.write_text(
        "#!/bin/sh\n" + f"cd {shlex.quote(str(folder))} || exit 1\n"
        + shlex.join(["/usr/bin/env", "-i", *env_args, binary, *LOGIN_ARGS[provider]]) + "\n"
        + 'result=$?\ncd /\n' + shlex.join(["/bin/rm", "-f", str(script)]) + "\n"
        + shlex.join(["/bin/rmdir", str(folder)]) + '\nexit "$result"\n',
        encoding="utf-8",
    )
    script.chmod(0o700)
    try:
        subprocess.run(["/usr/bin/open", "-a", "Terminal", str(script)], timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        shutil.rmtree(folder)
        raise RuntimeError("Terminal could not open the official sign-in flow.") from None


def _mac_managed_settings() -> bool:
    if sys.platform != "darwin":
        return False
    import ctypes

    library = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    library.CFStringCreateWithCString.argtypes = (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32)
    library.CFStringCreateWithCString.restype = ctypes.c_void_p
    library.CFPreferencesAppValueIsForced.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    library.CFPreferencesAppValueIsForced.restype = ctypes.c_ubyte
    library.CFRelease.argtypes = (ctypes.c_void_p,)
    names = (b"com.openai.codex", b"config_toml_base64", b"requirements_toml_base64")
    strings = [library.CFStringCreateWithCString(None, name, 0x08000100) for name in names]
    try:
        return not all(strings) or any(library.CFPreferencesAppValueIsForced(key, strings[0]) for key in strings[1:])
    finally:
        for value in filter(None, strings):
            library.CFRelease(value)


def codex_auth_config() -> list[str]:
    """Read only the official client's settings. Credential files and keychain entries stay with Codex."""
    home = Path.home()
    system = Path("/etc/codex")
    settings = home / ".codex"
    if _mac_managed_settings() or any((base / filename).exists() for base in (system, settings)
                                     for filename in ("managed_config.toml", "requirements.toml")) or (system / "config.toml").exists():
        raise ValueError("Managed Codex configuration requires an eligibility review.")
    if any(os.path.lexists(settings / filename) for filename in ("AGENTS.md", "AGENTS.override.md")):
        raise ValueError("Codex has global agent instructions which this integration cannot exclude from requests.")
    config = settings / "config.toml"
    try:
        with config.open("rb") as file:
            data = tomllib.load(file)
    except FileNotFoundError:
        data = {}
    # app-server reads the user configuration. Exec skips it and receives only the store selector.
    # Reject custom providers/endpoints and policy/auth settings that could make the two differ.
    if any(key in data for key in ("model_providers", "openai_base_url", "chatgpt_base_url", "forced_login_method",
                                    "forced_chatgpt_workspace_id", "cloud_provider", "profile", "profiles", "include"))             or data.get("model_provider", "openai") != "openai":
        raise ValueError("Custom Codex backend or sign-in settings make subscription billing ambiguous.")
    store = data.get("cli_auth_credentials_store", "file")
    if store not in ("file", "keyring", "auto"):
        raise ValueError("The Codex credential store cannot be verified.")
    return ["-c", f'cli_auth_credentials_store="{store}"']


def codex_account(binary: str, auth_config: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read the public app-server account and quota protocol. This sends no model prompt."""
    with tempfile.TemporaryDirectory(prefix="bop_codex_status_") as cwd:
        with subprocess.Popen([binary, "app-server", *CODEX_OPTIONS, *auth_config], cwd=cwd, env=client_env(binary),
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(proc.stdout, selectors.EVENT_READ)
                    deadline = time.monotonic() + 12
                    buffer = b""

                    def request(method: str, params: dict[str, Any], request_id: int | None = None) -> dict[str, Any]:
                        nonlocal buffer
                        msg = {"method": method, "params": params}
                        if request_id is not None:
                            msg["id"] = request_id
                        proc.stdin.write(json.dumps(msg).encode() + b"\n")
                        proc.stdin.flush()
                        while request_id is not None:
                            while b"\n" in buffer:
                                line, buffer = buffer.split(b"\n", 1)
                                event = json.loads(line)
                                if event.get("id") == request_id:
                                    return event.get("result") or {}
                            if not selector.select(max(0, deadline - time.monotonic())):
                                raise TimeoutError
                            chunk = os.read(proc.stdout.fileno(), 65536)
                            if not chunk or len(buffer) + len(chunk) > 1024 * 1024:
                                raise ValueError("Invalid Codex response")
                            buffer += chunk
                        return {}

                    request("initialize", {"clientInfo": {"name": "bang_on_pro", "version": "1.6.0"}}, 1)
                    request("initialized", {})
                    doc = request("account/read", {"refreshToken": False}, 2)
                    account = doc.get("account") or {}
                    if account.get("type") != "chatgpt":
                        return account, {}
                    return account, request("account/rateLimits/read", {}, 3)
            finally:
                proc.kill()


def subscription_access(provider: str, binary: str) -> dict[str, Any]:
    """Require affirmative account and billing evidence before handing a prompt to any client."""
    if provider in BLOCKED:
        return {"ready": False, "message": BLOCKED[provider]}
    try:
        auth_config = codex_auth_config()
        account, limits = codex_account(binary, auth_config)
        tier = account.get("planType")
        if account.get("type") != "chatgpt":
            return {"ready": False, "message": "Codex could not verify a ChatGPT account session."}
        if tier not in PERSONAL_CODEX_PLANS:
            return {"ready": False, "message": "This ChatGPT plan could not be verified for included Codex usage."}
        primary = limits.get("rateLimits") or {}
        snapshots = [primary, *((limits.get("rateLimitsByLimitId") or {}).values())]
        credits = primary.get("credits") or {}
        if (not zero_credits(credits)
                or any(bucket.get("credits") is not None and not zero_credits(bucket["credits"]) for bucket in snapshots)):
            return {"ready": False, "message": "Codex did not confirm zero additional usage credits."}
        windows = [bucket.get(key) or {} for bucket in snapshots for key in ("primary", "secondary")]
        reset_times = [w["resetsAt"] for w in windows if type(w.get("resetsAt")) is int and w["resetsAt"] > time.time()]
        limited = [w["resetsAt"] for w in windows if (w.get("usedPercent") or 0) >= 100 and w.get("resetsAt") in reset_times]
        if limits.get("ordinaryUsageAllowed") is not True or any(bucket.get("rateLimitReachedType") for bucket in snapshots):
            return {"ready": False, "message": "Codex has not confirmed included usage is available.", "retry_at": max(limited, default=None)}
        return {"ready": True, "message": f"ChatGPT {tier.title()} included usage verified",
                "reset_at": min(reset_times, default=None), "auth_config": auth_config}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"ready": False, "message": "Codex account, configuration or billing status could not be verified."}


def zero_credits(credits: dict[str, Any]) -> bool:
    return credits.get("hasCredits") is False and credits.get("unlimited") is False and credits.get("balance") in ("0", "0.0", "0.00")
