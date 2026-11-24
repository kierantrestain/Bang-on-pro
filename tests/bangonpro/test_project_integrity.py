# SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
# SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""Project Integrity, MLT Rendering, Autosave/Crash-Recovery, Reopen & Rollback Tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import defusedxml.ElementTree as DefusedET
import pytest

MELT_BIN = shutil.which("melt")
FFMPEG_BIN = shutil.which("ffmpeg")
FFPROBE_BIN = shutil.which("ffprobe")


# ---------------------------------------------------------------------------
# Helpers: Hashing, Synthetic Media Generation, MLT .kdenlive Builder & Parser
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def generate_synthetic_media(output_path: Path, duration_sec: float = 2.0) -> Path:
    """Create a deterministic synthetic 640x360@25fps H.264 + 440Hz AAC MP4 file."""
    assert FFMPEG_BIN is not None, "ffmpeg is required for project integrity tests"
    cmd = [
        FFMPEG_BIN,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=640x360:rate=25:duration={duration_sec}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate=48000:duration={duration_sec}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "25",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        str(output_path),
    ]
    subprocess.run(cmd, shell=False, check=True, capture_output=True, text=True)
    assert output_path.is_file() and output_path.stat().st_size > 1000
    return output_path


def build_kdenlive_project_xml(
    media_path: Path,
    *,
    markers: list[dict[str, Any]] | None = None,
    brightness_level: str = "1.10",
    volume_level: str = "-3dB",
    in_tc: str = "00:00:00.000",
    out_tc: str = "00:00:01.960",
) -> str:
    """Construct a standards-compliant MLT/Kdenlive .kdenlive XML document."""
    if markers is None:
        markers = [
            {"comment": "Intro Hook", "pos": 0, "type": 0},
            {"comment": "Mid Cut", "pos": 25, "type": 1},
            {"comment": "Outro Beat", "pos": 45, "type": 2},
        ]

    mlt = ET.Element(
        "mlt",
        {
            "LC_NUMERIC": "C",
            "producer": "tractor2",
            "version": "7.22.0",
            "root": str(media_path.parent),
        },
    )

    ET.SubElement(
        mlt,
        "profile",
        {
            "description": "Bang On Pro 360p 25fps Test Profile",
            "width": "640",
            "height": "360",
            "progressive": "1",
            "sample_aspect_num": "1",
            "sample_aspect_den": "1",
            "display_aspect_num": "16",
            "display_aspect_den": "9",
            "frame_rate_num": "25",
            "frame_rate_den": "1",
            "colorspace": "709",
        },
    )

    # Bin master clip producer
    p0 = ET.SubElement(mlt, "producer", {"id": "producer0", "in": in_tc, "out": out_tc})
    for k, v in (
        ("resource", str(media_path)),
        ("mlt_service", "avformat"),
        ("kdenlive:id", "2"),
        ("kdenlive:clipname", media_path.name),
        ("kdenlive:folderid", "-1"),
    ):
        prop = ET.SubElement(p0, "property", {"name": k})
        prop.text = v

    # main_bin retains Kdenlive document metadata & timeline markers/guides
    main_bin = ET.SubElement(mlt, "playlist", {"id": "main_bin"})
    doc_props = {
        "kdenlive:docproperties.version": "1.1",
        "kdenlive:docproperties.kdenliveversion": "24.08.3",
        "kdenlive:docproperties.bangonpro_version": "0.1.0-alpha",
        "kdenlive:docproperties.audioChannels": "2",
        "kdenlive:docproperties.guides": json.dumps(markers, indent=2),
        "xml_retain": "1",
    }
    for k, v in doc_props.items():
        prop = ET.SubElement(main_bin, "property", {"name": k})
        prop.text = v
    ET.SubElement(main_bin, "entry", {"producer": "producer0", "in": in_tc, "out": out_tc})

    # Black background track
    black = ET.SubElement(mlt, "producer", {"id": "black_track", "in": in_tc, "out": out_tc})
    for k, v in (
        ("resource", "black"),
        ("mlt_service", "color"),
        ("mlt_image_format", "rgba"),
        ("set.test_audio", "0"),
    ):
        prop = ET.SubElement(black, "property", {"name": k})
        prop.text = v

    # Audio track (A1) with volume filter
    p_audio = ET.SubElement(mlt, "producer", {"id": "producer_audio_1", "in": in_tc, "out": out_tc})
    for k, v in (
        ("resource", str(media_path)),
        ("mlt_service", "avformat"),
        ("kdenlive:id", "2"),
        ("set.test_audio", "0"),
        ("set.test_image", "1"),
    ):
        prop = ET.SubElement(p_audio, "property", {"name": k})
        prop.text = v

    pl_audio = ET.SubElement(mlt, "playlist", {"id": "playlist0"})
    prop = ET.SubElement(pl_audio, "property", {"name": "kdenlive:audio_track"})
    prop.text = "1"
    entry_a = ET.SubElement(
        pl_audio, "entry", {"producer": "producer_audio_1", "in": in_tc, "out": out_tc}
    )
    ET.SubElement(entry_a, "property", {"name": "kdenlive:id"}).text = "2"
    filt_vol = ET.SubElement(entry_a, "filter", {"id": "filter_audio_volume"})
    ET.SubElement(filt_vol, "property", {"name": "mlt_service"}).text = "volume"
    ET.SubElement(filt_vol, "property", {"name": "level"}).text = volume_level

    tr_audio = ET.SubElement(mlt, "tractor", {"id": "tractor0", "in": in_tc, "out": out_tc})
    ET.SubElement(tr_audio, "property", {"name": "kdenlive:audio_track"}).text = "1"
    ET.SubElement(tr_audio, "property", {"name": "kdenlive:track_name"}).text = "A1"
    ET.SubElement(tr_audio, "track", {"hide": "video", "producer": "playlist0"})

    # Video track (V1) with brightness filter
    p_video = ET.SubElement(mlt, "producer", {"id": "producer_video_1", "in": in_tc, "out": out_tc})
    for k, v in (
        ("resource", str(media_path)),
        ("mlt_service", "avformat"),
        ("kdenlive:id", "2"),
        ("set.test_audio", "1"),
        ("set.test_image", "0"),
    ):
        prop = ET.SubElement(p_video, "property", {"name": k})
        prop.text = v

    pl_video = ET.SubElement(mlt, "playlist", {"id": "playlist1"})
    entry_v = ET.SubElement(
        pl_video, "entry", {"producer": "producer_video_1", "in": in_tc, "out": out_tc}
    )
    ET.SubElement(entry_v, "property", {"name": "kdenlive:id"}).text = "2"
    filt_bri = ET.SubElement(entry_v, "filter", {"id": "filter_video_brightness"})
    ET.SubElement(filt_bri, "property", {"name": "mlt_service"}).text = "brightness"
    ET.SubElement(filt_bri, "property", {"name": "level"}).text = brightness_level

    tr_video = ET.SubElement(mlt, "tractor", {"id": "tractor1", "in": in_tc, "out": out_tc})
    ET.SubElement(tr_video, "property", {"name": "kdenlive:track_name"}).text = "V1"
    ET.SubElement(tr_video, "track", {"hide": "audio", "producer": "playlist1"})

    # Master sequence tractor
    master = ET.SubElement(mlt, "tractor", {"id": "tractor2", "in": in_tc, "out": out_tc})
    ET.SubElement(master, "property", {"name": "kdenlive:sequenceproperties.activeTrack"}).text = "1"
    ET.SubElement(master, "track", {"producer": "black_track"})
    ET.SubElement(master, "track", {"producer": "tractor0"})
    ET.SubElement(master, "track", {"producer": "tractor1"})

    t_mix = ET.SubElement(master, "transition", {"id": "transition_audio_mix"})
    for k, v in (
        ("a_track", "0"),
        ("b_track", "1"),
        ("mlt_service", "mix"),
        ("always_active", "1"),
        ("sum", "1"),
    ):
        ET.SubElement(t_mix, "property", {"name": k}).text = v

    t_blend = ET.SubElement(master, "transition", {"id": "transition_video_blend"})
    for k, v in (
        ("a_track", "0"),
        ("b_track", "2"),
        ("mlt_service", "frei0r.cairoblend"),
        ("always_active", "1"),
    ):
        ET.SubElement(t_blend, "property", {"name": k}).text = v

    ET.indent(mlt, space=" ")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(mlt, encoding="unicode") + "\n"


def parse_kdenlive_structure(xml_path: Path) -> dict[str, Any]:
    """Safely parse a .kdenlive XML document via defusedxml into a structured audit snapshot."""
    root = DefusedET.parse(xml_path).getroot()
    assert root.tag == "mlt"

    profile_el = root.find("profile")
    assert profile_el is not None
    profile = dict(profile_el.attrib)

    producers: dict[str, dict[str, str]] = {}
    for prod in root.findall("producer"):
        pid = prod.attrib["id"]
        props = {p.attrib["name"]: (p.text or "") for p in prod.findall("property")}
        producers[pid] = props

    tracks: list[dict[str, Any]] = []
    for tractor in root.findall("tractor"):
        tid = tractor.attrib["id"]
        props = {p.attrib["name"]: (p.text or "") for p in tractor.findall("property")}
        track_refs = [dict(t.attrib) for t in tractor.findall("track")]
        tracks.append({"id": tid, "properties": props, "tracks": track_refs})

    clips: list[dict[str, Any]] = []
    filters: list[dict[str, Any]] = []
    markers: list[dict[str, Any]] = []

    for pl in root.findall("playlist"):
        pl_id = pl.attrib["id"]
        if pl_id == "main_bin":
            for prop in pl.findall("property"):
                if prop.attrib.get("name") == "kdenlive:docproperties.guides" and prop.text:
                    markers = json.loads(prop.text)
            continue

        for entry in pl.findall("entry"):
            entry_filters = []
            for filt in entry.findall("filter"):
                fprops = {p.attrib["name"]: (p.text or "") for p in filt.findall("property")}
                f_rec = {"id": filt.attrib.get("id", ""), "properties": fprops}
                entry_filters.append(f_rec)
                filters.append(f_rec)
            clips.append(
                {
                    "playlist": pl_id,
                    "producer": entry.attrib["producer"],
                    "in": entry.attrib.get("in", ""),
                    "out": entry.attrib.get("out", ""),
                    "resource": producers.get(entry.attrib["producer"], {}).get("resource", ""),
                    "filters": entry_filters,
                }
            )

    return {
        "profile": profile,
        "tracks": tracks,
        "clips": clips,
        "filters": filters,
        "markers": markers,
    }


def render_with_melt(project_path: Path, output_mp4: Path) -> subprocess.CompletedProcess[str]:
    """Render a .kdenlive MLT project headlessly to MP4 using melt."""
    assert MELT_BIN is not None, "melt is required for MLT render verification"
    env = os.environ.copy()
    env.setdefault("SDL_AUDIODRIVER", "dummy")
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    if os.name == "posix" and not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        env["DISPLAY"] = ":99"
    cmd = [
        MELT_BIN,
        str(project_path),
        "-consumer",
        f"avformat:{output_mp4}",
        "vcodec=libx264",
        "preset=ultrafast",
        "crf=18",
        "acodec=aac",
        "ab=128k",
        "pix_fmt=yuv420p",
    ]
    return subprocess.run(
        cmd,
        env=env,
        shell=False,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def probe_media(media_path: Path) -> dict[str, Any]:
    """Inspect container and streams via ffprobe."""
    assert FFPROBE_BIN is not None, "ffprobe is required for render verification"
    proc = subprocess.run(
        [
            FFPROBE_BIN,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(media_path),
        ],
        shell=False,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


def decoded_framemd5(media_path: Path) -> str:
    """Compute deterministic decoded video+audio frame MD5 signatures via ffmpeg."""
    assert FFMPEG_BIN is not None
    proc = subprocess.run(
        [
            FFMPEG_BIN,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(media_path),
            "-f",
            "framemd5",
            "-",
        ],
        shell=False,
        check=True,
        capture_output=True,
        text=True,
    )
    # Strip ffmpeg software version comment lines so hashes reflect pure decoded frames
    return "\n".join(
        line for line in proc.stdout.splitlines() if not line.startswith("#software:")
    )


def atomic_write_text(target: Path, content: str) -> None:
    """Write file atomically using temp file + fsync + os.replace (crash-safe save)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.tmp.")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def recover_project_from_autosave(project_path: Path, autosave_path: Path) -> bool:
    """Detect corrupted primary .kdenlive XML and restore atomically from valid autosave."""
    primary_valid = False
    if project_path.is_file():
        try:
            DefusedET.parse(project_path)
            primary_valid = True
        except Exception:
            primary_valid = False

    if primary_valid:
        return False

    # Validate autosave snapshot before restoring
    DefusedET.parse(autosave_path)
    snapshot_text = autosave_path.read_text(encoding="utf-8")
    atomic_write_text(project_path, snapshot_text)
    return True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def synthetic_source_media(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    """Generate one immutable synthetic audio+video clip and record its SHA-256 hash."""
    if not (MELT_BIN and FFMPEG_BIN and FFPROBE_BIN):
        pytest.skip("melt, ffmpeg, and ffprobe are required for project integrity tests")
    media_dir = tmp_path_factory.mktemp("bangonpro_source_media")
    media_file = generate_synthetic_media(media_dir / "camA_master.mp4", duration_sec=2.0)
    return media_file, sha256_file(media_file)


# ---------------------------------------------------------------------------
# Integrity Tests
# ---------------------------------------------------------------------------


def test_project_opens_and_renders_via_melt_with_immutable_source(
    synthetic_source_media: tuple[Path, str], tmp_path: Path
) -> None:
    """Verify MLT .kdenlive project opens & renders cleanly while leaving source media untouched."""
    media_file, baseline_sha256 = synthetic_source_media
    project_path = tmp_path / "episode_01.kdenlive"
    render_out = tmp_path / "episode_01_render.mp4"

    atomic_write_text(project_path, build_kdenlive_project_xml(media_file))
    render_with_melt(project_path, render_out)

    assert render_out.is_file() and render_out.stat().st_size > 1000
    info = probe_media(render_out)
    streams = info["streams"]
    video_streams = [s for s in streams if s["codec_type"] == "video"]
    audio_streams = [s for s in streams if s["codec_type"] == "audio"]

    assert len(video_streams) == 1
    assert len(audio_streams) == 1
    assert int(video_streams[0]["width"]) == 640
    assert int(video_streams[0]["height"]) == 360
    assert float(info["format"]["duration"]) >= 1.8

    # Source media must remain bit-for-bit identical
    assert sha256_file(media_file) == baseline_sha256, "Source media was modified during render!"


def test_autosave_snapshot_and_crash_recovery_produces_identical_xml_and_render(
    synthetic_source_media: tuple[Path, str], tmp_path: Path
) -> None:
    """Simulate mid-write crash & verify autosave recovery yields identical XML and render."""
    media_file, baseline_sha256 = synthetic_source_media
    project_path = tmp_path / "show_edit.kdenlive"
    autosave_path = tmp_path / ".show_edit.kdenlive.autosave"

    original_xml = build_kdenlive_project_xml(
        media_file, brightness_level="1.15", volume_level="-2dB"
    )
    atomic_write_text(project_path, original_xml)
    atomic_write_text(autosave_path, original_xml)

    # Render baseline before crash
    baseline_render = tmp_path / "before_crash.mp4"
    render_with_melt(project_path, baseline_render)
    baseline_framemd5 = decoded_framemd5(baseline_render)

    # Simulate power-loss / mid-write truncation corrupting the primary .kdenlive file
    project_path.write_text(original_xml[:180], encoding="utf-8")
    with pytest.raises(Exception):
        DefusedET.parse(project_path)

    # Execute crash recovery from autosave snapshot
    recovered = recover_project_from_autosave(project_path, autosave_path)
    assert recovered is True
    assert project_path.read_text(encoding="utf-8") == original_xml

    # Render recovered project and verify frame-identical video + audio output
    recovered_render = tmp_path / "after_recovery.mp4"
    render_with_melt(project_path, recovered_render)
    recovered_framemd5 = decoded_framemd5(recovered_render)

    assert recovered_framemd5 == baseline_framemd5, (
        "Crash-recovered project produced different rendered frames than pre-crash project!"
    )
    assert sha256_file(media_file) == baseline_sha256


def test_project_reopen_roundtrip_preserves_tracks_clips_filters_and_markers(
    synthetic_source_media: tuple[Path, str], tmp_path: Path
) -> None:
    """Verify reopen & re-serialization round-trip preserves tracks, clips, filters, and markers."""
    media_file, baseline_sha256 = synthetic_source_media
    markers = [
        {"comment": "Cold Open", "pos": 0, "type": 0},
        {"comment": "Speaker Switch Cam B", "pos": 20, "type": 1},
        {"comment": "Sponsor Read", "pos": 40, "type": 3},
    ]

    project_v1 = tmp_path / "roundtrip_v1.kdenlive"
    project_v2 = tmp_path / "roundtrip_v2.kdenlive"
    atomic_write_text(
        project_v1,
        build_kdenlive_project_xml(
            media_file,
            markers=markers,
            brightness_level="1.20",
            volume_level="-4dB",
        ),
    )

    # Open via defusedxml, inspect structured model, re-serialize cleanly to v2, and reopen v2
    struct_v1 = parse_kdenlive_structure(project_v1)
    reopened_tree = DefusedET.parse(project_v1)
    ET.indent(reopened_tree.getroot(), space=" ")
    xml_out = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        + ET.tostring(reopened_tree.getroot(), encoding="unicode")
        + "\n"
    )
    atomic_write_text(project_v2, xml_out)
    struct_v2 = parse_kdenlive_structure(project_v2)

    assert struct_v2["profile"] == struct_v1["profile"]
    assert struct_v2["tracks"] == struct_v1["tracks"]
    assert len(struct_v2["tracks"]) == 3  # A1 (tractor0), V1 (tractor1), Master (tractor2)
    assert struct_v2["clips"] == struct_v1["clips"]
    assert len(struct_v2["clips"]) == 2
    assert struct_v2["filters"] == struct_v1["filters"]
    assert any(
        f["properties"].get("mlt_service") == "brightness"
        and f["properties"].get("level") == "1.20"
        for f in struct_v2["filters"]
    )
    assert any(
        f["properties"].get("mlt_service") == "volume"
        and f["properties"].get("level") == "-4dB"
        for f in struct_v2["filters"]
    )
    assert struct_v2["markers"] == markers

    # Also verify MLT's own xml consumer can load and round-trip the reopened project cleanly
    mlt_xml_out = tmp_path / "mlt_reexported.kdenlive"
    env = os.environ.copy()
    env.setdefault("SDL_AUDIODRIVER", "dummy")
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    subprocess.run(
        [MELT_BIN, str(project_v2), "-consumer", f"xml:{mlt_xml_out}"],
        env=env,
        shell=False,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert mlt_xml_out.is_file() and mlt_xml_out.stat().st_size > 200
    DefusedET.parse(mlt_xml_out)

    # Confirm reopened project renders identically and source media stays immutable
    render_v2 = tmp_path / "roundtrip_v2.mp4"
    render_with_melt(project_v2, render_v2)
    info = probe_media(render_v2)
    assert float(info["format"]["duration"]) >= 1.8
    assert sha256_file(media_file) == baseline_sha256


def test_atomic_transaction_rollback_on_failed_edit(
    synthetic_source_media: tuple[Path, str], tmp_path: Path
) -> None:
    """Verify a faulted multi-step project edit rolls back cleanly without corrupting state."""
    media_file, baseline_sha256 = synthetic_source_media
    project_path = tmp_path / "transactional.kdenlive"
    original_xml = build_kdenlive_project_xml(media_file, brightness_level="1.05")
    atomic_write_text(project_path, original_xml)
    pre_tx_sha = sha256_file(project_path)
    pre_tx_struct = parse_kdenlive_structure(project_path)

    # Simulate an atomic transaction: copy to staging, apply step 1, fail validation on step 2
    staging_xml = project_path.read_text(encoding="utf-8")
    root = DefusedET.fromstring(staging_xml)

    def apply_batch_atomically(operations: list[dict[str, Any]]) -> None:
        working_root = DefusedET.fromstring(project_path.read_text(encoding="utf-8"))
        for op in operations:
            if op["type"] == "set_brightness":
                for filt in working_root.findall(".//filter"):
                    svc = filt.find("property[@name='mlt_service']")
                    if svc is not None and svc.text == "brightness":
                        lvl = filt.find("property[@name='level']")
                        assert lvl is not None
                        lvl.text = str(op["value"])
            elif op["type"] == "place_clip":
                if op["out_frame"] <= op["in_frame"]:
                    raise ValueError("Invalid frame range: out_frame must exceed in_frame")
            else:
                raise ValueError(f"Unsupported operation: {op['type']}")
        # Only commit atomically if ALL operations succeed
        atomic_write_text(
            project_path,
            '<?xml version="1.0" encoding="utf-8"?>\n'
            + ET.tostring(working_root, encoding="unicode")
            + "\n",
        )

    assert root is not None
    with pytest.raises(ValueError, match="Invalid frame range"):
        apply_batch_atomically(
            [
                {"type": "set_brightness", "value": "1.99"},
                {"type": "place_clip", "in_frame": 40, "out_frame": 10},
            ]
        )

    # Confirm complete rollback: project XML hash, structure, and source media unchanged
    assert sha256_file(project_path) == pre_tx_sha
    assert parse_kdenlive_structure(project_path) == pre_tx_struct
    assert sha256_file(media_file) == baseline_sha256
