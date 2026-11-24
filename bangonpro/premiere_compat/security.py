"""Hardened ZIP archive and XML security controls for Premiere preset ingestion.

Enforces:
- Zip-slip path traversal rejection (absolute paths, '..', drive prefixes, NUL bytes)
- Symlink entry rejection
- Strict file count, per-file byte limit, total uncompressed byte limit, and compression ratio checks
- Streaming bounded reads so falsified ZIP headers cannot bypass size/bomb checks
- Defused XML parsing (XXE, external entities, and DTD expansion completely disabled)
- Bounded, sanitized XML snippet preservation without executing embedded content

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Union
from xml.etree.ElementTree import Element

import defusedxml.ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

# Strict resource limits required by Bang On Pro security policy
MAX_FILES: int = 256
MAX_SINGLE_FILE_BYTES: int = 32 * 1024 * 1024  # 32 MiB
MAX_TOTAL_UNCOMPRESSED_BYTES: int = 256 * 1024 * 1024  # 256 MiB
MAX_COMPRESSION_RATIO: float = 100.0
MAX_XML_SNIPPET_CHARS: int = 16_384

_CHUNK_SIZE: int = 65_536
_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class SecurityViolationError(ValueError):
    """Base exception raised when untrusted archive or XML input violates security policy."""


class ZipSlipError(SecurityViolationError):
    """Raised on path traversal or unsafe archive member paths."""


class SymlinkError(SecurityViolationError):
    """Raised when an archive contains a symbolic link entry."""


class ArchiveLimitError(SecurityViolationError):
    """Raised when file count or byte size limits are exceeded."""


class ZipBombError(SecurityViolationError):
    """Raised when compression ratio or actual decompressed size indicates a zip bomb."""


class XmlSecurityError(SecurityViolationError):
    """Raised when XML input triggers XXE, DTD, entity expansion, or parse errors."""


@dataclass(frozen=True)
class ArchiveSecurityLimits:
    """Configurable security limits with hardened defaults."""

    max_files: int = MAX_FILES
    max_single_file_bytes: int = MAX_SINGLE_FILE_BYTES
    max_total_uncompressed_bytes: int = MAX_TOTAL_UNCOMPRESSED_BYTES
    max_compression_ratio: float = MAX_COMPRESSION_RATIO


def compute_sha256_bytes(data: bytes) -> str:
    """Return lowercase hexadecimal SHA-256 digest for raw bytes."""
    return hashlib.sha256(data).hexdigest()


def compute_sha256_file(path: Union[str, Path]) -> str:
    """Compute SHA-256 of a file using bounded streaming reads."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def validate_member_path(member_name: str) -> str:
    """Validate and normalize a ZIP member path against zip-slip attacks.

    Rejects:
    - Empty names or NUL bytes
    - Absolute POSIX or Windows paths (`/etc/passwd`, `\\foo`, `C:\\bar`)
    - Parent traversal segments (`..`)
    """
    if not member_name or "\x00" in member_name:
        raise ZipSlipError(f"Invalid archive member path: {member_name!r}")

    normalized_slashes = member_name.replace("\\", "/")

    if normalized_slashes.startswith("/") or _DRIVE_LETTER_RE.match(normalized_slashes):
        raise ZipSlipError(f"Absolute path rejected in archive member: {member_name!r}")

    parts = [p for p in normalized_slashes.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ZipSlipError(f"Path traversal ('..') rejected in archive member: {member_name!r}")

    clean = posixpath.normpath("/".join(parts)) if parts else ""
    if clean in ("", ".", "..") or clean.startswith("../") or clean.startswith("/"):
        raise ZipSlipError(f"Unsafe normalized path rejected in archive member: {member_name!r}")

    return clean


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    """Detect Unix symbolic link entries via external_attr mode bits."""
    unix_mode = info.external_attr >> 16
    return stat.S_ISLNK(unix_mode)


def safe_read_zip_entries(
    zip_path: Union[str, Path],
    limits: ArchiveSecurityLimits = ArchiveSecurityLimits(),
) -> Dict[str, bytes]:
    """Safely inspect and read all regular files from a ZIP archive into memory.

    Never extracts directly onto the filesystem and strictly enforces:
    - No symlinks
    - No zip-slip traversal paths
    - File count <= max_files
    - Single file size <= max_single_file_bytes (both header and actual streamed bytes)
    - Total uncompressed size <= max_total_uncompressed_bytes (both header and actual)
    - Compression ratio <= max_compression_ratio (both header and actual)
    """
    path = Path(zip_path)
    if not path.is_file():
        raise FileNotFoundError(f"Archive not found: {path}")

    entries: Dict[str, bytes] = {}
    total_bytes = 0
    file_count = 0

    try:
        zf = zipfile.ZipFile(path, mode="r")
    except zipfile.BadZipFile as exc:
        raise SecurityViolationError(f"Corrupt or invalid ZIP archive: {path.name}") from exc

    with zf:
        infos: List[zipfile.ZipInfo] = zf.infolist()

        for info in infos:
            # Always check symlinks even if flagged as directory
            if _is_zip_symlink(info):
                raise SymlinkError(f"Symlink entry rejected in archive: {info.filename!r}")

            # Skip pure directory entries (still validate their path!)
            raw_name = info.filename
            if raw_name.endswith("/") or raw_name.endswith("\\"):
                trimmed = raw_name.rstrip("/\\")
                if trimmed:
                    validate_member_path(trimmed)
                continue

            clean_path = validate_member_path(raw_name)
            file_count += 1
            if file_count > limits.max_files:
                raise ArchiveLimitError(
                    f"Archive exceeds maximum file count ({file_count} > {limits.max_files})"
                )

            # Header pre-checks
            if info.file_size > limits.max_single_file_bytes:
                raise ArchiveLimitError(
                    f"Archive member {clean_path!r} exceeds single-file byte limit "
                    f"({info.file_size} > {limits.max_single_file_bytes})"
                )

            if info.file_size > 0:
                comp_size = max(info.compress_size, 1)
                header_ratio = info.file_size / comp_size
                if header_ratio > limits.max_compression_ratio:
                    raise ZipBombError(
                        f"Archive member {clean_path!r} exceeds max compression ratio "
                        f"({header_ratio:.1f}x > {limits.max_compression_ratio:.1f}x)"
                    )

            if total_bytes + info.file_size > limits.max_total_uncompressed_bytes:
                raise ArchiveLimitError(
                    f"Archive total uncompressed size exceeds limit "
                    f"({total_bytes + info.file_size} > {limits.max_total_uncompressed_bytes})"
                )

            # Streaming bounded read to defeat spoofed ZIP headers
            chunks: List[bytes] = []
            member_bytes = 0
            comp_denom = max(info.compress_size, 1)

            with zf.open(info, mode="r") as stream:
                while True:
                    chunk = stream.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    member_bytes += len(chunk)
                    total_bytes += len(chunk)

                    if member_bytes > limits.max_single_file_bytes:
                        raise ArchiveLimitError(
                            f"Actual size of {clean_path!r} exceeds single-file limit "
                            f"({member_bytes} > {limits.max_single_file_bytes})"
                        )
                    if total_bytes > limits.max_total_uncompressed_bytes:
                        raise ArchiveLimitError(
                            f"Actual total uncompressed size exceeds limit "
                            f"({total_bytes} > {limits.max_total_uncompressed_bytes})"
                        )
                    if member_bytes > 4096 and (member_bytes / comp_denom) > limits.max_compression_ratio:
                        raise ZipBombError(
                            f"Actual compression ratio of {clean_path!r} exceeds limit "
                            f"({member_bytes / comp_denom:.1f}x > {limits.max_compression_ratio:.1f}x)"
                        )
                    chunks.append(chunk)

            entries[clean_path] = b"".join(chunks)

    return entries


def safe_parse_xml(xml_data: Union[bytes, str]) -> Element:
    """Parse XML strictly with defusedxml (DTD, entity expansion, XXE all forbidden)."""
    raw: bytes
    if isinstance(xml_data, str):
        raw = xml_data.encode("utf-8", errors="strict")
    else:
        raw = xml_data

    if len(raw) > MAX_SINGLE_FILE_BYTES:
        raise ArchiveLimitError(
            f"XML document exceeds single-file byte limit ({len(raw)} > {MAX_SINGLE_FILE_BYTES})"
        )

    try:
        return DefusedET.fromstring(
            raw,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
    except DefusedXmlException as exc:
        raise XmlSecurityError(f"Blocked unsafe XML construct (DTD/XXE/entity): {exc}") from exc
    except Exception as exc:
        raise XmlSecurityError(f"Malformed or disallowed XML document: {exc}") from exc


def sanitize_xml_snippet(xml_text: str, max_chars: int = MAX_XML_SNIPPET_CHARS) -> str:
    """Sanitize and bound a preserved XML snippet for safe display & storage."""
    if not xml_text:
        return ""
    cleaned = _CONTROL_CHARS_RE.sub("", xml_text)
    if len(cleaned) > max_chars:
        return cleaned[: max_chars - 32] + "\n<!-- [truncated by Bang On Pro] -->"
    return cleaned
