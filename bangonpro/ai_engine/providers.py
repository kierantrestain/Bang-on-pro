"""Configurable AI Provider Registry with Local-First Defaults & External Consent Gate.

Bang On Pro runs 100% locally by default with zero paid API keys and zero
network transmission. Optional external providers (OpenAI-compatible, Anthropic,
Deepgram) may be configured via user JSON configuration or environment variables
(never hardcoded keys) and MUST pass ``require_external_consent`` before any
media, audio, or transcript payload leaves the machine.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import copy
import os
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

DEFAULT_LOCAL_PROVIDER_ID = "local_deterministic"


class ExternalConsentError(PermissionError):
    """Raised when an external AI provider is requested without explicit user consent."""

    def __init__(
        self,
        provider_id: str,
        payload_summary: str = "",
        message: str | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.payload_summary = payload_summary
        detail = message or (
            f"Explicit user consent (consent_granted=True) is required before transmitting "
            f"data to external AI provider '{provider_id}'"
            + (f" (payload: {payload_summary})" if payload_summary else "")
            + ". Switch to a built-in local provider (e.g. 'local_deterministic') "
            "or explicitly grant external transmission consent."
        )
        super().__init__(detail)


@dataclass(frozen=True)
class ConsentRecord:
    """Immutable audit record capturing a provider transmission consent decision."""

    consent_id: str
    timestamp_utc: str
    provider_id: str
    provider_name: str
    is_external: bool
    consent_granted: bool
    payload_summary: str
    data_categories: tuple[str, ...] = ()
    actor: str = "user"

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["data_categories"] = list(self.data_categories)
        return raw


@dataclass
class AiProviderConfig:
    """Configuration for a local or configurable external AI provider."""

    provider_id: str
    display_name: str
    kind: str = "local"  # "local" | "external"
    capabilities: tuple[str, ...] = ("transcription",)
    requires_network: bool = False
    requires_api_key: bool = False
    endpoint_url: str | None = None
    model: str | None = None
    api_key_env: str | None = None
    consent_granted: bool = False
    enabled: bool = True
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.kind = str(self.kind).strip().lower()
        if self.kind not in ("local", "external"):
            raise ValueError(
                f"Unsupported provider kind '{self.kind}'; expected 'local' or 'external'"
            )
        if isinstance(self.capabilities, list):
            self.capabilities = tuple(str(c) for c in self.capabilities)
        if self.kind == "external":
            self.requires_network = True

    @property
    def is_external(self) -> bool:
        """Return True if this provider transmits data outside the local machine."""
        return self.kind == "external" or self.requires_network

    @property
    def is_local(self) -> bool:
        """Return True if this provider executes strictly locally without external transmission."""
        return not self.is_external

    @property
    def requires_consent(self) -> bool:
        """Return True if explicit user consent is required before invoking this provider."""
        return self.is_external

    def resolve_api_key(self) -> str | None:
        """Resolve an API key from environment variable or private runtime extra config."""
        if self.api_key_env:
            env_val = os.environ.get(self.api_key_env, "").strip()
            if env_val:
                return env_val
        raw = self.extra.get("api_key")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return None

    def has_api_key(self) -> bool:
        return self.resolve_api_key() is not None

    def to_dict(self, *, redact_secrets: bool = True) -> dict[str, Any]:
        safe_extra = copy.deepcopy(self.extra)
        if redact_secrets and "api_key" in safe_extra:
            safe_extra["api_key"] = "***REDACTED***"
        return {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "kind": self.kind,
            "is_local": self.is_local,
            "is_external": self.is_external,
            "requires_consent": self.requires_consent,
            "capabilities": list(self.capabilities),
            "requires_network": self.requires_network,
            "requires_api_key": self.requires_api_key,
            "api_key_configured": self.has_api_key() if self.requires_api_key else False,
            "api_key_env": self.api_key_env,
            "endpoint_url": self.endpoint_url,
            "model": self.model,
            "consent_granted": bool(self.consent_granted),
            "enabled": bool(self.enabled),
            "description": self.description,
            "extra": safe_extra,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AiProviderConfig:
        if not isinstance(data, dict):
            raise ValueError("AiProviderConfig payload must be a JSON object")  # noqa: TRY004
        pid = str(data.get("provider_id", "")).strip()
        if not pid:
            raise ValueError("AiProviderConfig requires a non-empty 'provider_id'")
        kind = str(
            data.get(
                "kind",
                "external" if pid in ("openai_compatible", "anthropic", "deepgram") else "local",
            )
        )
        caps_raw = data.get("capabilities", ("transcription",))
        caps = (
            tuple(str(c) for c in caps_raw)
            if isinstance(caps_raw, (list, tuple))
            else (str(caps_raw),)
        )
        return cls(
            provider_id=pid,
            display_name=str(data.get("display_name") or pid),
            kind=kind,
            capabilities=caps,
            requires_network=bool(data.get("requires_network", kind == "external")),
            requires_api_key=bool(data.get("requires_api_key", kind == "external")),
            endpoint_url=data.get("endpoint_url"),
            model=data.get("model"),
            api_key_env=data.get("api_key_env"),
            consent_granted=bool(data.get("consent_granted", False)),
            enabled=bool(data.get("enabled", True)),
            description=str(data.get("description", "")),
            extra=dict(data.get("extra") or {}),
        )


def _builtin_provider_configs() -> list[AiProviderConfig]:
    """Return built-in local-first and optional external provider definitions (zero hardcoded keys)."""
    configs = [
        AiProviderConfig(
            provider_id="local_deterministic",
            display_name="Local Deterministic Engine (GCC-PHAT + VAD + FFmpeg)",
            kind="local",
            capabilities=("sync", "vad", "diarisation", "audio_qc"),
            requires_network=False,
            requires_api_key=False,
            consent_granted=False,
            enabled=True,
            description=(
                "Default zero-cost, 100% offline deterministic studio engine "
                "(DSP spectral VAD speech segmentation; install Whisper ASR for spoken text)."
            ),
        ),
        AiProviderConfig(
            provider_id="local_faster_whisper",
            display_name="Local Faster-Whisper ASR",
            kind="local",
            capabilities=("transcription", "word_timestamps"),
            requires_network=False,
            requires_api_key=False,
            model="faster-whisper-tiny",
            consent_granted=False,
            enabled=True,
            description="Offline CTranslate2 Whisper ASR using locally cached weights.",
        ),
        AiProviderConfig(
            provider_id="local_openai_whisper",
            display_name="Local OpenAI Whisper ASR (PyTorch)",
            kind="local",
            capabilities=("transcription", "word_timestamps"),
            requires_network=False,
            requires_api_key=False,
            model="openai-whisper-tiny",
            consent_granted=False,
            enabled=True,
            description="Offline PyTorch OpenAI Whisper ASR using locally verified checkpoints.",
        ),
    ]
    # Reasoning providers are the editor's own signed-in CLIs, routed by the department's shared
    # router (``mcp/reasoning.py``: priority order, failover, cooldowns, one budget). They are the only
    # external providers Bang On Pro calls; authorising one is the editor's consent.
    from bangonpro.ai_engine.mcp.reasoning import PROVIDER_INFO, PROVIDERS, load_config

    authorised = {k for k, v in (load_config().get("providers") or {}).items() if v.get("authorised")}
    for pid in PROVIDERS:
        info = PROVIDER_INFO[pid]
        configs.append(
            AiProviderConfig(
                provider_id=f"reasoning_{pid}",
                display_name=f"{info['title']} (shared reasoning router)",
                kind="external",
                capabilities=("editorial_llm", "instruction_interpretation", "planning"),
                requires_network=True,
                requires_api_key=False,
                consent_granted=pid in authorised,
                enabled=pid in authorised,
                description=f"Signed-in {info['binary']} CLI. Authorise with: python -m bangonpro.ai_engine.mcp authorise --client {pid}",
                extra={"router_provider": pid},
            )
        )
    return configs


def _append_consent_audit(
    audit_log: Any,
    *,
    event: str,
    record: ConsentRecord,
    state: Any = None,
) -> None:
    """Append a consent audit entry to an ImmutableActivityLog or Python list."""
    if audit_log is None:
        return
    if hasattr(audit_log, "append") and state is not None:
        # Duck-typed support for orchestrator.ImmutableActivityLog
        try:
            audit_log.append(
                stage="ai_provider_consent",
                event=event,
                state=state,
                details=record.to_dict(),
            )
            return
        except TypeError:
            pass
    if isinstance(audit_log, list):
        entry = record.to_dict()
        entry["event"] = event
        audit_log.append(entry)


class AiProviderRegistry:
    """Registry managing local-first built-in and configurable external AI providers."""

    def __init__(self, *, include_builtins: bool = True) -> None:
        self._providers: dict[str, AiProviderConfig] = {}
        self.consent_records: list[ConsentRecord] = []
        self.active_provider_id: str = DEFAULT_LOCAL_PROVIDER_ID
        if include_builtins:
            for cfg in _builtin_provider_configs():
                self._providers[cfg.provider_id] = cfg

    def get(self, provider_id: str) -> AiProviderConfig:
        if provider_id not in self._providers:
            available = ", ".join(sorted(self._providers))
            raise KeyError(f"Unknown AI provider '{provider_id}'. Available: {available}")
        return self._providers[provider_id]

    def list_providers(self, *, include_external: bool = True) -> list[AiProviderConfig]:
        providers = list(self._providers.values())
        if not include_external:
            providers = [p for p in providers if not p.is_external]
        return sorted(providers, key=lambda p: (p.is_external, p.provider_id))

    def require_external_consent(
        self,
        provider_id: str,
        consent_granted: bool,
        payload_summary: str,
        audit_log: Any = None,
        *,
        state: Any = None,
        data_categories: Sequence[str] = (),
        actor: str = "user",
    ) -> ConsentRecord:
        """Enforce explicit user consent before any external provider data transmission.

        Local providers pass automatically with zero network transmission.
        External providers MUST have ``consent_granted=True``; otherwise
        ``ExternalConsentError`` is raised immediately and logged to ``audit_log``.
        """
        cfg = self.get(provider_id)
        now_utc = datetime.now(UTC).isoformat()
        categories = tuple(str(c) for c in data_categories)

        if not cfg.is_external:
            record = ConsentRecord(
                consent_id=f"cns_{uuid.uuid4().hex[:12]}",
                timestamp_utc=now_utc,
                provider_id=cfg.provider_id,
                provider_name=cfg.display_name,
                is_external=False,
                consent_granted=True,
                payload_summary=payload_summary,
                data_categories=categories,
                actor=actor,
            )
            self.consent_records.append(record)
            _append_consent_audit(
                audit_log,
                event="local_provider_selected",
                record=record,
                state=state,
            )
            return record

        if consent_granted is not True:
            blocked = ConsentRecord(
                consent_id=f"cns_{uuid.uuid4().hex[:12]}",
                timestamp_utc=now_utc,
                provider_id=cfg.provider_id,
                provider_name=cfg.display_name,
                is_external=True,
                consent_granted=False,
                payload_summary=payload_summary,
                data_categories=categories,
                actor=actor,
            )
            self.consent_records.append(blocked)
            _append_consent_audit(
                audit_log,
                event="external_consent_blocked",
                record=blocked,
                state=state,
            )
            raise ExternalConsentError(cfg.provider_id, payload_summary)

        approved = ConsentRecord(
            consent_id=f"cns_{uuid.uuid4().hex[:12]}",
            timestamp_utc=now_utc,
            provider_id=cfg.provider_id,
            provider_name=cfg.display_name,
            is_external=True,
            consent_granted=True,
            payload_summary=payload_summary,
            data_categories=categories,
            actor=actor,
        )
        self.consent_records.append(approved)
        _append_consent_audit(
            audit_log,
            event="external_consent_granted",
            record=approved,
            state=state,
        )
        return approved


def require_external_consent(
    provider_id: str,
    consent_granted: bool,
    payload_summary: str,
    audit_log: Any = None,
    *,
    registry: AiProviderRegistry | None = None,
    state: Any = None,
    data_categories: Sequence[str] = (),
    actor: str = "user",
) -> ConsentRecord:
    """Module-level consent gate delegating to ``AiProviderRegistry.require_external_consent``."""
    reg = registry or AiProviderRegistry()
    return reg.require_external_consent(
        provider_id=provider_id,
        consent_granted=consent_granted,
        payload_summary=payload_summary,
        audit_log=audit_log,
        state=state,
        data_categories=data_categories,
        actor=actor,
    )
