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
import importlib.util
import json
import os
import shutil
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
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
    return [
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
        AiProviderConfig(
            provider_id="local_whisper_cpp",
            display_name="Local whisper.cpp ASR",
            kind="local",
            capabilities=("transcription", "word_timestamps"),
            requires_network=False,
            requires_api_key=False,
            model="whisper-cpp-base-en",
            consent_granted=False,
            enabled=True,
            description="Offline native whisper.cpp CLI speech recognition.",
        ),
        AiProviderConfig(
            provider_id="local_ollama",
            display_name="Local Ollama LLM (127.0.0.1)",
            kind="local",
            capabilities=("editorial_llm", "transcript_summary", "chapter_titles"),
            requires_network=False,
            requires_api_key=False,
            endpoint_url="http://127.0.0.1:11434",
            model="llama3.2",
            consent_granted=False,
            enabled=True,
            description="Local loopback Ollama server for offline editorial assistance.",
        ),
        AiProviderConfig(
            provider_id="openai_compatible",
            display_name="OpenAI-Compatible External API",
            kind="external",
            capabilities=("transcription", "editorial_llm"),
            requires_network=True,
            requires_api_key=True,
            endpoint_url="https://api.openai.com/v1",
            model="whisper-1",
            api_key_env="OPENAI_API_KEY",
            consent_granted=False,
            enabled=False,
            description="Optional external OpenAI-compatible API. Requires explicit user consent before upload.",
        ),
        AiProviderConfig(
            provider_id="anthropic",
            display_name="Anthropic Claude External API",
            kind="external",
            capabilities=("editorial_llm", "transcript_summary"),
            requires_network=True,
            requires_api_key=True,
            endpoint_url="https://api.anthropic.com/v1",
            model="claude-3-5-sonnet-latest",
            api_key_env="ANTHROPIC_API_KEY",
            consent_granted=False,
            enabled=False,
            description="Optional external Anthropic editorial assistant. Requires explicit user consent before upload.",
        ),
        AiProviderConfig(
            provider_id="deepgram",
            display_name="Deepgram External Speech-to-Text",
            kind="external",
            capabilities=("transcription", "diarisation", "word_timestamps"),
            requires_network=True,
            requires_api_key=True,
            endpoint_url="https://api.deepgram.com/v1/listen",
            model="nova-2",
            api_key_env="DEEPGRAM_API_KEY",
            consent_granted=False,
            enabled=False,
            description="Optional external Deepgram STT/diarisation. Requires explicit user consent before upload.",
        ),
    ]


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

    def register(self, config: AiProviderConfig) -> AiProviderConfig:
        """Register or update an ``AiProviderConfig`` in the registry."""
        self._providers[config.provider_id] = copy.deepcopy(config)
        return self._providers[config.provider_id]

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

    def check_local_availability(
        self,
        provider_id: str,
        *,
        model_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        """Honest feature detection separating package installation from downloaded weights on disk."""
        from bangonpro.ai_engine.model_manager import ModelManager

        cfg = self.get(provider_id)
        mgr = ModelManager(model_dir=model_dir)
        backend_pkg = False
        weights_on_disk = False
        status = "not_installed"

        if cfg.provider_id == "local_deterministic":
            backend_pkg = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
            weights_on_disk = True
            available = backend_pkg
            status = "ready" if available else "backend_missing"
        elif cfg.provider_id == "local_faster_whisper":
            backend_pkg = importlib.util.find_spec("faster_whisper") is not None
            model_statuses = mgr.list_models(family="asr_whisper")
            fw_statuses = [m for m in model_statuses if m.runtime_backend == "faster_whisper"]
            weights_on_disk = any(m.model_available_on_disk for m in fw_statuses)
            available = backend_pkg and weights_on_disk
            if available:
                status = "ready"
            elif backend_pkg:
                status = "weights_missing"
            elif weights_on_disk:
                status = "backend_missing"
        elif cfg.provider_id == "local_openai_whisper":
            backend_pkg = (
                importlib.util.find_spec("whisper") is not None
                and importlib.util.find_spec("torch") is not None
            )
            ow_statuses = [
                m
                for m in mgr.list_models(family="asr_whisper")
                if m.runtime_backend == "openai_whisper"
            ]
            weights_on_disk = any(m.model_available_on_disk for m in ow_statuses)
            available = backend_pkg and weights_on_disk
            if available:
                status = "ready"
            elif backend_pkg:
                status = "weights_missing"
            elif weights_on_disk:
                status = "backend_missing"
        elif cfg.provider_id == "local_whisper_cpp":
            backend_pkg = bool(shutil.which("whisper-cli") or shutil.which("whisper-cpp"))
            wc_statuses = [
                m
                for m in mgr.list_models(family="asr_whisper")
                if m.runtime_backend == "whisper_cpp"
            ]
            weights_on_disk = any(m.model_available_on_disk for m in wc_statuses)
            available = backend_pkg and weights_on_disk
            if available:
                status = "ready"
            elif backend_pkg:
                status = "weights_missing"
            elif weights_on_disk:
                status = "backend_missing"
        elif cfg.provider_id == "local_ollama":
            backend_pkg = bool(shutil.which("ollama"))
            weights_on_disk = False
            available = backend_pkg
            status = "ready" if available else "backend_missing"
        else:
            backend_pkg = cfg.enabled
            weights_on_disk = False
            available = cfg.enabled and (not cfg.requires_api_key or cfg.has_api_key())
            status = "ready" if available else "disabled"

        return {
            "provider_id": cfg.provider_id,
            "is_local": cfg.is_local,
            "is_external": cfg.is_external,
            "requires_consent": cfg.requires_consent,
            "backend_package_installed": backend_pkg,
            "model_available_on_disk": weights_on_disk,
            "available": available,
            "status": status,
        }

    def configure_from_dict(self, payload: dict[str, Any]) -> AiProviderConfig:
        """Merge a user provider configuration dictionary into the registry and return active provider."""
        if not isinstance(payload, dict):
            raise ValueError("Provider config payload must be a JSON object")  # noqa: TRY004

        if "providers" in payload and isinstance(payload["providers"], list):
            for entry in payload["providers"]:
                cfg = self._merge_provider_entry(entry)
                self._providers[cfg.provider_id] = cfg
            active_id = str(
                payload.get("active_provider")
                or payload.get("provider_id")
                or self.active_provider_id
            )
            if active_id not in self._providers:
                raise KeyError(f"Configured active_provider '{active_id}' is not registered")
            if "consent_granted" in payload:
                self._providers[active_id].consent_granted = bool(payload["consent_granted"])
            self.active_provider_id = active_id
            return self._providers[active_id]

        # Single-provider config object
        cfg = self._merge_provider_entry(payload)
        self._providers[cfg.provider_id] = cfg
        self.active_provider_id = cfg.provider_id
        return cfg

    def _merge_provider_entry(self, entry: dict[str, Any]) -> AiProviderConfig:
        pid = str(entry.get("provider_id", "")).strip()
        if pid in self._providers:
            base = copy.deepcopy(self._providers[pid])
            if "display_name" in entry:
                base.display_name = str(entry["display_name"])
            if "kind" in entry:
                base.kind = str(entry["kind"]).strip().lower()
                if base.kind == "external":
                    base.requires_network = True
            if "capabilities" in entry:
                base.capabilities = tuple(str(c) for c in entry["capabilities"])
            if "requires_network" in entry:
                base.requires_network = bool(entry["requires_network"])
            if "requires_api_key" in entry:
                base.requires_api_key = bool(entry["requires_api_key"])
            if "endpoint_url" in entry:
                base.endpoint_url = entry["endpoint_url"]
            if "model" in entry:
                base.model = entry["model"]
            if "api_key_env" in entry:
                base.api_key_env = entry["api_key_env"]
            if "consent_granted" in entry:
                base.consent_granted = bool(entry["consent_granted"])
            if "enabled" in entry:
                base.enabled = bool(entry["enabled"])
            else:
                base.enabled = True
            if "description" in entry:
                base.description = str(entry["description"])
            if "extra" in entry and isinstance(entry["extra"], dict):
                base.extra.update(entry["extra"])
            return base
        return AiProviderConfig.from_dict(entry)

    def load_from_json_file(self, path: str | Path) -> AiProviderConfig:
        """Load provider settings from a user JSON file and return the active ``AiProviderConfig``."""
        cfg_path = Path(path).expanduser().resolve()
        if not cfg_path.is_file():
            raise FileNotFoundError(f"AI provider config file not found: {cfg_path}")
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        return self.configure_from_dict(raw)

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
