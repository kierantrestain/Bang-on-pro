"""Shared, content-addressed analysis cache for the Bang On Pro production department.

Specialist agents share expensive intermediate artefacts (SHA-256 digests, ffprobe
results, sync offsets, transcripts) through this cache instead of recomputing them.
Entries are keyed by a SHA-256 over the namespace, agent version and key material
(typically source-media content hashes plus the parameters that influence the
result), so a change to any input automatically produces a cache miss.

Storage is local-only (default ``~/.cache/bangonpro/analysis`` or
``$BANGONPRO_CACHE_DIR``), bounded by an LRU byte budget, and safe to delete.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

DEFAULT_MAX_CACHE_BYTES = 2 * 1024 * 1024 * 1024


def default_cache_dir() -> Path:
    env = os.environ.get("BANGONPRO_CACHE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".cache" / "bangonpro" / "analysis").resolve()


class AnalysisCache:
    """Thread-safe JSON artefact cache with hit/miss accounting and LRU pruning."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        max_bytes: int = DEFAULT_MAX_CACHE_BYTES,
        enabled: bool = True,
    ) -> None:
        self.root = Path(root).expanduser().resolve() if root else default_cache_dir()
        self.max_bytes = int(max_bytes)
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        self.hits: dict[str, int] = {}
        self.misses: dict[str, int] = {}

    @staticmethod
    def make_key(namespace: str, version: str, parts: Any) -> str:
        material = json.dumps(
            {"ns": namespace, "v": version, "parts": parts}, sort_keys=True, default=str
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _path(self, namespace: str, key: str) -> Path:
        return self._ns_dir(namespace) / key[:2] / f"{key}.json"

    def get(self, namespace: str, version: str, parts: Any) -> Any | None:
        if not self.enabled:
            return None
        key = self.make_key(namespace, version, parts)
        path = self._path(namespace, key)
        with self._lock:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                os.utime(path, None)  # LRU touch
                self.hits[namespace] = self.hits.get(namespace, 0) + 1
                return payload.get("value")
            except (OSError, ValueError):
                self.misses[namespace] = self.misses.get(namespace, 0) + 1
                return None

    def put(self, namespace: str, version: str, parts: Any, value: Any) -> Path | None:
        if not self.enabled:
            return None
        key = self.make_key(namespace, version, parts)
        path = self._path(namespace, key)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"namespace": namespace, "version": version, "value": value}, default=str),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        self.prune()
        return path

    def size_bytes(self) -> int:
        if not self.root.exists():
            return 0
        return sum(p.stat().st_size for p in self.root.rglob("*.json") if p.is_file())

    def prune(self) -> int:
        """Delete least-recently-used entries until the cache fits ``max_bytes``."""
        if not self.root.exists():
            return 0
        files = [p for p in self.root.rglob("*.json") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        if total <= self.max_bytes:
            return 0
        removed = 0
        for p in sorted(files, key=lambda f: f.stat().st_mtime):
            if total <= self.max_bytes:
                break
            try:
                size = p.stat().st_size
                p.unlink()
                total -= size
                removed += 1
            except OSError:
                continue
        return removed

    def namespaces(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def _ns_dir(self, namespace: str) -> Path:
        safe_ns = "".join(c for c in namespace if c.isalnum() or c in "_-") or "misc"
        return self.root / safe_ns

    def clear(self, namespace: str | None = None) -> int:
        """Delete every entry, or only one namespace's entries (e.g. ``editorial``)."""
        base = self._ns_dir(namespace) if namespace else self.root
        removed = 0
        if base.exists():
            for p in base.rglob("*.json"):
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    def prune_to(self, max_bytes: int, namespace: str | None = None) -> int:
        """LRU-prune the whole cache (or one namespace) down to ``max_bytes``."""
        base = self._ns_dir(namespace) if namespace else self.root
        if not base.exists():
            return 0
        files = [p for p in base.rglob("*.json") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        removed = 0
        for p in sorted(files, key=lambda f: f.stat().st_mtime):
            if total <= max_bytes:
                break
            try:
                size = p.stat().st_size
                p.unlink()
                total -= size
                removed += 1
            except OSError:
                continue
        return removed

    def namespace_stats(self) -> list[dict[str, Any]]:
        rows = []
        for ns in self.namespaces():
            files = [p for p in self._ns_dir(ns).rglob("*.json") if p.is_file()]
            rows.append({"namespace": ns, "entries": len(files), "size_bytes": sum(p.stat().st_size for p in files), "newest_mtime": max((p.stat().st_mtime for p in files), default=None)})
        return rows

    def stats(self, *, per_namespace: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "root": str(self.root),
            "enabled": self.enabled,
            "max_bytes": self.max_bytes,
            "size_bytes": self.size_bytes(),
            "hits": dict(self.hits),
            "misses": dict(self.misses),
        }
        if per_namespace:
            out["namespaces"] = self.namespace_stats()
            out["entries"] = sum(r["entries"] for r in out["namespaces"])
        return out
