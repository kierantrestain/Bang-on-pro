"""Data models and constants for Bang On Pro Premiere Preset Compatibility Engine.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

# Adobe Premiere Pro internal timebase: 254,016,000,000 ticks per second
# (LCM of 24, 25, 29.97, 30, 48, 50, 59.94, 60, 44100, 48000, 96000)
PR_TICKS_PER_SECOND: int = 254_016_000_000

# Premiere Pro negative sentinel tick used in <StartKeyframe> for static defaults
PR_SENTINEL_TICK_THRESHOLD: int = -10_000_000_000_000

SCHEMA_VERSION: str = "1.0"


class CompatibilityStatus(str, Enum):
    """Honest compatibility classification for a Premiere preset or effect."""

    EXACT_VALIDATED = "exact_validated"
    APPROXIMATED = "approximated"
    UNSUPPORTED = "unsupported"
    MISSING_DEPENDENCY = "missing_dependency"


class InterpolationMode(str, Enum):
    """Normalized keyframe interpolation mode."""

    LINEAR = "linear"
    HOLD = "hold"
    BEZIER = "bezier"
    EASE_IN = "ease_in"
    EASE_OUT = "ease_out"


@dataclass
class Keyframe:
    """Single keyframe converted from Premiere Pro ticks."""

    raw_ticks: int
    time_seconds: float
    frame: int
    value: Union[float, Tuple[float, ...], str]
    interpolation: InterpolationMode = InterpolationMode.LINEAR
    bezier_handles: Optional[Tuple[float, float, float, float]] = None
    is_sentinel: bool = False

    def to_dict(self) -> Dict[str, Any]:
        val: Any = list(self.value) if isinstance(self.value, tuple) else self.value
        data: Dict[str, Any] = {
            "raw_ticks": self.raw_ticks,
            "time_seconds": round(self.time_seconds, 6),
            "frame": self.frame,
            "value": val,
            "interpolation": self.interpolation.value,
        }
        if self.bezier_handles is not None:
            data["bezier_handles"] = [round(v, 6) for v in self.bezier_handles]
        if self.is_sentinel:
            data["is_sentinel"] = True
        return data


@dataclass
class Parameter:
    """A single parameter on a Premiere effect component."""

    name: str
    match_name: str = ""
    param_type: str = "scalar"
    current_value: Any = None
    start_value: Any = None
    keyframes: List[Keyframe] = field(default_factory=list)
    raw_arb_data: str = ""
    is_animated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        val = list(self.current_value) if isinstance(self.current_value, tuple) else self.current_value
        start = list(self.start_value) if isinstance(self.start_value, tuple) else self.start_value
        data: Dict[str, Any] = {
            "name": self.name,
            "match_name": self.match_name,
            "param_type": self.param_type,
            "current_value": val,
            "is_animated": self.is_animated,
            "keyframe_count": len(self.keyframes),
        }
        if start is not None:
            data["start_value"] = start
        if self.keyframes:
            data["keyframes"] = [kf.to_dict() for kf in self.keyframes]
        return data


@dataclass
class EffectComponent:
    """A single VideoFilterComponent or AudioFilterComponent inside a preset."""

    match_name: str
    display_name: str
    media_type: str = "video"  # "video" or "audio"
    bypass: bool = False
    parameters: List[Parameter] = field(default_factory=list)
    object_id: str = ""

    def param_map(self) -> Dict[str, Parameter]:
        """Case-insensitive lookup map by parameter name and match_name."""
        out: Dict[str, Parameter] = {}
        for p in self.parameters:
            if p.name:
                out[p.name.strip().lower()] = p
            if p.match_name:
                out[p.match_name.strip().lower()] = p
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "match_name": self.match_name,
            "display_name": self.display_name,
            "media_type": self.media_type,
            "bypass": self.bypass,
            "parameters": [p.to_dict() for p in self.parameters],
        }


@dataclass
class MissingDependency:
    """An unresolved external LUT, font, codec, or proprietary/platform plugin."""

    kind: str  # "lut", "font", "plugin", "audio_unit", "codec", "ir_audio"
    name: str
    effect_match_name: str = ""
    platform: str = "cross_platform"  # "cross_platform", "macos_only", "third_party"
    path: str = ""
    resolved: bool = False
    remediation: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "kind": self.kind,
            "name": self.name,
            "effect_match_name": self.effect_match_name,
            "platform": self.platform,
            "resolved": self.resolved,
            "remediation": self.remediation,
        }
        if self.path:
            data["path"] = self.path
        return data


@dataclass
class MltFilterSpec:
    """Single MLT filter specification emitted for a converted effect."""

    service: str
    media_type: str = "video"  # "video" or "audio"
    properties: Dict[str, str] = field(default_factory=dict)
    source_effect: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "service": self.service,
            "media_type": self.media_type,
            "properties": dict(self.properties),
            "source_effect": self.source_effect,
        }
        if self.notes:
            data["notes"] = list(self.notes)
        return data


@dataclass
class ParsedPreset:
    """Raw structured preset extracted from a .prfpset file before mapping."""

    preset_id: str
    name: str
    pack: str
    source_file: str
    folder_path: List[str] = field(default_factory=list)
    attribution: str = ""
    description: str = ""
    preset_type: str = "scale"  # "scale", "anchor_in", "anchor_out", "unknown"
    effects: List[EffectComponent] = field(default_factory=list)
    original_xml: str = ""
    original_xml_preserved: bool = True
    fps: float = 25.0


@dataclass
class MappedEffectResult:
    """Result of mapping a single Premiere EffectComponent to MLT."""

    component: EffectComponent
    status: CompatibilityStatus
    mlt_filters: List[MltFilterSpec] = field(default_factory=list)
    missing_dependencies: List[MissingDependency] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    mapped_services: List[str] = field(default_factory=list)

    def to_chain_entry(self) -> Dict[str, Any]:
        entry = self.component.to_dict()
        entry["compatibility_status"] = self.status.value
        entry["mapped_mlt_services"] = list(self.mapped_services)
        if self.notes:
            entry["notes"] = list(self.notes)
        return entry


@dataclass
class MappedPreset:
    """Fully mapped preset record conforming to premiere_preset_report_v1.schema.json."""

    preset_id: str
    name: str
    pack: str
    attribution: str
    folder_path: List[str]
    source_file: str
    preset_type: str
    compatibility_status: CompatibilityStatus
    notes: List[str] = field(default_factory=list)
    effect_chain: List[Dict[str, Any]] = field(default_factory=list)
    missing_dependencies: List[MissingDependency] = field(default_factory=list)
    mlt_filters: List[MltFilterSpec] = field(default_factory=list)
    kdenlive_effect_xml: Optional[str] = None
    original_xml_preserved: bool = True
    original_xml: str = ""
    description: str = ""
    category: str = ""
    tags: List[str] = field(default_factory=list)
    preview_before_path: Optional[str] = None
    preview_after_path: Optional[str] = None
    preview_split_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "preset_id": self.preset_id,
            "name": self.name,
            "pack": self.pack,
            "attribution": self.attribution,
            "folder_path": list(self.folder_path),
            "source_file": self.source_file,
            "preset_type": self.preset_type,
            "compatibility_status": self.compatibility_status.value,
            "notes": list(self.notes),
            "effect_chain": list(self.effect_chain),
            "missing_dependencies": [d.to_dict() for d in self.missing_dependencies],
            "mlt_filters": [f.to_dict() for f in self.mlt_filters],
            "kdenlive_effect_xml": self.kdenlive_effect_xml,
            "original_xml_preserved": self.original_xml_preserved,
        }
        if self.original_xml:
            data["original_xml"] = self.original_xml
        if self.description:
            data["description"] = self.description
        if self.category:
            data["category"] = self.category
        if self.tags:
            data["tags"] = list(self.tags)
        if self.preview_before_path is not None:
            data["preview_before_path"] = self.preview_before_path
        if self.preview_after_path is not None:
            data["preview_after_path"] = self.preview_after_path
        if self.preview_split_path is not None:
            data["preview_split_path"] = self.preview_split_path
        return data


@dataclass
class SourceArchiveInfo:
    """Metadata and integrity hash of the imported preset archive/source."""

    name: str
    sha256: str
    file_count: int
    redistribution_restricted: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "sha256": self.sha256,
            "file_count": self.file_count,
            "redistribution_restricted": self.redistribution_restricted,
        }


@dataclass
class MissingSourceInput:
    """Structured blocker record emitted when proprietary source inputs are absent."""

    input_name: str
    expected_path: str
    reason: str
    remediation: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_name": self.input_name,
            "expected_path": self.expected_path,
            "reason": self.reason,
            "remediation": self.remediation,
        }


@dataclass
class CompatibilityReport:
    """Complete compatibility report matching premiere_preset_report_v1.schema.json."""

    schema_version: str
    generated_at: str
    source_archive: SourceArchiveInfo
    presets: List[MappedPreset]
    audit_reconciliation: Optional[Dict[str, Any]] = None
    missing_source_inputs: List[MissingSourceInput] = field(default_factory=list)

    def summary(self) -> Dict[str, int]:
        distinct_effects = set()
        counts = {
            CompatibilityStatus.EXACT_VALIDATED: 0,
            CompatibilityStatus.APPROXIMATED: 0,
            CompatibilityStatus.UNSUPPORTED: 0,
            CompatibilityStatus.MISSING_DEPENDENCY: 0,
        }
        for preset in self.presets:
            counts[preset.compatibility_status] += 1
            for eff in preset.effect_chain:
                mn = str(eff.get("match_name", "")).strip()
                if mn:
                    distinct_effects.add(mn)
        return {
            "total_presets": len(self.presets),
            "distinct_effects": len(distinct_effects),
            "exact_validated": counts[CompatibilityStatus.EXACT_VALIDATED],
            "approximated": counts[CompatibilityStatus.APPROXIMATED],
            "unsupported": counts[CompatibilityStatus.UNSUPPORTED],
            "missing_dependency": counts[CompatibilityStatus.MISSING_DEPENDENCY],
        }

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "source_archive": self.source_archive.to_dict(),
            "summary": self.summary(),
            "presets": [p.to_dict() for p in self.presets],
        }
        if self.audit_reconciliation is not None:
            data["audit_reconciliation"] = self.audit_reconciliation
        if self.missing_source_inputs:
            data["missing_source_inputs"] = [m.to_dict() for m in self.missing_source_inputs]
        return data
