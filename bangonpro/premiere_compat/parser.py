"""Hardened Adobe Premiere Pro (.prfpset) archive & XML parser.

Supports:
- Single .prfpset XML files, directories of .prfpset files, and .zip preset packs
- Real Premiere Pro <PremiereData Version="3"> ObjectID / ObjectRef graph traversal
- <BinTreeItem>, <FilterPresetItem>, <VideoFilterComponent>, <AudioFilterComponent>,
  <VideoComponentParam>, <AudioComponentParam>, <ArbVideoComponentParam>, <PointComponentParam>
- Accurate Premiere tick conversion (254,016,000,000 ticks/second)
- Premiere comma/semicolon keyframe lists with linear, hold, ease-in, ease-out, and Bezier handles
- Bounded & sanitized raw XML preservation for every preset
- Optional cross-checking against Bang_on_pro_premiere_preset_audit.json

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import logging
import re
import xml.etree.ElementTree as StdET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element

from .models import (
    PR_SENTINEL_TICK_THRESHOLD,
    PR_TICKS_PER_SECOND,
    EffectComponent,
    InterpolationMode,
    Keyframe,
    Parameter,
    ParsedPreset,
    SourceArchiveInfo,
)
from .security import (
    MAX_COMPRESSION_RATIO,
    MAX_SINGLE_FILE_BYTES,
    ArchiveLimitError,
    ArchiveSecurityLimits,
    ZipBombError,
    compute_sha256_bytes,
    compute_sha256_file,
    safe_parse_xml,
    safe_read_zip_entries,
    sanitize_xml_snippet,
)

logger = logging.getLogger(__name__)

# Sidecar extensions recognized as bundled asset files (LUTs, fonts, IR audio)
BUNDLED_ASSET_EXTENSIONS = {
    ".cube",
    ".look",
    ".3dl",
    ".itx",
    ".ttf",
    ".otf",
    ".ttc",
    ".woff",
    ".woff2",
    ".wav",
    ".aif",
    ".aiff",
}

_PRESET_TYPE_MAP = {
    "0": "scale",
    "scale": "scale",
    "1": "anchor_in",
    "anchor_in": "anchor_in",
    "anchorin": "anchor_in",
    "anchortoinpoint": "anchor_in",
    "anchor to in point": "anchor_in",
    "2": "anchor_out",
    "anchor_out": "anchor_out",
    "anchorout": "anchor_out",
    "anchortooutpoint": "anchor_out",
    "anchor to out point": "anchor_out",
}

_INTERP_CODE_MAP: dict[str, InterpolationMode] = {
    "0": InterpolationMode.LINEAR,
    "1": InterpolationMode.EASE_IN,
    "2": InterpolationMode.EASE_OUT,
    "3": InterpolationMode.BEZIER,
    "4": InterpolationMode.HOLD,
    "5": InterpolationMode.BEZIER,
    "6": InterpolationMode.BEZIER,
    "8": InterpolationMode.LINEAR,
    "linear": InterpolationMode.LINEAR,
    "hold": InterpolationMode.HOLD,
    "discrete": InterpolationMode.HOLD,
    "step": InterpolationMode.HOLD,
    "bezier": InterpolationMode.BEZIER,
    "smooth": InterpolationMode.BEZIER,
    "ease_in": InterpolationMode.EASE_IN,
    "easein": InterpolationMode.EASE_IN,
    "ease_out": InterpolationMode.EASE_OUT,
    "easeout": InterpolationMode.EASE_OUT,
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class ParsedSourceBundle:
    """Result of ingesting a ZIP archive, directory, or single .prfpset file."""

    archive_info: SourceArchiveInfo
    presets: list[ParsedPreset] = field(default_factory=list)
    bundled_assets: dict[str, str] = field(default_factory=dict)  # lowercase basename -> virtual/real path
    audit_data: dict[str, Any] | None = None
    parse_warnings: list[str] = field(default_factory=list)


def ticks_to_seconds(ticks: int) -> float:
    """Convert Adobe Premiere Pro ticks (254,016,000,000 ticks/sec) to seconds."""
    return float(ticks) / float(PR_TICKS_PER_SECOND)


def ticks_to_frame(ticks: int, fps: float = 25.0) -> int:
    """Convert Adobe Premiere Pro ticks to nearest integer frame at `fps`."""
    if fps <= 0:
        raise ValueError(f"fps must be positive, got {fps}")
    return round(ticks_to_seconds(ticks) * fps)


def _slugify(text: str) -> str:
    slug = _SLUG_RE.sub("_", text.lower()).strip("_")
    return slug or "preset"


def _make_preset_id(pack: str, folder_path: list[str], name: str, index: int) -> str:
    raw = f"{pack}|{'/'.join(folder_path)}|{name}|{index}"
    short_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{_slugify(pack)[:24]}__{_slugify(name)[:36]}__{short_hash}"


def _parse_scalar_or_point(token: str) -> float | tuple[float, ...] | str:
    """Parse a Premiere keyframe value token (float, colon-separated 2D/4D point, or string)."""
    cleaned = token.strip()
    if ":" in cleaned:
        parts = cleaned.split(":")
        try:
            return tuple(float(p.strip()) for p in parts)
        except ValueError:
            return cleaned
    try:
        return float(cleaned)
    except ValueError:
        return cleaned


def _decode_interp_mode(token: str) -> InterpolationMode:
    key = token.strip().lower()
    return _INTERP_CODE_MAP.get(key, InterpolationMode.LINEAR)


def parse_premiere_keyframes(
    raw_text: str,
    fps: float = 25.0,
    is_point: bool = False,
) -> list[Keyframe]:
    """Parse Premiere Pro semicolon/comma-separated keyframe string into Keyframe objects.

    Supports formats observed across real Premiere Pro .prfpset exports:
    - Scalar: `914457600000,100.0,0,0,0,0.166667,0,0.166667;...`
    - Colon-point: `914457600000,0.5:0.5,5,0,0.2,0.3,0.4,0.5;...`
    - Comma-point (when is_point=True and no colon): `914457600000,960.0,540.0,5,0,0.2,0.3,0.4,0.5;...`
    - Sentinel start keyframe: `-91445760000000000,100.0,0,0,0,0,0,0`
    """
    if not raw_text or not raw_text.strip():
        return []

    keyframes: list[Keyframe] = []
    entries = [seg.strip() for seg in raw_text.strip().split(";") if seg.strip()]

    for entry in entries:
        fields = [f.strip() for f in entry.split(",")]
        if len(fields) < 2:
            continue

        try:
            raw_ticks = int(float(fields[0]))
        except ValueError:
            continue

        is_sentinel = raw_ticks <= PR_SENTINEL_TICK_THRESHOLD

        # Detect point format: either colon-separated `x:y` in fields[1], or comma-separated `x,y`
        val: float | tuple[float, ...] | str
        interp_idx = 2
        if ":" in fields[1]:
            val = _parse_scalar_or_point(fields[1])
            interp_idx = 2
        elif is_point and len(fields) >= 4:
            # Check if fields[1] and fields[2] are both floats while fields[3] is an interp token
            try:
                x_val = float(fields[1])
                y_val = float(fields[2])
                val = (x_val, y_val)
                interp_idx = 3
            except ValueError:
                val = _parse_scalar_or_point(fields[1])
                interp_idx = 2
        else:
            val = _parse_scalar_or_point(fields[1])
            interp_idx = 2

        interp = InterpolationMode.LINEAR
        if len(fields) > interp_idx:
            interp = _decode_interp_mode(fields[interp_idx])

        # Premiere Bezier handles typically appear at indices [interp+2 .. interp+5]
        # (fields[interp+1] is flags/tangent mode)
        bezier_handles: tuple[float, float, float, float] | None = None
        handle_start = interp_idx + 2
        if len(fields) >= handle_start + 4:
            try:
                h = tuple(float(fields[handle_start + i]) for i in range(4))
                if any(abs(v) > 1e-9 for v in h) or interp in (
                    InterpolationMode.BEZIER,
                    InterpolationMode.EASE_IN,
                    InterpolationMode.EASE_OUT,
                ):
                    bezier_handles = (h[0], h[1], h[2], h[3])
            except ValueError:
                bezier_handles = None

        time_sec = 0.0 if is_sentinel else ticks_to_seconds(raw_ticks)
        frame = 0 if is_sentinel else ticks_to_frame(raw_ticks, fps=fps)

        keyframes.append(
            Keyframe(
                raw_ticks=raw_ticks,
                time_seconds=time_sec,
                frame=frame,
                value=val,
                interpolation=interp,
                bezier_handles=bezier_handles,
                is_sentinel=is_sentinel,
            )
        )

    return keyframes


def _normalize_preset_keyframes(effects: list[EffectComponent], fps: float) -> None:
    """Rebase non-sentinel keyframe times relative to the earliest keyframe tick in the preset."""
    non_sentinel_ticks: list[int] = []
    for eff in effects:
        for param in eff.parameters:
            for kf in param.keyframes:
                if not kf.is_sentinel:
                    non_sentinel_ticks.append(kf.raw_ticks)

    if not non_sentinel_ticks:
        return

    base_tick = min(non_sentinel_ticks)
    for eff in effects:
        for param in eff.parameters:
            for kf in param.keyframes:
                if kf.is_sentinel:
                    kf.time_seconds = 0.0
                    kf.frame = 0
                else:
                    delta_ticks = kf.raw_ticks - base_tick
                    kf.time_seconds = ticks_to_seconds(delta_ticks)
                    kf.frame = ticks_to_frame(delta_ticks, fps=fps)


def _first_text(elem: Element, paths: list[str]) -> str:
    """Return stripped text of the first matching child path, or empty string."""
    for p in paths:
        node = elem.find(p)
        if node is not None and node.text and node.text.strip():
            return node.text.strip()
    return ""


def _build_object_index(root: Element) -> tuple[dict[str, Element], dict[str, Element]]:
    """Index all XML elements by ObjectID and ObjectUID for fast O(1) ObjectRef resolution."""
    by_id: dict[str, Element] = {}
    by_uid: dict[str, Element] = {}
    for elem in root.iter():
        oid = elem.attrib.get("ObjectID")
        if oid:
            by_id[oid] = elem
        ouid = elem.attrib.get("ObjectUID")
        if ouid:
            by_uid[ouid] = elem
    return by_id, by_uid


def _resolve_ref(
    ref_elem: Element | None,
    by_id: dict[str, Element],
    by_uid: dict[str, Element],
) -> Element | None:
    if ref_elem is None:
        return None
    oid = ref_elem.attrib.get("ObjectRef")
    if oid and oid in by_id:
        return by_id[oid]
    ouid = ref_elem.attrib.get("ObjectURef")
    if ouid and ouid in by_uid:
        return by_uid[ouid]
    return None


def _decode_arb_data(raw_text: str) -> str:
    """Best-effort safe decoding of ArbVideoComponentParam base64/text payloads.

    Extracts readable UTF-8/UTF-16 strings (e.g., LUT filenames or font names) without
    ever executing content.
    """
    text = raw_text.strip()
    if not text:
        return ""
    # If already contains readable hints (e.g. .cube, .look, font, XML), keep it directly
    if any(tok in text.lower() for tok in (".cube", ".look", ".3dl", ".ttf", ".otf", "font", "lut")):
        return text[:4096]
    # Try base64 decoding to reveal embedded UTF-8 or UTF-16LE strings
    compact = re.sub(r"\s+", "", text)
    if len(compact) >= 8 and len(compact) % 4 == 0 and re.fullmatch(r"[A-Za-z0-9+/=]+", compact):
        try:
            decoded = base64.b64decode(compact, validate=True)
            # Extract ASCII printable sequences (length >= 4) and UTF-16LE printable sequences
            ascii_strings = re.findall(rb"[\x20-\x7e]{4,}", decoded)
            utf16_strings = re.findall(rb"(?:[\x20-\x7e]\x00){4,}", decoded)
            fragments: list[str] = [s.decode("ascii", errors="ignore") for s in ascii_strings]
            fragments.extend(s.decode("utf-16le", errors="ignore") for s in utf16_strings)
            if fragments:
                return " | ".join(fragments)[:4096]
        except ValueError as exc:
            logger.debug("Arbitrary payload was not valid base64 text: %s", exc)
    return text[:4096]


def _parse_parameter(param_elem: Element, fps: float) -> Parameter:
    """Parse VideoComponentParam / AudioComponentParam / ArbVideoComponentParam / PointComponentParam."""
    tag = param_elem.tag
    is_point = "Point" in tag
    is_arb = "Arb" in tag

    name = _first_text(param_elem, ["Name", "Node/Properties/Name", "DisplayName"])
    match_name = _first_text(param_elem, ["MatchName", "ParamID", "ID"])

    param_type = "point" if is_point else ("arbitrary" if is_arb else "scalar")

    start_raw = _first_text(param_elem, ["StartKeyframe"])
    start_kfs = parse_premiere_keyframes(start_raw, fps=fps, is_point=is_point)
    start_val: Any = start_kfs[0].value if start_kfs else None

    kfs_raw = _first_text(param_elem, ["Keyframes"])
    keyframes = parse_premiere_keyframes(kfs_raw, fps=fps, is_point=is_point)

    # Also support explicit nested <Keyframe> elements in alternate Premiere exports
    if not keyframes:
        for kf_node in param_elem.findall(".//Keyframe"):
            t_str = _first_text(kf_node, ["Time", "Tick"]) or kf_node.attrib.get("time", "")
            v_str = _first_text(kf_node, ["Value"]) or kf_node.attrib.get("value", "")
            i_str = _first_text(kf_node, ["Interpolation", "Type"]) or kf_node.attrib.get("interp", "0")
            if t_str and v_str:
                parsed = parse_premiere_keyframes(f"{t_str},{v_str},{i_str}", fps=fps, is_point=is_point)
                keyframes.extend(parsed)

    cur_raw = _first_text(param_elem, ["CurrentValue", "Value", "DefaultValue"])
    current_val: Any = None
    if cur_raw:
        current_val = _parse_scalar_or_point(cur_raw)
    elif keyframes:
        current_val = keyframes[0].value
    elif start_val is not None:
        current_val = start_val

    arb_raw = _first_text(param_elem, ["ArbData", "ArbitraryData", "Data", "Value", "StringValue"])
    decoded_arb = _decode_arb_data(arb_raw) if (is_arb or arb_raw) else ""

    # If Arb param holds readable string and current_value is empty, populate current_value
    if is_arb and current_val is None and decoded_arb:
        current_val = decoded_arb

    is_time_varying_str = _first_text(param_elem, ["IsTimeVarying"]).lower()
    non_sentinel_count = sum(1 for kf in keyframes if not kf.is_sentinel)
    is_animated = non_sentinel_count >= 2 or (is_time_varying_str == "true" and non_sentinel_count >= 1)

    # Filter out sentinel keyframes from the animated list if real keyframes exist
    clean_kfs = [kf for kf in keyframes if not kf.is_sentinel]

    return Parameter(
        name=name or match_name or "UnnamedParam",
        match_name=match_name,
        param_type=param_type,
        current_value=current_val,
        start_value=start_val,
        keyframes=clean_kfs,
        raw_arb_data=decoded_arb if is_arb or any(
            ext in decoded_arb.lower() for ext in (".cube", ".look", ".3dl", ".ttf", ".otf")
        ) else "",
        is_animated=is_animated,
    )


def _parse_component(
    comp_elem: Element,
    by_id: dict[str, Element],
    by_uid: dict[str, Element],
    fps: float,
    referenced_ids: set[str],
) -> EffectComponent | None:
    """Parse a VideoFilterComponent or AudioFilterComponent (including inner <Component> ref)."""
    if comp_elem.attrib.get("ObjectID"):
        referenced_ids.add(comp_elem.attrib["ObjectID"])

    tag_lower = comp_elem.tag.lower()
    media_type = "audio" if "audio" in tag_lower else "video"

    # Premiere often nests <Component ObjectRef="..."/> pointing to the base VideoComponent/AudioComponent
    inner_comp = _resolve_ref(comp_elem.find("Component"), by_id, by_uid)
    if inner_comp is None:
        inner_comp = comp_elem.find("Component")
    if inner_comp is not None and inner_comp.attrib.get("ObjectID"):
        referenced_ids.add(inner_comp.attrib["ObjectID"])

    match_name = (
        _first_text(comp_elem, ["MatchName", "FilterMatchName", "Component/MatchName"])
        or (
            _first_text(inner_comp, ["MatchName", "FilterMatchName"])
            if inner_comp is not None
            else ""
        )
    )
    display_name = (
        _first_text(comp_elem, ["DisplayName", "Name", "Node/Properties/Name", "Component/DisplayName"])
        or (
            _first_text(inner_comp, ["DisplayName", "Name"])
            if inner_comp is not None
            else ""
        )
        or match_name
    )

    if not match_name and not display_name:
        return None

    bypass_text = (
        _first_text(comp_elem, ["Bypass", "Disabled"])
        or (_first_text(inner_comp, ["Bypass", "Disabled"]) if inner_comp is not None else "")
    ).lower()
    bypass = bypass_text in ("true", "1", "yes")

    # Collect parameters from both comp_elem and inner_comp
    param_elements: list[Element] = []
    seen_param_ids: set[int] = set()

    for container in [c for c in (comp_elem, inner_comp) if c is not None]:
        params_node = container.find("Params")
        if params_node is not None:
            for child in list(params_node):
                resolved = _resolve_ref(child, by_id, by_uid)
                target = resolved if resolved is not None else child
                if id(target) not in seen_param_ids:
                    seen_param_ids.add(id(target))
                    param_elements.append(target)
                    if target.attrib.get("ObjectID"):
                        referenced_ids.add(target.attrib["ObjectID"])
        # Direct parameter children without <Params> wrapper
        for child in list(container):
            if child.tag.endswith("ComponentParam") and id(child) not in seen_param_ids:
                seen_param_ids.add(id(child))
                param_elements.append(child)
                if child.attrib.get("ObjectID"):
                    referenced_ids.add(child.attrib["ObjectID"])

    parameters = [_parse_parameter(pe, fps=fps) for pe in param_elements]

    return EffectComponent(
        match_name=match_name or display_name,
        display_name=display_name or match_name,
        media_type=media_type,
        bypass=bypass,
        parameters=parameters,
        object_id=comp_elem.attrib.get("ObjectID", ""),
    )


def _serialize_preset_snippet(
    preset_elem: Element,
    referenced_ids: set[str],
    by_id: dict[str, Element],
) -> str:
    """Build a bounded, sanitized XML snippet preserving the preset and its referenced components."""
    parts: list[str] = ["<PremierePresetSnippet>"]
    try:
        parts.append(StdET.tostring(preset_elem, encoding="unicode"))
        preset_oid = preset_elem.attrib.get("ObjectID")
        for oid in sorted(referenced_ids):
            if oid != preset_oid and oid in by_id:
                parts.append(StdET.tostring(by_id[oid], encoding="unicode"))
    except (TypeError, ValueError) as exc:
        logger.debug("Failed to serialize original XML snippet fragment: %s", exc)
    parts.append("</PremierePresetSnippet>")
    return sanitize_xml_snippet("\n".join(parts))


def _extract_preset_from_item(
    item_elem: Element,
    by_id: dict[str, Element],
    by_uid: dict[str, Element],
    folder_path: list[str],
    pack_name: str,
    source_file: str,
    index: int,
    fps: float,
) -> ParsedPreset | None:
    """Extract a ParsedPreset from a <FilterPresetItem> (or <FilterPreset>) element."""
    referenced_ids: set[str] = set()
    if item_elem.attrib.get("ObjectID"):
        referenced_ids.add(item_elem.attrib["ObjectID"])

    # Some schemas nest <FilterPreset ObjectRef="..."/> inside <FilterPresetItem>
    inner_preset = _resolve_ref(item_elem.find("FilterPreset"), by_id, by_uid)
    if inner_preset is None:
        inner_preset = item_elem.find("FilterPreset")
    if inner_preset is not None and inner_preset.attrib.get("ObjectID"):
        referenced_ids.add(inner_preset.attrib["ObjectID"])

    name = (
        _first_text(item_elem, ["Node/Properties/Name", "Name", "DisplayName"])
        or (_first_text(inner_preset, ["Node/Properties/Name", "Name"]) if inner_preset is not None else "")
        or f"Unnamed Preset {index + 1}"
    )

    description = (
        _first_text(item_elem, ["Description", "Comment", "Node/Properties/Description"])
        or (_first_text(inner_preset, ["Description", "Comment"]) if inner_preset is not None else "")
    )
    attribution = (
        _first_text(item_elem, ["Attribution", "Author", "Creator"])
        or (_first_text(inner_preset, ["Attribution", "Author", "Creator"]) if inner_preset is not None else "")
    )

    raw_type = (
        _first_text(item_elem, ["Type", "PresetType"])
        or (_first_text(inner_preset, ["Type", "PresetType"]) if inner_preset is not None else "")
    ).lower()
    preset_type = _PRESET_TYPE_MAP.get(raw_type, "scale" if not raw_type else "unknown")

    # Collect component references from <Components>, <Filter>, <AudioFilter>, <VideoFilter>
    comp_elements: list[Element] = []
    seen_comp_ids: set[int] = set()

    for container in [c for c in (item_elem, inner_preset) if c is not None]:
        # <Components><Component ObjectRef="..."/>...</Components> or inline components
        for list_tag in ("Components", "Filters", "Effects"):
            list_node = container.find(list_tag)
            if list_node is not None:
                for child in list(list_node):
                    resolved = _resolve_ref(child, by_id, by_uid)
                    target = resolved if resolved is not None else child
                    if id(target) not in seen_comp_ids:
                        seen_comp_ids.add(id(target))
                        comp_elements.append(target)

        # Single <Filter ObjectRef="..."/> / <VideoFilterComponent> / <AudioFilterComponent>
        for single_tag in ("Filter", "VideoFilterComponent", "AudioFilterComponent", "Component"):
            for child in container.findall(single_tag):
                resolved = _resolve_ref(child, by_id, by_uid)
                target = resolved if resolved is not None else child
                if (
                    target.tag in ("VideoFilterComponent", "AudioFilterComponent")
                    or target.find("MatchName") is not None
                ) and id(target) not in seen_comp_ids:
                    seen_comp_ids.add(id(target))
                    comp_elements.append(target)

    effects: list[EffectComponent] = []
    for comp in comp_elements:
        parsed_comp = _parse_component(comp, by_id, by_uid, fps=fps, referenced_ids=referenced_ids)
        if parsed_comp is not None:
            effects.append(parsed_comp)

    _normalize_preset_keyframes(effects, fps=fps)

    xml_snippet = _serialize_preset_snippet(item_elem, referenced_ids, by_id)

    return ParsedPreset(
        preset_id=_make_preset_id(pack_name, folder_path, name, index),
        name=name,
        pack=pack_name,
        source_file=source_file,
        folder_path=list(folder_path),
        attribution=attribution,
        description=description,
        preset_type=preset_type,
        effects=effects,
        original_xml=xml_snippet,
        original_xml_preserved=True,
        fps=fps,
    )


def _walk_bin_tree(
    node: Element,
    by_id: dict[str, Element],
    by_uid: dict[str, Element],
    current_path: list[str],
    visited: set[int],
    discovered: list[tuple[Element, list[str]]],
) -> None:
    """Recursively walk Premiere <BinTreeItem> / <TreeItem> folder hierarchies."""
    if id(node) in visited:
        return
    visited.add(id(node))

    tag = node.tag
    if tag in ("FilterPresetItem", "FilterPreset"):
        discovered.append((node, list(current_path)))
        return

    next_path = list(current_path)
    if tag in ("BinTreeItem", "TreeItem", "Bin"):
        bin_name = _first_text(node, ["Node/Properties/Name", "Name"])
        # Skip Premiere synthetic root names like "Presets" only if empty
        if bin_name:
            next_path.append(bin_name)

    items_node = node.find("Items")
    if items_node is not None:
        for child in list(items_node):
            resolved = _resolve_ref(child, by_id, by_uid)
            target = resolved if resolved is not None else child
            _walk_bin_tree(target, by_id, by_uid, next_path, visited, discovered)

    # Also support direct nested BinTreeItem / FilterPresetItem elements
    for child in list(node):
        if child.tag in ("BinTreeItem", "TreeItem", "FilterPresetItem", "FilterPreset"):
            resolved = _resolve_ref(child, by_id, by_uid)
            target = resolved if resolved is not None else child
            _walk_bin_tree(target, by_id, by_uid, next_path, visited, discovered)


def _maybe_decompress_prfpset(raw_bytes: bytes) -> bytes:
    """Safely decompress gzipped .prfpset payloads with strict bomb limits."""
    if len(raw_bytes) >= 2 and raw_bytes[:2] == b"\x1f\x8b":
        comp_len = max(len(raw_bytes), 1)
        out_chunks: list[bytes] = []
        total = 0
        with gzip.GzipFile(fileobj=io.BytesIO(raw_bytes), mode="rb") as gz:
            while True:
                chunk = gz.read(65_536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_SINGLE_FILE_BYTES:
                    raise ArchiveLimitError(
                        f"Decompressed .prfpset exceeds single-file limit ({total} > {MAX_SINGLE_FILE_BYTES})"
                    )
                if total > 4096 and (total / comp_len) > MAX_COMPRESSION_RATIO:
                    raise ZipBombError(
                        f"Gzipped .prfpset exceeds max compression ratio ({total / comp_len:.1f}x)"
                    )
                out_chunks.append(chunk)
        return b"".join(out_chunks)
    return raw_bytes


def parse_prfpset_bytes(
    raw_bytes: bytes,
    source_file: str,
    pack_name: str | None = None,
    fps: float = 25.0,
) -> list[ParsedPreset]:
    """Parse a single Premiere Pro .prfpset XML document into ParsedPreset records."""
    xml_bytes = _maybe_decompress_prfpset(raw_bytes)
    root = safe_parse_xml(xml_bytes)

    resolved_pack = pack_name or Path(source_file).stem
    by_id, by_uid = _build_object_index(root)

    # Find root BinTreeItems (those NOT referenced by another BinTreeItem's <Items>)
    referenced_bins: set[str] = set()
    for bin_elem in root.iter():
        if bin_elem.tag in ("BinTreeItem", "TreeItem", "Bin"):
            items_node = bin_elem.find("Items")
            if items_node is not None:
                for child in list(items_node):
                    ref = child.attrib.get("ObjectRef")
                    if ref:
                        referenced_bins.add(ref)

    discovered: list[tuple[Element, list[str]]] = []
    visited: set[int] = set()

    # Walk from top-level / unreferenced BinTreeItems first to preserve full folder_path
    for elem in list(root):
        if elem.tag in ("BinTreeItem", "TreeItem", "Bin"):
            oid = elem.attrib.get("ObjectID")
            if not oid or oid not in referenced_bins:
                _walk_bin_tree(elem, by_id, by_uid, [], visited, discovered)

    # Also capture any FilterPresetItem elements not reached from a BinTreeItem
    discovered_ids = {id(item) for item, _ in discovered}
    for elem in root.iter():
        if elem.tag == "FilterPresetItem" and id(elem) not in discovered_ids:
            discovered.append((elem, []))
            discovered_ids.add(id(elem))

    # Fallback: standalone <FilterPreset> if no <FilterPresetItem> exists
    if not discovered:
        for elem in root.iter():
            if elem.tag == "FilterPreset" and id(elem) not in discovered_ids:
                discovered.append((elem, []))
                discovered_ids.add(id(elem))

    presets: list[ParsedPreset] = []
    for idx, (preset_elem, folder_path) in enumerate(discovered):
        parsed = _extract_preset_from_item(
            item_elem=preset_elem,
            by_id=by_id,
            by_uid=by_uid,
            folder_path=folder_path,
            pack_name=resolved_pack,
            source_file=source_file,
            index=idx,
            fps=fps,
        )
        if parsed is not None:
            presets.append(parsed)

    return presets


def load_audit_json(audit_path: str | Path) -> dict[str, Any]:
    """Load an optional Bang_on_pro_premiere_preset_audit.json for count & attribution cross-checks."""
    path = Path(audit_path)
    raw = path.read_bytes()
    if len(raw) > MAX_SINGLE_FILE_BYTES:
        raise ArchiveLimitError(f"Audit JSON exceeds single-file limit: {len(raw)}")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise TypeError("Audit JSON root must be a JSON object")
    return data


def _apply_audit_metadata(presets: list[ParsedPreset], audit_data: dict[str, Any]) -> None:
    """Enrich preset attribution and pack metadata from optional audit JSON."""
    pack_meta: dict[str, dict[str, Any]] = {}
    for entry in audit_data.get("packs", []) or []:
        if isinstance(entry, dict):
            key = str(entry.get("name") or entry.get("file") or "").strip().lower()
            if key:
                pack_meta[key] = entry
            file_key = str(entry.get("source_file") or entry.get("filename") or "").strip().lower()
            if file_key:
                pack_meta[file_key] = entry

    preset_meta: dict[str, dict[str, Any]] = {}
    for entry in audit_data.get("presets", []) or []:
        if isinstance(entry, dict):
            pname = str(entry.get("name", "")).strip().lower()
            if pname:
                preset_meta[pname] = entry

    default_attr = str(audit_data.get("default_attribution", "")).strip()

    for preset in presets:
        pm = preset_meta.get(preset.name.strip().lower())
        if pm:
            if not preset.attribution and pm.get("attribution"):
                preset.attribution = str(pm["attribution"])
            if pm.get("pack"):
                preset.pack = str(pm["pack"])

        src_base = Path(preset.source_file).name.lower()
        pack_entry = pack_meta.get(preset.pack.strip().lower()) or pack_meta.get(src_base)
        if pack_entry:
            if not preset.attribution and pack_entry.get("attribution"):
                preset.attribution = str(pack_entry["attribution"])
            if pack_entry.get("pack_name"):
                preset.pack = str(pack_entry["pack_name"])

        if not preset.attribution and default_attr:
            preset.attribution = default_attr


def parse_premiere_input(
    input_path: str | Path,
    audit_json_path: str | Path | None = None,
    fps: float = 25.0,
    limits: ArchiveSecurityLimits | None = None,
    redistribution_restricted: bool = True,
) -> ParsedSourceBundle:
    """Ingest a .zip archive, a directory of .prfpset files, or a single .prfpset file."""
    limits = limits or ArchiveSecurityLimits()
    path = Path(input_path)
    if not path.exists():
        raise FileNotFoundError(f"Input path does not exist: {path}")

    presets: list[ParsedPreset] = []
    bundled_assets: dict[str, str] = {}
    warnings: list[str] = []
    file_count = 0

    if path.is_file() and path.suffix.lower() == ".zip":
        sha256 = compute_sha256_file(path)
        entries = safe_read_zip_entries(path, limits=limits)
        file_count = len(entries)

        for member_path, data in sorted(entries.items()):
            ext = Path(member_path).suffix.lower()
            if ext in BUNDLED_ASSET_EXTENSIONS:
                bundled_assets[Path(member_path).name.lower()] = member_path
            elif ext in (".prfpset", ".xml"):
                pack_name = Path(member_path).stem
                parsed = parse_prfpset_bytes(
                    data,
                    source_file=member_path,
                    pack_name=pack_name,
                    fps=fps,
                )
                presets.extend(parsed)
    elif path.is_file():
        raw = path.read_bytes()
        if len(raw) > limits.max_single_file_bytes:
            raise ArchiveLimitError(
                f"Input file exceeds single-file byte limit ({len(raw)} > {limits.max_single_file_bytes})"
            )
        sha256 = compute_sha256_bytes(raw)
        file_count = 1
        presets.extend(
            parse_prfpset_bytes(raw, source_file=path.name, pack_name=path.stem, fps=fps)
        )
    elif path.is_dir():
        digest = hashlib.sha256()
        all_files = sorted(p for p in path.rglob("*") if p.is_file() and not p.is_symlink())
        if len(all_files) > limits.max_files:
            raise ArchiveLimitError(
                f"Directory exceeds maximum file count ({len(all_files)} > {limits.max_files})"
            )
        file_count = len(all_files)
        total_bytes = 0
        for fpath in all_files:
            rel = fpath.relative_to(path).as_posix()
            size = fpath.stat().st_size
            total_bytes += size
            if size > limits.max_single_file_bytes:
                raise ArchiveLimitError(f"File {rel!r} exceeds single-file byte limit")
            if total_bytes > limits.max_total_uncompressed_bytes:
                raise ArchiveLimitError("Directory total bytes exceeds limit")
            raw = fpath.read_bytes()
            digest.update(rel.encode("utf-8"))
            digest.update(raw)
            ext = fpath.suffix.lower()
            if ext in BUNDLED_ASSET_EXTENSIONS:
                bundled_assets[fpath.name.lower()] = str(fpath)
            elif ext in (".prfpset", ".xml"):
                parsed = parse_prfpset_bytes(
                    raw,
                    source_file=rel,
                    pack_name=fpath.stem,
                    fps=fps,
                )
                presets.extend(parsed)
        sha256 = digest.hexdigest()
    else:
        raise ValueError(f"Unsupported input path type: {path}")

    audit_data: dict[str, Any] | None = None
    if audit_json_path is not None:
        audit_data = load_audit_json(audit_json_path)
        _apply_audit_metadata(presets, audit_data)

    archive_info = SourceArchiveInfo(
        name=path.name,
        sha256=sha256,
        file_count=file_count,
        redistribution_restricted=redistribution_restricted,
    )
    return ParsedSourceBundle(
        archive_info=archive_info,
        presets=presets,
        bundled_assets=bundled_assets,
        audit_data=audit_data,
        parse_warnings=warnings,
    )
