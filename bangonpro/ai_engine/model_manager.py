"""Managed AI Model Runtime & Hardware-Aware Model Catalog for Bang On Pro.

Manages local Whisper-family ASR models (``faster-whisper``, ``whisper.cpp``,
``openai-whisper``) and SAM-family segmentation models (MobileSAM ONNX,
SAM 2.1 Hiera Tiny/Small) under a configurable ``BANGONPRO_MODEL_DIR``.

Explicitly distinguishes:
- ``backend_package_installed``: whether the Python package / native CLI exists
- ``model_available_on_disk``: whether verified model weights exist in ``BANGONPRO_MODEL_DIR``
- ``model_loaded``: whether weights are currently resident in process memory
- ``actual_inference_executed``: whether genuine neural inference ran in this session

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bangonpro.ai_engine.orchestrator import CancellationToken, PipelineCancelledError

MODEL_DIR_ENV_VAR = "BANGONPRO_MODEL_DIR"
_SAFE_MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")

ModelProgressCallback = Callable[[float, int, int, str], None]


class ModelManagerError(RuntimeError):
    """Base error for managed AI model operations."""


class ModelChecksumError(ModelManagerError):
    """Raised when a downloaded/copied model artifact fails SHA-256 verification."""


class ModelNotFoundError(KeyError):
    """Raised when a requested model ID is not present in the catalog."""


@dataclass(frozen=True)
class HardwareCapabilities:
    """Detected host hardware and local AI acceleration capabilities."""

    os_name: str
    arch: str
    is_apple_silicon: bool
    cpu_count: int
    total_ram_gb: float
    ram_tier: str  # "low_8gb" | "medium_16gb" | "high_32gb"
    mps_available: bool
    cuda_available: bool
    coreml_available: bool
    installed_backends: dict[str, bool]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


FASTER_WHISPER_COMPANIONS: tuple[str, ...] = ("config.json", "tokenizer.json", "vocabulary.txt")


@dataclass(frozen=True)
class ModelCatalogEntry:
    """Declarative metadata for a supported open-source local AI model."""

    model_id: str
    display_name: str
    family: str  # "asr_whisper" | "segmentation_sam"
    runtime_backend: str  # "faster_whisper" | "whisper_cpp" | "openai_whisper" | "onnxruntime" | "sam2"
    upstream_model_ref: str
    license_spdx: str
    min_ram_gb: int
    ram_tier: str  # "low_8gb" | "medium_16gb" | "high_32gb"
    disk_size_mb: int
    mps_compatible: bool
    coreml_compatible: bool
    cpu_compatible: bool
    download_url: str
    artifact_filename: str
    sha256: str | None = None
    recommended_for_apple_silicon: bool = False
    notes: str = ""
    # Additional files that must sit beside the artifact for the runtime to load it
    # (V1.2 fix: CTranslate2 faster-whisper models need config/tokenizer/vocabulary).
    companion_files: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ModelRuntimeStatus:
    """Honest runtime status snapshot for a single catalog model."""

    model_id: str
    display_name: str
    family: str
    runtime_backend: str
    license_spdx: str
    ram_tier: str
    min_ram_gb: int
    disk_size_mb: int
    mps_compatible: bool
    coreml_compatible: bool
    cpu_compatible: bool
    backend_package_installed: bool
    model_available_on_disk: bool
    model_loaded: bool
    actual_inference_executed: bool
    status: str  # "loaded" | "ready" | "weights_missing" | "backend_missing" | "not_installed"
    hardware_compatible: bool
    recommended: bool
    compatibility_notes: str
    install_path: str | None
    disk_usage_bytes: int
    installed_sha256: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


MODEL_CATALOG: tuple[ModelCatalogEntry, ...] = (
    ModelCatalogEntry(
        model_id="faster-whisper-tiny",
        display_name="Faster-Whisper Tiny (CTranslate2)",
        family="asr_whisper",
        runtime_backend="faster_whisper",
        upstream_model_ref="Systran/faster-whisper-tiny",
        license_spdx="MIT",
        min_ram_gb=4,
        ram_tier="low_8gb",
        disk_size_mb=75,
        mps_compatible=False,
        coreml_compatible=False,
        cpu_compatible=True,
        download_url="https://huggingface.co/Systran/faster-whisper-tiny/resolve/main/model.bin",
        artifact_filename="model.bin",
        companion_files=FASTER_WHISPER_COMPANIONS,
        recommended_for_apple_silicon=False,
        notes="Fastest CPU CTranslate2 Whisper model; suitable for 8 GB systems and quick rough transcripts.",
    ),
    ModelCatalogEntry(
        model_id="faster-whisper-small",
        display_name="Faster-Whisper Small (CTranslate2)",
        family="asr_whisper",
        runtime_backend="faster_whisper",
        upstream_model_ref="Systran/faster-whisper-small",
        license_spdx="MIT",
        min_ram_gb=8,
        ram_tier="low_8gb",
        disk_size_mb=465,
        mps_compatible=False,
        coreml_compatible=False,
        cpu_compatible=True,
        download_url="https://huggingface.co/Systran/faster-whisper-small/resolve/main/model.bin",
        artifact_filename="model.bin",
        companion_files=FASTER_WHISPER_COMPANIONS,
        recommended_for_apple_silicon=False,
        notes="Balanced CPU accuracy/speed for studio podcast and interview transcription.",
    ),
    ModelCatalogEntry(
        model_id="faster-whisper-medium",
        display_name="Faster-Whisper Medium (CTranslate2)",
        family="asr_whisper",
        runtime_backend="faster_whisper",
        upstream_model_ref="Systran/faster-whisper-medium",
        license_spdx="MIT",
        min_ram_gb=16,
        ram_tier="medium_16gb",
        disk_size_mb=1500,
        mps_compatible=False,
        coreml_compatible=False,
        cpu_compatible=True,
        download_url="https://huggingface.co/Systran/faster-whisper-medium/resolve/main/model.bin",
        artifact_filename="model.bin",
        companion_files=FASTER_WHISPER_COMPANIONS,
        recommended_for_apple_silicon=False,
        notes="High-accuracy multilingual CTranslate2 Whisper for 16 GB+ workstations.",
    ),
    ModelCatalogEntry(
        model_id="faster-whisper-large-v3",
        display_name="Faster-Whisper Large-v3 (CTranslate2)",
        family="asr_whisper",
        runtime_backend="faster_whisper",
        upstream_model_ref="Systran/faster-whisper-large-v3",
        license_spdx="MIT",
        min_ram_gb=24,
        ram_tier="high_32gb",
        disk_size_mb=3000,
        mps_compatible=False,
        coreml_compatible=False,
        cpu_compatible=True,
        download_url="https://huggingface.co/Systran/faster-whisper-large-v3/resolve/main/model.bin",
        artifact_filename="model.bin",
        companion_files=FASTER_WHISPER_COMPANIONS,
        recommended_for_apple_silicon=False,
        notes="Maximum accuracy Whisper ASR; requires 24-32 GB+ RAM or CUDA GPU.",
    ),
    ModelCatalogEntry(
        model_id="whisper-cpp-base-en",
        display_name="whisper.cpp GGML Base.en (Metal/CoreML)",
        family="asr_whisper",
        runtime_backend="whisper_cpp",
        upstream_model_ref="ggerganov/whisper.cpp:ggml-base.en.bin",
        license_spdx="MIT",
        min_ram_gb=4,
        ram_tier="low_8gb",
        disk_size_mb=148,
        mps_compatible=True,
        coreml_compatible=True,
        cpu_compatible=True,
        download_url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin",
        artifact_filename="ggml-base.en.bin",
        recommended_for_apple_silicon=True,
        notes="Native whisper.cpp GGML model with Apple Silicon CoreML/Metal and CPU acceleration.",
    ),
    ModelCatalogEntry(
        model_id="openai-whisper-tiny",
        display_name="OpenAI Whisper Tiny (PyTorch)",
        family="asr_whisper",
        runtime_backend="openai_whisper",
        upstream_model_ref="openai/whisper:tiny",
        license_spdx="MIT",
        min_ram_gb=4,
        ram_tier="low_8gb",
        disk_size_mb=75,
        mps_compatible=True,
        coreml_compatible=False,
        cpu_compatible=True,
        download_url="https://openaipublic.azureedge.net/main/whisper/models/65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9/tiny.pt",
        artifact_filename="tiny.pt",
        sha256="65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9",
        recommended_for_apple_silicon=True,
        notes="Reference PyTorch Whisper Tiny checkpoint with verified SHA-256 and MPS/CPU support.",
    ),
    ModelCatalogEntry(
        model_id="openai-whisper-small",
        display_name="OpenAI Whisper Small (PyTorch)",
        family="asr_whisper",
        runtime_backend="openai_whisper",
        upstream_model_ref="openai/whisper:small",
        license_spdx="MIT",
        min_ram_gb=8,
        ram_tier="low_8gb",
        disk_size_mb=461,
        mps_compatible=True,
        coreml_compatible=False,
        cpu_compatible=True,
        download_url="https://openaipublic.azureedge.net/main/whisper/models/9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794/small.pt",
        artifact_filename="small.pt",
        sha256="9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794",
        recommended_for_apple_silicon=True,
        notes="Reference PyTorch Whisper Small checkpoint with verified SHA-256 and MPS/CPU support.",
    ),
    ModelCatalogEntry(
        model_id="mobilesam-onnx",
        display_name="MobileSAM ViT-Tiny (ONNX / CoreML)",
        family="segmentation_sam",
        runtime_backend="onnxruntime",
        upstream_model_ref="ChaoningZhang/MobileSAM",
        license_spdx="Apache-2.0",
        min_ram_gb=4,
        ram_tier="low_8gb",
        disk_size_mb=40,
        mps_compatible=False,
        coreml_compatible=True,
        cpu_compatible=True,
        download_url="https://huggingface.co/Acly/MobileSAM/resolve/main/mobile_sam_image_encoder.onnx",
        artifact_filename="mobile_sam_image_encoder.onnx",
        companion_files=("sam_mask_decoder_single.onnx",),
        recommended_for_apple_silicon=True,
        notes="Lightweight ONNX segmentation encoder ideal for CoreML on Apple Silicon and CPU rotoscoping.",
    ),
    ModelCatalogEntry(
        model_id="sam2.1-hiera-tiny",
        display_name="SAM 2.1 Hiera Tiny (PyTorch)",
        family="segmentation_sam",
        runtime_backend="sam2",
        upstream_model_ref="facebook/sam2.1-hiera-tiny",
        license_spdx="Apache-2.0",
        min_ram_gb=8,
        ram_tier="low_8gb",
        disk_size_mb=156,
        mps_compatible=True,
        coreml_compatible=False,
        cpu_compatible=True,
        download_url="https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_tiny.pt",
        artifact_filename="sam2.1_hiera_tiny.pt",
        recommended_for_apple_silicon=True,
        notes="Meta SAM 2.1 Hiera Tiny checkpoint for interactive video masking on MPS/CUDA/CPU.",
    ),
    ModelCatalogEntry(
        model_id="sam2.1-hiera-small",
        display_name="SAM 2.1 Hiera Small (PyTorch)",
        family="segmentation_sam",
        runtime_backend="sam2",
        upstream_model_ref="facebook/sam2.1-hiera-small",
        license_spdx="Apache-2.0",
        min_ram_gb=16,
        ram_tier="medium_16gb",
        disk_size_mb=185,
        mps_compatible=True,
        coreml_compatible=False,
        cpu_compatible=True,
        download_url="https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_small.pt",
        artifact_filename="sam2.1_hiera_small.pt",
        recommended_for_apple_silicon=True,
        notes="Higher-fidelity SAM 2.1 Hiera Small segmentation checkpoint for 16 GB+ machines.",
    ),
)

_CATALOG_BY_ID: dict[str, ModelCatalogEntry] = {entry.model_id: entry for entry in MODEL_CATALOG}


def resolve_model_dir(model_dir: str | Path | None = None) -> Path:
    """Resolve the managed AI model storage root (``BANGONPRO_MODEL_DIR`` or platform cache)."""
    if model_dir is not None:
        return Path(model_dir).expanduser().resolve()
    env_dir = os.environ.get(MODEL_DIR_ENV_VAR, "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    if platform.system() == "Darwin":
        return (Path.home() / "Library" / "Caches" / "BangOnPro" / "models").resolve()
    xdg_cache = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg_cache).expanduser() if xdg_cache else (Path.home() / ".cache")
    return (base / "bangonpro" / "models").resolve()


def _detect_total_ram_gb() -> float:
    system = platform.system()
    if system == "Linux":
        meminfo = Path("/proc/meminfo")
        if meminfo.is_file():
            try:
                for line in meminfo.read_text(encoding="utf-8").splitlines():
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return round(kb / (1024.0 * 1024.0), 2)
            except (OSError, ValueError, IndexError):
                pass
    elif system == "Darwin":
        sysctl_bin = shutil.which("sysctl")
        if sysctl_bin:
            try:
                proc = subprocess.run(
                    [sysctl_bin, "-n", "hw.memsize"],
                    capture_output=True,
                    text=True,
                    check=False,
                    shell=False,
                    timeout=3,
                )
                if proc.returncode == 0 and proc.stdout.strip().isdigit():
                    return round(int(proc.stdout.strip()) / (1024.0**3), 2)
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
    return 8.0


def detect_hardware_capabilities() -> HardwareCapabilities:
    """Inspect host OS, architecture, RAM tier, and installed AI runtime packages."""
    os_name = platform.system()
    arch = platform.machine().lower()
    is_apple_silicon = os_name == "Darwin" and arch in ("arm64", "aarch64")
    cpu_count = os.cpu_count() or 1
    total_ram_gb = _detect_total_ram_gb()

    if total_ram_gb >= 24.0:
        ram_tier = "high_32gb"
    elif total_ram_gb >= 14.0:
        ram_tier = "medium_16gb"
    else:
        ram_tier = "low_8gb"

    installed_backends: dict[str, bool] = {
        "faster_whisper": importlib.util.find_spec("faster_whisper") is not None,
        "openai_whisper": importlib.util.find_spec("whisper") is not None,
        "whisperx": importlib.util.find_spec("whisperx") is not None,
        "whisper_cpp": bool(shutil.which("whisper-cli") or shutil.which("whisper-cpp")),
        "onnxruntime": importlib.util.find_spec("onnxruntime") is not None,
        "sam2": importlib.util.find_spec("sam2") is not None,
        "torch": importlib.util.find_spec("torch") is not None,
    }

    mps_available = False
    cuda_available = False
    if installed_backends["torch"]:
        try:
            import torch  # type: ignore[import-not-found]

            cuda_available = bool(torch.cuda.is_available())
            mps_backend = getattr(getattr(torch, "backends", None), "mps", None)
            mps_available = bool(mps_backend and mps_backend.is_available())
        except Exception:  # noqa: BLE001
            mps_available = False
            cuda_available = False

    coreml_available = False
    if installed_backends["onnxruntime"]:
        try:
            import onnxruntime as ort  # type: ignore[import-not-found]

            providers = ort.get_available_providers()
            coreml_available = "CoreMLExecutionProvider" in providers
        except Exception:  # noqa: BLE001
            coreml_available = False
    elif is_apple_silicon and installed_backends["whisper_cpp"]:
        coreml_available = True

    return HardwareCapabilities(
        os_name=os_name,
        arch=arch,
        is_apple_silicon=is_apple_silicon,
        cpu_count=cpu_count,
        total_ram_gb=total_ram_gb,
        ram_tier=ram_tier,
        mps_available=mps_available,
        cuda_available=cuda_available,
        coreml_available=coreml_available,
        installed_backends=installed_backends,
    )


def _compute_sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


class ModelManager:
    """Managed lifecycle controller for local Whisper & SAM AI models."""

    def __init__(
        self,
        model_dir: str | Path | None = None,
        *,
        hardware: HardwareCapabilities | None = None,
    ) -> None:
        self.model_dir: Path = resolve_model_dir(model_dir)
        self.hardware: HardwareCapabilities = hardware or detect_hardware_capabilities()
        self._loaded_models: set[str] = set()
        self._executed_models: set[str] = set()

    @staticmethod
    def get_catalog_entry(model_id: str) -> ModelCatalogEntry:
        if model_id not in _CATALOG_BY_ID:
            known = ", ".join(sorted(_CATALOG_BY_ID))
            raise ModelNotFoundError(f"Unknown model_id '{model_id}'. Available: {known}")
        return _CATALOG_BY_ID[model_id]

    @staticmethod
    def list_catalog(*, family: str | None = None) -> list[ModelCatalogEntry]:
        if family is None:
            return list(MODEL_CATALOG)
        return [entry for entry in MODEL_CATALOG if entry.family == family]

    def _safe_model_target_dir(self, model_id: str) -> Path:
        if not _SAFE_MODEL_ID_RE.fullmatch(model_id):
            raise ModelManagerError(f"Invalid model_id '{model_id}'")
        root = self.model_dir.resolve()
        target = (root / model_id).resolve()
        if target.parent != root:
            raise ModelManagerError(
                f"Refusing path outside managed model directory for '{model_id}'"
            )
        return target

    def _inspect_disk_install(
        self, entry: ModelCatalogEntry
    ) -> tuple[bool, Path | None, int, str | None]:
        target_dir = self._safe_model_target_dir(entry.model_id)
        if not target_dir.is_dir():
            return False, None, 0, None

        manifest_path = target_dir / "manifest.json"
        artifact_path = target_dir / entry.artifact_filename
        manifest_sha: str | None = None
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest_sha = manifest.get("sha256")
            except (OSError, json.JSONDecodeError):
                manifest_sha = None

        # Consider weights available if the declared artifact file or non-empty weight files exist
        weight_files = [
            p
            for p in target_dir.rglob("*")
            if p.is_file() and p.name != "manifest.json" and p.stat().st_size > 0
        ]
        if artifact_path.is_file() and artifact_path.stat().st_size > 0:
            return True, artifact_path, _dir_size_bytes(target_dir), manifest_sha
        if weight_files:
            return True, target_dir, _dir_size_bytes(target_dir), manifest_sha
        return False, None, _dir_size_bytes(target_dir), manifest_sha

    def _is_backend_installed(self, runtime_backend: str) -> bool:
        installed = self.hardware.installed_backends
        if runtime_backend == "sam2":
            return bool(installed.get("sam2") and installed.get("torch"))
        if runtime_backend == "openai_whisper":
            return bool(installed.get("openai_whisper") and installed.get("torch"))
        return bool(installed.get(runtime_backend, False))

    def get_model_status(self, model_id: str) -> ModelRuntimeStatus:
        """Return honest runtime status separating package, disk weights, loaded, and executed state."""
        entry = self.get_catalog_entry(model_id)
        pkg_installed = self._is_backend_installed(entry.runtime_backend)
        on_disk, install_path, disk_bytes, installed_sha = self._inspect_disk_install(entry)
        is_loaded = model_id in self._loaded_models and on_disk and pkg_installed
        executed = model_id in self._executed_models

        if is_loaded:
            status = "loaded"
        elif pkg_installed and on_disk:
            status = "ready"
        elif pkg_installed and not on_disk:
            status = "weights_missing"
        elif on_disk and not pkg_installed:
            status = "backend_missing"
        else:
            status = "not_installed"

        ram_ok = self.hardware.total_ram_gb + 0.5 >= float(entry.min_ram_gb)
        accel_ok = (
            entry.cpu_compatible
            or (entry.mps_compatible and self.hardware.mps_available)
            or (entry.coreml_compatible and self.hardware.coreml_available)
        )
        hardware_compatible = ram_ok and accel_ok

        notes_parts: list[str] = []
        if not ram_ok:
            notes_parts.append(
                f"Requires >= {entry.min_ram_gb} GB RAM (detected {self.hardware.total_ram_gb:.1f} GB)."
            )
        if not pkg_installed:
            notes_parts.append(f"Runtime backend '{entry.runtime_backend}' is not installed.")
        if not on_disk:
            notes_parts.append("Model weights are not downloaded in BANGONPRO_MODEL_DIR.")
        if not notes_parts:
            notes_parts.append("Backend package and weights are installed locally.")

        recommended = hardware_compatible and (
            (self.hardware.is_apple_silicon and entry.recommended_for_apple_silicon)
            or (
                not self.hardware.is_apple_silicon
                and entry.model_id in ("faster-whisper-small", "mobilesam-onnx")
            )
        )

        return ModelRuntimeStatus(
            model_id=entry.model_id,
            display_name=entry.display_name,
            family=entry.family,
            runtime_backend=entry.runtime_backend,
            license_spdx=entry.license_spdx,
            ram_tier=entry.ram_tier,
            min_ram_gb=entry.min_ram_gb,
            disk_size_mb=entry.disk_size_mb,
            mps_compatible=entry.mps_compatible,
            coreml_compatible=entry.coreml_compatible,
            cpu_compatible=entry.cpu_compatible,
            backend_package_installed=pkg_installed,
            model_available_on_disk=on_disk,
            model_loaded=is_loaded,
            actual_inference_executed=executed,
            status=status,
            hardware_compatible=hardware_compatible,
            recommended=recommended,
            compatibility_notes=" ".join(notes_parts),
            install_path=str(install_path) if install_path is not None else None,
            disk_usage_bytes=disk_bytes,
            installed_sha256=installed_sha,
        )

    def list_models(self, *, family: str | None = None) -> list[ModelRuntimeStatus]:
        return [
            self.get_model_status(entry.model_id)
            for entry in self.list_catalog(family=family)
        ]

    def resolve_installed_model_path(self, model_id: str) -> Path | None:
        entry = self.get_catalog_entry(model_id)
        on_disk, install_path, _, _ = self._inspect_disk_install(entry)
        return install_path if on_disk else None

    def mark_loaded(self, model_id: str) -> None:
        self.get_catalog_entry(model_id)
        self._loaded_models.add(model_id)

    def unload_model(self, model_id: str) -> None:
        self._loaded_models.discard(model_id)

    def mark_inference_executed(self, model_id: str) -> None:
        self.get_catalog_entry(model_id)
        self._executed_models.add(model_id)

    def install_model(
        self,
        model_id: str,
        *,
        source_path: str | Path | None = None,
        source_url: str | None = None,
        expected_sha256: str | None = None,
        progress_callback: ModelProgressCallback | None = None,
        cancellation_token: CancellationToken | None = None,
        chunk_size_bytes: int = 65536,
        timeout_sec: float = 60.0,
    ) -> dict[str, Any]:
        """Install a catalog model into ``BANGONPRO_MODEL_DIR`` with progress, cancellation & SHA-256 verification."""
        entry = self.get_catalog_entry(model_id)
        if cancellation_token is not None:
            cancellation_token.check(f"before installing model '{model_id}'")

        target_dir = self._safe_model_target_dir(model_id)
        staging_root = (self.model_dir / ".staging").resolve()
        staging_root.mkdir(parents=True, exist_ok=True)
        staging_file = staging_root / f"{model_id}.{entry.artifact_filename}.part"
        expected_hash = (expected_sha256 or entry.sha256 or "").strip().lower() or None

        def _emit_progress(done: int, total: int, msg: str) -> None:
            if progress_callback is not None:
                frac = min(1.0, max(0.0, (done / total) if total > 0 else 0.0))
                progress_callback(round(frac, 4), done, total, msg)

        hasher = hashlib.sha256()
        bytes_written = 0
        total_bytes = 0

        try:
            if source_path is not None:
                src = Path(source_path).expanduser().resolve()
                if not src.is_file():
                    raise FileNotFoundError(f"Model source file not found: {src}")
                total_bytes = src.stat().st_size
                _emit_progress(0, total_bytes, f"Staging local weights for {model_id}")
                with src.open("rb") as in_fh, staging_file.open("wb") as out_fh:
                    while True:
                        if cancellation_token is not None:
                            cancellation_token.check(f"installing model '{model_id}'")
                        chunk = in_fh.read(max(1024, int(chunk_size_bytes)))
                        if not chunk:
                            break
                        out_fh.write(chunk)
                        hasher.update(chunk)
                        bytes_written += len(chunk)
                        _emit_progress(
                            bytes_written,
                            total_bytes,
                            f"Copying {entry.artifact_filename} ({bytes_written}/{total_bytes} bytes)",
                        )
            else:
                url = (source_url or entry.download_url).strip()
                parsed = urllib.parse.urlparse(url)
                if parsed.scheme != "https":
                    raise ModelManagerError(
                        f"Refusing insecure model download URL scheme '{parsed.scheme}'; only https:// is permitted."
                    )
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "BangOnPro-ModelManager/1.2.0"},
                )
                with urllib.request.urlopen(req, timeout=timeout_sec) as resp:  # noqa: S310
                    content_len = resp.headers.get("Content-Length")
                    total_bytes = int(content_len) if content_len and content_len.isdigit() else 0
                    _emit_progress(0, total_bytes, f"Downloading {model_id} from {parsed.netloc}")
                    with staging_file.open("wb") as out_fh:
                        while True:
                            if cancellation_token is not None:
                                cancellation_token.check(f"downloading model '{model_id}'")
                            chunk = resp.read(max(1024, int(chunk_size_bytes)))
                            if not chunk:
                                break
                            out_fh.write(chunk)
                            hasher.update(chunk)
                            bytes_written += len(chunk)
                            _emit_progress(
                                bytes_written,
                                total_bytes,
                                f"Downloading {entry.artifact_filename} ({bytes_written} bytes)",
                            )

            actual_sha256 = hasher.hexdigest().lower()
            if expected_hash is not None and actual_sha256 != expected_hash:
                raise ModelChecksumError(
                    f"SHA-256 verification failed for '{model_id}': "
                    f"expected {expected_hash}, got {actual_sha256}"
                )

            if cancellation_token is not None:
                cancellation_token.check(f"finalizing model '{model_id}'")

            target_dir.mkdir(parents=True, exist_ok=True)
            final_artifact = target_dir / entry.artifact_filename
            shutil.move(str(staging_file), str(final_artifact))
            companions_installed, companions_missing = self._install_companions(
                entry, target_dir, source_path=source_path, source_url=source_url, timeout_sec=timeout_sec
            )

            manifest = {
                "model_id": entry.model_id,
                "display_name": entry.display_name,
                "family": entry.family,
                "runtime_backend": entry.runtime_backend,
                "license_spdx": entry.license_spdx,
                "artifact_filename": entry.artifact_filename,
                "sha256": actual_sha256,
                "checksum_verified": expected_hash is not None,
                "size_bytes": bytes_written,
                "installed_at_utc": datetime.now(UTC).isoformat(),
                "companion_files": companions_installed,
                "companion_files_missing": companions_missing,
                "runtime_loadable": not companions_missing,
            }
            (target_dir / "manifest.json").write_text(
                json.dumps(manifest, indent=2) + "\n",
                encoding="utf-8",
            )
            _emit_progress(bytes_written, max(total_bytes, bytes_written), f"Installed {model_id}")

            return {
                "installed": True,
                "model_id": entry.model_id,
                "install_path": str(final_artifact),
                "size_bytes": bytes_written,
                "sha256": actual_sha256,
                "checksum_verified": expected_hash is not None,
                "status": self.get_model_status(model_id).to_dict(),
            }
        except (PipelineCancelledError, ModelChecksumError, OSError, ModelManagerError):
            staging_file.unlink(missing_ok=True)
            raise

    def _install_companions(
        self,
        entry: ModelCatalogEntry,
        target_dir: Path,
        *,
        source_path: str | Path | None,
        source_url: str | None,
        timeout_sec: float,
    ) -> tuple[list[str], list[str]]:
        """Fetch companion files next to the artifact (local sibling copy, else HTTPS from the artifact's base URL)."""
        installed: list[str] = []
        missing: list[str] = []
        for name in entry.companion_files:
            dest = target_dir / name
            try:
                if source_path is not None:
                    sibling = Path(source_path).expanduser().resolve().parent / name
                    if not sibling.is_file():
                        missing.append(name)
                        continue
                    shutil.copyfile(sibling, dest)
                else:
                    base = (source_url or entry.download_url).strip().rsplit("/", 1)[0]
                    url = f"{base}/{name}"
                    if urllib.parse.urlparse(url).scheme != "https":
                        missing.append(name)
                        continue
                    req = urllib.request.Request(url, headers={"User-Agent": "BangOnPro-ModelManager/1.2.0"})
                    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:  # noqa: S310 - https enforced above
                        dest.write_bytes(resp.read())
                if dest.stat().st_size <= 0:
                    dest.unlink(missing_ok=True)
                    missing.append(name)
                    continue
                installed.append(name)
            except OSError:
                missing.append(name)
        return installed, missing

    def remove_model(self, model_id: str) -> dict[str, Any]:
        """Safely remove a managed model directory from ``BANGONPRO_MODEL_DIR`` without touching user media/projects."""
        self.get_catalog_entry(model_id)
        target_dir = self._safe_model_target_dir(model_id)
        self._loaded_models.discard(model_id)

        if not target_dir.exists():
            return {
                "removed": False,
                "model_id": model_id,
                "freed_bytes": 0,
                "message": f"Model '{model_id}' is not installed on disk.",
            }

        freed_bytes = _dir_size_bytes(target_dir)
        shutil.rmtree(target_dir)
        return {
            "removed": True,
            "model_id": model_id,
            "freed_bytes": freed_bytes,
            "message": f"Removed managed model '{model_id}' ({freed_bytes} bytes freed).",
        }

    def get_storage_report(self) -> dict[str, Any]:
        """Return structured disk usage and runtime availability report across all catalog models."""
        statuses = self.list_models()
        total_bytes = sum(s.disk_usage_bytes for s in statuses)
        installed_count = sum(1 for s in statuses if s.model_available_on_disk)
        ready_count = sum(1 for s in statuses if s.status in ("ready", "loaded"))
        return {
            "model_dir": str(self.model_dir),
            "hardware": self.hardware.to_dict(),
            "total_bytes": total_bytes,
            "total_mb": round(total_bytes / (1024.0 * 1024.0), 3),
            "installed_count": installed_count,
            "ready_count": ready_count,
            "models": [s.to_dict() for s in statuses],
        }
