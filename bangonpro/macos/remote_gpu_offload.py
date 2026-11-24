"""Consent-gated remote GPU offload protocol for optional cloud/LAN acceleration.

Bang On Pro is strictly local-first. This module enforces:
1. Explicit opt-in user consent token & non-expired consent record before ANY
   payload preparation or remote request.
2. Strict HTTPS/TLS transport verification (non-TLS remote endpoints are rejected).
3. Media-hash manifests and PII/path metadata scrubbing — raw source footage is
   NEVER uploaded unless explicitly opted in separately.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import hashlib
import json
import ssl
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class RemoteOffloadError(Exception):
    """Base error for remote GPU offload policy and transport failures."""


class ConsentRequiredError(RemoteOffloadError):
    """Raised when remote offload is attempted without explicit valid user consent."""


class ConsentExpiredError(RemoteOffloadError):
    """Raised when an otherwise valid user consent record has expired."""


class InsecureTransportError(RemoteOffloadError):
    """Raised when a remote endpoint does not use HTTPS with TLS verification."""


class RawMediaUploadDeniedError(RemoteOffloadError):
    """Raised when raw footage upload is requested without explicit raw-upload opt-in."""


_ALLOWED_TECHNICAL_META_KEYS = frozenset(
    {
        "width",
        "height",
        "fps",
        "duration_sec",
        "codec_name",
        "pixel_format",
        "sample_rate",
        "channels",
        "bit_rate_bps",
        "has_audio",
    }
)


@dataclass(frozen=True)
class RemoteGpuEndpointConfig:
    """Configuration for a remote GPU worker endpoint (HTTPS/TLS mandatory)."""

    endpoint_url: str
    timeout_sec: float = 30.0
    verify_tls: bool = True
    ca_bundle_path: str | None = None

    def validate(self) -> None:
        parsed = urlparse(self.endpoint_url)
        if parsed.scheme.lower() != "https":
            raise InsecureTransportError(
                f"Remote GPU offload requires HTTPS/TLS; rejected endpoint scheme '{parsed.scheme or 'none'}' "
                f"for URL '{self.endpoint_url}'."
            )
        if not parsed.hostname:
            raise InsecureTransportError(f"Remote GPU offload endpoint missing hostname: '{self.endpoint_url}'")
        if parsed.username or parsed.password:
            raise InsecureTransportError(
                "Embedded credentials in remote GPU endpoint URL are forbidden; use headers/consent tokens instead."
            )
        if not self.verify_tls:
            raise InsecureTransportError(
                "Disabling TLS certificate verification (verify_tls=False) is forbidden for remote GPU offload."
            )

    @property
    def hostname(self) -> str:
        return (urlparse(self.endpoint_url).hostname or "").lower()


@dataclass(frozen=True)
class RemoteOffloadConsent:
    """Explicit user opt-in consent record gating any remote GPU offload."""

    user_consented: bool = False
    consent_token: str = ""
    allow_raw_media_upload: bool = False
    allow_proxy_upload: bool = False
    allow_audio_features_only: bool = True
    granted_at_utc: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    expires_at_utc: str | None = None
    approved_endpoint_host: str | None = None


@dataclass(frozen=True)
class OffloadJobRequest:
    """Structured remote GPU job request."""

    job_id: str
    task_type: str
    media_paths: list[Path]
    technical_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    include_proxy_media: bool = False
    include_raw_media: bool = False
    extra_params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OffloadJobResponse:
    """Structured response from a remote GPU worker."""

    job_id: str
    accepted: bool
    status_code: int
    payload_mode: str
    worker_response: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def scrub_metadata_dict(raw_metadata: dict[str, Any]) -> dict[str, Any]:
    """Allow-list technical stream attributes and strip all paths, usernames, GPS, and PII."""
    scrubbed: dict[str, Any] = {}
    for key, val in raw_metadata.items():
        if key in _ALLOWED_TECHNICAL_META_KEYS and isinstance(val, (int, float, bool, str)):
            scrubbed[key] = val
    return scrubbed


def build_scrubbed_manifest(
    media_paths: Iterable[str | Path],
    technical_metadata: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build a privacy-preserving media-hash manifest without leaking local filesystem paths."""
    meta_lookup = technical_metadata or {}
    manifest: list[dict[str, Any]] = []

    for idx, item in enumerate(media_paths, start=1):
        path = Path(item)
        if not path.is_file():
            raise FileNotFoundError(f"Cannot hash missing media file: {path}")

        file_hash = _sha256_file(path)
        size_bytes = path.stat().st_size
        raw_meta = meta_lookup.get(str(item)) or meta_lookup.get(str(path)) or {}

        manifest.append(
            {
                "asset_id": f"asset_{idx:03d}",
                "sha256": file_hash,
                "size_bytes": size_bytes,
                "extension": path.suffix.lower().lstrip("."),
                "technical_metadata": scrub_metadata_dict(raw_meta),
            }
        )

    return manifest


TransportFn = Callable[[str, bytes, dict[str, str], float, ssl.SSLContext], tuple[int, bytes]]


def _default_https_transport(
    url: str,
    body: bytes,
    headers: dict[str, str],
    timeout_sec: float,
    ssl_context: ssl.SSLContext,
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_sec, context=ssl_context) as resp:
        return int(resp.status), resp.read()


class RemoteGpuOffloadClient:
    """Strict consent-gated, TLS-enforced client for optional remote GPU workers."""

    PROTOCOL_VERSION = "1.0"

    def __init__(
        self,
        endpoint: RemoteGpuEndpointConfig,
        *,
        transport: TransportFn | None = None,
    ) -> None:
        endpoint.validate()
        self.endpoint = endpoint
        self._transport: TransportFn = transport if transport is not None else _default_https_transport

    def validate_request(
        self,
        request: OffloadJobRequest,
        consent: RemoteOffloadConsent | None,
        *,
        now_utc: datetime | None = None,
    ) -> str:
        """Enforce transport, consent, expiration, host-pinning, and raw-media upload gates.

        Returns the validated ``payload_mode`` ("features_only", "proxy_media", or "raw_media").
        """
        self.endpoint.validate()

        if consent is None or not consent.user_consented:
            raise ConsentRequiredError(
                "Remote GPU offload is blocked: explicit user opt-in consent (user_consented=True) is required."
            )

        if not consent.consent_token or len(consent.consent_token.strip()) < 8:
            raise ConsentRequiredError(
                "Remote GPU offload is blocked: a non-empty user consent token (>= 8 chars) is required."
            )

        if consent.expires_at_utc:
            current_time = now_utc if now_utc is not None else datetime.now(UTC)
            expires = datetime.fromisoformat(consent.expires_at_utc)
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if current_time >= expires:
                raise ConsentExpiredError(
                    f"Remote GPU offload consent expired at {consent.expires_at_utc}."
                )

        if consent.approved_endpoint_host and consent.approved_endpoint_host.lower() != self.endpoint.hostname:
            raise ConsentRequiredError(
                f"Consent is pinned to host '{consent.approved_endpoint_host}', "
                f"cannot submit to '{self.endpoint.hostname}'."
            )

        if request.include_raw_media:
            if not consent.allow_raw_media_upload:
                raise RawMediaUploadDeniedError(
                    "Raw source footage upload is denied unless allow_raw_media_upload=True is explicitly granted."
                )
            return "raw_media"

        if request.include_proxy_media:
            if not consent.allow_proxy_upload:
                raise ConsentRequiredError(
                    "Proxy media upload requires allow_proxy_upload=True in user consent."
                )
            return "proxy_media"

        if not consent.allow_audio_features_only:
            raise ConsentRequiredError(
                "Consent does not permit feature-manifest offload."
            )
        return "features_only"

    def prepare_job_payload(
        self,
        request: OffloadJobRequest,
        consent: RemoteOffloadConsent | None,
        *,
        now_utc: datetime | None = None,
    ) -> dict[str, Any]:
        """Validate all policy gates and build the scrubbed JSON request envelope."""
        payload_mode = self.validate_request(request, consent, now_utc=now_utc)
        assert consent is not None

        manifest = build_scrubbed_manifest(request.media_paths, request.technical_metadata)
        token_digest = hashlib.sha256(consent.consent_token.encode("utf-8")).hexdigest()

        return {
            "protocol_version": self.PROTOCOL_VERSION,
            "job_id": request.job_id,
            "task_type": request.task_type,
            "payload_mode": payload_mode,
            "consent_token_sha256": token_digest,
            "consent_granted_at_utc": consent.granted_at_utc,
            "media_hash_manifest": manifest,
            "raw_media_included": payload_mode == "raw_media",
            "proxy_media_included": payload_mode == "proxy_media",
            "params": dict(request.extra_params),
        }

    def submit_job(
        self,
        request: OffloadJobRequest,
        consent: RemoteOffloadConsent | None,
        *,
        now_utc: datetime | None = None,
    ) -> OffloadJobResponse:
        """Submit a validated, scrubbed job over HTTPS/TLS to the configured remote GPU worker."""
        envelope = self.prepare_job_payload(request, consent, now_utc=now_utc)
        body = json.dumps(envelope, sort_keys=True).encode("utf-8")

        ssl_ctx = ssl.create_default_context(cafile=self.endpoint.ca_bundle_path)
        ssl_ctx.check_hostname = True
        ssl_ctx.verify_mode = ssl.CERT_REQUIRED

        headers = {
            "Content-Type": "application/json",
            "X-BangOnPro-Protocol": self.PROTOCOL_VERSION,
            "X-BangOnPro-Consent-Hash": envelope["consent_token_sha256"],
        }

        status_code, resp_bytes = self._transport(
            self.endpoint.endpoint_url,
            body,
            headers,
            self.endpoint.timeout_sec,
            ssl_ctx,
        )
        try:
            worker_data = json.loads(resp_bytes.decode("utf-8")) if resp_bytes else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            worker_data = {"raw": resp_bytes.decode("utf-8", errors="replace")}

        return OffloadJobResponse(
            job_id=request.job_id,
            accepted=200 <= status_code < 300,
            status_code=status_code,
            payload_mode=envelope["payload_mode"],
            worker_response=worker_data,
        )

