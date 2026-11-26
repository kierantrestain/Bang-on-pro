# SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
# SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""Offline regression coverage. Provider replies and account metadata are simulated."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bangonpro.ai_engine.department.revisions import interpret_revision
from bangonpro.ai_engine.intent import parse_production_instruction
from bangonpro.ai_engine.mcp import __main__ as cli
from bangonpro.ai_engine.mcp import reasoning, subscriptions

CONNECTED = {"mode": "connected_subscriptions", "consent": True, "metadata": False, "transcript": False}


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BANGONPRO_CONFIG_DIR", str(tmp_path / "router"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")


def test_finder_path_and_clean_client_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / ".local/bin/claude"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    assert subscriptions.client_path("claude") == str(binary)
    for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_ACCESS_TOKEN",
                "OPENAI_BASE_URL", "XAI_API_KEY", "AGY_ADC_AUTH", "GOOGLE_APPLICATION_CREDENTIALS", "HTTP_PROXY"):
        monkeypatch.setenv(key, "synthetic-secret")
    assert "synthetic-secret" not in subscriptions.client_env(str(binary)).values()


def test_connect_needs_supported_sign_in(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(subscriptions, "client_path", lambda provider: "/usr/bin/true" if provider == "codex" else None)
    auth = ["signed_out"]
    launched = []
    monkeypatch.setattr(subscriptions, "local_auth", lambda *_: auth[0])
    monkeypatch.setattr(subscriptions, "launch_login", lambda provider, path: launched.append((provider, path)))
    monkeypatch.setattr(subscriptions, "subscription_access", lambda *_: {"ready": True, "message": "Mock included usage"})

    assert cli.main(["providers", "--client", "codex", "--connect", "--check-auth"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert launched == [("codex", "/usr/bin/true")]
    assert status["providers"]["codex"]["auth"] == "signed_out"
    assert not status["providers"]["codex"]["authorised"]
    assert not status["providers"]["agy"]["installed"]

    auth[0] = "account"
    assert cli.main(["providers", "--client", "codex", "--check-sign-in", "--check-auth"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["providers"]["codex"]["authorised"]
    assert not status["providers"]["codex"]["available"]
    assert status["providers"]["codex"]["last_test"] is None
    assert status["budget"]["used_today"] == 0

    assert cli.main(["providers", "--client", "codex", "--disconnect", "--check-auth"]) == 0
    ready = json.loads(capsys.readouterr().out)["providers"]["codex"]
    assert ready["auth"] == "account" and not ready["authorised"]


@pytest.mark.parametrize("ordinary,credit,ready", [
    (True, {"hasCredits": False, "unlimited": False, "balance": "0"}, True),
    (None, {"hasCredits": False, "unlimited": False, "balance": "0"}, False),
    (False, {"hasCredits": False, "unlimited": False, "balance": "0"}, False),
    (True, {"hasCredits": True, "unlimited": False, "balance": "5"}, False),
    (True, {"hasCredits": False, "unlimited": False}, False),
    (True, None, False),
])
def test_codex_requires_affirmative_included_usage(monkeypatch: pytest.MonkeyPatch, ordinary, credit, ready) -> None:
    monkeypatch.setattr(subscriptions, "codex_auth_config", lambda: [])
    monkeypatch.setattr(subscriptions, "codex_account", lambda *_: (
        {"type": "chatgpt", "planType": "plus"},
        {"ordinaryUsageAllowed": ordinary, "rateLimits": {"credits": credit}},
    ))
    assert subscriptions.subscription_access("codex", "/official/codex")["ready"] is ready


def test_codex_rejects_custom_backend_without_reading_credentials(tmp_path: Path) -> None:
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text('model_provider = "custom"\n')
    with pytest.raises(ValueError, match="ambiguous"):
        subscriptions.codex_auth_config()
    with pytest.raises(ValueError, match="included subscription"):
        reasoning.authorise("codex", allow_api_billing=True)


class FakeAdapter(reasoning.Adapter):
    def __init__(self, name: str, calls: list[str], *, fail: bool = False) -> None:
        self.name, self.calls, self.unavailable = name, calls, fail

    def call(self, prompt: str, **kwargs):
        assert kwargs["allow_api_billing"] is False
        self.calls.append(self.name + " " + prompt)
        if self.unavailable:
            raise reasoning.ProviderFailure("unavailable", "mock outage")
        return '{"ok": true}', {}


def test_policy_reload_failover_and_shared_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subscriptions, "BLOCKED", {})
    calls = []
    adapters = {"claude": FakeAdapter("claude", calls, fail=True), "codex": FakeAdapter("codex", calls)}
    make_router = lambda: reasoning.ProviderRouter(adapters=adapters, installed=lambda _: True)
    reasoning.authorise("claude")
    reasoning.authorise("codex")
    router = make_router()
    with pytest.raises(reasoning.AllProvidersUnavailable, match="consent"):
        router.plan("must stay local")
    assert calls == []

    reasoning.configure({"privacy": CONNECTED, "auto_failover": False, "max_calls_per_day": 2})
    with pytest.raises(reasoning.AllProvidersUnavailable):
        router.plan("synthetic")
    assert calls == ["claude synthetic"]
    reasoning.configure({"priority": ["codex", "claude"], "auto_failover": True})
    assert make_router().plan(lambda policy: "synthetic" if not policy["metadata"] else "private") == {"ok": True}
    status = reasoning.reasoning_status()
    assert status["priority"] == ["codex", "claude", "grok", "agy"]
    assert status["budget"]["used_today"] == 2
    with pytest.raises(reasoning.BudgetExhausted):
        make_router().plan("synthetic")
    reasoning.revoke()
    assert reasoning.reasoning_status()["priority"] == status["priority"]
    assert reasoning.reasoning_status()["budget"] == status["budget"]
    reasoning.configure({"privacy": {"mode": "local_only", "consent": False}})
    with pytest.raises(reasoning.AllProvidersUnavailable, match="consent"):
        router.plan("local again")
    assert calls == ["claude synthetic", "codex synthetic"]


def test_unknown_part_defers_the_entire_instruction() -> None:
    for instruction in ("Add captions and put a city montage over the guest", "Add captions with a slow fade"):
        assert parse_production_instruction(instruction).needs_clarification
        assert interpret_revision(instruction, {})["not_understood"]
    assert parse_production_instruction("Add captions").add_captions
    assert not interpret_revision("Tighten the opening but keep the natural conversation", {})["not_understood"]
