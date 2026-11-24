"""Preset-level mapping, Kdenlive XML export, MLT XML generation, and schema-validated reporting.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import datetime as dt
import json
import xml.etree.ElementTree as StdET
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

import jsonschema

from .catalog import MappingContext, map_effect_component
from .models import (
    SCHEMA_VERSION,
    CompatibilityReport,
    CompatibilityStatus,
    MappedEffectResult,
    MappedPreset,
    MissingDependency,
    MissingSourceInput,
    MltFilterSpec,
    ParsedPreset,
    SourceArchiveInfo,
)
from .parser import ParsedSourceBundle

DEFAULT_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "schemas" / "premiere_preset_report_v1.schema.json"
)


def _infer_category_and_tags(
    parsed: ParsedPreset,
    mlt_filters: Sequence[MltFilterSpec],
) -> tuple[str, List[str]]:
    """Infer a high-level browser category and tags from effects and folder path."""
    tags: List[str] = []
    for folder in parsed.folder_path:
        cleaned = folder.strip().lower()
        if cleaned and cleaned not in tags:
            tags.append(cleaned)

    match_names = " ".join(
        f"{c.match_name} {c.display_name}".lower() for c in parsed.effects
    )
    media_types = {c.media_type for c in parsed.effects} | {f.media_type for f in mlt_filters}

    if media_types == {"audio"} or ("audio" in media_types and "video" not in media_types):
        category = "Broadcast Audio"
    elif any(k in match_names for k in ("dissolve", "dip to", "push", "slide", "wipe", "whip pan")):
        category = "Transitions"
    elif any(k in match_names for k in ("key", "matte", "mask", "alpha")):
        category = "Keying & Matte"
    elif any(k in match_names for k in ("lumetri", "color", "curves", "tint", "black & white", "levels", "vibrance", "procamp", "brightness")):
        category = "Colour & Film Looks"
    elif any(k in match_names for k in ("blur", "glow", "shadow", "sharpen", "unsharp", "mosaic", "noise", "grain", "vignette", "strobe", "chromatic", "posterize", "echo", "stroke", "wave", "turbulent")):
        category = "Blur & Stylize"
    elif any(k in match_names for k in ("motion", "geometry", "transform", "opacity", "crop", "mirror", "offset", "corner pin", "basic 3d", "stabiliz", "timewarp")):
        category = "Motion & Transforms"
    elif parsed.folder_path:
        category = parsed.folder_path[0]
    else:
        category = "Effects"

    for flt in mlt_filters:
        svc = flt.service.split(".")[-1].lower()
        if svc and svc not in tags:
            tags.append(svc)
    return category, tags


def _aggregate_status(
    results: Sequence[MappedEffectResult],
    missing_deps: Sequence[MissingDependency],
) -> CompatibilityStatus:
    """Honest worst-case status aggregation across a preset's effect chain."""
    if not results:
        return CompatibilityStatus.UNSUPPORTED
    statuses = {r.status for r in results}
    if CompatibilityStatus.UNSUPPORTED in statuses:
        return CompatibilityStatus.UNSUPPORTED
    if CompatibilityStatus.MISSING_DEPENDENCY in statuses or any(
        not dep.resolved for dep in missing_deps
    ):
        return CompatibilityStatus.MISSING_DEPENDENCY
    if CompatibilityStatus.APPROXIMATED in statuses:
        return CompatibilityStatus.APPROXIMATED
    return CompatibilityStatus.EXACT_VALIDATED


def _indent_xml(elem: StdET.Element, level: int = 0) -> None:
    """Deterministic pretty indentation compatible with Python 3.8+."""
    indent_str = "\n" + "    " * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = indent_str + "    "
        for idx, child in enumerate(elem):
            _indent_xml(child, level + 1)
            if not child.tail or not child.tail.strip():
                child.tail = indent_str + ("    " if idx < len(elem) - 1 else "")
    elif level and (not elem.tail or not elem.tail.strip()):
        elem.tail = indent_str


def generate_kdenlive_effect_xml(
    preset_name: str,
    mlt_filters: Sequence[MltFilterSpec],
    description: str = "",
    preset_id: str = "",
    compatibility_status: CompatibilityStatus = CompatibilityStatus.APPROXIMATED,
) -> Optional[str]:
    """Emit a Kdenlive-compatible `<effectgroup>` custom effect XML string.

    Returns None when there are no executable MLT filters (e.g. fully unsupported preset).
    """
    if not mlt_filters:
        return None

    root = StdET.Element(
        "effectgroup",
        attrib={
            "id": preset_name,
            "parentIn": "0",
            "preset_id": preset_id,
            "compatibility_status": compatibility_status.value,
        },
    )

    for flt in mlt_filters:
        eff_el = StdET.SubElement(root, "effect", attrib={"id": flt.service})
        for prop_name, prop_val in sorted(flt.properties.items()):
            prop_el = StdET.SubElement(eff_el, "property", attrib={"name": prop_name})
            prop_el.text = str(prop_val)
        collapsed = StdET.SubElement(eff_el, "property", attrib={"name": "kdenlive:collapsed"})
        collapsed.text = "0"

    desc_el = StdET.SubElement(root, "description")
    desc_el.text = (
        description.strip()
        if description.strip()
        else f"Imported from Adobe Premiere Pro preset '{preset_name}' ({compatibility_status.value})."
    )

    _indent_xml(root)
    xml_body = StdET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="utf-8"?>\n{xml_body}\n'


def generate_mlt_filter_xml(
    input_media: str,
    mlt_filters: Sequence[MltFilterSpec],
    out_frame: int = 24,
    fps_num: int = 25,
    fps_den: int = 1,
    width: int = 640,
    height: int = 360,
) -> str:
    """Build a complete, self-contained MLT XML document applying `mlt_filters` to `input_media`.

    Uses `xml.etree.ElementTree` so all paths and property values are strictly XML-escaped.
    """
    root = StdET.Element("mlt", attrib={"LC_NUMERIC": "C", "version": "7.22.0"})
    StdET.SubElement(
        root,
        "profile",
        attrib={
            "description": "Bang On Pro Verification Profile",
            "width": str(width),
            "height": str(height),
            "progressive": "1",
            "sample_aspect_num": "1",
            "sample_aspect_den": "1",
            "display_aspect_num": str(width),
            "display_aspect_den": str(height),
            "frame_rate_num": str(fps_num),
            "frame_rate_den": str(fps_den),
            "colorspace": "709",
        },
    )

    producer = StdET.SubElement(
        root,
        "producer",
        attrib={"id": "producer0", "in": "0", "out": str(out_frame)},
    )
    res_prop = StdET.SubElement(producer, "property", attrib={"name": "resource"})
    res_prop.text = input_media

    for idx, flt in enumerate(mlt_filters):
        flt_el = StdET.SubElement(
            producer,
            "filter",
            attrib={"id": f"filter{idx}", "in": "0", "out": str(out_frame)},
        )
        svc_prop = StdET.SubElement(flt_el, "property", attrib={"name": "mlt_service"})
        svc_prop.text = flt.service
        for k, v in sorted(flt.properties.items()):
            p_el = StdET.SubElement(flt_el, "property", attrib={"name": k})
            p_el.text = str(v)

    playlist = StdET.SubElement(root, "playlist", attrib={"id": "playlist0"})
    StdET.SubElement(
        playlist,
        "entry",
        attrib={"producer": "producer0", "in": "0", "out": str(out_frame)},
    )

    tractor = StdET.SubElement(
        root,
        "tractor",
        attrib={"id": "tractor0", "in": "0", "out": str(out_frame)},
    )
    StdET.SubElement(tractor, "track", attrib={"producer": "playlist0"})

    _indent_xml(root)
    return '<?xml version="1.0" encoding="utf-8"?>\n' + StdET.tostring(root, encoding="unicode") + "\n"


def map_preset(parsed: ParsedPreset, ctx: Optional[MappingContext] = None) -> MappedPreset:
    """Translate a ParsedPreset into a MappedPreset with MLT filters and Kdenlive XML."""
    context = ctx or MappingContext(fps=parsed.fps)
    effect_results: List[MappedEffectResult] = []
    effect_chain: List[Dict[str, Any]] = []
    all_mlt_filters: List[MltFilterSpec] = []
    all_missing_deps: List[MissingDependency] = []
    notes: List[str] = []

    if not parsed.effects:
        notes.append(
            "Preset contained no VideoFilterComponent or AudioFilterComponent entries; "
            "original XML preserved."
        )

    for comp in parsed.effects:
        if comp.bypass:
            entry = comp.to_dict()
            entry["compatibility_status"] = CompatibilityStatus.EXACT_VALIDATED.value
            entry["mapped_mlt_services"] = []
            entry["notes"] = ["Effect is bypassed in source Premiere preset; omitted from active chain."]
            effect_chain.append(entry)
            notes.append(f"Skipped bypassed effect '{comp.display_name}'.")
            continue

        mapped = map_effect_component(comp, context)
        effect_results.append(mapped)
        effect_chain.append(mapped.to_chain_entry())
        all_mlt_filters.extend(mapped.mlt_filters)
        all_missing_deps.extend(mapped.missing_dependencies)
        for n in mapped.notes:
            if n not in notes:
                notes.append(n)

    preset_status = _aggregate_status(effect_results, all_missing_deps)
    category, tags = _infer_category_and_tags(parsed, all_mlt_filters)

    kdenlive_xml = generate_kdenlive_effect_xml(
        preset_name=parsed.name,
        mlt_filters=all_mlt_filters,
        description=parsed.description,
        preset_id=parsed.preset_id,
        compatibility_status=preset_status,
    )

    return MappedPreset(
        preset_id=parsed.preset_id,
        name=parsed.name,
        pack=parsed.pack,
        attribution=parsed.attribution,
        folder_path=list(parsed.folder_path),
        source_file=parsed.source_file,
        preset_type=parsed.preset_type,
        compatibility_status=preset_status,
        notes=notes,
        effect_chain=effect_chain,
        missing_dependencies=all_missing_deps,
        mlt_filters=all_mlt_filters,
        kdenlive_effect_xml=kdenlive_xml,
        original_xml_preserved=parsed.original_xml_preserved,
        original_xml=parsed.original_xml,
        description=parsed.description,
        category=category,
        tags=tags,
    )


def _reconcile_audit(
    mapped_presets: Sequence[MappedPreset],
    audit_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Compare parsed/mapped counts against optional Bang_on_pro_premiere_preset_audit.json."""
    expected_total = audit_data.get("expected_total_presets")
    if expected_total is None and isinstance(audit_data.get("summary"), dict):
        expected_total = audit_data["summary"].get("total_presets")
    parsed_total = len(mapped_presets)
    matched = (expected_total == parsed_total) if isinstance(expected_total, int) else None
    return {
        "audit_supplied": True,
        "expected_total_presets": expected_total,
        "parsed_total_presets": parsed_total,
        "count_matches": matched,
        "audit_version": str(audit_data.get("version", audit_data.get("schema_version", "unknown"))),
    }


def build_compatibility_report(
    bundle: ParsedSourceBundle,
    fps: float = 25.0,
    width: int = 1920,
    height: int = 1080,
    asset_search_dirs: Optional[List[Path]] = None,
    platform_name: str = "linux",
    missing_source_inputs: Optional[Sequence[MissingSourceInput]] = None,
) -> CompatibilityReport:
    """Map all presets in a ParsedSourceBundle and produce a CompatibilityReport."""
    ctx = MappingContext(
        fps=fps,
        width=width,
        height=height,
        bundled_assets=dict(bundle.bundled_assets),
        asset_search_dirs=list(asset_search_dirs or []),
        platform_name=platform_name,
    )
    mapped = [map_preset(p, ctx=ctx) for p in bundle.presets]

    audit_rec = (
        _reconcile_audit(mapped, bundle.audit_data) if bundle.audit_data is not None else None
    )

    generated_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    return CompatibilityReport(
        schema_version=SCHEMA_VERSION,
        generated_at=generated_at,
        source_archive=bundle.archive_info,
        presets=mapped,
        audit_reconciliation=audit_rec,
        missing_source_inputs=list(missing_source_inputs or []),
    )


def build_missing_inputs_report(
    missing_inputs: Sequence[MissingSourceInput],
    source_name: str = "missing_source_inputs",
) -> CompatibilityReport:
    """Construct an honest zero-preset blocker report when required proprietary archives are absent.

    Never fabricates synthetic or fake preset entries.
    """
    generated_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    return CompatibilityReport(
        schema_version=SCHEMA_VERSION,
        generated_at=generated_at,
        source_archive=SourceArchiveInfo(
            name=source_name,
            sha256="0" * 64,
            file_count=0,
            redistribution_restricted=True,
        ),
        presets=[],
        missing_source_inputs=list(missing_inputs),
    )


def validate_report_schema(
    report_dict: Dict[str, Any],
    schema_path: Optional[Path] = None,
) -> None:
    """Validate report JSON dict against premiere_preset_report_v1.schema.json."""
    path = schema_path or DEFAULT_SCHEMA_PATH
    schema = json.loads(path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(report_dict), key=lambda e: list(e.path))
    if errors:
        formatted = "; ".join(f"{'.'.join(str(x) for x in e.path) or '<root>'}: {e.message}" for e in errors[:5])
        raise jsonschema.ValidationError(f"Report failed schema validation: {formatted}")


def render_markdown_report(report: CompatibilityReport) -> str:
    """Render a comprehensive human-readable Markdown compatibility report."""
    summary = report.summary()
    total = max(summary["total_presets"], 1)
    lines: List[str] = [
        "# Bang On Pro — Premiere Pro Preset Compatibility Report",
        "",
        f"- **Generated (UTC):** `{report.generated_at}`",
        f"- **Schema Version:** `{report.schema_version}`",
        f"- **Source Archive:** `{report.source_archive.name}`",
        f"- **Archive SHA-256:** `{report.source_archive.sha256}`",
        f"- **Files Inspected:** `{report.source_archive.file_count}`",
        f"- **Redistribution Restricted:** `{report.source_archive.redistribution_restricted}`",
        "",
        "## Executive Summary",
        "",
        "| Status | Count | Share |",
        "| --- | ---: | ---: |",
        f"| `exact_validated` | {summary['exact_validated']} | {summary['exact_validated'] / total * 100:.1f}% |",
        f"| `approximated` | {summary['approximated']} | {summary['approximated'] / total * 100:.1f}% |",
        f"| `missing_dependency` | {summary['missing_dependency']} | {summary['missing_dependency'] / total * 100:.1f}% |",
        f"| `unsupported` | {summary['unsupported']} | {summary['unsupported'] / total * 100:.1f}% |",
        f"| **Total Presets** | **{summary['total_presets']}** | **100.0%** |",
        "",
        f"- **Distinct Premiere Effects Encountered:** `{summary['distinct_effects']}`",
        "",
    ]

    if report.missing_source_inputs:
        lines.extend(
            [
                "## Missing Source Inputs (Audit Blocker)",
                "",
                "| Input | Expected Path | Reason | Remediation |",
                "| --- | --- | --- | --- |",
            ]
        )
        for m in report.missing_source_inputs:
            lines.append(
                f"| `{m.input_name}` | `{m.expected_path}` | {m.reason} | {m.remediation} |"
            )
        lines.append("")

    if report.audit_reconciliation:
        rec = report.audit_reconciliation
        lines.extend(
            [
                "## Audit Cross-Check",
                "",
                f"- Expected presets: `{rec.get('expected_total_presets')}`",
                f"- Parsed presets: `{rec.get('parsed_total_presets')}`",
                f"- Count matches: `{rec.get('count_matches')}`",
                "",
            ]
        )

    # Missing dependencies table
    dep_rows: List[str] = []
    for preset in report.presets:
        for dep in preset.missing_dependencies:
            dep_rows.append(
                f"| `{preset.name}` | `{dep.kind}` | `{dep.name}` | `{dep.platform}` | {dep.remediation} |"
            )

    if dep_rows:
        lines.extend(
            [
                "## Missing Dependencies & Proprietary / Platform Plugins",
                "",
                "| Preset | Kind | Dependency | Platform | Remediation |",
                "| --- | --- | --- | --- | --- |",
                *dep_rows,
                "",
            ]
        )

    # Per-preset detailed breakdown
    lines.extend(
        [
            "## Preset Compatibility Matrix",
            "",
            "| Preset | Pack | Folder | Status | MLT Filters | Notes |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )

    for p in report.presets:
        folder = " / ".join(p.folder_path) if p.folder_path else "(root)"
        mlt_list = ", ".join(f"`{f.service}`" for f in p.mlt_filters) if p.mlt_filters else "_none_"
        note_text = "; ".join(p.notes).replace("|", "\\|").replace("\n", " ") if p.notes else "-"
        lines.append(
            f"| {p.name} | {p.pack} | {folder} | `{p.compatibility_status.value}` | {mlt_list} | {note_text} |"
        )

    lines.append("")
    return "\n".join(lines)


def export_report_bundle(
    report: CompatibilityReport,
    output_dir: Union[str, Path],
    schema_path: Optional[Path] = None,
) -> Dict[str, Path]:
    """Write validated JSON report, Markdown report, Kdenlive custom XMLs, and preserved XML snippets."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    effects_dir = out / "kdenlive_effects"
    preserved_dir = out / "preserved_xml"
    effects_dir.mkdir(exist_ok=True)
    preserved_dir.mkdir(exist_ok=True)

    report_dict = report.to_dict()
    validate_report_schema(report_dict, schema_path=schema_path)

    json_path = out / "compatibility_report.json"
    md_path = out / "compatibility_report.md"

    json_path.write_text(json.dumps(report_dict, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown_report(report), encoding="utf-8")

    for preset in report.presets:
        if preset.kdenlive_effect_xml:
            (effects_dir / f"{preset.preset_id}.xml").write_text(
                preset.kdenlive_effect_xml, encoding="utf-8"
            )
        if preset.original_xml:
            (preserved_dir / f"{preset.preset_id}.xml").write_text(
                preset.original_xml, encoding="utf-8"
            )

    return {
        "json_report": json_path,
        "markdown_report": md_path,
        "kdenlive_effects_dir": effects_dir,
        "preserved_xml_dir": preserved_dir,
    }
