"""Built-in Legal Native Starter Effects Library for Bang On Pro.

Provides 64 curated, 100% original, GPL-3.0 native MLT/FFmpeg/frei0r presets spanning
all mappable Premiere Pro effect categories so editors have an immediately usable,
searchable, previewable effects library even without importing proprietary .prfpset packs.

SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .mapper import export_report_bundle, generate_kdenlive_effect_xml
from .models import (
    SCHEMA_VERSION,
    CompatibilityReport,
    CompatibilityStatus,
    MappedPreset,
    MltFilterSpec,
    SourceArchiveInfo,
)

# Each entry:
# (slug, name, category, tags, description, media_type, status, list[(service, properties)])
_PresetDef = Tuple[
    str,
    str,
    str,
    Tuple[str, ...],
    str,
    str,
    CompatibilityStatus,
    List[Tuple[str, Dict[str, str]]],
]


BUILTIN_STARTER_DEFINITIONS: Tuple[_PresetDef, ...] = (
    # =========================================================================
    # 1. Colour & Film Looks (12 presets)
    # =========================================================================
    (
        "color_cinematic_teal_orange",
        "Cinematic Teal & Orange",
        "Colour & Film Looks",
        ("cinema", "teal-orange", "lumetri", "grade"),
        "Balanced teal shadows and warm skin-tone highlights with subtle film contrast.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.eq", {"av.contrast": "1.1400", "av.saturation": "1.1200", "av.gamma": "0.9800"}),
            ("avfilter.colorbalance", {"av.rs": "-0.0800", "av.bs": "0.1000", "av.rh": "0.1000", "av.bh": "-0.0800"}),
        ],
    ),
    (
        "color_film_noir_bw",
        "Film Noir High-Contrast B&W",
        "Colour & Film Looks",
        ("bw", "noir", "monochrome", "classic"),
        "Classic punchy monochrome grade with crushed blacks and crisp midtone separation.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("greyscale", {}),
            ("avfilter.eq", {"av.contrast": "1.3500", "av.brightness": "-0.0400"}),
        ],
    ),
    (
        "color_vintage_warm_sepia",
        "Vintage Warm Sepia Tint",
        "Colour & Film Looks",
        ("vintage", "sepia", "tint", "retro"),
        "Archival warm duotone tint using frei0r.tint0r with gentle vignette.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("frei0r.tint0r", {"0": "#1a1008", "1": "#f6dec2", "2": "0.7500"}),
            ("avfilter.vignette", {"av.angle": "0.4500", "av.mode": "forward"}),
        ],
    ),
    (
        "color_broadcast_rec709_clean",
        "Broadcast Rec.709 Clean Pop",
        "Colour & Film Looks",
        ("broadcast", "clean", "youtube", "interview"),
        "Neutral interview polish lifting contrast and natural saturation without clipping.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.eq", {"av.brightness": "0.0200", "av.contrast": "1.0800", "av.saturation": "1.0800"}),
            ("avfilter.vibrance", {"av.intensity": "0.2200"}),
        ],
    ),
    (
        "color_neon_vibrance_boost",
        "Creator Neon Vibrance Boost",
        "Colour & Film Looks",
        ("vibrance", "vivid", "creator", "gaming"),
        "Smart skin-safe saturation boost for tech, gaming, and vibrant creator footage.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.vibrance", {"av.intensity": "0.6500"}),
            ("avfilter.eq", {"av.contrast": "1.1000"}),
        ],
    ),
    (
        "color_golden_hour_warmth",
        "Golden Hour Warmth",
        "Colour & Film Looks",
        ("warm", "sunset", "golden", "lifestyle"),
        "Soft amber midtones and warm golden highlights ideal for outdoor vlogs.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.colorbalance", {"av.rm": "0.1400", "av.gm": "0.0400", "av.bm": "-0.1200"}),
            ("avfilter.eq", {"av.gamma": "1.0500", "av.saturation": "1.1000"}),
        ],
    ),
    (
        "color_bleach_bypass",
        "Bleach Bypass Gritty",
        "Colour & Film Looks",
        ("bleach", "gritty", "documentary", "cinema"),
        "Desaturated high-contrast metallic film process look.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.eq", {"av.contrast": "1.3200", "av.saturation": "0.4500", "av.gamma": "0.9200"}),
        ],
    ),
    (
        "color_cool_documentary_slate",
        "Cool Documentary Slate",
        "Colour & Film Looks",
        ("cool", "documentary", "blue", "editorial"),
        "Restrained cool steel midtones with clean neutral highlights.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.colorbalance", {"av.rm": "-0.0800", "av.bm": "0.1100", "av.rs": "-0.0400", "av.bs": "0.0600"}),
            ("avfilter.eq", {"av.contrast": "1.0800", "av.saturation": "0.9000"}),
        ],
    ),
    (
        "color_s_curve_film_punch",
        "RGB S-Curve Film Punch",
        "Colour & Film Looks",
        ("curves", "s-curve", "contrast", "film"),
        "Classic photographic S-curve tone response via FFmpeg RGB curves.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.curves", {"av.master": "0/0 0.25/0.19 0.5/0.5 0.75/0.82 1/1"}),
        ],
    ),
    (
        "color_matte_pastel_fade",
        "Matte Pastel Faded Blacks",
        "Colour & Film Looks",
        ("matte", "pastel", "faded", "levels"),
        "Lifted matte shadow floor and gentle highlight roll-off via frei0r.levels.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("frei0r.levels", {"0": "0.3", "1": "0.0000", "2": "1.0000", "3": "0.2600", "4": "0.0900", "5": "0.9400"}),
            ("avfilter.eq", {"av.saturation": "0.8600"}),
        ],
    ),
    (
        "color_anamorphic_vignette",
        "Anamorphic Cinema Vignette",
        "Colour & Film Looks",
        ("vignette", "cinema", "lens", "framing"),
        "Natural optical corner falloff that draws attention toward center subject.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.vignette", {"av.angle": "0.6200", "av.mode": "forward"}),
        ],
    ),
    (
        "color_three_way_studio_lift",
        "Three-Way Studio Shadow Lift",
        "Colour & Film Looks",
        ("lift-gamma-gain", "three-way", "studio", "grade"),
        "Three-way lift/gamma/gain studio correction opening crushed shadows cleanly.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            (
                "lift_gamma_gain",
                {
                    "lift_r": "0.0400",
                    "lift_g": "0.0400",
                    "lift_b": "0.0550",
                    "gamma_r": "1.0600",
                    "gamma_g": "1.0500",
                    "gamma_b": "1.0200",
                    "gain_r": "1.0400",
                    "gain_g": "1.0300",
                    "gain_b": "1.0100",
                },
            ),
        ],
    ),
    # =========================================================================
    # 2. Motion & Transforms (11 presets)
    # =========================================================================
    (
        "motion_punch_zoom_in",
        "Creator Punch Zoom In",
        "Motion & Transforms",
        ("zoom", "punch", "motion", "youtube"),
        "Snappy 100% to 122% emphasis punch-in for comedic beats and callouts.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("qtblend", {"rect": "0~=0 0 1920 1080 1.0000;8=-211 -119 2342 1318 1.0000", "compositing": "0", "rotate_center": "1"}),
        ],
    ),
    (
        "motion_ken_burns_slow_push",
        "Ken Burns Slow Push-In",
        "Motion & Transforms",
        ("ken-burns", "push", "documentary", "photo"),
        "Smooth documentary 100% to 112% linear zoom over 5 seconds.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("qtblend", {"rect": "0=0 0 1920 1080 1.0000;125=-115 -65 2150 1210 1.0000", "compositing": "0", "rotate_center": "1"}),
        ],
    ),
    (
        "motion_impact_camera_shake",
        "Impact Camera Shake",
        "Motion & Transforms",
        ("shake", "impact", "motion", "action"),
        "High-energy multi-frame hit shake with micro-scale compensation.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            (
                "qtblend",
                {
                    "rect": "0=-96 -54 2112 1188 1.0000;2=-60 -84 2112 1188 1.0000;4=-128 -24 2112 1188 1.0000;6=-80 -68 2112 1188 1.0000;10=-96 -54 2112 1188 1.0000",
                    "rotation": "0=0.0;2=2.2;4=-2.0;6=1.0;10=0.0",
                    "compositing": "0",
                    "rotate_center": "1",
                },
            ),
        ],
    ),
    (
        "motion_handheld_drift",
        "Handheld Organic Drift",
        "Motion & Transforms",
        ("handheld", "drift", "subtle", "vlog"),
        "Subtle organic handheld float for locked tripod shots.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            (
                "qtblend",
                {
                    "rect": "0~=-48 -27 2016 1134 1.0000;25~=-36 -35 2016 1134 1.0000;50~=-58 -20 2016 1134 1.0000;75=-48 -27 2016 1134 1.0000",
                    "compositing": "0",
                    "rotate_center": "1",
                },
            ),
        ],
    ),
    (
        "motion_pip_top_right",
        "Picture-in-Picture Top Right",
        "Motion & Transforms",
        ("pip", "overlay", "tutorial", "webcam"),
        "Clean 32% corner Picture-in-Picture framing for reaction and screen-recording overlays.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("qtblend", {"rect": "1240 48 614 346 1.0000", "compositing": "0", "rotate_center": "1"}),
        ],
    ),
    (
        "motion_cinemascope_239_crop",
        "Cinemascope 2.39:1 Letterbox Crop",
        "Motion & Transforms",
        ("letterbox", "crop", "anamorphic", "widescreen"),
        "True 2.39:1 widescreen crop bars via MLT qtcrop.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("qtcrop", {"rect": "0 138 1920 804", "color": "#000000ff"}),
        ],
    ),
    (
        "motion_horizontal_mirror",
        "Horizontal Symmetry Mirror",
        "Motion & Transforms",
        ("mirror", "flip", "symmetry", "stylize"),
        "Clean horizontal mirror reflection across vertical center axis.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("mirror", {"mirror": "horizontal", "reverse": "0"}),
        ],
    ),
    (
        "motion_vertical_mirror",
        "Vertical Water Reflection Mirror",
        "Motion & Transforms",
        ("mirror", "vertical", "reflection"),
        "Vertical mirror reflection for surreal horizon and music-video shots.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("mirror", {"mirror": "vertical", "reverse": "0"}),
        ],
    ),
    (
        "motion_corner_pin_perspective",
        "Perspective Screen Corner Pin",
        "Motion & Transforms",
        ("corner-pin", "perspective", "screen", "mockup"),
        "Four-corner homography warp via frei0r.c0rners for angled display replacements.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            (
                "frei0r.c0rners",
                {
                    "0": "0.36500",
                    "1": "0.36500",
                    "2": "0.64000",
                    "3": "0.34500",
                    "4": "0.65000",
                    "5": "0.64500",
                    "6": "0.35500",
                    "7": "0.66000",
                },
            ),
        ],
    ),
    (
        "motion_basic_3d_card_tilt",
        "2.5D Tilted Card Perspective",
        "Motion & Transforms",
        ("3d", "tilt", "card", "motion-graphics"),
        "2.5D tilted perspective card presentation approximated with frei0r.c0rners.",
        "video",
        CompatibilityStatus.APPROXIMATED,
        [
            (
                "frei0r.c0rners",
                {
                    "0": "0.37000",
                    "1": "0.35500",
                    "2": "0.63000",
                    "3": "0.33333",
                    "4": "0.63000",
                    "5": "0.64500",
                    "6": "0.37000",
                    "7": "0.66666",
                },
            ),
        ],
    ),
    (
        "motion_deshake_stabilize",
        "2D Deshake Handheld Stabilizer",
        "Motion & Transforms",
        ("stabilize", "deshake", "smooth", "camera"),
        "Single-pass 2D optical stabilization reducing camera jitter.",
        "video",
        CompatibilityStatus.APPROXIMATED,
        [
            ("avfilter.deshake", {"av.rx": "24", "av.ry": "24", "av.edge": "mirror"}),
        ],
    ),
    # =========================================================================
    # 3. Blur & Stylize (11 presets)
    # =========================================================================
    (
        "stylize_soft_gaussian_blur",
        "Soft Background Gaussian Blur",
        "Blur & Stylize",
        ("blur", "gaussian", "background", "bokeh"),
        "Smooth isotropic Gaussian defocus for text backgrounds and B-roll overlays.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.gblur", {"av.sigma": "12.000", "av.steps": "2", "av.planes": "15"}),
        ],
    ),
    (
        "stylize_whip_directional_blur",
        "Speed Whip Directional Blur",
        "Blur & Stylize",
        ("blur", "directional", "whip", "speed"),
        "Horizontal directional streak blur for whip pans and kinetic motion.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.dblur", {"av.angle": "0.00", "av.radius": "22.00"}),
        ],
    ),
    (
        "stylize_frosted_box_blur",
        "Frosted Glass Box Blur",
        "Blur & Stylize",
        ("blur", "box", "frosted", "ui"),
        "Fast multi-pass box blur ideal for frosted UI cards and lower-third backdrops.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.boxblur", {"av.luma_radius": "10", "av.luma_power": "3"}),
        ],
    ),
    (
        "stylize_dreamy_soft_glow",
        "Dreamy Halation Soft Glow",
        "Blur & Stylize",
        ("glow", "dreamy", "halation", "music-video"),
        "Blooming highlight glow for music videos, interviews, and title cards.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("frei0r.softglow", {"0": "0.3500", "1": "0.6500", "2": "0.5500", "3": "0.3500"}),
        ],
    ),
    (
        "stylize_broadcast_unsharp",
        "Crisp Broadcast Unsharp Mask",
        "Blur & Stylize",
        ("sharpen", "unsharp", "clarity", "broadcast"),
        "Controlled luma edge sharpening restoring micro-detail without haloing.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.unsharp", {"av.luma_msize_x": "5", "av.luma_msize_y": "5", "av.luma_amount": "1.150"}),
        ],
    ),
    (
        "stylize_rgb_chromatic_glitch",
        "RGB Chromatic Split Glitch",
        "Blur & Stylize",
        ("glitch", "rgb-split", "chromatic", "cyberpunk"),
        "Horizontal red/blue channel separation via frei0r.rgbsplit0r.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("frei0r.rgbsplit0r", {"0": "0.0450", "1": "0.0000"}),
        ],
    ),
    (
        "stylize_privacy_mosaic",
        "Privacy Pixelate Mosaic",
        "Blur & Stylize",
        ("mosaic", "pixelate", "censor", "privacy"),
        "Crisp mosaic pixelation for license plates, screens, and retro game accents.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("frei0r.pixeliz0r", {"0": "0.03000", "1": "0.03000"}),
        ],
    ),
    (
        "stylize_pop_art_posterize",
        "Pop-Art Comic Posterize",
        "Blur & Stylize",
        ("posterize", "comic", "stylize", "graphic"),
        "Graphic stepped tonal bands for bold comic and social cutaways.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("frei0r.posterize", {"0": "0.1400"}),
        ],
    ),
    (
        "stylize_35mm_film_grain",
        "35mm Organic Film Grain",
        "Blur & Stylize",
        ("grain", "noise", "film", "texture"),
        "Subtle temporal uniform 35mm texture removing sterile digital banding.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.noise", {"av.alls": "14", "av.allf": "t+u"}),
        ],
    ),
    (
        "stylize_inverted_flash",
        "Negative Inverted Impact Flash",
        "Blur & Stylize",
        ("invert", "negative", "flash", "music-video"),
        "Full RGB negative inversion for beat-synced strobe and impact hits.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("invert", {}),
        ],
    ),
    (
        "stylize_action_cam_lens_fix",
        "Action-Cam Fisheye De-Warp",
        "Blur & Stylize",
        ("lens", "fisheye", "distortion", "action-cam"),
        "Radial barrel distortion correction straightening wide-angle action footage.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.lenscorrection", {"av.cx": "0.5", "av.cy": "0.5", "av.k1": "-0.2200", "av.k2": "-0.0400"}),
        ],
    ),
    # =========================================================================
    # 4. Keying & Matte (8 presets)
    # =========================================================================
    (
        "key_studio_green_screen",
        "Studio Green Screen Key",
        "Keying & Matte",
        ("greenscreen", "chromakey", "ultra-key", "studio"),
        "Clean studio green-screen chromakey with balanced edge blending.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.chromakey", {"av.color": "0x00ff00", "av.similarity": "0.2400", "av.blend": "0.1000"}),
        ],
    ),
    (
        "key_blue_screen_chroma",
        "Blue Screen Chroma Key",
        "Keying & Matte",
        ("bluescreen", "chromakey", "keying"),
        "Broadcast blue-screen extraction with gentle edge feathering.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.chromakey", {"av.color": "0x0040ff", "av.similarity": "0.2400", "av.blend": "0.1000"}),
        ],
    ),
    (
        "key_color_key_exact",
        "RGB Color Key Knockout",
        "Keying & Matte",
        ("colorkey", "knockout", "graphics", "overlay"),
        "Exact RGB colour key for motion-graphics plates and solid backdrops.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.colorkey", {"av.color": "0x00ff00", "av.similarity": "0.1800", "av.blend": "0.0600"}),
        ],
    ),
    (
        "key_dark_background_luma",
        "Black Background Luma Key",
        "Keying & Matte",
        ("lumakey", "black", "particles", "lens-flare"),
        "Removes pure black backgrounds from stock fire, dust, and light-leak overlays.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.lumakey", {"av.threshold": "0.0800", "av.tolerance": "0.1000", "av.softness": "0.06"}),
        ],
    ),
    (
        "key_bright_sky_luma",
        "Overexposed Sky Luma Key",
        "Keying & Matte",
        ("lumakey", "sky", "highlights", "matte"),
        "Isolates blown-out highlights for sky replacement composites.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.lumakey", {"av.threshold": "0.9200", "av.tolerance": "0.1200", "av.softness": "0.08"}),
        ],
    ),
    (
        "key_center_roto_box_matte",
        "Center Box Rotoscope Matte",
        "Keying & Matte",
        ("rotoscoping", "mask", "matte", "framing"),
        "Feathered rectangular MLT rotoscoping alpha cutout ready for vertex refinement.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            (
                "rotoscoping",
                {
                    "mode": "alpha",
                    "alpha_operation": "clear",
                    "invert": "0",
                    "feather": "12",
                    "spline": '{"0":[[[0.15,0.15],[0.15,0.15],[0.15,0.15]],[[0.85,0.15],[0.85,0.15],[0.85,0.15]],[[0.85,0.85],[0.85,0.85],[0.85,0.85]],[[0.15,0.85],[0.15,0.85],[0.15,0.85]]]}',
                },
            ),
        ],
    ),
    (
        "key_alpha_edge_soften",
        "Alpha Matte Soften & Choke",
        "Keying & Matte",
        ("alpha", "matte", "choke", "feather"),
        "Refines jagged keyed matte edges with soft alpha erosion.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("frei0r.alpha0ps", {"0": "0.0", "1": "0", "2": "0.36", "3": "0.5000", "4": "0.20", "5": "0"}),
        ],
    ),
    (
        "key_alpha_invert_matte",
        "Invert Alpha Channel Matte",
        "Keying & Matte",
        ("alpha", "invert", "matte", "mask"),
        "Inverts an existing clip alpha matte for complementary background grading.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("frei0r.alpha0ps", {"0": "0.0", "1": "0", "2": "0.00", "3": "0.5000", "4": "0.00", "5": "1"}),
        ],
    ),
    # =========================================================================
    # 5. Transitions (10 presets)
    # =========================================================================
    (
        "trans_cross_dissolve",
        "Smooth Cross Dissolve",
        "Transitions",
        ("dissolve", "fade", "transition", "classic"),
        "Clean 12-frame smooth opacity dissolve ramp.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("qtblend", {"rect": "0~=0 0 1920 1080 0.0000;12=0 0 1920 1080 1.0000", "compositing": "0", "rotate_center": "1"}),
        ],
    ),
    (
        "trans_film_dissolve",
        "Cinema Film Dissolve",
        "Transitions",
        ("film-dissolve", "cinema", "transition"),
        "20-frame smooth dissolve mimicking gentle optical film lap dissolve.",
        "video",
        CompatibilityStatus.APPROXIMATED,
        [
            ("qtblend", {"rect": "0~=0 0 1920 1080 0.0000;20=0 0 1920 1080 1.0000", "compositing": "0", "rotate_center": "1"}),
        ],
    ),
    (
        "trans_dip_to_black",
        "Dip to Black Fade-In",
        "Transitions",
        ("dip", "black", "fade", "transition"),
        "Classic 12-frame fade-in from pure broadcast black.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.fade", {"av.type": "in", "av.start_frame": "0", "av.nb_frames": "12", "av.color": "black"}),
        ],
    ),
    (
        "trans_dip_to_white",
        "Flash Dip to White",
        "Transitions",
        ("dip", "white", "flash", "transition"),
        "Crisp 10-frame camera-flash transition from white.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.fade", {"av.type": "in", "av.start_frame": "0", "av.nb_frames": "10", "av.color": "white"}),
        ],
    ),
    (
        "trans_push_left",
        "Push Left Reveal",
        "Transitions",
        ("push", "slide", "left", "transition"),
        "Smooth horizontal slide-in from right to center.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("qtblend", {"rect": "0~=1920 0 1920 1080 1.0000;12=0 0 1920 1080 1.0000", "compositing": "0", "rotate_center": "1"}),
        ],
    ),
    (
        "trans_push_right",
        "Push Right Reveal",
        "Transitions",
        ("push", "slide", "right", "transition"),
        "Smooth horizontal slide-in from left to center.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("qtblend", {"rect": "0~=-1920 0 1920 1080 1.0000;12=0 0 1920 1080 1.0000", "compositing": "0", "rotate_center": "1"}),
        ],
    ),
    (
        "trans_slide_up_card",
        "Slide Up Title Reveal",
        "Transitions",
        ("slide", "up", "title", "transition"),
        "Vertical slide-up reveal great for social titles and lower thirds.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("qtblend", {"rect": "0~=0 1080 1920 1080 1.0000;12=0 0 1920 1080 1.0000", "compositing": "0", "rotate_center": "1"}),
        ],
    ),
    (
        "trans_linear_wipe_ltr",
        "Linear Wipe Left-to-Right",
        "Transitions",
        ("wipe", "linear", "reveal", "transition"),
        "Hard-edge horizontal wipe reveal using animated qtcrop.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("qtcrop", {"rect": "0=0 0 40 1080;12=0 0 1920 1080", "color": "#00000000"}),
        ],
    ),
    (
        "trans_crash_zoom_in",
        "Crash Zoom Transition",
        "Transitions",
        ("zoom", "crash", "creator", "transition"),
        "Fast 10-frame zoom-in settle from 160% to 100%.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("qtblend", {"rect": "0~=-576 -324 3072 1728 1.0000;10=0 0 1920 1080 1.0000", "compositing": "0", "rotate_center": "1"}),
        ],
    ),
    (
        "trans_whip_pan_blur",
        "Whip Pan Speed Blur",
        "Transitions",
        ("whip-pan", "blur", "fast", "transition"),
        "Energetic whip-pan sweep combining horizontal slide and directional streak blur.",
        "video",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("qtblend", {"rect": "0~=-960 0 1920 1080 1.0000;10=0 0 1920 1080 1.0000", "compositing": "0", "rotate_center": "1"}),
            ("avfilter.dblur", {"av.angle": "0.00", "av.radius": "26.00"}),
        ],
    ),
    # =========================================================================
    # 6. Broadcast Audio (12 presets)
    # =========================================================================
    (
        "audio_podcast_voice_master",
        "Broadcast Podcast Voice Master",
        "Broadcast Audio",
        ("podcast", "voice", "essential-audio", "broadcast"),
        "Complete studio dialogue chain: 85Hz rumble cut, 2.8kHz presence boost, 3.5:1 compression, and -1 dB limiter.",
        "audio",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.highpass", {"av.f": "85.0", "av.w": "0.707"}),
            ("avfilter.equalizer", {"av.f": "2800.0", "av.t": "q", "av.w": "1.200", "av.g": "3.50"}),
            ("avfilter.acompressor", {"av.threshold": "0.12589", "av.ratio": "3.50", "av.attack": "10.00", "av.release": "120.00", "av.makeup": "1.5849"}),
            ("avfilter.alimiter", {"av.limit": "0.89125", "av.level_in": "1.00000", "av.release": "50.0"}),
        ],
    ),
    (
        "audio_warm_radio_voice",
        "Warm Radio Announcer EQ",
        "Broadcast Audio",
        ("eq", "radio", "warm", "voice"),
        "Rich 130Hz chest warmth paired with 3.5kHz broadcast articulation.",
        "audio",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.equalizer", {"av.f": "130.0", "av.t": "q", "av.w": "1.100", "av.g": "4.00"}),
            ("avfilter.equalizer", {"av.f": "3500.0", "av.t": "q", "av.w": "1.400", "av.g": "3.00"}),
        ],
    ),
    (
        "audio_crisp_vocal_clarity",
        "Crisp Vocal Clarity Presence",
        "Broadcast Audio",
        ("eq", "clarity", "dialogue", "interview"),
        "Cuts boxy 320Hz room mud and lifts 4.2kHz speech intelligibility.",
        "audio",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.equalizer", {"av.f": "320.0", "av.t": "q", "av.w": "1.600", "av.g": "-3.50"}),
            ("avfilter.equalizer", {"av.f": "4200.0", "av.t": "q", "av.w": "1.200", "av.g": "4.00"}),
        ],
    ),
    (
        "audio_studio_compressor",
        "Studio Dialogue Compressor",
        "Broadcast Audio",
        ("compressor", "dynamics", "dialogue", "level"),
        "Smooth 4:1 vocal dynamics levelling for consistent spoken volume.",
        "audio",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.acompressor", {"av.threshold": "0.10000", "av.ratio": "4.00", "av.attack": "8.00", "av.release": "100.00", "av.makeup": "1.7783"}),
        ],
    ),
    (
        "audio_youtube_14lufs",
        "YouTube / Streaming -14 LUFS",
        "Broadcast Audio",
        ("loudness", "lufs", "youtube", "master"),
        "Integrated -14 LUFS loudness normalization with -1.5 dBTP ceiling.",
        "audio",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.loudnorm", {"av.I": "-14.0", "av.LRA": "11", "av.TP": "-1.5"}),
        ],
    ),
    (
        "audio_brickwall_limiter",
        "Brickwall -1 dB True Limiter",
        "Broadcast Audio",
        ("limiter", "master", "protection", "broadcast"),
        "Zero-clip master peak limiter holding ceiling at -1.0 dBFS.",
        "audio",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.alimiter", {"av.limit": "0.89125", "av.level_in": "1.25893", "av.release": "45.0"}),
        ],
    ),
    (
        "audio_rumble_highpass_100hz",
        "100Hz Rumble & Wind Highpass",
        "Broadcast Audio",
        ("highpass", "rumble", "wind", "cleanup"),
        "Removes AC hum, mic stand thumps, and outdoor wind rumble below 100 Hz.",
        "audio",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.highpass", {"av.f": "100.0", "av.w": "0.707"}),
        ],
    ),
    (
        "audio_telephone_bandpass",
        "Telephone / Walkie Bandpass",
        "Broadcast Audio",
        ("telephone", "bandpass", "fx", "sound-design"),
        "Classic 380Hz-3000Hz narrowband communication voice effect.",
        "audio",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.highpass", {"av.f": "380.0", "av.w": "0.800"}),
            ("avfilter.lowpass", {"av.f": "3000.0", "av.w": "0.800"}),
        ],
    ),
    (
        "audio_deesser_vocal",
        "Vocal Sibilance De-Esser",
        "Broadcast Audio",
        ("deesser", "sibilance", "dialogue", "polish"),
        "Tames harsh 'S' and 'T' sibilance peaks on condenser microphones.",
        "audio",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.deesser", {"av.i": "0.450", "av.m": "0.5", "av.f": "0.5"}),
        ],
    ),
    (
        "audio_fft_denoise",
        "Background Noise FFT DeNoise",
        "Broadcast Audio",
        ("denoise", "cleanup", "room-tone", "fft"),
        "Reduces steady fan, hiss, and room-floor background noise via FFmpeg afftdn.",
        "audio",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.afftdn", {"av.nr": "18.0", "av.nf": "-40"}),
        ],
    ),
    (
        "audio_slapback_analog_delay",
        "Slapback Analog Delay",
        "Broadcast Audio",
        ("delay", "echo", "slapback", "creative"),
        "Warm vintage 180ms/360ms dual-tap analog delay.",
        "audio",
        CompatibilityStatus.EXACT_VALIDATED,
        [
            ("avfilter.aecho", {"av.in_gain": "0.85", "av.out_gain": "0.85", "av.delays": "180|360", "av.decays": "0.40|0.20"}),
        ],
    ),
    (
        "audio_studio_hall_reverb",
        "Studio Room Reverb",
        "Broadcast Audio",
        ("reverb", "space", "hall", "creative"),
        "Spacious multi-tap room reflection tail via avfilter.aecho.",
        "audio",
        CompatibilityStatus.APPROXIMATED,
        [
            ("avfilter.aecho", {"av.in_gain": "0.8", "av.out_gain": "0.700", "av.delays": "55|115", "av.decays": "0.42|0.25"}),
        ],
    ),
)


def get_builtin_starter_presets() -> List[MappedPreset]:
    """Instantiate all 64 legal native Bang On Pro starter presets."""
    presets: List[MappedPreset] = []
    for slug, name, category, tags, description, media_type, status, filter_defs in BUILTIN_STARTER_DEFINITIONS:
        preset_id = f"bangonpro_native__{slug}"
        mlt_filters = [
            MltFilterSpec(
                service=svc,
                media_type=media_type,
                properties=dict(props),
                source_effect=f"BangOnPro.Native.{slug}",
            )
            for svc, props in filter_defs
        ]
        effect_chain = [
            {
                "match_name": f"BangOnPro.Native.{slug}",
                "display_name": name,
                "media_type": media_type,
                "bypass": False,
                "parameters": [],
                "compatibility_status": status.value,
                "mapped_mlt_services": [f.service for f in mlt_filters],
            }
        ]
        kdenlive_xml = generate_kdenlive_effect_xml(
            preset_name=name,
            mlt_filters=mlt_filters,
            description=description,
            preset_id=preset_id,
            compatibility_status=status,
        )
        presets.append(
            MappedPreset(
                preset_id=preset_id,
                name=name,
                pack="Bang On Pro Native Starter Library",
                attribution="Bang On Pro Contributors (GPL-3.0-only)",
                folder_path=[category],
                source_file="builtin_native_library",
                preset_type="scale",
                compatibility_status=status,
                notes=[description],
                effect_chain=effect_chain,
                missing_dependencies=[],
                mlt_filters=mlt_filters,
                kdenlive_effect_xml=kdenlive_xml,
                original_xml_preserved=False,
                description=description,
                category=category,
                tags=list(tags),
            )
        )
    return presets


def build_builtin_starter_report() -> CompatibilityReport:
    """Build a schema-valid CompatibilityReport for the 64-preset native starter library."""
    presets = get_builtin_starter_presets()
    fingerprint_src = "|".join(p.preset_id for p in presets).encode("utf-8")
    digest = hashlib.sha256(fingerprint_src).hexdigest()
    generated_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    return CompatibilityReport(
        schema_version=SCHEMA_VERSION,
        generated_at=generated_at,
        source_archive=SourceArchiveInfo(
            name="Bang On Pro Native Starter Library (GPL-3.0)",
            sha256=digest,
            file_count=len(presets),
            redistribution_restricted=False,
        ),
        presets=presets,
    )


def export_builtin_starter_library(
    output_dir: Union[str, Path],
    *,
    generate_previews: bool = False,
    preview_dir: Optional[Union[str, Path]] = None,
    preview_limit: Optional[int] = None,
) -> Dict[str, Path]:
    """Export the native starter library report bundle (with optional visual thumbnails)."""
    report = build_builtin_starter_report()
    if generate_previews:
        from .preview_generator import attach_previews_to_report

        pdir = Path(preview_dir) if preview_dir is not None else (Path(output_dir) / "previews")
        attach_previews_to_report(report, cache_dir=pdir, max_presets=preview_limit)
    return export_report_bundle(report, output_dir=output_dir)
