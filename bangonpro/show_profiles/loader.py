"""Bang On Pro Show Profile loader and JSON Schema validator.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


class ShowProfileValidationError(ValueError):
    """Raised when a Show Profile fails JSON Schema or semantic validation."""


def get_profiles_dir() -> Path:
    """Return directory containing built-in Show Profile JSON files."""
    return Path(__file__).resolve().parent


def get_show_profile_schema_path() -> Path:
    """Return path to ``bangonpro/schemas/show_profile_v1.schema.json``."""
    return Path(__file__).resolve().parent.parent / "schemas" / "show_profile_v1.schema.json"


@lru_cache(maxsize=1)
def _get_validator() -> Draft202012Validator:
    schema_path = get_show_profile_schema_path()
    with schema_path.open("r", encoding="utf-8") as fh:
        schema = json.load(fh)
    return Draft202012Validator(schema)


def _normalize_custom_dirs(
    custom_dirs: Iterable[str | Path] | str | Path | None,
) -> list[Path]:
    if custom_dirs is None:
        return []
    if isinstance(custom_dirs, (str, Path)):
        raw_list = [custom_dirs]
    else:
        raw_list = list(custom_dirs)
    resolved: list[Path] = []
    for item in raw_list:
        p = Path(item).expanduser()
        if p.is_dir():
            resolved.append(p.resolve())
    return resolved


def validate_show_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Validate a Show Profile dictionary against ``show_profile_v1.schema.json`` and semantic rules."""
    if not isinstance(profile, dict):
        raise ShowProfileValidationError("Show profile must be a JSON object.")

    validator = _get_validator()
    errors = sorted(validator.iter_errors(profile), key=lambda err: list(err.path))
    if errors:
        formatted = "; ".join(
            f"{'.'.join(str(p) for p in err.path) or '<root>'}: {err.message}"
            for err in errors
        )
        raise ShowProfileValidationError(f"Show profile schema validation failed: {formatted}")

    # Semantic checks beyond JSON Schema structural rules
    editing_rules = profile["editing_rules"]
    if editing_rules["max_shot_sec"] < editing_rules["min_shot_sec"]:
        raise ShowProfileValidationError(
            "editing_rules.max_shot_sec must be >= editing_rules.min_shot_sec"
        )

    interruption_gap = editing_rules.get("interruption_gap_sec")
    if interruption_gap is not None and float(interruption_gap) < float(editing_rules["min_silence_sec"]):
        raise ShowProfileValidationError(
            "editing_rules.interruption_gap_sec must be >= editing_rules.min_silence_sec"
        )

    audio_rules = profile.get("audio_rules", {})
    if "target_lufs" in audio_rules:
        lufs = float(audio_rules["target_lufs"])
        if lufs < -40.0 or lufs > -6.0:
            raise ShowProfileValidationError(
                f"audio_rules.target_lufs ({lufs}) must be between -40.0 and -6.0 LUFS"
            )
    if "true_peak_dbtp" in audio_rules:
        tp = float(audio_rules["true_peak_dbtp"])
        if tp < -12.0 or tp > 0.0:
            raise ShowProfileValidationError(
                f"audio_rules.true_peak_dbtp ({tp}) must be between -12.0 and 0.0 dBTP"
            )

    cam_ids = [cam["camera_id"] for cam in profile["cameras"]]
    if len(cam_ids) != len(set(cam_ids)):
        raise ShowProfileValidationError("cameras contains duplicate camera_id values")

    return profile


def list_show_profiles(
    custom_dirs: Iterable[str | Path] | str | Path | None = None,
) -> list[str]:
    """Return sorted profile IDs of all built-in and optional custom Show Profiles."""
    profile_ids: set[str] = {p.stem for p in get_profiles_dir().glob("*.json")}
    for cdir in _normalize_custom_dirs(custom_dirs):
        for path in cdir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                pid = str(data.get("profile_id") or path.stem).strip()
                if pid:
                    profile_ids.add(pid)
            except (OSError, json.JSONDecodeError):
                continue
    return sorted(profile_ids)


def resolve_show_profile_path(
    profile_ref: str | Path,
    *,
    custom_dirs: Iterable[str | Path] | str | Path | None = None,
) -> Path:
    """Resolve a profile ID, filename, or filesystem path to a JSON file."""
    candidate = Path(profile_ref)
    if candidate.is_file():
        return candidate.resolve()

    search_dirs = [get_profiles_dir(), *_normalize_custom_dirs(custom_dirs)]
    name = str(profile_ref)
    for directory in search_dirs:
        if not name.endswith(".json"):
            by_id = directory / f"{name}.json"
            if by_id.is_file():
                return by_id.resolve()
        by_name = directory / name
        if by_name.is_file():
            return by_name.resolve()

    # Fallback: scan custom_dirs for matching profile_id field inside JSON
    for directory in _normalize_custom_dirs(custom_dirs):
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("profile_id") == name:
                    return path.resolve()
            except (OSError, json.JSONDecodeError):
                continue

    available = ", ".join(list_show_profiles(custom_dirs=custom_dirs))
    raise FileNotFoundError(
        f"Show profile '{profile_ref}' not found. Available profiles: {available}"
    )


def load_show_profile(
    profile_ref: str | Path,
    *,
    custom_dirs: Iterable[str | Path] | str | Path | None = None,
) -> dict[str, Any]:
    """Load and validate a Show Profile by built-in/custom ID or JSON file path."""
    path = resolve_show_profile_path(profile_ref, custom_dirs=custom_dirs)
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ShowProfileValidationError(f"Invalid JSON in show profile '{path}': {exc}") from exc

    return validate_show_profile(data)


def create_custom_profile(
    *,
    profile_id: str,
    display_name: str,
    format_type: str = "custom",
    base_profile: str | Path | dict[str, Any] | None = None,
    description: str | None = None,
    video_spec: dict[str, Any] | None = None,
    cameras: list[dict[str, Any]] | None = None,
    camera_preferences: dict[str, Any] | None = None,
    editing_rules: dict[str, Any] | None = None,
    audio_rules: dict[str, Any] | None = None,
    graphics_rules: dict[str, Any] | None = None,
    export_presets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create and strictly validate a custom Show Profile dictionary.

    Can optionally inherit from a built-in/custom ``base_profile`` and override
    camera preferences, pacing, silence/interruption thresholds, graphics,
    audio LUFS/restoration rules, and export presets.
    """
    if base_profile is not None:
        if isinstance(base_profile, dict):
            profile = copy.deepcopy(base_profile)
        else:
            profile = copy.deepcopy(load_show_profile(base_profile))
    else:
        profile = {
            "schema_version": "1.0",
            "profile_id": profile_id,
            "display_name": display_name,
            "format_type": format_type,
            "video_spec": {
                "width": 1920,
                "height": 1080,
                "fps_num": 25,
                "fps_den": 1,
                "color_space": "bt709",
            },
            "cameras": [
                {"camera_id": "cam_wide", "role": "wide", "speaker_label": None, "priority": 1},
                {"camera_id": "cam_host", "role": "host", "speaker_label": "Host", "priority": 2},
                {"camera_id": "cam_guest", "role": "guest", "speaker_label": "Guest", "priority": 2},
            ],
            "camera_preferences": {
                "prefer_wide_on_overlap": True,
                "default_opening_role": "wide",
                "avoid_jump_cuts": True,
            },
            "editing_rules": {
                "min_shot_sec": 2.0,
                "max_shot_sec": 10.0,
                "reaction_cut_interval_sec": 5.0,
                "silence_threshold_db": -36.0,
                "min_silence_sec": 0.7,
                "interruption_gap_sec": 10.0,
                "excise_interruption_gaps": True,
                "overlap_cut_to_wide": True,
                "keep_padding_sec": 0.2,
                "trim_setup_delay": True,
                "review_confidence_threshold": 0.72,
            },
            "audio_rules": {
                "target_lufs": -16.0,
                "true_peak_dbtp": -1.5,
                "highpass_hz": 80.0,
                "enable_denoise": True,
                "enable_compressor": True,
                "enable_mic_level_matching": True,
                "max_mic_trim_db": 24.0,
                "music_duck_db": -18.0,
            },
            "graphics_rules": {
                "enable_captions": True,
                "enable_lower_thirds": True,
                "lower_third_duration_sec": 4.5,
                "brand_accent_hex": "#FF5A1F",
            },
            "export_presets": [
                {
                    "name": "master_1080p",
                    "container": "mp4",
                    "video_codec": "libx264",
                    "audio_codec": "aac",
                    "video_bitrate": "12M",
                    "audio_bitrate": "192k",
                }
            ],
        }

    profile["schema_version"] = "1.0"
    profile["profile_id"] = profile_id
    profile["display_name"] = display_name
    profile["format_type"] = format_type
    if description is not None:
        profile["description"] = description

    if video_spec is not None:
        profile["video_spec"] = {**profile.get("video_spec", {}), **copy.deepcopy(video_spec)}
    if cameras is not None:
        profile["cameras"] = copy.deepcopy(cameras)
    if camera_preferences is not None:
        profile["camera_preferences"] = {
            **profile.get("camera_preferences", {}),
            **copy.deepcopy(camera_preferences),
        }
    if editing_rules is not None:
        profile["editing_rules"] = {
            **profile.get("editing_rules", {}),
            **copy.deepcopy(editing_rules),
        }
    if audio_rules is not None:
        profile["audio_rules"] = {
            **profile.get("audio_rules", {}),
            **copy.deepcopy(audio_rules),
        }
    if graphics_rules is not None:
        profile["graphics_rules"] = {
            **profile.get("graphics_rules", {}),
            **copy.deepcopy(graphics_rules),
        }
    if export_presets is not None:
        profile["export_presets"] = copy.deepcopy(export_presets)

    return validate_show_profile(profile)


def save_custom_profile(profile: dict[str, Any], dest_path: str | Path) -> Path:
    """Validate a custom Show Profile strictly against schema and write it to disk."""
    validated = validate_show_profile(profile)
    target = Path(dest_path).expanduser()
    if target.is_dir():
        target = target / f"{validated['profile_id']}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(validated, indent=2) + "\n", encoding="utf-8")
    return target.resolve()


def get_profile_fps(profile: dict[str, Any]) -> float:
    """Return exact floating-point frame rate of a validated Show Profile."""
    spec = profile["video_spec"]
    return float(spec["fps_num"]) / float(spec["fps_den"])
