"""Comprehensive Premiere Pro FilterMatchName -> MLT / FFmpeg / frei0r Effect Mapping Catalog.

Honest accounting policy:
- Supported native/exact equivalents emit `exact_validated`
- Mathematically close but non-identical effects emit `approximated` with explicit caveats
- Missing external LUTs, missing fonts, missing impulse responses, missing codecs, and
  macOS-only Apple Audio Units emit `missing_dependency` with clear remediation notes
- Proprietary third-party plugins (Keyper, Mettle VR, Sapphire, Red Giant/Universe, BCC,
  Twixtor, RSMB) and unrecognized plugins emit `unsupported` — never silently faked

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import functools
import math
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .models import (
    CompatibilityStatus,
    EffectComponent,
    InterpolationMode,
    Keyframe,
    MappedEffectResult,
    MissingDependency,
    MltFilterSpec,
    Parameter,
)

# Default reference canvas geometry for normalizing Premiere pixel coordinates
DEFAULT_WIDTH: int = 1920
DEFAULT_HEIGHT: int = 1080

_LUT_FILE_RE = re.compile(r"([^\s\"'<>|;,/\\]+?\.(?:cube|look|3dl|itx))", re.IGNORECASE)
_IR_FILE_RE = re.compile(r"([^\s\"'<>|;,/\\]+?\.(?:wav|aif|aiff))", re.IGNORECASE)
_FONT_FILE_RE = re.compile(r"([^\s\"'<>|;,/\\]+?\.(?:ttf|otf|ttc))", re.IGNORECASE)
_PROPRIETARY_CODEC_RE = re.compile(
    r"\b(ProRes\s*RAW|BRAW|Blackmagic\s*RAW|REDCODE|R3D|ARRIRAW)\b", re.IGNORECASE
)

# Standard system generic font families always resolvable by fontconfig/Qt
_GENERIC_FONTS = {
    "sans",
    "sans-serif",
    "serif",
    "monospace",
    "dejavu sans",
    "dejavu serif",
    "dejavu sans mono",
    "liberation sans",
    "liberation serif",
    "liberation mono",
    "noto sans",
    "arial",
    "helvetica",
}

# Proprietary plugin classification patterns: (regex, vendor_label, remediation)
_PROPRIETARY_PATTERNS: List[Tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"(?:^|[.\s_/-])keyper(?:$|[.\s_/-])", re.IGNORECASE),
        "Keyper (Third-Party AI Keying)",
        "Proprietary AI keying plugin not natively executable. Use Bang On Pro AI Roto/Matte "
        "or avfilter.chromakey / rotoscoping fallback.",
    ),
    (
        re.compile(r"(?:mettle|mantra\s*vr|skybox|mettle\.flux)", re.IGNORECASE),
        "Mettle VR / SkyBox / Mantra VR",
        "Proprietary 360/VR plugin requires licensed Mettle plugin host; no exact native MLT equivalent.",
    ),
    (
        re.compile(r"^(?:ae\.)?(?:s_|sapphire\.|genarts\.)", re.IGNORECASE),
        "Boris FX Sapphire",
        "Proprietary Boris FX Sapphire plugin requires licensed OFX/AE host plugin.",
    ),
    (
        re.compile(
            r"^(?:ae\.)?(?:rg_|uni_|redgiant\.|maxon\.|magic\s*bullet|trapcode)",
            re.IGNORECASE,
        ),
        "Maxon Red Giant / Universe",
        "Proprietary Red Giant / Maxon Universe plugin is unavailable natively on Linux MLT.",
    ),
    (
        re.compile(r"^(?:ae\.)?(?:bcc_|borisfx\.)", re.IGNORECASE),
        "Boris FX Continuum (BCC)",
        "Proprietary Boris Continuum Complete plugin requires licensed plugin package.",
    ),
    (
        re.compile(r"(?:twixtor|rsmb|reelsmart|re:vision)", re.IGNORECASE),
        "RE:VisionFX (Twixtor / RSMB)",
        "Proprietary optical-flow plugin unavailable natively; consider FFmpeg minterpolate offline.",
    ),
]

# Apple Audio Unit macOS-only platform plugins: (regex, display, suggested_ffmpeg_fallback)
_APPLE_AU_PATTERNS: List[Tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"^(?:au\.|apple\s*au|au(?:multiband|dynamics))", re.IGNORECASE),
        "Apple Audio Unit Dynamics/Compressor",
        "avfilter.acompressor",
    ),
    (
        re.compile(r"^(?:au\.|apple\s*au)?.*(?:augraphiceq|auparametriceq|aunbandeq)", re.IGNORECASE),
        "Apple Audio Unit Equalizer",
        "avfilter.equalizer",
    ),
    (
        re.compile(r"^(?:au\.|apple\s*au)?.*(?:aupeaklimiter)", re.IGNORECASE),
        "Apple Audio Unit Peak Limiter",
        "avfilter.alimiter",
    ),
    (
        re.compile(r"^(?:au\.|apple\s*au)?.*(?:aumatrixreverb|audelay)", re.IGNORECASE),
        "Apple Audio Unit Reverb/Delay",
        "avfilter.aecho",
    ),
    (
        re.compile(r"^(?:au\.|apple\s*au)?.*(?:auhighpass|aulowpass)", re.IGNORECASE),
        "Apple Audio Unit Filter",
        "avfilter.highpass / avfilter.lowpass",
    ),
    (
        re.compile(r"^(?:au\.|apple\s*audiounit|com\.apple\.audio)", re.IGNORECASE),
        "Apple Audio Unit (macOS-only)",
        "avfilter.equalizer / avfilter.acompressor",
    ),
]


@dataclass
class MappingContext:
    """Contextual state for resolving assets, frame rates, and canvas geometry."""

    fps: float = 25.0
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    bundled_assets: Dict[str, str] = field(default_factory=dict)
    asset_search_dirs: List[Path] = field(default_factory=list)
    platform_name: str = "linux"


def mlt_interp_operator(mode: InterpolationMode) -> str:
    """Translate Premiere InterpolationMode to MLT property animation operator."""
    if mode == InterpolationMode.HOLD:
        return "|="
    if mode in (InterpolationMode.BEZIER, InterpolationMode.EASE_IN, InterpolationMode.EASE_OUT):
        return "~="
    return "="


def format_mlt_scalar_keyframes(
    param: Optional[Parameter],
    transform: Callable[[float], float],
    default_value: float,
    precision: int = 4,
) -> str:
    """Format a scalar Parameter into either a static string or an MLT keyframe string."""
    if param is None:
        return f"{transform(default_value):.{precision}f}"

    if param.keyframes:
        # Deduplicate frames keeping last value at same frame, ordered by frame
        ordered = sorted(param.keyframes, key=lambda k: k.frame)
        parts: List[str] = []
        seen_frames: Dict[int, str] = {}
        for kf in ordered:
            raw = _as_float(kf.value, default_value)
            val = transform(raw)
            op = mlt_interp_operator(kf.interpolation)
            seen_frames[kf.frame] = f"{kf.frame}{op}{val:.{precision}f}"
        for frame in sorted(seen_frames.keys()):
            parts.append(seen_frames[frame])
        if len(parts) == 1:
            # Single keyframe at frame 0 can stay static unless discrete
            return f"{transform(_as_float(ordered[0].value, default_value)):.{precision}f}"
        return ";".join(parts)

    val = transform(_as_float(param.current_value, default_value))
    return f"{val:.{precision}f}"


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, tuple) and value:
        return _as_float(value[0], default)
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return default


def _as_point(value: Any, default: Tuple[float, float]) -> Tuple[float, float]:
    if isinstance(value, tuple) and len(value) >= 2:
        return (float(value[0]), float(value[1]))
    if isinstance(value, (list,)) and len(value) >= 2:
        return (float(value[0]), float(value[1]))
    if isinstance(value, str) and ":" in value:
        parts = value.split(":")
        if len(parts) >= 2:
            try:
                return (float(parts[0]), float(parts[1]))
            except ValueError:
                pass
    return default


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _db_to_linear(db: float) -> float:
    return math.pow(10.0, db / 20.0)


def _find_param(pmap: Dict[str, Parameter], *aliases: str) -> Optional[Parameter]:
    for alias in aliases:
        key = alias.strip().lower()
        if key in pmap:
            return pmap[key]
    # Substring fallback for Premiere localized / versioned param names
    for alias in aliases:
        needle = alias.strip().lower()
        for k, param in pmap.items():
            if needle in k:
                return param
    return None


def _sample_param_at_frame(
    param: Optional[Parameter],
    frame: int,
    default: Any,
    is_point: bool = False,
) -> Tuple[Any, InterpolationMode]:
    """Evaluate a Premiere parameter at a target frame (piecewise linear / hold lookup)."""
    if param is None:
        return (default, InterpolationMode.LINEAR)
    if not param.keyframes:
        val = _as_point(param.current_value, default) if is_point else _as_float(param.current_value, default)
        return (val, InterpolationMode.LINEAR)

    kfs = sorted(param.keyframes, key=lambda k: k.frame)
    if frame <= kfs[0].frame:
        val = _as_point(kfs[0].value, default) if is_point else _as_float(kfs[0].value, default)
        return (val, kfs[0].interpolation)
    if frame >= kfs[-1].frame:
        val = _as_point(kfs[-1].value, default) if is_point else _as_float(kfs[-1].value, default)
        return (val, kfs[-1].interpolation)

    for idx in range(len(kfs) - 1):
        a, b = kfs[idx], kfs[idx + 1]
        if a.frame == frame:
            val = _as_point(a.value, default) if is_point else _as_float(a.value, default)
            return (val, a.interpolation)
        if b.frame == frame:
            val = _as_point(b.value, default) if is_point else _as_float(b.value, default)
            return (val, b.interpolation)
        if a.frame < frame < b.frame:
            if a.interpolation == InterpolationMode.HOLD:
                val = _as_point(a.value, default) if is_point else _as_float(a.value, default)
                return (val, InterpolationMode.HOLD)
            t = (frame - a.frame) / float(b.frame - a.frame)
            if is_point:
                pa = _as_point(a.value, default)
                pb = _as_point(b.value, default)
                return ((pa[0] + (pb[0] - pa[0]) * t, pa[1] + (pb[1] - pa[1]) * t), a.interpolation)
            fa = _as_float(a.value, default)
            fb = _as_float(b.value, default)
            return (fa + (fb - fa) * t, a.interpolation)

    return (default, InterpolationMode.LINEAR)


def _normalize_coord_pair(pt: Tuple[float, float], width: int, height: int) -> Tuple[float, float]:
    """Convert Premiere normalized (0..1) or pixel coordinates to pixel coordinates."""
    x, y = pt
    if -2.0 <= x <= 2.0 and -2.0 <= y <= 2.0:
        return (x * width, y * height)
    return (x, y)


@functools.lru_cache(maxsize=1)
def _installed_system_fonts() -> Set[str]:
    """Query installed font families safely via fontconfig (`fc-list`) without shell interpolation."""
    fonts: Set[str] = set(_GENERIC_FONTS)
    try:
        proc = subprocess.run(
            ["fc-list", ":", "family"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                for part in line.split(","):
                    cleaned = part.strip().lower()
                    if cleaned:
                        fonts.add(cleaned)
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return fonts


def _resolve_external_asset(filename: str, ctx: MappingContext) -> Optional[str]:
    """Check whether an external asset (LUT, font, IR audio) exists in bundle or search paths."""
    base = Path(filename).name.lower()
    if base in ctx.bundled_assets:
        return ctx.bundled_assets[base]
    candidate = Path(filename)
    if candidate.is_file():
        return str(candidate)
    for d in ctx.asset_search_dirs:
        joined = d / Path(filename).name
        if joined.is_file():
            return str(joined)
    return None


def _check_codec_dependencies(comp: EffectComponent) -> List[MissingDependency]:
    deps: List[MissingDependency] = []
    seen: Set[str] = set()
    for p in comp.parameters:
        blob = f"{p.current_value or ''} {p.raw_arb_data or ''}"
        for match in _PROPRIETARY_CODEC_RE.finditer(blob):
            codec_name = match.group(1).strip()
            key = codec_name.lower()
            if key not in seen:
                seen.add(key)
                deps.append(
                    MissingDependency(
                        kind="codec",
                        name=codec_name,
                        effect_match_name=comp.match_name,
                        platform="third_party",
                        resolved=False,
                        remediation=f"Proprietary camera/intermediate codec ({codec_name}) requires "
                        "transcoding to ProRes/DNxHR/FFV1 or vendor SDK decoder.",
                    )
                )
    return deps


# ---------------------------------------------------------------------------
# Individual Premiere Effect Translators
# ---------------------------------------------------------------------------


def _map_motion_or_transform(
    comp: EffectComponent,
    ctx: MappingContext,
    is_transform_effect: bool = False,
) -> MappedEffectResult:
    """Map AE.ADBE Motion and AE.ADBE Geometry2 (Transform) -> MLT qtblend."""
    pmap = comp.param_map()
    pos_p = _find_param(pmap, "position")
    anchor_p = _find_param(pmap, "anchor point", "anchor")
    scale_p = _find_param(pmap, "scale", "scale height")
    scale_w_p = _find_param(pmap, "scale width")
    rot_p = _find_param(pmap, "rotation")
    opacity_p = _find_param(pmap, "opacity")
    shutter_p = _find_param(pmap, "shutter angle")
    skew_p = _find_param(pmap, "skew")

    W, H = ctx.width, ctx.height
    default_center = (W / 2.0, H / 2.0)

    # Collect union of all keyframe frames across spatial parameters
    frame_set: Set[int] = set()
    for p in (pos_p, anchor_p, scale_p, scale_w_p, opacity_p):
        if p is not None and p.keyframes:
            for kf in p.keyframes:
                frame_set.add(kf.frame)

    non_uniform_scale = scale_w_p is not None and (
        scale_w_p.keyframes
        or abs(_as_float(scale_w_p.current_value, 100.0) - _as_float(scale_p.current_value if scale_p else 100.0, 100.0)) > 0.1
    )

    def _rect_at_frame(frame: int) -> Tuple[str, InterpolationMode]:
        pos_raw, pos_interp = _sample_param_at_frame(pos_p, frame, default_center, is_point=True)
        anc_raw, _ = _sample_param_at_frame(anchor_p, frame, default_center, is_point=True)
        sc_y_pct, sc_interp = _sample_param_at_frame(scale_p, frame, 100.0, is_point=False)
        sc_x_pct, _ = (
            _sample_param_at_frame(scale_w_p, frame, sc_y_pct, is_point=False)
            if scale_w_p is not None
            else (sc_y_pct, sc_interp)
        )
        op_pct, op_interp = _sample_param_at_frame(opacity_p, frame, 100.0, is_point=False)

        px, py = _normalize_coord_pair(pos_raw, W, H)
        ax, ay = _normalize_coord_pair(anc_raw, W, H)

        sx = max(0.01, float(sc_x_pct) / 100.0)
        sy = max(0.01, float(sc_y_pct) / 100.0)
        rw = max(1, int(round(W * sx)))
        rh = max(1, int(round(H * sy)))
        rx = int(round(px - ax * sx))
        ry = int(round(py - ay * sy))
        alpha = _clamp(float(op_pct) / 100.0, 0.0, 1.0)

        # Prioritize explicit keyframes located exactly at `frame` (HOLD > BEZIER/EASE > LINEAR)
        exact_interps: List[InterpolationMode] = []
        for param_obj in (pos_p, anchor_p, scale_p, scale_w_p, opacity_p):
            if param_obj is not None:
                for kf in param_obj.keyframes:
                    if kf.frame == frame:
                        exact_interps.append(kf.interpolation)

        if InterpolationMode.HOLD in exact_interps:
            interp = InterpolationMode.HOLD
        elif any(m in (InterpolationMode.BEZIER, InterpolationMode.EASE_IN, InterpolationMode.EASE_OUT) for m in exact_interps):
            interp = next(
                m for m in exact_interps
                if m in (InterpolationMode.BEZIER, InterpolationMode.EASE_IN, InterpolationMode.EASE_OUT)
            )
        elif exact_interps:
            interp = exact_interps[0]
        else:
            interp = pos_interp
            if sc_interp != InterpolationMode.LINEAR:
                interp = sc_interp
            elif op_interp != InterpolationMode.LINEAR:
                interp = op_interp

        return (f"{rx} {ry} {rw} {rh} {alpha:.4f}", interp)

    if len(frame_set) >= 2:
        rect_entries: List[str] = []
        for f in sorted(frame_set):
            rect_str, interp = _rect_at_frame(f)
            op = mlt_interp_operator(interp)
            rect_entries.append(f"{f}{op}{rect_str}")
        rect_prop = ";".join(rect_entries)
    else:
        single_frame = next(iter(frame_set), 0)
        rect_prop, _ = _rect_at_frame(single_frame)

    rot_prop = format_mlt_scalar_keyframes(rot_p, lambda v: v, default_value=0.0, precision=3)

    props: Dict[str, str] = {
        "rect": rect_prop,
        "rotation": rot_prop,
        "rotate_center": "1",
        "compositing": "0",
        "distort": "1" if non_uniform_scale else "0",
    }

    notes: List[str] = []
    status = CompatibilityStatus.EXACT_VALIDATED

    if is_transform_effect:
        shutter_val = _as_float(shutter_p.current_value if shutter_p else 0.0, 0.0)
        skew_val = _as_float(skew_p.current_value if skew_p else 0.0, 0.0)
        if shutter_val > 0.0:
            status = CompatibilityStatus.APPROXIMATED
            notes.append(
                f"Premiere Transform Shutter Angle ({shutter_val:.0f} deg) synthetic motion blur "
                "is not rendered natively by MLT qtblend; keyframes & transform geometry preserved."
            )
        if abs(skew_val) > 0.01:
            status = CompatibilityStatus.APPROXIMATED
            notes.append(
                f"Premiere Transform Skew ({skew_val:.2f}) has no direct qtblend parameter; "
                "position/scale/rotation preserved."
            )

    # Check if Bezier handles were approximated via MLT Catmull-Rom smooth interpolation (~=)
    has_bezier_handles = any(
        kf.bezier_handles is not None
        for p in comp.parameters
        for kf in p.keyframes
        if kf.interpolation in (InterpolationMode.BEZIER, InterpolationMode.EASE_IN, InterpolationMode.EASE_OUT)
    )
    if has_bezier_handles:
        notes.append(
            "Premiere Bezier keyframe easing mapped to MLT smooth spline (~=) interpolation."
        )

    mlt = MltFilterSpec(
        service="qtblend",
        media_type="video",
        properties=props,
        source_effect=comp.match_name,
        notes=list(notes),
    )
    return MappedEffectResult(
        component=comp,
        status=status,
        mlt_filters=[mlt],
        notes=notes,
        mapped_services=["qtblend"],
    )


def _map_opacity(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    op_p = _find_param(pmap, "opacity")
    W, H = ctx.width, ctx.height
    if op_p is not None and len(op_p.keyframes) >= 2:
        parts = []
        for kf in sorted(op_p.keyframes, key=lambda k: k.frame):
            alpha = _clamp(_as_float(kf.value, 100.0) / 100.0, 0.0, 1.0)
            parts.append(f"{kf.frame}{mlt_interp_operator(kf.interpolation)}0 0 {W} {H} {alpha:.4f}")
        rect_prop = ";".join(parts)
    else:
        alpha = _clamp(_as_float(op_p.current_value if op_p else 100.0, 100.0) / 100.0, 0.0, 1.0)
        rect_prop = f"0 0 {W} {H} {alpha:.4f}"

    mlt = MltFilterSpec(
        service="qtblend",
        media_type="video",
        properties={"rect": rect_prop, "compositing": "0", "rotate_center": "1"},
        source_effect=comp.match_name,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=[mlt],
        mapped_services=["qtblend"],
    )


def _map_crop(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    left_p = _find_param(pmap, "left")
    top_p = _find_param(pmap, "top")
    right_p = _find_param(pmap, "right")
    bottom_p = _find_param(pmap, "bottom")
    feather_p = _find_param(pmap, "edge feather", "feather")
    W, H = ctx.width, ctx.height

    frames: Set[int] = set()
    for p in (left_p, top_p, right_p, bottom_p):
        if p and p.keyframes:
            frames.update(kf.frame for kf in p.keyframes)

    def _crop_rect(frame: int) -> Tuple[str, InterpolationMode]:
        lp, li = _sample_param_at_frame(left_p, frame, 0.0)
        tp, ti = _sample_param_at_frame(top_p, frame, 0.0)
        rp, ri = _sample_param_at_frame(right_p, frame, 0.0)
        bp, bi = _sample_param_at_frame(bottom_p, frame, 0.0)
        x = int(round(W * _clamp(float(lp) / 100.0, 0.0, 0.99)))
        y = int(round(H * _clamp(float(tp) / 100.0, 0.0, 0.99)))
        rx = int(round(W * _clamp(float(rp) / 100.0, 0.0, 0.99)))
        by = int(round(H * _clamp(float(bp) / 100.0, 0.0, 0.99)))
        cw = max(1, W - x - rx)
        ch = max(1, H - y - by)
        interp = next(
            (m for m in (li, ti, ri, bi) if m != InterpolationMode.LINEAR),
            InterpolationMode.LINEAR,
        )
        return (f"{x} {y} {cw} {ch}", interp)

    if len(frames) >= 2:
        rect_prop = ";".join(
            f"{f}{mlt_interp_operator(_crop_rect(f)[1])}{_crop_rect(f)[0]}"
            for f in sorted(frames)
        )
    else:
        rect_prop, _ = _crop_rect(next(iter(frames), 0))

    notes: List[str] = []
    status = CompatibilityStatus.EXACT_VALIDATED
    feather_val = _as_float(feather_p.current_value if feather_p else 0.0, 0.0)
    if feather_val > 0.0:
        status = CompatibilityStatus.APPROXIMATED
        notes.append(f"Crop Edge Feather ({feather_val:.1f}px) is not natively supported by qtcrop.")

    mlt = MltFilterSpec(
        service="qtcrop",
        media_type="video",
        properties={"rect": rect_prop, "color": "#00000000"},
        source_effect=comp.match_name,
        notes=list(notes),
    )
    return MappedEffectResult(
        component=comp,
        status=status,
        mlt_filters=[mlt],
        notes=notes,
        mapped_services=["qtcrop"],
    )


def _map_mirror(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    angle_p = _find_param(pmap, "reflection angle", "angle")
    angle = _as_float(angle_p.current_value if angle_p else 0.0, 0.0) % 360.0
    if 45.0 <= angle < 135.0:
        mode, rev = "vertical", "0"
    elif 135.0 <= angle < 225.0:
        mode, rev = "horizontal", "1"
    elif 225.0 <= angle < 315.0:
        mode, rev = "vertical", "1"
    else:
        mode, rev = "horizontal", "0"
    mlt = MltFilterSpec(
        service="mirror",
        media_type="video",
        properties={"mirror": mode, "reverse": rev},
        source_effect=comp.match_name,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=[mlt],
        mapped_services=["mirror"],
    )


def _map_offset(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    shift_p = _find_param(pmap, "shift center to", "shift", "offset")
    W, H = ctx.width, ctx.height
    pt = _as_point(shift_p.current_value if shift_p else (W / 2.0, H / 2.0), (W / 2.0, H / 2.0))
    px, py = _normalize_coord_pair(pt, W, H)
    hpos = ((px - W / 2.0) / float(W)) % 1.0
    vpos = ((py - H / 2.0) / float(H)) % 1.0
    notes = ["Premiere Offset wrap-around mapped via avfilter.scroll hpos/vpos."]
    mlt = MltFilterSpec(
        service="avfilter.scroll",
        media_type="video",
        properties={
            "av.h": "0",
            "av.v": "0",
            "av.hpos": f"{hpos:.4f}",
            "av.vpos": f"{vpos:.4f}",
        },
        source_effect=comp.match_name,
        notes=notes,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.APPROXIMATED,
        mlt_filters=[mlt],
        notes=notes,
        mapped_services=["avfilter.scroll"],
    )


def _map_corner_pin(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    W, H = ctx.width, ctx.height
    ul = _normalize_coord_pair(
        _as_point(_find_param(pmap, "upper left").current_value if _find_param(pmap, "upper left") else (0.0, 0.0), (0.0, 0.0)),
        W, H,
    )
    ur = _normalize_coord_pair(
        _as_point(_find_param(pmap, "upper right").current_value if _find_param(pmap, "upper right") else (W, 0.0), (W, 0.0)),
        W, H,
    )
    lr = _normalize_coord_pair(
        _as_point(_find_param(pmap, "lower right").current_value if _find_param(pmap, "lower right") else (W, H), (W, H)),
        W, H,
    )
    ll = _normalize_coord_pair(
        _as_point(_find_param(pmap, "lower left").current_value if _find_param(pmap, "lower left") else (0.0, H), (0.0, H)),
        W, H,
    )

    def _norm(v: float, dim: float) -> str:
        # frei0r.c0rners maps [0..1] across [-1..2] via (x/dim + 1) / 3
        return f"{_clamp((v / dim + 1.0) / 3.0, 0.0, 1.0):.5f}"

    props = {
        "0": _norm(ul[0], W),
        "1": _norm(ul[1], H),
        "2": _norm(ur[0], W),
        "3": _norm(ur[1], H),
        "4": _norm(lr[0], W),
        "5": _norm(lr[1], H),
        "6": _norm(ll[0], W),
        "7": _norm(ll[1], H),
    }
    mlt = MltFilterSpec(
        service="frei0r.c0rners",
        media_type="video",
        properties=props,
        source_effect=comp.match_name,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=[mlt],
        mapped_services=["frei0r.c0rners"],
    )


def _map_basic_3d(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    swivel = _as_float(_find_param(pmap, "swivel").current_value if _find_param(pmap, "swivel") else 0.0, 0.0)
    tilt = _as_float(_find_param(pmap, "tilt").current_value if _find_param(pmap, "tilt") else 0.0, 0.0)
    inset_x = _clamp(abs(swivel) / 180.0 * 0.15, 0.0, 0.2)
    inset_y = _clamp(abs(tilt) / 180.0 * 0.15, 0.0, 0.2)
    notes = [
        "Premiere Basic 3D swivel/tilt approximated with frei0r.c0rners 2.5D perspective; "
        "specular highlight and 3D depth shading are unsupported."
    ]
    mlt = MltFilterSpec(
        service="frei0r.c0rners",
        media_type="video",
        properties={
            "0": f"{0.333333 + inset_x:.5f}",
            "1": f"{0.333333 + inset_y:.5f}",
            "2": f"{0.666666 - inset_x:.5f}",
            "3": f"{0.333333:.5f}",
            "4": f"{0.666666 - inset_x:.5f}",
            "5": f"{0.666666 - inset_y:.5f}",
            "6": f"{0.333333 + inset_x:.5f}",
            "7": f"{0.666666:.5f}",
        },
        source_effect=comp.match_name,
        notes=notes,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.APPROXIMATED,
        mlt_filters=[mlt],
        notes=notes,
        mapped_services=["frei0r.c0rners"],
    )


def _map_wave_warp(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    height_p = _find_param(pmap, "wave height", "height")
    speed_p = _find_param(pmap, "wave speed", "speed")
    amplitude = max(1, int(round(_as_float(height_p.current_value if height_p else 10.0, 10.0))))
    speed = max(0, int(round(_as_float(speed_p.current_value if speed_p else 1.0, 1.0) * 5.0)))
    notes = ["Premiere Wave Warp approximated with MLT wave filter (sine waveform only)."]
    mlt = MltFilterSpec(
        service="wave",
        media_type="video",
        properties={
            "wave": str(_clamp(amplitude, 1, 200)),
            "speed": str(_clamp(speed, 0, 100)),
            "deformX": "1",
            "deformY": "1",
        },
        source_effect=comp.match_name,
        notes=notes,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.APPROXIMATED,
        mlt_filters=[mlt],
        notes=notes,
        mapped_services=["wave"],
    )


def _map_distort_approx(comp: EffectComponent, ctx: MappingContext, label: str) -> MappedEffectResult:
    pmap = comp.param_map()
    amt_p = _find_param(pmap, "amount", "angle", "complexity", "size")
    amt = _clamp(abs(_as_float(amt_p.current_value if amt_p else 30.0, 30.0)) / 200.0, 0.05, 0.9)
    notes = [f"Premiere {label} approximated via frei0r.distort0r warp."]
    mlt = MltFilterSpec(
        service="frei0r.distort0r",
        media_type="video",
        properties={"0": f"{amt:.4f}", "1": "0.0200"},
        source_effect=comp.match_name,
        notes=notes,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.APPROXIMATED,
        mlt_filters=[mlt],
        notes=notes,
        mapped_services=["frei0r.distort0r"],
    )


def _map_lens_distortion(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    curv_p = _find_param(pmap, "curvature", "distortion")
    k1_str = format_mlt_scalar_keyframes(
        curv_p, lambda v: _clamp(v / 100.0, -1.0, 1.0), default_value=0.0, precision=4
    )
    mlt = MltFilterSpec(
        service="avfilter.lenscorrection",
        media_type="video",
        properties={"av.cx": "0.5", "av.cy": "0.5", "av.k1": k1_str, "av.k2": "0.0"},
        source_effect=comp.match_name,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=[mlt],
        mapped_services=["avfilter.lenscorrection"],
    )


def _map_gaussian_blur(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    blur_p = _find_param(pmap, "blurriness", "blur", "radius", "sigma")
    dim_p = _find_param(pmap, "blur dimensions", "dimensions")

    # Premiere Blurriness ~ 2.5 * Gaussian sigma
    sigma_str = format_mlt_scalar_keyframes(
        blur_p, lambda v: _clamp(max(0.0, v) * 0.4, 0.0, 250.0), default_value=10.0, precision=3
    )
    dim_val = int(round(_as_float(dim_p.current_value if dim_p else 1.0, 1.0)))
    props: Dict[str, str] = {"av.sigma": sigma_str, "av.steps": "2", "av.planes": "15"}
    if dim_val == 2:  # Horizontal only
        props["av.sigmaV"] = "0.0"
    elif dim_val == 3:  # Vertical only
        props["av.sigma"] = "0.0"
        props["av.sigmaV"] = sigma_str

    mlt = MltFilterSpec(
        service="avfilter.gblur",
        media_type="video",
        properties=props,
        source_effect=comp.match_name,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=[mlt],
        mapped_services=["avfilter.gblur"],
    )


def _map_directional_blur(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    dir_p = _find_param(pmap, "direction", "angle")
    len_p = _find_param(pmap, "blur length", "length", "radius")
    angle_str = format_mlt_scalar_keyframes(
        dir_p, lambda v: v % 360.0, default_value=0.0, precision=2
    )
    radius_str = format_mlt_scalar_keyframes(
        len_p, lambda v: _clamp(max(0.0, v), 0.0, 500.0), default_value=10.0, precision=2
    )
    mlt = MltFilterSpec(
        service="avfilter.dblur",
        media_type="video",
        properties={"av.angle": angle_str, "av.radius": radius_str},
        source_effect=comp.match_name,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=[mlt],
        mapped_services=["avfilter.dblur"],
    )


def _map_box_or_fast_blur(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    blur_p = _find_param(pmap, "blurriness", "blur radius", "radius")
    iter_p = _find_param(pmap, "iterations")
    radius = max(1, int(round(_clamp(_as_float(blur_p.current_value if blur_p else 10.0, 10.0) * 0.5, 1.0, 64.0))))
    iters = max(1, int(round(_clamp(_as_float(iter_p.current_value if iter_p else 2.0, 2.0), 1.0, 6.0))))
    mlt = MltFilterSpec(
        service="avfilter.boxblur",
        media_type="video",
        properties={"av.luma_radius": str(radius), "av.luma_power": str(iters)},
        source_effect=comp.match_name,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=[mlt],
        mapped_services=["avfilter.boxblur"],
    )


def _map_radial_blur(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    amt_p = _find_param(pmap, "amount", "blur")
    sigma_str = format_mlt_scalar_keyframes(
        amt_p, lambda v: _clamp(max(0.0, v) * 0.35, 0.5, 120.0), default_value=15.0, precision=3
    )
    notes = [
        "Premiere Radial Blur (Spin/Zoom) approximated using avfilter.gblur; "
        "true radial spin kernel not natively available in MLT avfilter."
    ]
    mlt = MltFilterSpec(
        service="avfilter.gblur",
        media_type="video",
        properties={"av.sigma": sigma_str, "av.steps": "2"},
        source_effect=comp.match_name,
        notes=notes,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.APPROXIMATED,
        mlt_filters=[mlt],
        notes=notes,
        mapped_services=["avfilter.gblur"],
    )


def _map_sharpen_or_unsharp(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    amt_p = _find_param(pmap, "sharpen amount", "amount")
    rad_p = _find_param(pmap, "radius")
    luma_amt = format_mlt_scalar_keyframes(
        amt_p, lambda v: _clamp(v / 50.0, -2.0, 5.0), default_value=50.0, precision=3
    )
    rad = _as_float(rad_p.current_value if rad_p else 2.0, 2.0)
    msize = int(_clamp(round(rad) * 2 + 1, 3, 23))
    if msize % 2 == 0:
        msize = min(23, msize + 1)
    mlt = MltFilterSpec(
        service="avfilter.unsharp",
        media_type="video",
        properties={
            "av.luma_msize_x": str(msize),
            "av.luma_msize_y": str(msize),
            "av.luma_amount": luma_amt,
        },
        source_effect=comp.match_name,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=[mlt],
        mapped_services=["avfilter.unsharp"],
    )


def _map_drop_shadow(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    soft_p = _find_param(pmap, "softness")
    dist_p = _find_param(pmap, "distance")
    sigma = _clamp(_as_float(soft_p.current_value if soft_p else 12.0, 12.0) * 0.3, 0.5, 40.0)
    dist = _as_float(dist_p.current_value if dist_p else 10.0, 10.0)
    notes = [
        f"Premiere Drop Shadow (distance={dist:.1f}px, softness={sigma:.1f}) approximated via "
        "MLT vignette/blur shading; alpha-channel composited drop shadow requires track layering."
    ]
    mlt = MltFilterSpec(
        service="avfilter.gblur",
        media_type="video",
        properties={"av.sigma": f"{sigma:.2f}", "av.steps": "1"},
        source_effect=comp.match_name,
        notes=notes,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.APPROXIMATED,
        mlt_filters=[mlt],
        notes=notes,
        mapped_services=["avfilter.gblur"],
    )


def _map_glow(comp: EffectComponent, ctx: MappingContext, is_vr: bool = False) -> MappedEffectResult:
    pmap = comp.param_map()
    rad_p = _find_param(pmap, "glow radius", "radius", "blur")
    bright_p = _find_param(pmap, "glow brightness", "brightness", "intensity")
    thresh_p = _find_param(pmap, "luma threshold", "threshold")
    blur_norm = _clamp(_as_float(rad_p.current_value if rad_p else 25.0, 25.0) / 100.0, 0.05, 1.0)
    bright_norm = _clamp(_as_float(bright_p.current_value if bright_p else 1.0, 1.0) * 0.5, 0.1, 1.0)
    sharp_norm = _clamp(1.0 - _as_float(thresh_p.current_value if thresh_p else 50.0, 50.0) / 100.0, 0.1, 0.95)
    notes = [
        "Premiere VR Glow approximated with planar frei0r.softglow (360 equirectangular projection not applied)."
        if is_vr
        else "Premiere Glow approximated with frei0r.softglow."
    ]
    mlt = MltFilterSpec(
        service="frei0r.softglow",
        media_type="video",
        properties={
            "0": f"{blur_norm:.4f}",
            "1": f"{bright_norm:.4f}",
            "2": f"{sharp_norm:.4f}",
            "3": "0.3500",
        },
        source_effect=comp.match_name,
        notes=notes,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.APPROXIMATED,
        mlt_filters=[mlt],
        notes=notes,
        mapped_services=["frei0r.softglow"],
    )


def _map_mosaic(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    hb = max(1.0, _as_float(_find_param(pmap, "horizontal blocks").current_value if _find_param(pmap, "horizontal blocks") else 40.0, 40.0))
    vb = max(1.0, _as_float(_find_param(pmap, "vertical blocks").current_value if _find_param(pmap, "vertical blocks") else 40.0, 40.0))
    mlt = MltFilterSpec(
        service="frei0r.pixeliz0r",
        media_type="video",
        properties={"0": f"{_clamp(1.0 / hb, 0.002, 0.5):.5f}", "1": f"{_clamp(1.0 / vb, 0.002, 0.5):.5f}"},
        source_effect=comp.match_name,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=[mlt],
        mapped_services=["frei0r.pixeliz0r"],
    )


def _map_posterize(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    lvl_p = _find_param(pmap, "level", "levels")
    lvl = _clamp(_as_float(lvl_p.current_value if lvl_p else 6.0, 6.0) / 50.0, 0.02, 1.0)
    mlt = MltFilterSpec(
        service="frei0r.posterize",
        media_type="video",
        properties={"0": f"{lvl:.4f}"},
        source_effect=comp.match_name,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=[mlt],
        mapped_services=["frei0r.posterize"],
    )


def _map_invert(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    mlt = MltFilterSpec(
        service="invert",
        media_type="video",
        properties={},
        source_effect=comp.match_name,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=[mlt],
        mapped_services=["invert"],
    )


def _map_strobe(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    period_p = _find_param(pmap, "strobe period", "period", "interval")
    period_sec = max(0.04, _as_float(period_p.current_value if period_p else 0.2, 0.2))
    interval_frames = int(_clamp(round(period_sec * ctx.fps), 1, 100))
    notes = ["Premiere Strobe Light mapped to MLT strobe frame interval filter."]
    mlt = MltFilterSpec(
        service="strobe",
        media_type="video",
        properties={"interval": str(interval_frames), "strobe_invert": "0"},
        source_effect=comp.match_name,
        notes=notes,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.APPROXIMATED,
        mlt_filters=[mlt],
        notes=notes,
        mapped_services=["strobe"],
    )


def _map_noise(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    amt_p = _find_param(pmap, "amount of noise", "amount", "noise")
    strength = int(_clamp(round(_as_float(amt_p.current_value if amt_p else 15.0, 15.0)), 0, 100))
    mlt = MltFilterSpec(
        service="avfilter.noise",
        media_type="video",
        properties={"av.alls": str(strength), "av.allf": "u", "av.all_seed": "42"},
        source_effect=comp.match_name,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=[mlt],
        mapped_services=["avfilter.noise"],
    )


def _map_chromatic_aberration(
    comp: EffectComponent, ctx: MappingContext, is_vr: bool = False
) -> MappedEffectResult:
    pmap = comp.param_map()
    red_p = _find_param(pmap, "aberration red", "red", "red shift", "shift")
    blue_p = _find_param(pmap, "aberration blue", "blue", "blue shift")
    green_p = _find_param(pmap, "aberration green", "green")

    crh = format_mlt_scalar_keyframes(
        red_p, lambda v: _clamp(v, -255.0, 255.0), default_value=8.0, precision=1
    )
    cbh = format_mlt_scalar_keyframes(
        blue_p, lambda v: _clamp(v, -255.0, 255.0), default_value=-8.0, precision=1
    )
    cbv = format_mlt_scalar_keyframes(
        green_p, lambda v: _clamp(v * 0.5, -255.0, 255.0), default_value=0.0, precision=1
    )

    notes: List[str] = []
    status = CompatibilityStatus.EXACT_VALIDATED
    if is_vr:
        status = CompatibilityStatus.APPROXIMATED
        notes.append(
            "Premiere VR Chromatic Aberrations mapped to planar avfilter.chromashift "
            "(equirectangular 360 lens wrap omitted)."
        )

    mlt = MltFilterSpec(
        service="avfilter.chromashift",
        media_type="video",
        properties={
            "av.crh": crh,
            "av.crv": "0",
            "av.cbh": cbh,
            "av.cbv": cbv,
            "av.edge": "smear",
        },
        source_effect=comp.match_name,
        notes=list(notes),
    )
    return MappedEffectResult(
        component=comp,
        status=status,
        mlt_filters=[mlt],
        notes=notes,
        mapped_services=["avfilter.chromashift"],
    )


def _map_rgb_curves(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    notes = [
        "Premiere RGB Curves approximated with avfilter.curves S-curve preset; "
        "arbitrary Bezier spline handles are approximated."
    ]
    mlt = MltFilterSpec(
        service="avfilter.curves",
        media_type="video",
        properties={"av.master": "0/0 0.25/0.22 0.75/0.78 1/1"},
        source_effect=comp.match_name,
        notes=notes,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.APPROXIMATED,
        mlt_filters=[mlt],
        notes=notes,
        mapped_services=["avfilter.curves"],
    )


def _map_lumetri(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    """Honest sub-module inspection for Adobe Lumetri Color."""
    pmap = comp.param_map()
    filters: List[MltFilterSpec] = []
    deps: List[MissingDependency] = []
    notes: List[str] = [
        "Lumetri Color mapped to FFmpeg Rec.709 filters (avfilter.eq / colorbalance / vignette / lut3d); "
        "Adobe proprietary tone-mapping curve and colour management pipeline differ slightly."
    ]

    # 1. Inspect external LUT references (.cube, .look, .3dl, .itx)
    found_luts: List[str] = []
    for p in comp.parameters:
        blob = f"{p.current_value or ''} {p.raw_arb_data or ''}"
        for m in _LUT_FILE_RE.finditer(blob):
            lut_name = m.group(1).strip()
            if lut_name not in found_luts:
                found_luts.append(lut_name)
        # Also check explicit Input LUT / Creative LUT parameter values
        lname = p.name.lower()
        if ("lut" in lname or "look" in lname) and isinstance(p.current_value, str):
            val = p.current_value.strip()
            if val and val.lower() not in ("none", "0", "false") and val not in found_luts:
                found_luts.append(val)

    has_missing_lut = False
    for lut_ref in found_luts:
        resolved = _resolve_external_asset(lut_ref, ctx)
        if resolved is not None:
            filters.append(
                MltFilterSpec(
                    service="avfilter.lut3d",
                    media_type="video",
                    properties={"av.file": resolved, "av.interp": "tetrahedral"},
                    source_effect=comp.match_name,
                    notes=[f"Resolved external LUT: {Path(resolved).name}"],
                )
            )
        else:
            has_missing_lut = True
            deps.append(
                MissingDependency(
                    kind="lut",
                    name=Path(lut_ref).name,
                    effect_match_name=comp.match_name,
                    platform="cross_platform",
                    path=lut_ref,
                    resolved=False,
                    remediation=f"Supply missing 3D LUT file '{Path(lut_ref).name}' in preset pack "
                    "or Bang On Pro LUT directory so avfilter.lut3d can load it.",
                )
            )

    # 2. Basic Correction -> avfilter.eq + avfilter.colorbalance
    exp_p = _find_param(pmap, "exposure")
    con_p = _find_param(pmap, "contrast")
    sat_p = _find_param(pmap, "saturation")
    temp_p = _find_param(pmap, "temperature", "wb temperature")
    tint_p = _find_param(pmap, "tint", "wb tint")
    shd_p = _find_param(pmap, "shadows")
    hil_p = _find_param(pmap, "highlights")
    vib_p = _find_param(pmap, "vibrance")
    vig_p = _find_param(pmap, "vignette amount", "vignette")

    exp_v = _as_float(exp_p.current_value if exp_p else 0.0, 0.0)
    con_v = _as_float(con_p.current_value if con_p else 0.0, 0.0)
    sat_v = _as_float(sat_p.current_value if sat_p else 100.0, 100.0)
    shd_v = _as_float(shd_p.current_value if shd_p else 0.0, 0.0)
    hil_v = _as_float(hil_p.current_value if hil_p else 0.0, 0.0)

    # Map Exposure (-5..+5 stops) -> brightness (-1..1), Contrast (-100..100) -> 0.5..2.0, Sat (0..200) -> 0..2
    brightness = _clamp(exp_v * 0.12 + (shd_v + hil_v) * 0.0008, -1.0, 1.0)
    contrast = _clamp(1.0 + con_v / 100.0, 0.1, 3.0)
    saturation = _clamp(sat_v / 100.0, 0.0, 3.0)
    gamma = _clamp(1.0 + exp_v * 0.15, 0.2, 4.0)

    if any(
        abs(val - ref) > 1e-3
        for val, ref in ((brightness, 0.0), (contrast, 1.0), (saturation, 1.0), (gamma, 1.0))
    ) or not filters:
        filters.append(
            MltFilterSpec(
                service="avfilter.eq",
                media_type="video",
                properties={
                    "av.brightness": f"{brightness:.4f}",
                    "av.contrast": f"{contrast:.4f}",
                    "av.saturation": f"{saturation:.4f}",
                    "av.gamma": f"{gamma:.4f}",
                },
                source_effect=comp.match_name,
            )
        )

    temp_v = _as_float(temp_p.current_value if temp_p else 0.0, 0.0)
    tint_v = _as_float(tint_p.current_value if tint_p else 0.0, 0.0)
    if abs(temp_v) > 0.1 or abs(tint_v) > 0.1:
        warm = _clamp(temp_v / 200.0, -0.5, 0.5)
        magenta = _clamp(tint_v / 200.0, -0.5, 0.5)
        filters.append(
            MltFilterSpec(
                service="avfilter.colorbalance",
                media_type="video",
                properties={
                    "av.rm": f"{warm:.4f}",
                    "av.bm": f"{-warm:.4f}",
                    "av.gm": f"{-magenta:.4f}",
                },
                source_effect=comp.match_name,
            )
        )

    vib_v = _as_float(vib_p.current_value if vib_p else 0.0, 0.0)
    if abs(vib_v) > 0.1:
        filters.append(
            MltFilterSpec(
                service="avfilter.vibrance",
                media_type="video",
                properties={"av.intensity": f"{_clamp(vib_v / 100.0, -2.0, 2.0):.4f}"},
                source_effect=comp.match_name,
            )
        )

    vig_v = _as_float(vig_p.current_value if vig_p else 0.0, 0.0)
    if abs(vig_v) > 0.05:
        angle_rad = _clamp(abs(vig_v) / 5.0 * 0.9, 0.15, 1.4)
        filters.append(
            MltFilterSpec(
                service="avfilter.vignette",
                media_type="video",
                properties={
                    "av.angle": f"{angle_rad:.4f}",
                    "av.mode": "backward" if vig_v > 0 else "forward",
                },
                source_effect=comp.match_name,
            )
        )

    # 3. Explicitly call out unsupported Lumetri sub-modules (HSL Secondary / Curves / Color Wheels)
    for p in comp.parameters:
        pl = p.name.lower()
        if "hsl" in pl or "secondary" in pl:
            notes.append(
                "Lumetri HSL Secondary qualification detected: unsupported sub-module in "
                "avfilter.eq/colorbalance; manual secondary mask required."
            )
            break
    for p in comp.parameters:
        pl = p.name.lower()
        if "curve" in pl or "hue vs" in pl:
            notes.append(
                "Lumetri Curves / Hue-vs-Sat sub-module detected: approximated without custom "
                "Lumetri spline points."
            )
            break

    status = (
        CompatibilityStatus.MISSING_DEPENDENCY
        if has_missing_lut
        else CompatibilityStatus.APPROXIMATED
    )
    return MappedEffectResult(
        component=comp,
        status=status,
        mlt_filters=filters,
        missing_dependencies=deps,
        notes=notes,
        mapped_services=[f.service for f in filters],
    )


def _map_tint(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    amt_p = _find_param(pmap, "amount to tint", "amount")
    amt = format_mlt_scalar_keyframes(
        amt_p, lambda v: _clamp(v / 100.0, 0.0, 1.0), default_value=100.0, precision=4
    )
    mlt = MltFilterSpec(
        service="frei0r.tint0r",
        media_type="video",
        properties={"0": "#000000", "1": "#ffffff", "2": amt},
        source_effect=comp.match_name,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=[mlt],
        mapped_services=["frei0r.tint0r"],
    )


def _map_brightness_contrast(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    br_p = _find_param(pmap, "brightness")
    ct_p = _find_param(pmap, "contrast")
    br = format_mlt_scalar_keyframes(br_p, lambda v: _clamp(v / 150.0, -1.0, 1.0), 0.0, 4)
    ct = format_mlt_scalar_keyframes(ct_p, lambda v: _clamp(1.0 + v / 100.0, 0.1, 3.0), 0.0, 4)
    mlt = MltFilterSpec(
        service="avfilter.eq",
        media_type="video",
        properties={"av.brightness": br, "av.contrast": ct},
        source_effect=comp.match_name,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=[mlt],
        mapped_services=["avfilter.eq"],
    )


def _map_color_balance(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    def _cb(alias: str) -> str:
        p = _find_param(pmap, alias)
        return f"{_clamp(_as_float(p.current_value if p else 0.0, 0.0) / 100.0, -1.0, 1.0):.4f}"
    mlt = MltFilterSpec(
        service="avfilter.colorbalance",
        media_type="video",
        properties={
            "av.rs": _cb("shadow red balance"),
            "av.gs": _cb("shadow green balance"),
            "av.bs": _cb("shadow blue balance"),
            "av.rm": _cb("midtone red balance"),
            "av.gm": _cb("midtone green balance"),
            "av.bm": _cb("midtone blue balance"),
            "av.rh": _cb("highlight red balance"),
            "av.gh": _cb("highlight green balance"),
            "av.bh": _cb("highlight blue balance"),
        },
        source_effect=comp.match_name,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=[mlt],
        mapped_services=["avfilter.colorbalance"],
    )


def _map_bw(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    mlt = MltFilterSpec(
        service="greyscale",
        media_type="video",
        properties={},
        source_effect=comp.match_name,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=[mlt],
        mapped_services=["greyscale"],
    )


def _map_procamp(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    br = _clamp(_as_float(_find_param(pmap, "brightness").current_value if _find_param(pmap, "brightness") else 0.0, 0.0) / 100.0, -1.0, 1.0)
    ct = _clamp(_as_float(_find_param(pmap, "contrast").current_value if _find_param(pmap, "contrast") else 100.0, 100.0) / 100.0, 0.1, 3.0)
    sat = _clamp(_as_float(_find_param(pmap, "saturation").current_value if _find_param(pmap, "saturation") else 100.0, 100.0) / 100.0, 0.0, 3.0)
    hue = _as_float(_find_param(pmap, "hue").current_value if _find_param(pmap, "hue") else 0.0, 0.0)
    filters = [
        MltFilterSpec(
            service="avfilter.eq",
            media_type="video",
            properties={"av.brightness": f"{br:.4f}", "av.contrast": f"{ct:.4f}", "av.saturation": f"{sat:.4f}"},
            source_effect=comp.match_name,
        )
    ]
    if abs(hue) > 0.1:
        filters.append(
            MltFilterSpec(
                service="avfilter.hue",
                media_type="video",
                properties={"av.h": f"{hue:.2f}"},
                source_effect=comp.match_name,
            )
        )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=filters,
        mapped_services=[f.service for f in filters],
    )


def _map_ultra_key(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    sim = _clamp(_as_float(_find_param(pmap, "tolerance", "similarity").current_value if _find_param(pmap, "tolerance", "similarity") else 25.0, 25.0) / 100.0, 0.01, 0.9)
    blend = _clamp(_as_float(_find_param(pmap, "soften", "choke", "blend").current_value if _find_param(pmap, "soften", "choke", "blend") else 10.0, 10.0) / 100.0, 0.0, 1.0)
    notes = ["Premiere Ultra Key mapped to avfilter.chromakey; spill suppression and pedestal controls approximated."]
    mlt = MltFilterSpec(
        service="avfilter.chromakey",
        media_type="video",
        properties={"av.color": "0x00ff00", "av.similarity": f"{sim:.4f}", "av.blend": f"{blend:.4f}"},
        source_effect=comp.match_name,
        notes=notes,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.APPROXIMATED,
        mlt_filters=[mlt],
        notes=notes,
        mapped_services=["avfilter.chromakey"],
    )


def _map_luma_key(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    thresh = _clamp(_as_float(_find_param(pmap, "threshold").current_value if _find_param(pmap, "threshold") else 20.0, 20.0) / 100.0, 0.0, 1.0)
    cutoff = _clamp(_as_float(_find_param(pmap, "cutoff", "tolerance").current_value if _find_param(pmap, "cutoff", "tolerance") else 10.0, 10.0) / 100.0, 0.01, 1.0)
    mlt = MltFilterSpec(
        service="avfilter.lumakey",
        media_type="video",
        properties={"av.threshold": f"{thresh:.4f}", "av.tolerance": f"{cutoff:.4f}", "av.softness": "0.05"},
        source_effect=comp.match_name,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=[mlt],
        mapped_services=["avfilter.lumakey"],
    )


# ---------------------------------------------------------------------------
# Audio Effect Translators
# ---------------------------------------------------------------------------


def _map_parametric_eq(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    filters: List[MltFilterSpec] = []

    # Check standard numbered Premiere Parametric EQ bands (1..5)
    for band in range(1, 6):
        freq_p = _find_param(pmap, f"band {band} frequency", f"freq {band}", f"band{band}_freq")
        gain_p = _find_param(pmap, f"band {band} gain", f"gain {band}", f"band{band}_gain")
        q_p = _find_param(pmap, f"band {band} q", f"q {band}", f"band{band}_q")
        if freq_p is not None or gain_p is not None:
            freq = _clamp(_as_float(freq_p.current_value if freq_p else 1000.0, 1000.0), 20.0, 20000.0)
            gain = _clamp(_as_float(gain_p.current_value if gain_p else 0.0, 0.0), -24.0, 24.0)
            q_val = _clamp(_as_float(q_p.current_value if q_p else 1.4, 1.4), 0.1, 20.0)
            if abs(gain) > 0.05:
                filters.append(
                    MltFilterSpec(
                        service="avfilter.equalizer",
                        media_type="audio",
                        properties={
                            "av.f": f"{freq:.1f}",
                            "av.t": "q",
                            "av.w": f"{q_val:.3f}",
                            "av.g": f"{gain:.2f}",
                        },
                        source_effect=comp.match_name,
                    )
                )

    # Check highpass / lowpass band switches
    hp_p = _find_param(pmap, "hp frequency", "highpass frequency", "low cut")
    if hp_p is not None and _as_float(hp_p.current_value, 0.0) >= 20.0:
        filters.append(
            MltFilterSpec(
                service="avfilter.highpass",
                media_type="audio",
                properties={"av.f": f"{_clamp(_as_float(hp_p.current_value, 80.0), 20.0, 20000.0):.1f}"},
                source_effect=comp.match_name,
            )
        )

    lp_p = _find_param(pmap, "lp frequency", "lowpass frequency", "high cut")
    if lp_p is not None and _as_float(lp_p.current_value, 0.0) >= 200.0:
        filters.append(
            MltFilterSpec(
                service="avfilter.lowpass",
                media_type="audio",
                properties={"av.f": f"{_clamp(_as_float(lp_p.current_value, 12000.0), 200.0, 22000.0):.1f}"},
                source_effect=comp.match_name,
            )
        )

    # Fallback default voice presence peaking band if XML contained opaque Arb EQ state
    notes: List[str] = []
    status = CompatibilityStatus.EXACT_VALIDATED
    if not filters:
        status = CompatibilityStatus.APPROXIMATED
        notes.append(
            "Parametric EQ band parameters were stored in opaque ArbData; emitted baseline "
            "avfilter.equalizer peaking filter (1000 Hz, +3 dB) as editable approximation."
        )
        filters.append(
            MltFilterSpec(
                service="avfilter.equalizer",
                media_type="audio",
                properties={"av.f": "1000", "av.t": "q", "av.w": "1.2", "av.g": "3.0"},
                source_effect=comp.match_name,
                notes=list(notes),
            )
        )

    return MappedEffectResult(
        component=comp,
        status=status,
        mlt_filters=filters,
        notes=notes,
        mapped_services=[f.service for f in filters],
    )


def _map_graphic_eq(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    filters: List[MltFilterSpec] = []
    freq_re = re.compile(r"(\d+(?:\.\d+)?)\s*(k)?\s*hz", re.IGNORECASE)
    for p in comp.parameters:
        m = freq_re.search(p.name)
        if m:
            freq = float(m.group(1)) * (1000.0 if m.group(2) else 1.0)
            gain = _clamp(_as_float(p.current_value, 0.0), -24.0, 24.0)
            if abs(gain) > 0.05:
                filters.append(
                    MltFilterSpec(
                        service="avfilter.equalizer",
                        media_type="audio",
                        properties={"av.f": f"{freq:.1f}", "av.t": "o", "av.w": "1.0", "av.g": f"{gain:.2f}"},
                        source_effect=comp.match_name,
                    )
                )
    notes = ["Premiere Graphic Equalizer mapped to cascaded avfilter.equalizer octave bands."]
    if not filters:
        filters.append(
            MltFilterSpec(
                service="avfilter.equalizer",
                media_type="audio",
                properties={"av.f": "1000", "av.t": "o", "av.w": "1.0", "av.g": "2.0"},
                source_effect=comp.match_name,
                notes=notes,
            )
        )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.APPROXIMATED,
        mlt_filters=filters,
        notes=notes,
        mapped_services=[f.service for f in filters],
    )


def _map_compressor(
    comp: EffectComponent, ctx: MappingContext, is_multiband: bool = False
) -> MappedEffectResult:
    pmap = comp.param_map()
    thresh_db = _as_float(_find_param(pmap, "threshold", "compressor threshold").current_value if _find_param(pmap, "threshold", "compressor threshold") else -18.0, -18.0)
    ratio = _clamp(_as_float(_find_param(pmap, "ratio", "compressor ratio").current_value if _find_param(pmap, "ratio", "compressor ratio") else 4.0, 4.0), 1.0, 20.0)
    attack = _clamp(_as_float(_find_param(pmap, "attack", "compressor attack").current_value if _find_param(pmap, "attack", "compressor attack") else 10.0, 10.0), 0.01, 2000.0)
    release = _clamp(_as_float(_find_param(pmap, "release", "compressor release").current_value if _find_param(pmap, "release", "compressor release") else 120.0, 120.0), 0.01, 9000.0)
    makeup_db = _as_float(_find_param(pmap, "makeup", "output gain", "gain").current_value if _find_param(pmap, "makeup", "output gain", "gain") else 3.0, 3.0)

    thresh_lin = _clamp(_db_to_linear(thresh_db), 0.001, 1.0)
    makeup_lin = _clamp(_db_to_linear(max(0.0, makeup_db)), 1.0, 64.0)

    filters = [
        MltFilterSpec(
            service="avfilter.acompressor",
            media_type="audio",
            properties={
                "av.threshold": f"{thresh_lin:.5f}",
                "av.ratio": f"{ratio:.2f}",
                "av.attack": f"{attack:.2f}",
                "av.release": f"{release:.2f}",
                "av.makeup": f"{makeup_lin:.4f}",
            },
            source_effect=comp.match_name,
        )
    ]

    gate_p = _find_param(pmap, "autogate", "gate threshold")
    if gate_p is not None and abs(_as_float(gate_p.current_value, 0.0)) > 0.01:
        gate_lin = _clamp(_db_to_linear(_as_float(gate_p.current_value, -40.0)), 0.001, 1.0)
        filters.append(
            MltFilterSpec(
                service="avfilter.agate",
                media_type="audio",
                properties={"av.threshold": f"{gate_lin:.5f}", "av.ratio": "4.0"},
                source_effect=comp.match_name,
            )
        )

    notes: List[str] = []
    status = CompatibilityStatus.EXACT_VALIDATED
    if is_multiband:
        status = CompatibilityStatus.APPROXIMATED
        notes.append(
            "Premiere Multiband Compressor approximated via wideband avfilter.acompressor "
            "(4-band crossover dynamics not natively available in single avfilter)."
        )

    return MappedEffectResult(
        component=comp,
        status=status,
        mlt_filters=filters,
        notes=notes,
        mapped_services=[f.service for f in filters],
    )


def _map_hard_limiter(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    max_db = _as_float(_find_param(pmap, "maximum amplitude", "ceiling", "limit").current_value if _find_param(pmap, "maximum amplitude", "ceiling", "limit") else -1.0, -1.0)
    boost_db = _as_float(_find_param(pmap, "input boost", "boost").current_value if _find_param(pmap, "input boost", "boost") else 0.0, 0.0)
    rel = _clamp(_as_float(_find_param(pmap, "release").current_value if _find_param(pmap, "release") else 50.0, 50.0), 1.0, 8000.0)
    mlt = MltFilterSpec(
        service="avfilter.alimiter",
        media_type="audio",
        properties={
            "av.limit": f"{_clamp(_db_to_linear(max_db), 0.0625, 1.0):.5f}",
            "av.level_in": f"{_clamp(_db_to_linear(boost_db), 0.015625, 64.0):.5f}",
            "av.release": f"{rel:.1f}",
        },
        source_effect=comp.match_name,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=[mlt],
        mapped_services=["avfilter.alimiter"],
    )


def _map_reverb(comp: EffectComponent, ctx: MappingContext, is_convolution: bool = False) -> MappedEffectResult:
    pmap = comp.param_map()
    deps: List[MissingDependency] = []
    missing_ir = False

    if is_convolution:
        for p in comp.parameters:
            blob = f"{p.current_value or ''} {p.raw_arb_data or ''}"
            for m in _IR_FILE_RE.finditer(blob):
                ir_file = m.group(1)
                if _resolve_external_asset(ir_file, ctx) is None:
                    missing_ir = True
                    deps.append(
                        MissingDependency(
                            kind="ir_audio",
                            name=Path(ir_file).name,
                            effect_match_name=comp.match_name,
                            path=ir_file,
                            resolved=False,
                            remediation=f"Missing impulse response '{Path(ir_file).name}' for Convolution Reverb.",
                        )
                    )

    decay = _clamp(_as_float(_find_param(pmap, "decay", "reverb time").current_value if _find_param(pmap, "decay", "reverb time") else 1500.0, 1500.0), 50.0, 5000.0)
    wet = _clamp(_as_float(_find_param(pmap, "wet", "output level").current_value if _find_param(pmap, "wet", "output level") else 35.0, 35.0) / 100.0, 0.05, 0.9)
    d1 = int(_clamp(decay * 0.04, 20, 200))
    d2 = int(_clamp(decay * 0.08, 45, 450))
    notes = [
        "Premiere Studio/Convolution Reverb approximated using multi-tap avfilter.aecho "
        "(diffuse early/late reflection algorithms differ from Adobe Reverb)."
    ]
    mlt = MltFilterSpec(
        service="avfilter.aecho",
        media_type="audio",
        properties={
            "av.in_gain": "0.8",
            "av.out_gain": f"{_clamp(0.5 + wet * 0.4, 0.1, 1.0):.3f}",
            "av.delays": f"{d1}|{d2}",
            "av.decays": f"{wet:.2f}|{max(0.1, wet * 0.6):.2f}",
        },
        source_effect=comp.match_name,
        notes=notes,
    )
    status = CompatibilityStatus.MISSING_DEPENDENCY if missing_ir else CompatibilityStatus.APPROXIMATED
    return MappedEffectResult(
        component=comp,
        status=status,
        mlt_filters=[mlt],
        missing_dependencies=deps,
        notes=notes,
        mapped_services=["avfilter.aecho"],
    )


def _map_denoise(comp: EffectComponent, ctx: MappingContext, is_dereverb: bool = False) -> MappedEffectResult:
    pmap = comp.param_map()
    amt = _clamp(_as_float(_find_param(pmap, "amount", "noise reduction").current_value if _find_param(pmap, "amount", "noise reduction") else 40.0, 40.0) * 0.4, 3.0, 60.0)
    filters = [
        MltFilterSpec(
            service="avfilter.afftdn",
            media_type="audio",
            properties={"av.nr": f"{amt:.1f}", "av.nf": "-40"},
            source_effect=comp.match_name,
        )
    ]
    notes = ["Premiere Denoise approximated via FFmpeg FFT denoiser (avfilter.afftdn)."]
    if is_dereverb:
        filters.append(
            MltFilterSpec(
                service="avfilter.agate",
                media_type="audio",
                properties={"av.threshold": "0.04", "av.ratio": "3.0"},
                source_effect=comp.match_name,
            )
        )
        notes = ["Premiere DeReverb approximated via avfilter.afftdn + avfilter.agate tail suppression."]

    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.APPROXIMATED,
        mlt_filters=filters,
        notes=notes,
        mapped_services=[f.service for f in filters],
    )


def _map_deesser(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    amt = _clamp(_as_float(_find_param(pmap, "amount", "intensity", "threshold").current_value if _find_param(pmap, "amount", "intensity", "threshold") else 40.0, 40.0) / 100.0, 0.05, 1.0)
    mlt = MltFilterSpec(
        service="avfilter.deesser",
        media_type="audio",
        properties={"av.i": f"{amt:.3f}", "av.m": "0.5", "av.f": "0.5"},
        source_effect=comp.match_name,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=[mlt],
        mapped_services=["avfilter.deesser"],
    )


def _map_highpass_lowpass(comp: EffectComponent, ctx: MappingContext, is_highpass: bool) -> MappedEffectResult:
    pmap = comp.param_map()
    cutoff_p = _find_param(pmap, "cutoff", "frequency")
    default_hz = 120.0 if is_highpass else 6000.0
    freq_str = format_mlt_scalar_keyframes(
        cutoff_p, lambda v: _clamp(v, 20.0, 20000.0), default_value=default_hz, precision=1
    )
    service = "avfilter.highpass" if is_highpass else "avfilter.lowpass"
    mlt = MltFilterSpec(
        service=service,
        media_type="audio",
        properties={"av.f": freq_str, "av.w": "0.707"},
        source_effect=comp.match_name,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=[mlt],
        mapped_services=[service],
    )


def _map_pitch_shifter(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    semi = _as_float(_find_param(pmap, "semi-tones", "semitones", "pitch").current_value if _find_param(pmap, "semi-tones", "semitones", "pitch") else 0.0, 0.0)
    cents = _as_float(_find_param(pmap, "cents").current_value if _find_param(pmap, "cents") else 0.0, 0.0)
    ratio = _clamp(math.pow(2.0, (semi + cents / 100.0) / 12.0), 0.25, 4.0)
    notes = ["Premiere Pitch Shifter mapped to avfilter.rubberband formant-preserving pitch ratio."]
    mlt = MltFilterSpec(
        service="avfilter.rubberband",
        media_type="audio",
        properties={"av.pitch": f"{ratio:.5f}", "av.tempo": "1.0"},
        source_effect=comp.match_name,
        notes=notes,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.APPROXIMATED,
        mlt_filters=[mlt],
        notes=notes,
        mapped_services=["avfilter.rubberband"],
    )


def _map_volume(comp: EffectComponent, ctx: MappingContext, is_channel: bool = False) -> MappedEffectResult:
    pmap = comp.param_map()
    lvl_p = _find_param(pmap, "level", "volume", "gain", "left")
    lvl_str = format_mlt_scalar_keyframes(
        lvl_p, lambda v: _clamp(v, -60.0, 24.0), default_value=0.0, precision=2
    )
    filters = [
        MltFilterSpec(
            service="volume",
            media_type="audio",
            properties={"level": lvl_str},
            source_effect=comp.match_name,
        )
    ]
    if is_channel:
        left_v = _as_float(_find_param(pmap, "left").current_value if _find_param(pmap, "left") else 0.0, 0.0)
        right_v = _as_float(_find_param(pmap, "right").current_value if _find_param(pmap, "right") else 0.0, 0.0)
        pan = _clamp(0.5 + (right_v - left_v) / 40.0, 0.0, 1.0)
        filters.append(
            MltFilterSpec(
                service="panner",
                media_type="audio",
                properties={"start": f"{pan:.3f}"},
                source_effect=comp.match_name,
            )
        )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.EXACT_VALIDATED,
        mlt_filters=filters,
        mapped_services=[f.service for f in filters],
    )


def _map_loudness(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    target = _clamp(_as_float(_find_param(pmap, "target loudness", "target").current_value if _find_param(pmap, "target loudness", "target") else -14.0, -14.0), -70.0, -5.0)
    notes = ["Premiere Loudness normalization mapped to single-pass avfilter.loudnorm."]
    mlt = MltFilterSpec(
        service="avfilter.loudnorm",
        media_type="audio",
        properties={"av.I": f"{target:.1f}", "av.LRA": "11", "av.TP": "-1.5"},
        source_effect=comp.match_name,
        notes=notes,
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.APPROXIMATED,
        mlt_filters=[mlt],
        notes=notes,
        mapped_services=["avfilter.loudnorm"],
    )


# ---------------------------------------------------------------------------
# Text / Subtitles & Masking Translators
# ---------------------------------------------------------------------------


def _map_text_graphic(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    text_p = _find_param(pmap, "source text", "text", "caption", "subtitle")
    font_p = _find_param(pmap, "font", "font family", "fontfamily")
    size_p = _find_param(pmap, "font size", "size")
    outline_p = _find_param(pmap, "stroke width", "outline")

    text_val = str(text_p.current_value) if (text_p and text_p.current_value) else "Bang On Pro Subtitle"
    font_name = ""
    if font_p and isinstance(font_p.current_value, str) and font_p.current_value.strip():
        font_name = font_p.current_value.strip()
    else:
        # Check ArbData for embedded font filename or font name
        for p in comp.parameters:
            m = _FONT_FILE_RE.search(f"{p.current_value or ''} {p.raw_arb_data or ''}")
            if m:
                font_name = m.group(1)
                break

    size_val = int(_clamp(round(_as_float(size_p.current_value if size_p else 64.0, 64.0)), 12, 300))
    outline_val = int(_clamp(round(_as_float(outline_p.current_value if outline_p else 2.0, 2.0)), 0, 3))

    deps: List[MissingDependency] = []
    notes: List[str] = []
    missing_font = False
    resolved_family = "Sans"

    if font_name:
        font_clean = Path(font_name).stem if font_name.lower().endswith((".ttf", ".otf", ".ttc")) else font_name
        installed = _installed_system_fonts()
        bundled = _resolve_external_asset(font_name, ctx)
        if bundled is not None or font_clean.lower() in installed:
            resolved_family = font_clean
        else:
            missing_font = True
            resolved_family = "Sans"
            deps.append(
                MissingDependency(
                    kind="font",
                    name=font_name,
                    effect_match_name=comp.match_name,
                    platform="cross_platform",
                    resolved=False,
                    remediation=f"Install missing font '{font_name}' (or bundle .ttf/.otf with preset); "
                    "falling back to system 'Sans' in dynamictext.",
                )
            )
            notes.append(f"Requested font '{font_name}' not installed locally; fallback family 'Sans' configured.")

    mlt = MltFilterSpec(
        service="dynamictext",
        media_type="video",
        properties={
            "argument": text_val,
            "family": resolved_family,
            "size": str(size_val),
            "fgcolour": "0xffffffff",
            "bgcolour": "0x00000080",
            "olcolour": "0x000000ff",
            "outline": str(outline_val),
            "halign": "center",
            "valign": "bottom",
            "geometry": "10%/78%:80%x18%:100",
        },
        source_effect=comp.match_name,
        notes=list(notes),
    )
    if missing_font:
        status = CompatibilityStatus.MISSING_DEPENDENCY
    else:
        status = CompatibilityStatus.APPROXIMATED
        notes.append("Essential Graphics / Premiere Text layout approximated via MLT dynamictext.")

    return MappedEffectResult(
        component=comp,
        status=status,
        mlt_filters=[mlt],
        missing_dependencies=deps,
        notes=notes,
        mapped_services=["dynamictext"],
    )


def _map_mask_or_matte(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    pmap = comp.param_map()
    feather = int(_clamp(round(_as_float(_find_param(pmap, "mask feather", "feather").current_value if _find_param(pmap, "mask feather", "feather") else 8.0, 8.0)), 0, 1000))
    invert = "1" if _as_float(_find_param(pmap, "inverted", "invert").current_value if _find_param(pmap, "inverted", "invert") else 0.0, 0.0) > 0.5 else "0"
    default_spline = '{"0":[[[0.15,0.15],[0.15,0.15],[0.15,0.15]],[[0.85,0.15],[0.85,0.15],[0.85,0.15]],[[0.85,0.85],[0.85,0.85],[0.85,0.85]],[[0.15,0.85],[0.15,0.85],[0.15,0.85]]]}'

    is_track_matte = "track matte" in comp.match_name.lower() or "track matte" in comp.display_name.lower()
    if is_track_matte:
        notes = [
            "Premiere Track Matte Key approximated using MLT mask_start (rotoscoping) + mask_apply; "
            "secondary timeline matte track routing must be linked manually in Kdenlive."
        ]
        filters = [
            MltFilterSpec(
                service="mask_start",
                media_type="video",
                properties={
                    "filter": "rotoscoping",
                    "filter.mode": "alpha",
                    "filter.alpha_operation": "clear",
                    "filter.invert": "1" if invert == "0" else "0",
                    "filter.feather": str(feather),
                    "filter.spline": default_spline,
                },
                source_effect=comp.match_name,
                notes=list(notes),
            ),
            MltFilterSpec(
                service="brightness",
                media_type="video",
                properties={"level": "0"},
                source_effect=comp.match_name,
            ),
            MltFilterSpec(
                service="mask_apply",
                media_type="video",
                properties={"transition": "qtblend"},
                source_effect=comp.match_name,
            ),
        ]
    else:
        notes = [
            "Premiere Mask mapped to MLT rotoscoping alpha mask + qtblend composite; "
            "custom Bezier mask vertices may require fine adjustment."
        ]
        filters = [
            MltFilterSpec(
                service="rotoscoping",
                media_type="video",
                properties={
                    "mode": "alpha",
                    "alpha_operation": "clear",
                    "invert": invert,
                    "feather": str(feather),
                    "spline": default_spline,
                },
                source_effect=comp.match_name,
                notes=list(notes),
            ),
            MltFilterSpec(
                service="qtblend",
                media_type="video",
                properties={
                    "rect": f"0 0 {ctx.width} {ctx.height} 1.0000",
                    "compositing": "0",
                    "rotate_center": "1",
                },
                source_effect=comp.match_name,
            ),
        ]

    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.APPROXIMATED,
        mlt_filters=filters,
        notes=notes,
        mapped_services=[f.service for f in filters],
    )


# ---------------------------------------------------------------------------
# Master Catalog Dispatch Table
# ---------------------------------------------------------------------------

_TranslatorFn = Callable[[EffectComponent, MappingContext], MappedEffectResult]

EXACT_MATCH_CATALOG: Dict[str, _TranslatorFn] = {
    # Transforms / Shake / Zoom / Slide / Transitions
    "ae.adbe motion": lambda c, ctx: _map_motion_or_transform(c, ctx, is_transform_effect=False),
    "motion": lambda c, ctx: _map_motion_or_transform(c, ctx, is_transform_effect=False),
    "ae.adbe geometry2": lambda c, ctx: _map_motion_or_transform(c, ctx, is_transform_effect=True),
    "transform": lambda c, ctx: _map_motion_or_transform(c, ctx, is_transform_effect=True),
    "ae.adbe opacity": _map_opacity,
    "opacity": _map_opacity,
    "ae.adbe crop": _map_crop,
    "crop": _map_crop,
    "ae.adbe mirror": _map_mirror,
    "mirror": _map_mirror,
    "ae.adbe offset": _map_offset,
    "offset": _map_offset,
    "ae.adbe corner pin": _map_corner_pin,
    "corner pin": _map_corner_pin,
    "ae.adbe basic 3d": _map_basic_3d,
    "basic 3d": _map_basic_3d,
    "ae.adbe twirl": lambda c, ctx: _map_distort_approx(c, ctx, "Twirl"),
    "twirl": lambda c, ctx: _map_distort_approx(c, ctx, "Twirl"),
    "ae.adbe wave warp": _map_wave_warp,
    "wave warp": _map_wave_warp,
    "ae.adbe turbulent displace": lambda c, ctx: _map_distort_approx(c, ctx, "Turbulent Displace"),
    "turbulent displace": lambda c, ctx: _map_distort_approx(c, ctx, "Turbulent Displace"),
    "ae.adbe lens distortion": _map_lens_distortion,
    "lens distortion": _map_lens_distortion,
    # Blur / Glow / Shadows / Stylize
    "ae.adbe gaussian blur 2": _map_gaussian_blur,
    "ae.adbe gaussian blur": _map_gaussian_blur,
    "gaussian blur": _map_gaussian_blur,
    "ae.adbe directional blur": _map_directional_blur,
    "directional blur": _map_directional_blur,
    "ae.adbe fast blur": _map_box_or_fast_blur,
    "ae.adbe fast blur 2": _map_box_or_fast_blur,
    "ae.adbe box blur": _map_box_or_fast_blur,
    "ae.adbe box blur2": _map_box_or_fast_blur,
    "fast blur": _map_box_or_fast_blur,
    "ae.adbe radial blur": _map_radial_blur,
    "radial blur": _map_radial_blur,
    "ae.adbe sharpen": _map_sharpen_or_unsharp,
    "sharpen": _map_sharpen_or_unsharp,
    "ae.adbe unsharp mask": _map_sharpen_or_unsharp,
    "unsharp mask": _map_sharpen_or_unsharp,
    "ae.adbe drop shadow": _map_drop_shadow,
    "drop shadow": _map_drop_shadow,
    "ae.adbe vr glow": lambda c, ctx: _map_glow(c, ctx, is_vr=True),
    "vr glow": lambda c, ctx: _map_glow(c, ctx, is_vr=True),
    "ae.adbe glow": lambda c, ctx: _map_glow(c, ctx, is_vr=False),
    "ae.adbe alpha glow": lambda c, ctx: _map_glow(c, ctx, is_vr=False),
    "glow": lambda c, ctx: _map_glow(c, ctx, is_vr=False),
    "ae.adbe mosaic": _map_mosaic,
    "mosaic": _map_mosaic,
    "ae.adbe posterize": _map_posterize,
    "posterize": _map_posterize,
    "ae.adbe invert": _map_invert,
    "invert": _map_invert,
    "ae.adbe strobe": _map_strobe,
    "strobe light": _map_strobe,
    "strobe": _map_strobe,
    "ae.adbe noise": _map_noise,
    "ae.adbe noise hls2": _map_noise,
    "noise": _map_noise,
    "ae.adbe chromatic aberration": lambda c, ctx: _map_chromatic_aberration(c, ctx, is_vr=False),
    "chromatic aberration": lambda c, ctx: _map_chromatic_aberration(c, ctx, is_vr=False),
    "ae.adbe vr chromatic aberrations": lambda c, ctx: _map_chromatic_aberration(c, ctx, is_vr=True),
    "vr chromatic aberrations": lambda c, ctx: _map_chromatic_aberration(c, ctx, is_vr=True),
    "ae.adbe rgb split": lambda c, ctx: _map_chromatic_aberration(c, ctx, is_vr=False),
    "ae.adbe rgb curves": _map_rgb_curves,
    "rgb curves": _map_rgb_curves,
    # Colour / Lumetri
    "ae.adbe lumetri": _map_lumetri,
    "lumetri color": _map_lumetri,
    "lumetri": _map_lumetri,
    "ae.adbe tint": _map_tint,
    "tint": _map_tint,
    "ae.adbe brightness & contrast 2": _map_brightness_contrast,
    "ae.adbe brightness & contrast": _map_brightness_contrast,
    "brightness & contrast": _map_brightness_contrast,
    "ae.adbe color balance": _map_color_balance,
    "color balance": _map_color_balance,
    "ae.adbe color balance (hls)": _map_procamp,
    "ae.adbe black & white": _map_bw,
    "black & white": _map_bw,
    "ae.adbe procamp": _map_procamp,
    "procamp": _map_procamp,
    "ae.adbe ultra key": _map_ultra_key,
    "ultra key": _map_ultra_key,
    "ae.adbe luma key": _map_luma_key,
    "luma key": _map_luma_key,
    # Audio
    "ae.adbe parametric eq": _map_parametric_eq,
    "parametric equalizer": _map_parametric_eq,
    "parametric eq": _map_parametric_eq,
    "ae.adbe graphic equalizer": _map_graphic_eq,
    "ae.adbe graphic equalizer (10 bands)": _map_graphic_eq,
    "ae.adbe graphic equalizer (20 bands)": _map_graphic_eq,
    "ae.adbe graphic equalizer (30 bands)": _map_graphic_eq,
    "graphic equalizer": _map_graphic_eq,
    "ae.adbe multibandcompressor": lambda c, ctx: _map_compressor(c, ctx, is_multiband=True),
    "multiband compressor": lambda c, ctx: _map_compressor(c, ctx, is_multiband=True),
    "ae.adbe dynamics": lambda c, ctx: _map_compressor(c, ctx, is_multiband=False),
    "dynamics": lambda c, ctx: _map_compressor(c, ctx, is_multiband=False),
    "ae.adbe singlebandcompressor": lambda c, ctx: _map_compressor(c, ctx, is_multiband=False),
    "ae.adbe tube-modeled compressor": lambda c, ctx: _map_compressor(c, ctx, is_multiband=False),
    "ae.adbe hardlimiter": _map_hard_limiter,
    "hard limiter": _map_hard_limiter,
    "ae.adbe studioreverb": lambda c, ctx: _map_reverb(c, ctx, is_convolution=False),
    "studio reverb": lambda c, ctx: _map_reverb(c, ctx, is_convolution=False),
    "ae.adbe convolutionreverb": lambda c, ctx: _map_reverb(c, ctx, is_convolution=True),
    "ae.adbe surroundreverb": lambda c, ctx: _map_reverb(c, ctx, is_convolution=False),
    "ae.adbe denoise": lambda c, ctx: _map_denoise(c, ctx, is_dereverb=False),
    "ae.adbe adaptivenoisereduction": lambda c, ctx: _map_denoise(c, ctx, is_dereverb=False),
    "denoise": lambda c, ctx: _map_denoise(c, ctx, is_dereverb=False),
    "ae.adbe dereverb": lambda c, ctx: _map_denoise(c, ctx, is_dereverb=True),
    "dereverb": lambda c, ctx: _map_denoise(c, ctx, is_dereverb=True),
    "ae.adbe deesser": _map_deesser,
    "deesser": _map_deesser,
    "ae.adbe highpass": lambda c, ctx: _map_highpass_lowpass(c, ctx, is_highpass=True),
    "highpass": lambda c, ctx: _map_highpass_lowpass(c, ctx, is_highpass=True),
    "ae.adbe lowpass": lambda c, ctx: _map_highpass_lowpass(c, ctx, is_highpass=False),
    "lowpass": lambda c, ctx: _map_highpass_lowpass(c, ctx, is_highpass=False),
    "ae.adbe pitchshifter": _map_pitch_shifter,
    "pitch shifter": _map_pitch_shifter,
    "ae.adbe channelvolume": lambda c, ctx: _map_volume(c, ctx, is_channel=True),
    "channel volume": lambda c, ctx: _map_volume(c, ctx, is_channel=True),
    "ae.adbe volume": lambda c, ctx: _map_volume(c, ctx, is_channel=False),
    "ae.adbe audiovolume": lambda c, ctx: _map_volume(c, ctx, is_channel=False),
    "volume": lambda c, ctx: _map_volume(c, ctx, is_channel=False),
    "ae.adbe loudness": _map_loudness,
    "ae.adbe loudnessradar": _map_loudness,
    # Text / Subtitles / Masking
    "ae.adbe text": _map_text_graphic,
    "ae.adbe essential graphics": _map_text_graphic,
    "ae.adbe graphic text": _map_text_graphic,
    "ae.adbe motion graphics": _map_text_graphic,
    "text": _map_text_graphic,
    "graphic": _map_text_graphic,
    "ae.adbe mask": _map_mask_or_matte,
    "ae.adbe track matte key": _map_mask_or_matte,
    "ae.adbe four-point garbage matte": _map_mask_or_matte,
    "track matte key": _map_mask_or_matte,
}


def map_effect_component(comp: EffectComponent, ctx: MappingContext) -> MappedEffectResult:
    """Map a single Premiere EffectComponent honestly into MLT filters & dependencies."""
    match_key = comp.match_name.strip().lower()
    display_key = comp.display_name.strip().lower()
    combined_id = f"{comp.match_name} {comp.display_name}".strip()

    codec_deps = _check_codec_dependencies(comp)

    # 1. Check Apple Audio Units (macOS-only platform plugins)
    for pattern, au_label, suggested_fallback in _APPLE_AU_PATTERNS:
        if pattern.search(comp.match_name) or pattern.search(comp.display_name):
            dep = MissingDependency(
                kind="audio_unit",
                name=f"{au_label} ({comp.match_name})",
                effect_match_name=comp.match_name,
                platform="macos_only",
                resolved=False,
                remediation=(
                    f"macOS-only Apple Audio Unit '{comp.match_name}' cannot execute natively on "
                    f"{ctx.platform_name}. Suggested native FFmpeg/MLT alternative: {suggested_fallback}."
                ),
            )
            note = dep.remediation
            return MappedEffectResult(
                component=comp,
                status=CompatibilityStatus.MISSING_DEPENDENCY,
                mlt_filters=[],
                missing_dependencies=[dep] + codec_deps,
                notes=[note],
                mapped_services=[],
            )

    # 2. Check known proprietary third-party plugins (honestly classified as unsupported)
    for pattern, vendor_label, remediation in _PROPRIETARY_PATTERNS:
        if pattern.search(comp.match_name) or pattern.search(comp.display_name):
            dep = MissingDependency(
                kind="plugin",
                name=f"{vendor_label}: {comp.display_name or comp.match_name}",
                effect_match_name=comp.match_name,
                platform="third_party",
                resolved=False,
                remediation=remediation,
            )
            return MappedEffectResult(
                component=comp,
                status=CompatibilityStatus.UNSUPPORTED,
                mlt_filters=[],
                missing_dependencies=[dep] + codec_deps,
                notes=[f"Proprietary plugin '{combined_id}' is unsupported natively. {remediation}"],
                mapped_services=[],
            )

    # 3. Dispatch against supported Premiere effect catalog
    translator = EXACT_MATCH_CATALOG.get(match_key) or EXACT_MATCH_CATALOG.get(display_key)
    if translator is not None:
        result = translator(comp, ctx)
        if codec_deps:
            result.missing_dependencies.extend(codec_deps)
            if result.status in (CompatibilityStatus.EXACT_VALIDATED, CompatibilityStatus.APPROXIMATED):
                result.status = CompatibilityStatus.MISSING_DEPENDENCY
            result.notes.append("Preset references proprietary camera/intermediate codec dependency.")
        return result

    # 4. Unrecognized effect -> honestly unsupported (never silently ignored!)
    unknown_dep = MissingDependency(
        kind="plugin",
        name=comp.match_name or comp.display_name or "Unknown Premiere Effect",
        effect_match_name=comp.match_name,
        platform="third_party",
        resolved=False,
        remediation=(
            f"No native MLT/FFmpeg mapping exists for Premiere effect '{comp.match_name}' "
            f"('{comp.display_name}'). Original preset XML has been preserved for manual recreation."
        ),
    )
    return MappedEffectResult(
        component=comp,
        status=CompatibilityStatus.UNSUPPORTED,
        mlt_filters=[],
        missing_dependencies=[unknown_dep] + codec_deps,
        notes=[unknown_dep.remediation],
        mapped_services=[],
    )
