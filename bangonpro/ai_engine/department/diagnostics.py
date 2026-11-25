"""Sanitised diagnostics export (V1.2.2).

``export_diagnostics()`` writes one zip an editor can send to the team when
something goes wrong. It is **sanitised by default**:

* never included: credentials, tokens, API keys, provider configuration and
  consent files, the preference store, source camera media;
* secret-looking keys and values are replaced by ``[redacted]``;
* home-folder paths are shortened to ``~``;
* media, frames, audio, transcripts, captions, timelines and checkpoints (which
  hold transcripts) are left out unless the editor explicitly opts in with
  ``include_media=True``; even then source camera media is never copied and
  files over ``MAX_OPT_IN_FILE_BYTES`` are skipped.

``manifest.json`` inside the zip lists every included file (size, SHA-256) and
every excluded item with the reason.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bangonpro.ai_engine.department.version import APP_VERSION

REDACTED = "[redacted]"
MAX_OPT_IN_FILE_BYTES = 200 * 1024 * 1024
# Job files that describe state and decisions without media content.
JOB_STATE_FILES = ("job.json", "plan.json", "events.jsonl", "summary.json", "summary.md")
DRAFT_REPORT_FILES = ("media_inventory.json", "qc_report.json", "delivery_manifest.json", "camera_plan.json")
# Content-bearing files: only with the explicit media opt-in.
OPT_IN_SUFFIXES = (".srt", ".ass", ".vtt", ".kdenlive", ".otio", ".mp4", ".mov", ".mkv", ".wav", ".m4a", ".mp3", ".aac", ".png", ".jpg", ".jpeg")
OPT_IN_NAMES = ("transcript.json", "timeline_commands.json", "analysis_state.json", "highlights.json")
NEVER_NAMES = re.compile(r"(provider|consent|credential|secret|token|keychain|\.env$|apikey|api_key)", re.IGNORECASE)
SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|apikey|access[_-]?key|secret|token|password|passwd|authorization|bearer|credential|cookie|session[_-]?id|private[_-]?key|provider_config|client[_-]?secret)",
    re.IGNORECASE,
)
SECRET_VALUE_RES = (
    re.compile(r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"hf_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{16,}=*"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*[^\s,;\"']+"),
)


class Sanitiser:
    def __init__(self) -> None:
        home = Path.home()
        self.homes = sorted({str(home), str(home.resolve())}, key=len, reverse=True)
        self.generic_home = re.compile(r"(/Users|/home)/[^/\s\"']+")
        self.counts = {"secret_keys": 0, "secret_values": 0, "home_paths": 0}

    def text(self, s: str) -> str:
        for rx in SECRET_VALUE_RES:
            s, n = rx.subn(REDACTED, s)
            self.counts["secret_values"] += n
        for h in self.homes:
            if h and h != "/" and h in s:
                self.counts["home_paths"] += s.count(h)
                s = s.replace(h, "~")
        s, n = self.generic_home.subn("~", s)
        self.counts["home_paths"] += n
        return s

    def value(self, v: Any, key: str = "") -> Any:
        if key and SECRET_KEY_RE.search(key) and not isinstance(v, (bool, type(None))):
            self.counts["secret_keys"] += 1
            return REDACTED
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, list):
            return [self.value(x) for x in v]
        if isinstance(v, dict):
            return {self.text(str(k)): self.value(x, str(k)) for k, x in v.items()}
        return v


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Bundle:
    def __init__(self, sanitiser: Sanitiser) -> None:
        self.s = sanitiser
        self.files: dict[str, bytes] = {}
        self.included: list[dict[str, Any]] = []
        self.excluded: list[dict[str, Any]] = []

    def add_json(self, arc: str, payload: Any, source: str = "") -> None:
        data = (json.dumps(self.s.value(payload), indent=2, default=str) + "\n").encode("utf-8")
        self._add(arc, data, source, "sanitised")

    def add_text(self, arc: str, text: str, source: str = "") -> None:
        self._add(arc, self.s.text(text).encode("utf-8"), source, "sanitised")

    def add_raw(self, arc: str, data: bytes, source: str = "") -> None:
        self._add(arc, data, source, "opt_in_unmodified")

    def _add(self, arc: str, data: bytes, source: str, treatment: str) -> None:
        self.files[arc] = data
        self.included.append({"path": arc, "bytes": len(data), "sha256": _sha(data), "source": self.s.text(source), "treatment": treatment})

    def exclude(self, what: str, reason: str) -> None:
        self.excluded.append({"item": self.s.text(what), "reason": reason})


def _add_job_file(b: _Bundle, arc: str, path: Path) -> None:
    if path.suffix == ".json":
        try:
            b.add_json(arc, json.loads(path.read_text(encoding="utf-8")), str(path))
            return
        except (OSError, ValueError):
            pass
    if path.suffix == ".jsonl":
        lines = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                lines.append(json.dumps(b.s.value(json.loads(line)), default=str))
            except ValueError:
                lines.append(b.s.text(line))
        b._add(arc, ("\n".join(lines) + "\n").encode("utf-8"), str(path), "sanitised")
        return
    b.add_text(arc, path.read_text(encoding="utf-8", errors="replace"), str(path))


def _collect_job(b: _Bundle, job_dir: Path, include_media: bool, errors: list[dict[str, Any]]) -> None:
    job_id = job_dir.name
    prefix = f"jobs/{job_id}/"
    sources: set[str] = set()
    try:
        doc = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        plan = json.loads((job_dir / "plan.json").read_text(encoding="utf-8")) if (job_dir / "plan.json").exists() else {}
        sources = {str(Path(p).resolve()) for p in (plan.get("inputs") or {}).values()}
        for aid, rec in (doc.get("agents") or {}).items():
            if rec.get("error") or rec.get("status") in ("failed", "blocked"):
                errors.append({"job_id": job_id, "agent_id": aid, "status": rec.get("status"), "error": rec.get("error"), "help": rec.get("help")})
        if doc.get("error"):
            errors.append({"job_id": job_id, "agent_id": None, "status": doc.get("status"), "error": doc.get("error"), "help": doc.get("error_help")})
    except (OSError, ValueError) as exc:
        errors.append({"job_id": job_id, "error": f"job.json unreadable: {exc}"})
    for name in JOB_STATE_FILES:
        p = job_dir / name
        if p.is_file():
            _add_job_file(b, prefix + name, p)
    for name in DRAFT_REPORT_FILES:
        p = job_dir / "draft" / name
        if p.is_file():
            _add_job_file(b, prefix + "draft/" + name, p)
    if (job_dir / "checkpoints").is_dir():
        b.exclude(prefix + "checkpoints/", "checkpoints hold full project state including transcripts" + ("" if include_media else " (not included without the media opt-in)"))
    draft = job_dir / "draft"
    if not draft.is_dir():
        return
    for f in sorted(draft.rglob("*")):
        if not f.is_file() or f.name in DRAFT_REPORT_FILES:
            continue
        rel = prefix + "draft/" + str(f.relative_to(draft))
        if NEVER_NAMES.search(f.name):
            b.exclude(rel, "credential or provider file: never included")
            continue
        if str(f.resolve()) in sources:
            b.exclude(rel, "source camera media: never included")
            continue
        content = f.suffix.lower() in OPT_IN_SUFFIXES or f.name in OPT_IN_NAMES
        if not content:
            b.exclude(rel, "not needed for diagnosis")
            continue
        if not include_media:
            b.exclude(rel, "media, transcript, caption or timeline content: excluded unless you opt in")
            continue
        if f.stat().st_size > MAX_OPT_IN_FILE_BYTES:
            b.exclude(rel, f"larger than {MAX_OPT_IN_FILE_BYTES // (1024 * 1024)} MB")
            continue
        if f.suffix.lower() in (".json", ".kdenlive", ".otio", ".srt", ".ass", ".vtt"):
            b.add_text(rel, f.read_text(encoding="utf-8", errors="replace"), str(f))
        else:
            b.add_raw(rel, f.read_bytes(), str(f))


def export_diagnostics(
    out_path: str | Path,
    *,
    job_dirs: list[str | Path] | None = None,
    job_root: str | Path | None = None,
    recent_jobs: int = 3,
    queue_root: str | Path | None = None,
    cache_dir: str | Path | None = None,
    model_dir: str | Path | None = None,
    include_media: bool = False,
    probe_hardware: bool = True,
) -> dict[str, Any]:
    from bangonpro.ai_engine.department.cache import AnalysisCache
    from bangonpro.ai_engine.department.readiness import check_system

    sanitiser = Sanitiser()
    b = _Bundle(sanitiser)
    created = datetime.now(UTC).isoformat()
    b.add_json("app.json", {"app_version": APP_VERSION, "created_at_utc": created, "os": platform.platform(), "system": platform.system(), "release": platform.release(), "machine": platform.machine(), "python": platform.python_version()})
    try:
        report = check_system(job_root=job_root, cache_dir=cache_dir, model_dir=model_dir, probe_hardware=probe_hardware)
        b.add_json("readiness.json", report)
        b.add_json("models.json", report.get("models") or {})
    except Exception as exc:  # noqa: BLE001 - diagnostics must still export
        b.add_json("readiness.json", {"error": f"{type(exc).__name__}: {exc}"})
    try:
        b.add_json("cache.json", AnalysisCache(cache_dir).stats(per_namespace=True))
    except Exception as exc:  # noqa: BLE001
        b.add_json("cache.json", {"error": f"{type(exc).__name__}: {exc}"})
    dirs = [Path(d).expanduser().resolve() for d in (job_dirs or [])]
    if not dirs and job_root and Path(job_root).expanduser().is_dir():
        cands = [d for d in Path(job_root).expanduser().iterdir() if (d / "job.json").is_file()]
        dirs = sorted(cands, key=lambda d: (d / "job.json").stat().st_mtime, reverse=True)[: max(0, int(recent_jobs))]
    errors: list[dict[str, Any]] = []
    for d in dirs:
        if (d / "job.json").is_file():
            _collect_job(b, d, include_media, errors)
        else:
            b.exclude(str(d), "no job.json (not a production job)")
    if queue_root:
        try:
            from bangonpro.ai_engine.department.queue import JobQueue

            b.add_json("queue.json", JobQueue(queue_root).status())
            for e in JobQueue(queue_root).entries():
                if e.get("error"):
                    errors.append({"queue_id": e["queue_id"], "job_id": e.get("job_id"), "status": e["status"], "error": e["error"], "help": e.get("help")})
        except Exception as exc:  # noqa: BLE001
            b.add_json("queue.json", {"error": f"{type(exc).__name__}: {exc}"})
    b.add_json("errors.json", errors)
    for what, reason in (
        ("API keys, tokens and passwords", "never included; secret-looking keys and values are replaced by [redacted]"),
        ("External AI provider configuration and consent", "never included"),
        ("Learned production preferences", "not included"),
        ("Source camera media", "never included (SHA-256 digests in media_inventory.json identify them)"),
    ):
        b.exclude(what, reason)
    manifest = {
        "schema_version": "1.0",
        "app_version": APP_VERSION,
        "created_at_utc": created,
        "sanitised": True,
        "include_media": bool(include_media),
        "home_paths_redacted_to": "~",
        "jobs": [d.name for d in dirs],
        "redactions": dict(sanitiser.counts),
        "included": sorted(b.included, key=lambda i: i["path"]),
        "excluded": b.excluded,
    }
    out = Path(out_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arc, data in sorted(b.files.items()):
            zf.writestr(arc, data)
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, default=str) + "\n")
    tmp.replace(out)
    return {"zip_path": str(out), "bytes": out.stat().st_size, "manifest": manifest}


__all__ = ["REDACTED", "Sanitiser", "export_diagnostics"]
