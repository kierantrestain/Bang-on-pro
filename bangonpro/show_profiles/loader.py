"""Bang On Pro Show Profile loader and JSON Schema validator.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
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

    cam_ids = [cam["camera_id"] for cam in profile["cameras"]]
    if len(cam_ids) != len(set(cam_ids)):
        raise ShowProfileValidationError("cameras contains duplicate camera_id values")

    return profile


def list_show_profiles() -> list[str]:
    """Return sorted profile IDs of all built-in Show Profiles."""
    profiles_dir = get_profiles_dir()
    return sorted(p.stem for p in profiles_dir.glob("*.json"))


def resolve_show_profile_path(profile_ref: str | Path) -> Path:
    """Resolve a profile ID, filename, or filesystem path to a JSON file."""
    candidate = Path(profile_ref)
    if candidate.is_file():
        return candidate.resolve()

    profiles_dir = get_profiles_dir()
    name = str(profile_ref)
    if not name.endswith(".json"):
        by_id = profiles_dir / f"{name}.json"
        if by_id.is_file():
            return by_id.resolve()

    by_name = profiles_dir / name
    if by_name.is_file():
        return by_name.resolve()

    available = ", ".join(list_show_profiles())
    raise FileNotFoundError(
        f"Show profile '{profile_ref}' not found. Available built-in profiles: {available}"
    )


def load_show_profile(profile_ref: str | Path) -> dict[str, Any]:
    """Load and validate a Show Profile by built-in ID or JSON file path."""
    path = resolve_show_profile_path(profile_ref)
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ShowProfileValidationError(f"Invalid JSON in show profile '{path}': {exc}") from exc

    return validate_show_profile(data)


def get_profile_fps(profile: dict[str, Any]) -> float:
    """Return exact floating-point frame rate of a validated Show Profile."""
    spec = profile["video_spec"]
    return float(spec["fps_num"]) / float(spec["fps_den"])
