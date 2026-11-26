"""First-launch and on-demand system readiness check (V1.2.2).

``check_system()`` returns one structured report that the native System check
dialog renders. Every item has a plain-language ``status`` (``ok``, ``warning``,
``error`` or ``info``), a one-line ``detail`` and, when something needs doing, a
``fix`` sentence plus a ``fix_action`` the dialog turns into a button
(see ``errors.FIX_ACTIONS``).

Checked: hardware (Apple Silicon / CPU / RAM / GPU acceleration, read through
``bangonpro.macos.hardware_profiles``), free space and write access on the job,
cache and output folders (including external volumes), ffmpeg / ffprobe / melt,
the Python runtime and its packages, local model status
(``bangonpro.ai_engine.model_manager``) and the analysis cache size.

Nothing here downloads, installs or deletes anything.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bangonpro.ai_engine.department.cache import AnalysisCache, default_cache_dir
from bangonpro.ai_engine.department.version import APP_VERSION

GIB = 1024**3
STORAGE_ERROR_BYTES = 2 * GIB  # below this a job is blocked before processing
STORAGE_WARNING_BYTES = 20 * GIB  # renders and caches of a long episode need more
REQUIRED_PACKAGES = ("numpy", "scipy", "jsonschema", "defusedxml", "opentimelineio", "PIL", "yaml")
OPTIONAL_PACKAGES = ("cv2", "faster_whisper", "onnxruntime", "soundfile")
STATUS_ORDER = {"error": 0, "warning": 1, "info": 2, "ok": 3}


def default_job_root() -> Path:
    env = os.environ.get("BANGONPRO_JOB_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".local" / "share" / "bangonpro" / "jobs").resolve()


def _item(item_id: str, title: str, status: str, detail: str, fix: str = "", fix_action: str | None = None, **data: Any) -> dict[str, Any]:
    return {"id": item_id, "title": title, "status": status, "detail": detail, "fix": fix, "fix_action": fix_action, "data": data}


def _existing_ancestor(path: Path) -> Path:
    cur = path
    while not cur.exists() and cur != cur.parent:
        cur = cur.parent
    return cur


def _is_writable(path: Path) -> bool:
    target = path if path.exists() else _existing_ancestor(path)
    if not target.is_dir():
        return False
    try:
        with tempfile.NamedTemporaryFile(dir=target, prefix=".bangonpro_write_test_", delete=True):
            pass
        return True
    except OSError:
        return False


def _volume_label(path: Path) -> tuple[str, bool]:
    """Return (label, external) for the volume holding ``path``."""
    parts = path.parts
    if len(parts) >= 3 and parts[1] == "Volumes":  # macOS external and network volumes
        return parts[2], True
    try:
        home_dev = Path.home().stat().st_dev
        dev = _existing_ancestor(path).stat().st_dev
        if dev != home_dev:
            return str(_existing_ancestor(path)), True
    except OSError:
        pass
    return "system drive", False


def free_bytes(path: str | Path) -> int:
    return int(shutil.disk_usage(_existing_ancestor(Path(path).expanduser().resolve())).free)


def check_storage(path: str | Path, *, purpose: str, item_id: str, required_bytes: int = STORAGE_ERROR_BYTES) -> dict[str, Any]:
    p = Path(path).expanduser().resolve()
    label, external = _volume_label(p)
    title = f"Free space for {purpose}"
    if len(p.parts) >= 3 and p.parts[1] == "Volumes" and not Path("/", "Volumes", p.parts[2]).exists():
        return _item(item_id, title, "error", f"The drive '{p.parts[2]}' is not connected.", "Connect the drive, or choose another folder.", "choose_job_folder", path=str(p), external=True)
    try:
        free = free_bytes(p)
    except OSError as exc:
        return _item(item_id, title, "error", f"Cannot read the drive: {exc.strerror or exc}", "Choose another folder.", "choose_job_folder", path=str(p))
    writable = _is_writable(p)
    where = f"{'external drive ' if external else ''}{label}"
    detail = f"{free / GIB:.1f} GB free on {where}"
    data = {"path": str(p), "free_bytes": free, "external": external, "writable": writable, "required_bytes": int(required_bytes)}
    if not writable:
        return _item(item_id, title, "error", f"{detail}, but Bang On Pro cannot write there.", "Choose a folder you own, or allow access in System Settings > Privacy & Security > Files and Folders.", "choose_job_folder", **data)
    if free < required_bytes:
        return _item(item_id, title, "error", f"{detail}. At least {required_bytes / GIB:.1f} GB is needed.", "Free space on that drive or choose another folder (an external SSD works).", "choose_job_folder", **data)
    if free < STORAGE_WARNING_BYTES:
        return _item(item_id, title, "warning", f"{detail}. Long episodes and renders may need more.", "Free space or move the job folder to a larger drive.", "choose_job_folder", **data)
    return _item(item_id, title, "ok", detail, **data)


def preflight_storage(path: str | Path, required_bytes: int) -> tuple[bool, str, int]:
    """Cheap check run before any processing: (ok, plain message, free bytes)."""
    p = Path(path).expanduser().resolve()
    try:
        free = free_bytes(p)
    except OSError as exc:
        return False, f"Cannot read the drive for {p}: {exc}", 0
    if free < required_bytes:
        return (
            False,
            f"Insufficient storage in {p}: {free / 1e9:.2f} GB free, ~{required_bytes / 1e9:.2f} GB required. "
            "Free disk space or choose another job folder, then resume the job.",
            free,
        )
    return True, "", free


def _hardware_items(disk_path: Path, probe: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    caps: dict[str, Any] = {}
    if probe:
        try:
            from bangonpro.macos.hardware_profiles import probe_hardware_capabilities

            caps = probe_hardware_capabilities(_existing_ancestor(disk_path)).to_dict()
        except Exception as exc:  # noqa: BLE001 - a probe failure is itself a finding
            caps = {"probe_error": f"{type(exc).__name__}: {exc}"}
    if not caps or "probe_error" in caps:
        caps.setdefault("platform_system", platform.system().lower())
        caps.setdefault("machine", platform.machine().lower())
        caps.setdefault("cpu_cores_logical", os.cpu_count() or 1)
        caps.setdefault("ffmpeg_path", shutil.which("ffmpeg"))
        caps.setdefault("ffprobe_path", shutil.which("ffprobe"))
        caps.setdefault("melt_path", shutil.which("melt") or shutil.which("melt-7"))
    system = str(caps.get("platform_system", ""))
    apple = bool(caps.get("is_apple_silicon"))
    ram = float(caps.get("total_ram_gb") or 0.0)
    cpu = str(caps.get("cpu_brand") or caps.get("machine") or "unknown CPU")
    cores = int(caps.get("cpu_cores_logical") or 0)
    tier = str(caps.get("host_tier") or "")
    items: list[dict[str, Any]] = []
    detail = f"{cpu}, {cores} cores, {ram:.0f} GB memory" if ram else f"{cpu}, {cores} cores"
    if apple:
        status, fix = "ok", ""
        if caps.get("is_virtualized"):
            status, fix = "info", "This is a virtual Mac. Timings are not representative of an editor's Mac."
    elif system == "darwin":
        status, fix = "warning", "Intel Macs work for testing, but Bang On Pro is tuned for Apple Silicon."
    else:
        status, fix = "info", "Not a Mac. Fine for development and CI; editors use the macOS build."
    items.append(_item("hardware", "Computer", status, detail, fix, None, host_tier=tier, apple_silicon=apple, total_ram_gb=ram))
    if ram and ram < 8:
        items.append(_item("memory", "Memory", "warning", f"{ram:.0f} GB memory", "Close other apps while a production runs; large local models need 16 GB.", None))
    meta = caps.get("metal_assessment") or {}
    accel = []
    if caps.get("videotoolbox_available"):
        accel.append("VideoToolbox encode and decode")
    if isinstance(meta, dict) and meta.get("ai_worker_mps_candidate"):
        accel.append("Metal for local AI models")
    if apple or system == "darwin":
        gpu_status = "ok" if accel else "warning"
        gpu_fix = "" if accel else "Hardware video acceleration was not found; renders use the CPU and take longer."
    else:
        gpu_status, gpu_fix = "info", ""
    items.append(_item("gpu", "Video acceleration", gpu_status, ", ".join(accel) if accel else "CPU only", gpu_fix, None))
    return items, caps


def _tool_items(caps: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for tool, key, version_key in (("ffmpeg", "ffmpeg_path", "ffmpeg_version"), ("ffprobe", "ffprobe_path", None), ("melt", "melt_path", "melt_version")):
        path = caps.get(key) or shutil.which(tool)
        if not path:
            items.append(_item(f"tool_{tool}", tool, "error", f"{tool} was not found.", "Reinstall Bang On Pro from the DMG (it includes ffmpeg, ffprobe and melt).", "run_system_check"))
            continue
        version = str(caps.get(version_key) or "") if version_key else ""
        version = version.split(" Copyright")[0].replace(" version ", " ")
        items.append(_item(f"tool_{tool}", tool, "ok", version or "Installed", "", None, path=str(path)))
    return items


def _python_item() -> dict[str, Any]:
    exe = sys.executable
    bundled = "python-env" in exe or "Resources" in Path(exe).parts
    missing = [p for p in REQUIRED_PACKAGES if importlib.util.find_spec(p) is None]
    optional = {p: importlib.util.find_spec(p) is not None for p in OPTIONAL_PACKAGES}
    detail = f"Python {platform.python_version()} ({'bundled with the app' if bundled else 'system'})"
    if missing:
        return _item("python", "AI runtime", "error", f"{detail}; missing {', '.join(missing)}", "Reinstall Bang On Pro from the DMG to restore the bundled AI runtime.", "run_system_check", bundled=bundled, missing=missing, optional=optional)
    return _item("python", "AI runtime", "ok", detail, "", None, bundled=bundled, optional=optional)


def model_status(model_dir: str | Path | None = None) -> dict[str, Any]:
    """Compact local-model status (no downloads)."""
    from bangonpro.ai_engine.model_manager import ModelManager

    report = ModelManager(model_dir=model_dir).get_storage_report()
    models = [
        {k: m.get(k) for k in ("model_id", "display_name", "family", "status", "model_available_on_disk", "backend_package_installed", "hardware_compatible", "recommended", "disk_usage_bytes")}
        for m in report.get("models", [])
    ]
    asr_ready = [m["model_id"] for m in models if m["family"] == "asr_whisper" and m["status"] in ("ready", "loaded")]
    seg_ready = [m["model_id"] for m in models if m["family"] == "segmentation_sam" and m["status"] in ("ready", "loaded")]
    return {
        "model_dir": report.get("model_dir"),
        "installed_count": report.get("installed_count", 0),
        "ready_count": report.get("ready_count", 0),
        "total_mb": report.get("total_mb", 0.0),
        "speech_ready": asr_ready,
        "segmentation_ready": seg_ready,
        "models": models,
    }


def _model_items(model_dir: str | Path | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        ms = model_status(model_dir)
    except Exception as exc:  # noqa: BLE001
        return [_item("models", "Local AI models", "warning", f"Model status unavailable ({type(exc).__name__})", "Open Model Manager to inspect the model folder.", "open_model_manager")], {}
    items = []
    if ms["speech_ready"]:
        items.append(_item("model_speech", "Speech recognition", "ok", f"Ready: {', '.join(ms['speech_ready'])}", "", None))
    else:
        items.append(
            _item(
                "model_speech",
                "Speech recognition",
                "warning",
                "No local speech model is installed. Cuts still work; captions and word-accurate transcripts are withheld.",
                "Install Whisper tiny or base in Model Manager.",
                "open_model_manager",
            )
        )
    if ms["segmentation_ready"]:
        items.append(_item("model_segmentation", "Subject masks", "ok", f"Ready: {', '.join(ms['segmentation_ready'])}", "", None))
    else:
        items.append(_item("model_segmentation", "Subject masks", "info", "No local segmentation model. Background blur uses the labelled fallback mask.", "Install MobileSAM in Model Manager for neural masks.", "open_model_manager"))
    return items, ms


def _cache_item(cache_dir: Path) -> dict[str, Any]:
    cache = AnalysisCache(cache_dir)
    st = cache.stats()
    size, budget = int(st["size_bytes"]), int(st["max_bytes"])
    detail = f"{size / (1024 * 1024):.1f} MB of {budget / GIB:.0f} GB"
    if budget and size > 0.9 * budget:
        return _item("cache", "Analysis cache", "warning", detail + " (nearly full)", "Prune the cache in System check > Maintenance.", None, **st)
    return _item("cache", "Analysis cache", "ok", detail, "", None, **st)


def overall_status(items: list[dict[str, Any]]) -> str:
    statuses = {i["status"] for i in items}
    if "error" in statuses:
        return "blocked"
    if "warning" in statuses:
        return "attention"
    return "ready"


def check_system(
    *,
    job_root: str | Path | None = None,
    cache_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
    model_dir: str | Path | None = None,
    probe_hardware: bool = True,
    include_models: bool = True,
) -> dict[str, Any]:
    jr = Path(job_root).expanduser().resolve() if job_root else default_job_root()
    cd = Path(cache_dir).expanduser().resolve() if cache_dir else default_cache_dir()
    od = Path(output_dir).expanduser().resolve() if output_dir else None
    items: list[dict[str, Any]] = []
    hw_items, caps = _hardware_items(jr, probe_hardware)
    items.extend(hw_items)
    items.append(check_storage(jr, purpose="productions", item_id="storage_jobs"))
    if cd != jr and _existing_ancestor(cd).stat().st_dev != _existing_ancestor(jr).stat().st_dev:
        items.append(check_storage(cd, purpose="the analysis cache", item_id="storage_cache", required_bytes=GIB // 2))
    else:
        items.append(_item("storage_cache", "Free space for the analysis cache", "ok", "Same drive as productions", "", None, path=str(cd)))
    if od is not None and od != jr:
        items.append(check_storage(od, purpose="exports", item_id="storage_output"))
    items.extend(_tool_items(caps))
    items.append(_python_item())
    models: dict[str, Any] = {}
    if include_models:
        model_items, models = _model_items(model_dir)
        items.extend(model_items)
    items.append(_cache_item(cd))
    items.sort(key=lambda i: STATUS_ORDER.get(i["status"], 9))
    return {
        "schema_version": "1.0",
        "app_version": APP_VERSION,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "overall": overall_status(items),
        "summary": {s: sum(1 for i in items if i["status"] == s) for s in ("error", "warning", "info", "ok")},
        "paths": {"job_root": str(jr), "cache_dir": str(cd), "output_dir": str(od) if od else None, "model_dir": models.get("model_dir")},
        "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine(), "python": platform.python_version()},
        "hardware": {k: caps.get(k) for k in ("platform_system", "machine", "is_apple_silicon", "cpu_brand", "cpu_cores_logical", "total_ram_gb", "host_tier", "is_virtualized", "videotoolbox_available")},
        "models": models,
        "items": items,
    }


__all__ = ["check_storage", "check_system", "default_job_root", "free_bytes", "model_status", "overall_status", "preflight_storage"]
