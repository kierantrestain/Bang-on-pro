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


def default_custom_profiles_dir() -> Path:
    """Where editors' custom profiles live (``$BANGONPRO_PROFILES_DIR`` overrides)."""
    import os

    env = os.environ.get("BANGONPRO_PROFILES_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".local" / "share" / "bangonpro" / "profiles").resolve()


def user_profile_dirs() -> list[Path]:
    """Custom profile folders searched after the built-in profiles (only existing ones)."""
    d = default_custom_profiles_dir()
    return [d] if d.is_dir() else []


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
    raw_list = [custom_dirs] if isinstance(custom_dirs, (str, Path)) else list(custom_dirs)
    resolved: list[Path] = []
    for item in raw_list:
        p = Path(item).expanduser()
        if p.is_dir():
            resolved.append(p.resolve())
    return resolved


def get_effective_profile_settings(profile: dict[str, Any]) -> dict[str, Any]:
    """Resolve effective V1.1 production profile rules with full v1.0 fallback compatibility."""
    editing = profile.get("editing_rules", {})
    audio = profile.get("audio_rules", {})
    graphics = profile.get("graphics_rules", {})
    video = profile.get("video_spec", {})

    camera_roles: dict[str, str] = {}
    speaker_camera_map: dict[str, str] = {}
    for cam in profile.get("cameras", []):
        cid = str(cam["camera_id"])
        role = str(cam["role"])
        camera_roles[cid] = role
        spk = cam.get("speaker_label")
        if spk and str(spk) not in speaker_camera_map and role not in ("wide", "broll", "reaction"):
            speaker_camera_map[str(spk)] = cid
    # Second pass for reaction/desk speakers if not yet mapped
    for cam in profile.get("cameras", []):
        cid = str(cam["camera_id"])
        spk = cam.get("speaker_label")
        if spk and str(spk) not in speaker_camera_map:
            speaker_camera_map[str(spk)] = cid

    # Explicit profile camera_roles / speaker_camera_map take priority
    if isinstance(profile.get("camera_roles"), dict):
        for cid, role in profile["camera_roles"].items():
            camera_roles[str(cid)] = str(role)
    if isinstance(profile.get("speaker_camera_map"), dict):
        for spk, cid in profile["speaker_camera_map"].items():
            speaker_camera_map[str(spk)] = str(cid)

    min_shot = float(
        profile.get(
            "min_shot_duration_sec",
            editing.get("min_shot_duration_sec", editing.get("min_shot_sec", 2.0)),
        )
    )
    max_shot = float(
        profile.get(
            "max_shot_duration_sec",
            editing.get("max_shot_duration_sec", editing.get("max_shot_sec", 10.0)),
        )
    )
    wide_interval = float(
        profile.get(
            "wide_shot_interval_sec",
            editing.get("wide_shot_interval_sec", max(max_shot * 1.5, 14.0)),
        )
    )
    reaction_freq = float(
        profile.get(
            "reaction_shot_frequency",
            editing.get("reaction_shot_frequency", 0.5),
        )
    )
    silence_thresh = float(
        profile.get(
            "silence_threshold_db",
            editing.get("silence_threshold_db", -36.0),
        )
    )
    loudness_lufs = float(
        profile.get(
            "loudness_target_lufs",
            audio.get(
                "loudness_target_lufs",
                audio.get(
                    "target_lufs",
                    profile.get("delivery_spec", {}).get("target_lufs", -16.0),
                ),
            ),
        )
    )

    intro_outro_defaults = {
        "open_on_wide": True,
        "opening_wide_sec": 2.0,
        "close_on_wide": True,
        "closing_wide_sec": 2.0,
        "intro_asset": None,
        "outro_asset": None,
        "fade_in_sec": 0.0,
        "fade_out_sec": 0.5,
    }
    intro_outro = {**intro_outro_defaults, **(profile.get("intro_outro_rules") or {})}

    accent_hex = str(graphics.get("brand_accent_hex", "#FF5A1F"))
    graphics_template_defaults = {
        "lower_third_template": "studio_clean_bar",
        "lower_third_position": "bottom_left",
        "title_card_template": "studio_opener",
        "bug_position": "top_right",
        "font_family": "DejaVu Sans",
        "accent_hex": accent_hex,
    }
    graphics_templates = {
        **graphics_template_defaults,
        **(profile.get("graphics_templates") or {}),
    }

    caption_style_defaults = {
        "font_family": "DejaVu Sans",
        "font_size_px": max(24, round(int(video.get("height", 1080)) * 0.044)),
        "primary_color_hex": "#FFFFFF",
        "highlight_color_hex": accent_hex,
        "outline_color_hex": "#121212",
        "outline_width": 3.0,
        "alignment": "bottom_center",
        "max_chars_per_line": 42,
        "uppercase": False,
        "burn_in": False,
    }
    caption_style = {**caption_style_defaults, **(profile.get("caption_style") or {})}

    first_preset = (profile.get("export_presets") or [{}])[0]
    delivery_defaults = {
        "container": str(first_preset.get("container", "mp4")),
        "video_codec": str(first_preset.get("video_codec", "libx264")),
        "audio_codec": str(first_preset.get("audio_codec", "aac")),
        "width": int(video.get("width", 1920)),
        "height": int(video.get("height", 1080)),
        "fps_num": int(video.get("fps_num", 25)),
        "fps_den": int(video.get("fps_den", 1)),
        "color_space": str(video.get("color_space", "bt709")),
        "audio_sample_rate": 48000,
        "audio_channels": 2,
        "target_lufs": loudness_lufs,
        "true_peak_dbtp": float(audio.get("true_peak_dbtp", -1.5)),
        "video_bitrate": str(first_preset.get("video_bitrate", "12M")),
        "audio_bitrate": str(first_preset.get("audio_bitrate", "192k")),
    }
    delivery_spec = {**delivery_defaults, **(profile.get("delivery_spec") or {})}

    return {
        "camera_roles": camera_roles,
        "speaker_camera_map": speaker_camera_map,
        "wide_shot_interval_sec": wide_interval,
        "reaction_shot_frequency": reaction_freq,
        "min_shot_duration_sec": min_shot,
        "max_shot_duration_sec": max_shot,
        "silence_threshold_db": silence_thresh,
        "intro_outro_rules": intro_outro,
        "graphics_templates": graphics_templates,
        "loudness_target_lufs": loudness_lufs,
        "caption_style": caption_style,
        "delivery_spec": delivery_spec,
    }


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

    min_dur = profile.get("min_shot_duration_sec", editing_rules.get("min_shot_duration_sec"))
    max_dur = profile.get("max_shot_duration_sec", editing_rules.get("max_shot_duration_sec"))
    if min_dur is not None and max_dur is not None and float(max_dur) < float(min_dur):
        raise ShowProfileValidationError(
            "max_shot_duration_sec must be >= min_shot_duration_sec"
        )

    eff_min = float(min_dur) if min_dur is not None else float(editing_rules["min_shot_sec"])
    wide_interval = profile.get(
        "wide_shot_interval_sec", editing_rules.get("wide_shot_interval_sec")
    )
    if wide_interval is not None and float(wide_interval) < eff_min:
        raise ShowProfileValidationError(
            "wide_shot_interval_sec must be >= min_shot_duration_sec"
        )

    reaction_freq = profile.get(
        "reaction_shot_frequency", editing_rules.get("reaction_shot_frequency")
    )
    if reaction_freq is not None and not (0.0 <= float(reaction_freq) <= 1.0):
        raise ShowProfileValidationError(
            "reaction_shot_frequency must be between 0.0 and 1.0"
        )

    interruption_gap = editing_rules.get("interruption_gap_sec")
    if interruption_gap is not None and float(interruption_gap) < float(
        editing_rules["min_silence_sec"]
    ):
        raise ShowProfileValidationError(
            "editing_rules.interruption_gap_sec must be >= editing_rules.min_silence_sec"
        )

    audio_rules = profile.get("audio_rules", {})
    for lufs_key, lufs_val in (
        ("audio_rules.target_lufs", audio_rules.get("target_lufs")),
        ("audio_rules.loudness_target_lufs", audio_rules.get("loudness_target_lufs")),
        ("loudness_target_lufs", profile.get("loudness_target_lufs")),
        ("delivery_spec.target_lufs", (profile.get("delivery_spec") or {}).get("target_lufs")),
    ):
        if lufs_val is not None:
            lufs = float(lufs_val)
            if lufs < -40.0 or lufs > -6.0:
                raise ShowProfileValidationError(
                    f"{lufs_key} ({lufs}) must be between -40.0 and -6.0 LUFS"
                )

    for tp_key, tp_val in (
        ("audio_rules.true_peak_dbtp", audio_rules.get("true_peak_dbtp")),
        (
            "delivery_spec.true_peak_dbtp",
            (profile.get("delivery_spec") or {}).get("true_peak_dbtp"),
        ),
    ):
        if tp_val is not None:
            tp = float(tp_val)
            if tp < -12.0 or tp > 0.0:
                raise ShowProfileValidationError(
                    f"{tp_key} ({tp}) must be between -12.0 and 0.0 dBTP"
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
    camera_roles: dict[str, str] | None = None,
    speaker_camera_map: dict[str, str] | None = None,
    camera_preferences: dict[str, Any] | None = None,
    wide_shot_interval_sec: float | None = None,
    reaction_shot_frequency: float | None = None,
    min_shot_duration_sec: float | None = None,
    max_shot_duration_sec: float | None = None,
    silence_threshold_db: float | None = None,
    loudness_target_lufs: float | None = None,
    intro_outro_rules: dict[str, Any] | None = None,
    editing_rules: dict[str, Any] | None = None,
    audio_rules: dict[str, Any] | None = None,
    graphics_rules: dict[str, Any] | None = None,
    graphics_templates: dict[str, Any] | None = None,
    caption_style: dict[str, Any] | None = None,
    delivery_spec: dict[str, Any] | None = None,
    export_presets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create and strictly validate a custom Show Profile dictionary.

    Can optionally inherit from a built-in/custom ``base_profile`` and override
    camera roles/maps, pacing, silence/interruption thresholds, intro/outro rules,
    graphics templates, caption styling, loudness/restoration rules, and delivery specs.
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
            "camera_roles": {
                "cam_wide": "wide",
                "cam_host": "host",
                "cam_guest": "guest",
            },
            "speaker_camera_map": {
                "Host": "cam_host",
                "Guest": "cam_guest",
            },
            "camera_preferences": {
                "prefer_wide_on_overlap": True,
                "default_opening_role": "wide",
                "avoid_jump_cuts": True,
            },
            "wide_shot_interval_sec": 16.0,
            "reaction_shot_frequency": 0.5,
            "min_shot_duration_sec": 2.0,
            "max_shot_duration_sec": 10.0,
            "silence_threshold_db": -36.0,
            "loudness_target_lufs": -16.0,
            "intro_outro_rules": {
                "open_on_wide": True,
                "opening_wide_sec": 2.0,
                "close_on_wide": True,
                "closing_wide_sec": 2.0,
                "intro_asset": None,
                "outro_asset": None,
                "fade_in_sec": 0.0,
                "fade_out_sec": 0.5,
            },
            "editing_rules": {
                "min_shot_sec": 2.0,
                "max_shot_sec": 10.0,
                "min_shot_duration_sec": 2.0,
                "max_shot_duration_sec": 10.0,
                "wide_shot_interval_sec": 16.0,
                "reaction_shot_frequency": 0.5,
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
                "loudness_target_lufs": -16.0,
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
            "graphics_templates": {
                "lower_third_template": "studio_clean_bar",
                "lower_third_position": "bottom_left",
                "title_card_template": "studio_opener",
                "bug_position": "top_right",
                "font_family": "DejaVu Sans",
                "accent_hex": "#FF5A1F",
            },
            "caption_style": {
                "font_family": "DejaVu Sans",
                "font_size_px": 48,
                "primary_color_hex": "#FFFFFF",
                "highlight_color_hex": "#FF5A1F",
                "outline_color_hex": "#121212",
                "outline_width": 3.0,
                "alignment": "bottom_center",
                "max_chars_per_line": 42,
                "uppercase": False,
                "burn_in": False,
            },
            "delivery_spec": {
                "container": "mp4",
                "video_codec": "libx264",
                "audio_codec": "aac",
                "width": 1920,
                "height": 1080,
                "fps_num": 25,
                "fps_den": 1,
                "color_space": "bt709",
                "audio_sample_rate": 48000,
                "audio_channels": 2,
                "target_lufs": -16.0,
                "true_peak_dbtp": -1.5,
                "video_bitrate": "12M",
                "audio_bitrate": "192k",
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
        profile["camera_roles"] = {
            str(cam["camera_id"]): str(cam["role"]) for cam in profile["cameras"]
        }
        profile["speaker_camera_map"] = {
            str(cam["speaker_label"]): str(cam["camera_id"])
            for cam in profile["cameras"]
            if cam.get("speaker_label")
        }
    if camera_roles is not None:
        profile["camera_roles"] = {
            **profile.get("camera_roles", {}),
            **copy.deepcopy(camera_roles),
        }
    if speaker_camera_map is not None:
        profile["speaker_camera_map"] = {
            **profile.get("speaker_camera_map", {}),
            **copy.deepcopy(speaker_camera_map),
        }
    if camera_preferences is not None:
        profile["camera_preferences"] = {
            **profile.get("camera_preferences", {}),
            **copy.deepcopy(camera_preferences),
        }
    if intro_outro_rules is not None:
        profile["intro_outro_rules"] = {
            **profile.get("intro_outro_rules", {}),
            **copy.deepcopy(intro_outro_rules),
        }
    if editing_rules is not None:
        merged_editing = {**profile.get("editing_rules", {}), **copy.deepcopy(editing_rules)}
        # Sync min/max shot duration aliases if caller only passed one form
        if "min_shot_sec" in editing_rules and "min_shot_duration_sec" not in editing_rules:
            merged_editing["min_shot_duration_sec"] = merged_editing["min_shot_sec"]
            profile["min_shot_duration_sec"] = merged_editing["min_shot_sec"]
        elif "min_shot_duration_sec" in editing_rules and "min_shot_sec" not in editing_rules:
            merged_editing["min_shot_sec"] = merged_editing["min_shot_duration_sec"]
            profile["min_shot_duration_sec"] = merged_editing["min_shot_duration_sec"]
        if "max_shot_sec" in editing_rules and "max_shot_duration_sec" not in editing_rules:
            merged_editing["max_shot_duration_sec"] = merged_editing["max_shot_sec"]
            profile["max_shot_duration_sec"] = merged_editing["max_shot_sec"]
        elif "max_shot_duration_sec" in editing_rules and "max_shot_sec" not in editing_rules:
            merged_editing["max_shot_sec"] = merged_editing["max_shot_duration_sec"]
            profile["max_shot_duration_sec"] = merged_editing["max_shot_duration_sec"]
        if "silence_threshold_db" in editing_rules:
            profile["silence_threshold_db"] = merged_editing["silence_threshold_db"]
        if "wide_shot_interval_sec" in editing_rules:
            profile["wide_shot_interval_sec"] = merged_editing["wide_shot_interval_sec"]
        if "reaction_shot_frequency" in editing_rules:
            profile["reaction_shot_frequency"] = merged_editing["reaction_shot_frequency"]
        profile["editing_rules"] = merged_editing

    if min_shot_duration_sec is not None:
        profile["min_shot_duration_sec"] = float(min_shot_duration_sec)
        profile["editing_rules"]["min_shot_sec"] = float(min_shot_duration_sec)
        profile["editing_rules"]["min_shot_duration_sec"] = float(min_shot_duration_sec)
    if max_shot_duration_sec is not None:
        profile["max_shot_duration_sec"] = float(max_shot_duration_sec)
        profile["editing_rules"]["max_shot_sec"] = float(max_shot_duration_sec)
        profile["editing_rules"]["max_shot_duration_sec"] = float(max_shot_duration_sec)
    if wide_shot_interval_sec is not None:
        profile["wide_shot_interval_sec"] = float(wide_shot_interval_sec)
        profile["editing_rules"]["wide_shot_interval_sec"] = float(wide_shot_interval_sec)
    if reaction_shot_frequency is not None:
        profile["reaction_shot_frequency"] = float(reaction_shot_frequency)
        profile["editing_rules"]["reaction_shot_frequency"] = float(reaction_shot_frequency)
    if silence_threshold_db is not None:
        profile["silence_threshold_db"] = float(silence_threshold_db)
        profile["editing_rules"]["silence_threshold_db"] = float(silence_threshold_db)

    if audio_rules is not None:
        merged_audio = {**profile.get("audio_rules", {}), **copy.deepcopy(audio_rules)}
        if "target_lufs" in audio_rules and "loudness_target_lufs" not in audio_rules:
            merged_audio["loudness_target_lufs"] = merged_audio["target_lufs"]
            profile["loudness_target_lufs"] = merged_audio["target_lufs"]
        elif "loudness_target_lufs" in audio_rules and "target_lufs" not in audio_rules:
            merged_audio["target_lufs"] = merged_audio["loudness_target_lufs"]
            profile["loudness_target_lufs"] = merged_audio["loudness_target_lufs"]
        profile["audio_rules"] = merged_audio

    if loudness_target_lufs is not None:
        profile["loudness_target_lufs"] = float(loudness_target_lufs)
        profile["audio_rules"]["target_lufs"] = float(loudness_target_lufs)
        profile["audio_rules"]["loudness_target_lufs"] = float(loudness_target_lufs)
        if "delivery_spec" in profile:
            profile["delivery_spec"]["target_lufs"] = float(loudness_target_lufs)

    if graphics_rules is not None:
        profile["graphics_rules"] = {
            **profile.get("graphics_rules", {}),
            **copy.deepcopy(graphics_rules),
        }
    if graphics_templates is not None:
        profile["graphics_templates"] = {
            **profile.get("graphics_templates", {}),
            **copy.deepcopy(graphics_templates),
        }
    if caption_style is not None:
        profile["caption_style"] = {
            **profile.get("caption_style", {}),
            **copy.deepcopy(caption_style),
        }
    if delivery_spec is not None:
        profile["delivery_spec"] = {
            **profile.get("delivery_spec", {}),
            **copy.deepcopy(delivery_spec),
        }
    if export_presets is not None:
        profile["export_presets"] = copy.deepcopy(export_presets)

    return validate_show_profile(profile)


def save_custom_profile(profile: dict[str, Any], dest_path: str | Path) -> Path:
    """Validate a custom Show Profile strictly against schema and write it to disk."""
    validated = validate_show_profile(profile)
    target = Path(dest_path).expanduser()
    # A folder that does not exist yet (no .json suffix) is a folder, not a file name.
    if target.is_dir() or (not target.exists() and target.suffix.lower() != ".json"):
        target = target / f"{validated['profile_id']}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(validated, indent=2) + "\n", encoding="utf-8")
    return target.resolve()


def get_profile_fps(profile: dict[str, Any]) -> float:
    """Return exact floating-point frame rate of a validated Show Profile."""
    spec = profile["video_spec"]
    return float(spec["fps_num"]) / float(spec["fps_den"])
