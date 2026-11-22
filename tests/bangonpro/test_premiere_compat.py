"""Comprehensive test suite for Bang On Pro Premiere Preset Compatibility Engine.

Covers:
1. Archive & XML security (XXE, DTD billion-laughs, zip-slip, symlinks, zip-bomb, file/size limits)
2. Accurate Adobe Premiere Pro <PremiereData Version="3"> ObjectID/ObjectRef graph parsing
3. Exact tick-to-seconds/frame conversion (254,016,000,000 ticks/sec) & keyframe easing/handles
4. Realistic creator packs (Finzar, TakeOver, ViralFX, Lumetri w/ & w/o LUT, Audio, Text, Mask)
5. Honest classification of all 4 statuses (exact_validated, approximated, missing_dependency, unsupported)
   including Keyper, Mettle VR, Sapphire, Red Giant, Apple Audio Units, missing LUTs & fonts
6. Schema validation against bangonpro/schemas/premiere_preset_report_v1.schema.json & Kdenlive XML export
7. End-to-end CLI import + optional audit JSON cross-check
8. Real deterministic `melt` + `ffmpeg` before/after visual & audio verification renders across all categories

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict, List

import pytest

from bangonpro.premiere_compat import (
    PR_TICKS_PER_SECOND,
    ArchiveLimitError,
    ArchiveSecurityLimits,
    CompatibilityStatus,
    InterpolationMode,
    MappingContext,
    SymlinkError,
    XmlSecurityError,
    ZipBombError,
    ZipSlipError,
    build_compatibility_report,
    export_report_bundle,
    parse_premiere_input,
    parse_premiere_keyframes,
    parse_prfpset_bytes,
    safe_parse_xml,
    safe_read_zip_entries,
    ticks_to_frame,
    ticks_to_seconds,
    validate_report_schema,
    verify_presets_batch,
)


# ---------------------------------------------------------------------------
# Realistic Synthetic Premiere Pro <PremiereData Version="3"> Fixture Builders
# ---------------------------------------------------------------------------

def _finzar_pack_prfpset() -> str:
    """Realistic Finzar-style creator pack with nested BinTreeItems, ObjectRefs, Bezier keyframes."""
    # Ticks at 25fps: frame 0 = 0, frame 5 = 50803200000, frame 10 = 101606400000, frame 15 = 152409600000
    t0 = 254_016_000_000  # 1.0s baseline (will normalize to frame 0)
    t5 = t0 + int(PR_TICKS_PER_SECOND * 5 / 25)
    t10 = t0 + int(PR_TICKS_PER_SECOND * 10 / 25)
    t15 = t0 + int(PR_TICKS_PER_SECOND * 15 / 25)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<PremiereData Version="3">
  <BinTreeItem ObjectID="1">
    <Node Version="1"><Properties><Name>Finzar Creator Pack</Name></Properties></Node>
    <Items Version="1">
      <Item Index="0" ObjectRef="2"/>
      <Item Index="1" ObjectRef="3"/>
    </Items>
  </BinTreeItem>

  <BinTreeItem ObjectID="2">
    <Node Version="1"><Properties><Name>Zooms &amp; Shakes</Name></Properties></Node>
    <Items Version="1">
      <Item Index="0" ObjectRef="10"/>
    </Items>
  </BinTreeItem>

  <BinTreeItem ObjectID="3">
    <Node Version="1"><Properties><Name>Audio &amp; Voice</Name></Properties></Node>
    <Items Version="1">
      <Item Index="0" ObjectRef="30"/>
      <Item Index="1" ObjectRef="40"/>
    </Items>
  </BinTreeItem>

  <!-- Preset 1: Finzar Punch Zoom + Chromatic Aberration (exact_validated) -->
  <FilterPresetItem ObjectID="10">
    <Node Version="1"><Properties><Name>Finzar Punch Zoom + RGB Split</Name></Properties></Node>
    <Description>Fast punch zoom with subtle shake and chromatic aberration</Description>
    <Type>0</Type>
    <Components>
      <Component ObjectRef="11"/>
      <Component ObjectRef="12"/>
    </Components>
  </FilterPresetItem>

  <VideoFilterComponent ObjectID="11">
    <MatchName>AE.ADBE Motion</MatchName>
    <DisplayName>Motion</DisplayName>
    <Params>
      <Param ObjectRef="101"/>
      <Param ObjectRef="102"/>
      <Param ObjectRef="103"/>
      <Param ObjectRef="104"/>
    </Params>
  </VideoFilterComponent>

  <PointComponentParam ObjectID="101">
    <Name>Position</Name>
    <StartKeyframe>-91445760000000000,960.0:540.0,0,0,0,0,0,0</StartKeyframe>
    <Keyframes>{t0},960.0:540.0,5,0,0.2,0.3,0.2,0.3;{t5},995.0:515.0,5,0,0.3,0.4,0.3,0.4;{t10},935.0:560.0,4,0,0,0,0,0;{t15},960.0:540.0,0,0,0,0,0,0</Keyframes>
  </PointComponentParam>

  <VideoComponentParam ObjectID="102">
    <Name>Scale</Name>
    <StartKeyframe>-91445760000000000,100.0,0,0,0,0,0,0</StartKeyframe>
    <Keyframes>{t0},100.0,5,0,0.1667,0.0,0.3333,1.0;{t5},138.0,5,0,0.3333,0.0,0.1667,1.0;{t15},105.0,0,0,0,0,0,0</Keyframes>
  </VideoComponentParam>

  <VideoComponentParam ObjectID="103">
    <Name>Rotation</Name>
    <Keyframes>{t0},0.0,0,0,0,0,0,0;{t5},3.5,5,0,0.2,0.2,0.2,0.2;{t15},0.0,0,0,0,0,0,0</Keyframes>
  </VideoComponentParam>

  <PointComponentParam ObjectID="104">
    <Name>Anchor Point</Name>
    <CurrentValue>960.0:540.0</CurrentValue>
  </PointComponentParam>

  <VideoFilterComponent ObjectID="12">
    <MatchName>AE.ADBE Chromatic Aberration</MatchName>
    <DisplayName>Chromatic Aberration</DisplayName>
    <Params>
      <Param ObjectRef="105"/>
      <Param ObjectRef="106"/>
    </Params>
  </VideoFilterComponent>

  <VideoComponentParam ObjectID="105">
    <Name>Aberration Red</Name>
    <CurrentValue>14.0</CurrentValue>
  </VideoComponentParam>

  <VideoComponentParam ObjectID="106">
    <Name>Aberration Blue</Name>
    <CurrentValue>-14.0</CurrentValue>
  </VideoComponentParam>

  <!-- Preset 2: Creator Podcast Voice Chain (approximated due to StudioReverb) -->
  <FilterPresetItem ObjectID="30">
    <Node Version="1"><Properties><Name>Creator Voice EQ + Comp + Reverb</Name></Properties></Node>
    <Description>Punchy voice chain with highpass, peaking EQ, dynamics, and room reverb</Description>
    <Type>1</Type>
    <Components>
      <Component ObjectRef="31"/>
      <Component ObjectRef="32"/>
      <Component ObjectRef="33"/>
      <Component ObjectRef="34"/>
    </Components>
  </FilterPresetItem>

  <AudioFilterComponent ObjectID="31">
    <MatchName>AE.ADBE Highpass</MatchName>
    <DisplayName>Highpass</DisplayName>
    <Params>
      <Param ObjectRef="301"/>
    </Params>
  </AudioFilterComponent>
  <AudioComponentParam ObjectID="301">
    <Name>Cutoff</Name>
    <CurrentValue>160.0</CurrentValue>
  </AudioComponentParam>

  <AudioFilterComponent ObjectID="32">
    <MatchName>AE.ADBE Parametric EQ</MatchName>
    <DisplayName>Parametric Equalizer</DisplayName>
    <Params>
      <Param ObjectRef="302"/>
      <Param ObjectRef="303"/>
      <Param ObjectRef="304"/>
    </Params>
  </AudioFilterComponent>
  <AudioComponentParam ObjectID="302"><Name>Band 1 Frequency</Name><CurrentValue>2200.0</CurrentValue></AudioComponentParam>
  <AudioComponentParam ObjectID="303"><Name>Band 1 Gain</Name><CurrentValue>8.5</CurrentValue></AudioComponentParam>
  <AudioComponentParam ObjectID="304"><Name>Band 1 Q</Name><CurrentValue>1.2</CurrentValue></AudioComponentParam>

  <AudioFilterComponent ObjectID="33">
    <MatchName>AE.ADBE Dynamics</MatchName>
    <DisplayName>Dynamics</DisplayName>
    <Params>
      <Param ObjectRef="305"/>
      <Param ObjectRef="306"/>
      <Param ObjectRef="307"/>
    </Params>
  </AudioFilterComponent>
  <AudioComponentParam ObjectID="305"><Name>Compressor Threshold</Name><CurrentValue>-20.0</CurrentValue></AudioComponentParam>
  <AudioComponentParam ObjectID="306"><Name>Compressor Ratio</Name><CurrentValue>6.0</CurrentValue></AudioComponentParam>
  <AudioComponentParam ObjectID="307"><Name>Makeup</Name><CurrentValue>6.0</CurrentValue></AudioComponentParam>

  <AudioFilterComponent ObjectID="34">
    <MatchName>AE.ADBE StudioReverb</MatchName>
    <DisplayName>Studio Reverb</DisplayName>
    <Params>
      <Param ObjectRef="308"/>
      <Param ObjectRef="309"/>
    </Params>
  </AudioFilterComponent>
  <AudioComponentParam ObjectID="308"><Name>Decay</Name><CurrentValue>1800.0</CurrentValue></AudioComponentParam>
  <AudioComponentParam ObjectID="309"><Name>Wet</Name><CurrentValue>45.0</CurrentValue></AudioComponentParam>

  <!-- Preset 3: macOS-only Apple Audio Unit (missing_dependency) -->
  <FilterPresetItem ObjectID="40">
    <Node Version="1"><Properties><Name>Mac Master AU Multiband</Name></Properties></Node>
    <Type>0</Type>
    <Components>
      <Component ObjectRef="41"/>
    </Components>
  </FilterPresetItem>
  <AudioFilterComponent ObjectID="41">
    <MatchName>AU.AUMultibandCompressor</MatchName>
    <DisplayName>AUMultibandCompressor</DisplayName>
  </AudioFilterComponent>
</PremiereData>
"""


def _takeover_viralfx_lumetri_prfpset() -> str:
    """TakeOver transitions, ViralFX blur/glow/shadow, Lumetri (with/without LUT), Text, Mask, Keyper, Mettle VR."""
    t0 = 0
    t8 = int(PR_TICKS_PER_SECOND * 8 / 25)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<PremiereData Version="3">
  <BinTreeItem ObjectID="1">
    <Node Version="1"><Properties><Name>TakeOver &amp; ViralFX Suite</Name></Properties></Node>
    <Items Version="1">
      <Item Index="0" ObjectRef="10"/>
      <Item Index="1" ObjectRef="20"/>
      <Item Index="2" ObjectRef="30"/>
      <Item Index="3" ObjectRef="40"/>
      <Item Index="4" ObjectRef="50"/>
      <Item Index="5" ObjectRef="60"/>
      <Item Index="6" ObjectRef="70"/>
      <Item Index="7" ObjectRef="80"/>
      <Item Index="8" ObjectRef="90"/>
    </Items>
  </BinTreeItem>

  <!-- TakeOver Whip Slide Transition (approximated: Shutter Angle 180) -->
  <FilterPresetItem ObjectID="10">
    <Node Version="1"><Properties><Name>TakeOver Whip Slide Right</Name></Properties></Node>
    <Type>2</Type>
    <Components>
      <Component ObjectRef="11"/>
      <Component ObjectRef="12"/>
    </Components>
  </FilterPresetItem>
  <VideoFilterComponent ObjectID="11">
    <MatchName>AE.ADBE Geometry2</MatchName>
    <DisplayName>Transform</DisplayName>
    <Params>
      <VideoComponentParam><Name>Scale</Name><Keyframes>{t0},100.0,0,0,0,0,0,0;{t8},125.0,5,0,0.3,0.1,0.3,0.9</Keyframes></VideoComponentParam>
      <VideoComponentParam><Name>Shutter Angle</Name><CurrentValue>180.0</CurrentValue></VideoComponentParam>
    </Params>
  </VideoFilterComponent>
  <VideoFilterComponent ObjectID="12">
    <MatchName>AE.ADBE Directional Blur</MatchName>
    <DisplayName>Directional Blur</DisplayName>
    <Params>
      <VideoComponentParam><Name>Direction</Name><CurrentValue>90.0</CurrentValue></VideoComponentParam>
      <VideoComponentParam><Name>Blur Length</Name><CurrentValue>24.0</CurrentValue></VideoComponentParam>
    </Params>
  </VideoFilterComponent>

  <!-- ViralFX Dreamy Blur + Glow + Drop Shadow -->
  <FilterPresetItem ObjectID="20">
    <Node Version="1"><Properties><Name>ViralFX Dreamy Glow</Name></Properties></Node>
    <Type>0</Type>
    <Components>
      <Component ObjectRef="21"/>
      <Component ObjectRef="22"/>
      <Component ObjectRef="23"/>
    </Components>
  </FilterPresetItem>
  <VideoFilterComponent ObjectID="21">
    <MatchName>AE.ADBE Gaussian Blur 2</MatchName>
    <DisplayName>Gaussian Blur</DisplayName>
    <Params>
      <VideoComponentParam><Name>Blurriness</Name><CurrentValue>18.0</CurrentValue></VideoComponentParam>
    </Params>
  </VideoFilterComponent>
  <VideoFilterComponent ObjectID="22">
    <MatchName>AE.ADBE VR Glow</MatchName>
    <DisplayName>VR Glow</DisplayName>
    <Params>
      <VideoComponentParam><Name>Glow Radius</Name><CurrentValue>35.0</CurrentValue></VideoComponentParam>
      <VideoComponentParam><Name>Glow Brightness</Name><CurrentValue>1.4</CurrentValue></VideoComponentParam>
    </Params>
  </VideoFilterComponent>
  <VideoFilterComponent ObjectID="23">
    <MatchName>AE.ADBE Drop Shadow</MatchName>
    <DisplayName>Drop Shadow</DisplayName>
    <Params>
      <VideoComponentParam><Name>Distance</Name><CurrentValue>14.0</CurrentValue></VideoComponentParam>
      <VideoComponentParam><Name>Softness</Name><CurrentValue>12.0</CurrentValue></VideoComponentParam>
    </Params>
  </VideoFilterComponent>

  <!-- Lumetri Color WITHOUT external LUT (basic + temp + vignette + HSL secondary caveat) -->
  <FilterPresetItem ObjectID="30">
    <Node Version="1"><Properties><Name>Cinematic Warm Lumetri</Name></Properties></Node>
    <Type>0</Type>
    <Components><Component ObjectRef="31"/></Components>
  </FilterPresetItem>
  <VideoFilterComponent ObjectID="31">
    <MatchName>AE.ADBE Lumetri</MatchName>
    <DisplayName>Lumetri Color</DisplayName>
    <Params>
      <VideoComponentParam><Name>Exposure</Name><CurrentValue>0.65</CurrentValue></VideoComponentParam>
      <VideoComponentParam><Name>Contrast</Name><CurrentValue>28.0</CurrentValue></VideoComponentParam>
      <VideoComponentParam><Name>Saturation</Name><CurrentValue>135.0</CurrentValue></VideoComponentParam>
      <VideoComponentParam><Name>Temperature</Name><CurrentValue>35.0</CurrentValue></VideoComponentParam>
      <VideoComponentParam><Name>Vignette Amount</Name><CurrentValue>-2.2</CurrentValue></VideoComponentParam>
      <VideoComponentParam><Name>HSL Secondary Hue</Name><CurrentValue>12.0</CurrentValue></VideoComponentParam>
    </Params>
  </VideoFilterComponent>

  <!-- Lumetri Color WITH external .cube LUT -->
  <FilterPresetItem ObjectID="40">
    <Node Version="1"><Properties><Name>Teal Orange LUT Grade</Name></Properties></Node>
    <Type>0</Type>
    <Components><Component ObjectRef="41"/></Components>
  </FilterPresetItem>
  <VideoFilterComponent ObjectID="41">
    <MatchName>AE.ADBE Lumetri</MatchName>
    <DisplayName>Lumetri Color</DisplayName>
    <Params>
      <ArbVideoComponentParam><Name>Input LUT</Name><ArbData>TealOrange_Cine.cube</ArbData></ArbVideoComponentParam>
      <VideoComponentParam><Name>Contrast</Name><CurrentValue>15.0</CurrentValue></VideoComponentParam>
    </Params>
  </VideoFilterComponent>

  <!-- Subtitle / Essential Graphics Text with missing custom font -->
  <FilterPresetItem ObjectID="50">
    <Node Version="1"><Properties><Name>Viral Pop Subtitle</Name></Properties></Node>
    <Type>0</Type>
    <Components><Component ObjectRef="51"/></Components>
  </FilterPresetItem>
  <VideoFilterComponent ObjectID="51">
    <MatchName>AE.ADBE Text</MatchName>
    <DisplayName>Text</DisplayName>
    <Params>
      <VideoComponentParam><Name>Source Text</Name><CurrentValue>BANG ON PRO</CurrentValue></VideoComponentParam>
      <VideoComponentParam><Name>Font Family</Name><CurrentValue>SuperRareCreatorFontXYZ999</CurrentValue></VideoComponentParam>
      <VideoComponentParam><Name>Font Size</Name><CurrentValue>54</CurrentValue></VideoComponentParam>
    </Params>
  </VideoFilterComponent>

  <!-- Masking Preset -->
  <FilterPresetItem ObjectID="60">
    <Node Version="1"><Properties><Name>Center Box Roto Mask</Name></Properties></Node>
    <Type>0</Type>
    <Components><Component ObjectRef="61"/></Components>
  </FilterPresetItem>
  <VideoFilterComponent ObjectID="61">
    <MatchName>AE.ADBE Mask</MatchName>
    <DisplayName>Mask</DisplayName>
    <Params>
      <VideoComponentParam><Name>Mask Feather</Name><CurrentValue>12.0</CurrentValue></VideoComponentParam>
    </Params>
  </VideoFilterComponent>

  <!-- Proprietary Keyper AI Keying (unsupported) -->
  <FilterPresetItem ObjectID="70">
    <Node Version="1"><Properties><Name>Keyper Background Removal</Name></Properties></Node>
    <Type>0</Type>
    <Components><Component ObjectRef="71"/></Components>
  </FilterPresetItem>
  <VideoFilterComponent ObjectID="71">
    <MatchName>com.stupidraisins.Keyper</MatchName>
    <DisplayName>Keyper</DisplayName>
  </VideoFilterComponent>

  <!-- Proprietary Mettle VR (unsupported) -->
  <FilterPresetItem ObjectID="80">
    <Node Version="1"><Properties><Name>Mettle SkyBox 360 Glow</Name></Properties></Node>
    <Type>0</Type>
    <Components><Component ObjectRef="81"/></Components>
  </FilterPresetItem>
  <VideoFilterComponent ObjectID="81">
    <MatchName>Mettle.SkyBox.Glow</MatchName>
    <DisplayName>Mettle VR Glow</DisplayName>
  </VideoFilterComponent>

  <!-- Proprietary Sapphire + Red Giant (unsupported) -->
  <FilterPresetItem ObjectID="90">
    <Node Version="1"><Properties><Name>Sapphire Shake + RG Looks</Name></Properties></Node>
    <Type>0</Type>
    <Components>
      <Component ObjectRef="91"/>
      <Component ObjectRef="92"/>
    </Components>
  </FilterPresetItem>
  <VideoFilterComponent ObjectID="91">
    <MatchName>S_Shake</MatchName>
    <DisplayName>Sapphire Shake</DisplayName>
  </VideoFilterComponent>
  <VideoFilterComponent ObjectID="92">
    <MatchName>RG_MagicBulletLooks</MatchName>
    <DisplayName>Red Giant Magic Bullet Looks</DisplayName>
  </VideoFilterComponent>
</PremiereData>
"""


def _write_simple_cube_lut(path: Path) -> Path:
    """Write a valid 2x2x2 3D .cube LUT (teal shadows, warm highlights) usable by FFmpeg lut3d."""
    content = "\n".join(
        [
            "TITLE \"BangOnPro Test TealOrange\"",
            "LUT_3D_SIZE 2",
            "0.00 0.08 0.14",
            "0.95 0.12 0.06",
            "0.04 0.88 0.16",
            "1.00 0.92 0.12",
            "0.02 0.16 0.92",
            "0.92 0.18 0.85",
            "0.06 0.92 0.94",
            "1.00 0.96 0.86",
            "",
        ]
    )
    path.write_text(content, encoding="utf-8")
    return path


def _build_creator_pack_zip(zip_path: Path) -> Path:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("packs/Finzar_Pack.prfpset", _finzar_pack_prfpset().encode("utf-8"))
        zf.writestr("packs/TakeOver_ViralFX.prfpset", _takeover_viralfx_lumetri_prfpset().encode("utf-8"))
    return zip_path


# ---------------------------------------------------------------------------
# 1. Security Hardening Tests
# ---------------------------------------------------------------------------


def test_security_rejects_xxe_and_dtd_expansion() -> None:
    xxe_payload = b"""<?xml version="1.0"?>
<!DOCTYPE foo [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<PremiereData Version="3"><Name>&xxe;</Name></PremiereData>
"""
    with pytest.raises(XmlSecurityError):
        safe_parse_xml(xxe_payload)

    billion_laughs = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<PremiereData Version="3"><Name>&lol3;</Name></PremiereData>
"""
    with pytest.raises(XmlSecurityError):
        safe_parse_xml(billion_laughs)


def test_security_rejects_zip_slip_traversal_and_absolute_paths(tmp_path: Path) -> None:
    for bad_name in ("../escape.prfpset", "packs/../../etc/evil.prfpset", "/etc/passwd", "C:\\Windows\\evil.prfpset"):
        zp = tmp_path / f"slip_{abs(hash(bad_name))}.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr(bad_name, b"<PremiereData/>")
        with pytest.raises(ZipSlipError):
            safe_read_zip_entries(zp)


def test_security_rejects_zip_symlink(tmp_path: Path) -> None:
    zp = tmp_path / "symlink.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        info = zipfile.ZipInfo("packs/link.prfpset")
        info.create_system = 3  # Unix
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, b"/etc/passwd")
    with pytest.raises(SymlinkError):
        safe_read_zip_entries(zp)


def test_security_rejects_zip_bomb_compression_ratio(tmp_path: Path) -> None:
    zp = tmp_path / "bomb.zip"
    # 512 KiB of zeroes compresses to ~600 bytes (>800x ratio, far exceeding MAX_COMPRESSION_RATIO=100)
    with zipfile.ZipFile(zp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr("packs/bomb.prfpset", b"A" * (512 * 1024))
    with pytest.raises(ZipBombError):
        safe_read_zip_entries(zp)


def test_security_rejects_max_files_and_byte_limits(tmp_path: Path) -> None:
    # Exceed MAX_FILES = 256
    zp_many = tmp_path / "too_many.zip"
    with zipfile.ZipFile(zp_many, "w", compression=zipfile.ZIP_STORED) as zf:
        for idx in range(260):
            zf.writestr(f"p/{idx}.prfpset", b"<PremiereData/>")
    with pytest.raises(ArchiveLimitError):
        safe_read_zip_entries(zp_many)

    # Exceed single-file byte limit using tighter custom limit
    zp_big = tmp_path / "too_big.zip"
    with zipfile.ZipFile(zp_big, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("p/big.prfpset", b"x" * 2048)
    with pytest.raises(ArchiveLimitError):
        safe_read_zip_entries(zp_big, limits=ArchiveSecurityLimits(max_single_file_bytes=1024))

    # Exceed total uncompressed byte limit
    zp_tot = tmp_path / "too_big_total.zip"
    with zipfile.ZipFile(zp_tot, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("p/a.prfpset", b"x" * 900)
        zf.writestr("p/b.prfpset", b"y" * 900)
    with pytest.raises(ArchiveLimitError):
        safe_read_zip_entries(
            zp_tot,
            limits=ArchiveSecurityLimits(max_single_file_bytes=1024, max_total_uncompressed_bytes=1500),
        )


# ---------------------------------------------------------------------------
# 2. Tick & Keyframe Parsing Tests
# ---------------------------------------------------------------------------


def test_premiere_tick_conversion_exact() -> None:
    assert PR_TICKS_PER_SECOND == 254_016_000_000
    assert ticks_to_seconds(PR_TICKS_PER_SECOND) == pytest.approx(1.0)
    assert ticks_to_frame(PR_TICKS_PER_SECOND, fps=25.0) == 25
    assert ticks_to_frame(PR_TICKS_PER_SECOND, fps=30.0) == 30
    assert ticks_to_frame(PR_TICKS_PER_SECOND, fps=24.0) == 24
    assert ticks_to_frame(PR_TICKS_PER_SECOND // 2, fps=60.0) == 30


def test_premiere_keyframe_modes_and_bezier_handles() -> None:
    t0 = 0
    t10 = int(PR_TICKS_PER_SECOND * 10 / 25)
    t20 = int(PR_TICKS_PER_SECOND * 20 / 25)
    t25 = PR_TICKS_PER_SECOND
    raw = (
        f"-91445760000000000,100.0,0,0,0,0,0,0;"
        f"{t0},100.0,0,0,0,0,0,0;"
        f"{t10},140.0,5,0,0.2,0.1,0.3,0.9;"
        f"{t20},140.0,4,0,0,0,0,0;"
        f"{t25},100.0,2,0,0.1,0.2,0.3,0.4"
    )
    kfs = parse_premiere_keyframes(raw, fps=25.0)
    assert len(kfs) == 5
    assert kfs[0].is_sentinel is True
    assert kfs[1].frame == 0 and kfs[1].interpolation == InterpolationMode.LINEAR
    assert kfs[2].frame == 10 and kfs[2].interpolation == InterpolationMode.BEZIER
    assert kfs[2].bezier_handles == pytest.approx((0.2, 0.1, 0.3, 0.9))
    assert kfs[3].frame == 20 and kfs[3].interpolation == InterpolationMode.HOLD
    assert kfs[4].frame == 25 and kfs[4].interpolation == InterpolationMode.EASE_OUT


# ---------------------------------------------------------------------------
# 3. Full Creator Pack Parsing, Mapping, Schema Validation & Honest Statuses
# ---------------------------------------------------------------------------


def test_creator_packs_honest_classification_and_schema_validation(tmp_path: Path) -> None:
    zip_path = _build_creator_pack_zip(tmp_path / "creator_packs.zip")
    audit_path = tmp_path / "Bang_on_pro_premiere_preset_audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "version": "1.0",
                "expected_total_presets": 12,
                "default_attribution": "Bang On Pro Synthetic Test Suite",
                "packs": [
                    {"name": "Finzar_Pack", "attribution": "Finzar Synthetic Reference"},
                    {"name": "TakeOver_ViralFX", "attribution": "TakeOver/ViralFX Synthetic Reference"},
                ],
            }
        ),
        encoding="utf-8",
    )

    bundle = parse_premiere_input(zip_path, audit_json_path=audit_path, fps=25.0)
    assert len(bundle.presets) == 12
    assert bundle.archive_info.file_count == 2
    assert len(bundle.archive_info.sha256) == 64

    report = build_compatibility_report(bundle, fps=25.0, platform_name="linux")
    report_dict = report.to_dict()

    # Strict JSON Schema validation against premiere_preset_report_v1.schema.json
    validate_report_schema(report_dict)

    assert report.audit_reconciliation is not None
    assert report.audit_reconciliation["count_matches"] is True

    by_name: Dict[str, object] = {p.name: p for p in report.presets}

    # Check all 4 compatibility statuses are represented honestly
    summary = report.summary()
    assert summary["total_presets"] == 12
    assert summary["exact_validated"] >= 1
    assert summary["approximated"] >= 4
    assert summary["missing_dependency"] >= 3  # AU, missing LUT, missing font
    assert summary["unsupported"] >= 3  # Keyper, Mettle VR, Sapphire+RG

    # 1. Finzar Punch Zoom + RGB Split -> exact_validated with smooth (~=) and hold (|=) keyframes
    finzar = by_name["Finzar Punch Zoom + RGB Split"]
    assert finzar.compatibility_status == CompatibilityStatus.EXACT_VALIDATED
    assert finzar.folder_path == ["Finzar Creator Pack", "Zooms & Shakes"]
    assert finzar.attribution == "Finzar Synthetic Reference"
    assert [f.service for f in finzar.mlt_filters] == ["qtblend", "avfilter.chromashift"]
    assert "~=" in finzar.mlt_filters[0].properties["rect"]
    assert "|=" in finzar.mlt_filters[0].properties["rect"]
    assert finzar.kdenlive_effect_xml is not None
    # Verify Kdenlive XML parses cleanly with defusedxml
    kdenlive_root = safe_parse_xml(finzar.kdenlive_effect_xml)
    assert kdenlive_root.tag == "effectgroup"

    # 2. TakeOver Whip Slide -> approximated (shutter angle motion blur caveat)
    takeover = by_name["TakeOver Whip Slide Right"]
    assert takeover.compatibility_status == CompatibilityStatus.APPROXIMATED
    assert takeover.preset_type == "anchor_out"
    assert any("Shutter Angle" in n for n in takeover.notes)

    # 3. Lumetri WITHOUT LUT -> approximated with Rec.709 & HSL Secondary caveats
    lumetri_warm = by_name["Cinematic Warm Lumetri"]
    assert lumetri_warm.compatibility_status == CompatibilityStatus.APPROXIMATED
    assert any("HSL Secondary" in n for n in lumetri_warm.notes)
    assert any(f.service == "avfilter.eq" for f in lumetri_warm.mlt_filters)
    assert any(f.service == "avfilter.colorbalance" for f in lumetri_warm.mlt_filters)
    assert any(f.service == "avfilter.vignette" for f in lumetri_warm.mlt_filters)

    # 4. Lumetri WITH missing .cube LUT -> missing_dependency
    lumetri_lut = by_name["Teal Orange LUT Grade"]
    assert lumetri_lut.compatibility_status == CompatibilityStatus.MISSING_DEPENDENCY
    assert any(d.kind == "lut" and d.name == "TealOrange_Cine.cube" for d in lumetri_lut.missing_dependencies)

    # 5. Apple Audio Unit -> missing_dependency (macOS-only, never falsely claimed native!)
    au = by_name["Mac Master AU Multiband"]
    assert au.compatibility_status == CompatibilityStatus.MISSING_DEPENDENCY
    assert au.mlt_filters == []
    assert any(d.kind == "audio_unit" and d.platform == "macos_only" for d in au.missing_dependencies)
    assert any("avfilter.acompressor" in d.remediation for d in au.missing_dependencies)

    # 6. Missing custom font -> missing_dependency with fallback dynamictext
    sub = by_name["Viral Pop Subtitle"]
    assert sub.compatibility_status == CompatibilityStatus.MISSING_DEPENDENCY
    assert any(d.kind == "font" and "SuperRareCreatorFontXYZ999" in d.name for d in sub.missing_dependencies)
    assert sub.mlt_filters[0].service == "dynamictext"

    # 7. Proprietary Keyper, Mettle VR, Sapphire+RG -> unsupported, XML preserved!
    for prop_name in ("Keyper Background Removal", "Mettle SkyBox 360 Glow", "Sapphire Shake + RG Looks"):
        preset = by_name[prop_name]
        assert preset.compatibility_status == CompatibilityStatus.UNSUPPORTED
        assert preset.mlt_filters == []
        assert preset.kdenlive_effect_xml is None
        assert preset.original_xml_preserved is True
        assert "<PremierePresetSnippet>" in preset.original_xml
        assert len(preset.missing_dependencies) >= 1

    # Export bundle writes JSON, Markdown, Kdenlive XMLs, and preserved XML snippets
    exported = export_report_bundle(report, tmp_path / "export_out")
    assert exported["json_report"].is_file()
    assert exported["markdown_report"].is_file()
    md_text = exported["markdown_report"].read_text(encoding="utf-8")
    assert "Missing Dependencies & Proprietary / Platform Plugins" in md_text
    assert "TealOrange_Cine.cube" in md_text
    assert len(list(exported["preserved_xml_dir"].glob("*.xml"))) == 12


def test_lumetri_resolves_local_cube_lut_to_avfilter_lut3d(tmp_path: Path) -> None:
    lut_dir = tmp_path / "luts"
    lut_dir.mkdir()
    lut_file = _write_simple_cube_lut(lut_dir / "TealOrange_Cine.cube")

    prfpset_file = tmp_path / "lut_grade.prfpset"
    prfpset_file.write_text(_takeover_viralfx_lumetri_prfpset(), encoding="utf-8")

    bundle = parse_premiere_input(prfpset_file, fps=25.0)
    report = build_compatibility_report(bundle, fps=25.0, asset_search_dirs=[lut_dir])
    by_name = {p.name: p for p in report.presets}
    lut_preset = by_name["Teal Orange LUT Grade"]

    assert lut_preset.compatibility_status == CompatibilityStatus.APPROXIMATED
    assert lut_preset.missing_dependencies == []
    lut_filters = [f for f in lut_preset.mlt_filters if f.service == "avfilter.lut3d"]
    assert len(lut_filters) == 1
    assert lut_filters[0].properties["av.file"] == str(lut_file)


def test_broad_effect_catalog_coverage() -> None:
    """Ensure broad catalog mappings across transforms, stylize, colour, and audio."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<PremiereData Version="3">
  <FilterPresetItem ObjectID="1">
    <Name>Extended Effect Kitchen Sink</Name>
    <Components>
      <VideoFilterComponent><MatchName>AE.ADBE Crop</MatchName><Params><VideoComponentParam><Name>Left</Name><CurrentValue>10</CurrentValue></VideoComponentParam></Params></VideoFilterComponent>
      <VideoFilterComponent><MatchName>AE.ADBE Mirror</MatchName><Params><VideoComponentParam><Name>Reflection Angle</Name><CurrentValue>0</CurrentValue></VideoComponentParam></Params></VideoFilterComponent>
      <VideoFilterComponent><MatchName>AE.ADBE Offset</MatchName></VideoFilterComponent>
      <VideoFilterComponent><MatchName>AE.ADBE Corner Pin</MatchName></VideoFilterComponent>
      <VideoFilterComponent><MatchName>AE.ADBE Wave Warp</MatchName></VideoFilterComponent>
      <VideoFilterComponent><MatchName>AE.ADBE Lens Distortion</MatchName></VideoFilterComponent>
      <VideoFilterComponent><MatchName>AE.ADBE Mosaic</MatchName></VideoFilterComponent>
      <VideoFilterComponent><MatchName>AE.ADBE Posterize</MatchName></VideoFilterComponent>
      <VideoFilterComponent><MatchName>AE.ADBE Invert</MatchName></VideoFilterComponent>
      <VideoFilterComponent><MatchName>AE.ADBE Noise</MatchName></VideoFilterComponent>
      <VideoFilterComponent><MatchName>AE.ADBE Tint</MatchName></VideoFilterComponent>
      <VideoFilterComponent><MatchName>AE.ADBE Black &amp; White</MatchName></VideoFilterComponent>
      <VideoFilterComponent><MatchName>AE.ADBE Ultra Key</MatchName></VideoFilterComponent>
      <VideoFilterComponent><MatchName>AE.ADBE Luma Key</MatchName></VideoFilterComponent>
      <AudioFilterComponent><MatchName>AE.ADBE HardLimiter</MatchName></AudioFilterComponent>
      <AudioFilterComponent><MatchName>AE.ADBE Denoise</MatchName></AudioFilterComponent>
      <AudioFilterComponent><MatchName>AE.ADBE DeEsser</MatchName></AudioFilterComponent>
      <AudioFilterComponent><MatchName>AE.ADBE PitchShifter</MatchName></AudioFilterComponent>
      <AudioFilterComponent><MatchName>AE.ADBE ChannelVolume</MatchName></AudioFilterComponent>
    </Components>
  </FilterPresetItem>
</PremiereData>
"""
    presets = parse_prfpset_bytes(xml.encode("utf-8"), source_file="kitchen.prfpset")
    assert len(presets) == 1
    from bangonpro.premiere_compat import map_preset

    mapped = map_preset(presets[0], MappingContext(fps=25.0))
    services = {f.service for f in mapped.mlt_filters}
    expected = {
        "qtcrop",
        "mirror",
        "avfilter.scroll",
        "frei0r.c0rners",
        "wave",
        "avfilter.lenscorrection",
        "frei0r.pixeliz0r",
        "frei0r.posterize",
        "invert",
        "avfilter.noise",
        "frei0r.tint0r",
        "greyscale",
        "avfilter.chromakey",
        "avfilter.lumakey",
        "avfilter.alimiter",
        "avfilter.afftdn",
        "avfilter.deesser",
        "avfilter.rubberband",
        "volume",
        "panner",
    }
    assert expected.issubset(services)
    assert mapped.compatibility_status == CompatibilityStatus.APPROXIMATED


# ---------------------------------------------------------------------------
# 4. Real Deterministic `melt` + `ffmpeg` Visual & Audio Verification Harness
# ---------------------------------------------------------------------------


def test_real_melt_before_after_deterministic_visual_and_audio_renders(tmp_path: Path) -> None:
    lut_dir = tmp_path / "luts"
    lut_dir.mkdir()
    _write_simple_cube_lut(lut_dir / "TealOrange_Cine.cube")

    zip_path = _build_creator_pack_zip(tmp_path / "verify_pack.zip")
    bundle = parse_premiere_input(zip_path, fps=25.0)
    report = build_compatibility_report(bundle, fps=25.0, asset_search_dirs=[lut_dir])
    by_name = {p.name: p for p in report.presets}

    # Verify one representative converted preset from every category:
    # 1. Transform/Zoom/Shake + Chromatic Shift
    # 2. Blur / Glow
    # 3. Lumetri Colour + Resolved 3D LUT (avfilter.lut3d)
    # 4. Audio EQ / Compression / Reverb
    # 5. Text / Subtitle + Masking
    targets = [
        by_name["Finzar Punch Zoom + RGB Split"],
        by_name["ViralFX Dreamy Glow"],
        by_name["Teal Orange LUT Grade"],
        by_name["Creator Voice EQ + Comp + Reverb"],
        by_name["Viral Pop Subtitle"],
        by_name["Center Box Roto Mask"],
    ]

    results = verify_presets_batch(
        targets,
        fps=25,
        width=320,
        height=180,
        duration_sec=0.6,
        check_determinism=True,
    )
    assert len(results) == len(targets)

    res_by_name = {r.preset_name: r for r in results}

    for name, res in res_by_name.items():
        assert res.rendered_ok is True, f"{name} failed melt render: {res.error}"
        assert res.deterministic is True, f"{name} render was not deterministic!"
        assert res.visual is not None and res.visual.frame_count >= 10
        assert res.audio is not None

    # Video categories must show real measured visual differences (MSE / PSNR / SSIM)
    for video_name in (
        "Finzar Punch Zoom + RGB Split",
        "ViralFX Dreamy Glow",
        "Teal Orange LUT Grade",
        "Viral Pop Subtitle",
        "Center Box Roto Mask",
    ):
        vres = res_by_name[video_name]
        assert vres.visual is not None
        assert vres.visual.visual_changed is True, f"{video_name} produced no measurable visual change!"
        assert vres.visual.mse > 1.0

    # Audio category must show real measured RMS / waveform / spectral delta
    ares = res_by_name["Creator Voice EQ + Comp + Reverb"]
    assert ares.audio is not None
    assert ares.audio.audio_changed is True
    assert ares.audio.waveform_l2_delta > 0.01


# ---------------------------------------------------------------------------
# 5. End-to-End CLI Test
# ---------------------------------------------------------------------------


def test_cli_import_and_verify_render(tmp_path: Path) -> None:
    zip_path = _build_creator_pack_zip(tmp_path / "cli_pack.zip")
    out_dir = tmp_path / "cli_out"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "bangonpro.premiere_compat.cli",
            "import",
            "--input",
            str(zip_path),
            "--output-dir",
            str(out_dir),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "ok"
    assert payload["summary"]["total_presets"] == 12
    assert (out_dir / "compatibility_report.json").is_file()
    assert (out_dir / "compatibility_report.md").is_file()

    # Validate exported JSON directly against schema
    exported_json = json.loads((out_dir / "compatibility_report.json").read_text(encoding="utf-8"))
    validate_report_schema(exported_json)

    # Exercise --verify-render end-to-end via CLI on a single prfpset with Track Matte + Motion
    verify_prfpset = tmp_path / "verify_cli.prfpset"
    verify_prfpset.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<PremiereData Version="3">
  <FilterPresetItem ObjectID="1">
    <Name>Track Matte Mask Preset</Name>
    <Components>
      <VideoFilterComponent>
        <MatchName>AE.ADBE Track Matte Key</MatchName>
        <DisplayName>Track Matte Key</DisplayName>
      </VideoFilterComponent>
    </Components>
  </FilterPresetItem>
</PremiereData>
""",
        encoding="utf-8",
    )
    verify_out = tmp_path / "cli_verify_out"
    vproc = subprocess.run(
        [
            sys.executable,
            "-m",
            "bangonpro.premiere_compat.cli",
            "import",
            "--input",
            str(verify_prfpset),
            "--output-dir",
            str(verify_out),
            "--verify-render",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert vproc.returncode == 0, vproc.stderr
    vpayload = json.loads(vproc.stdout)
    assert vpayload["verification"]["verified_count"] == 1
    assert vpayload["verification"]["passed_deterministic"] == 1
    vresults = json.loads((verify_out / "verification_results.json").read_text(encoding="utf-8"))
    assert vresults[0]["rendered_ok"] is True
    assert vresults[0]["deterministic"] is True
    assert vresults[0]["visual"]["visual_changed"] is True

    # Verify CLI rejects malicious archive with exit code 2
    bad_zip = tmp_path / "bad_slip.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("../../evil.prfpset", b"<PremiereData/>")
    bad_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "bangonpro.premiere_compat.cli",
            "import",
            "--input",
            str(bad_zip),
            "--output-dir",
            str(tmp_path / "bad_out"),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert bad_proc.returncode == 2
    assert "SECURITY REJECTION" in bad_proc.stderr
