"""V1.4 specialist roles for the 14 production agents: brief, edit permissions, completion criteria.

The agents keep their V1.3 identities and deterministic engines. V1.4 gives each one what an
experienced YouTube editor in that seat would know (``brief``), exactly which native timeline
operations it may perform and on which tracks (``ops`` / ``tracks`` / ``effects``), and measurable
``done_when`` checks evaluated by :mod:`draft_qc` on the actual draft. A reasoning client acts *as*
one of these agents; every edit is attributed to it and checked against these permissions.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

from typing import Any

from bangonpro.ai_engine.department.registry import AGENT_ORDER, AGENTS_BY_ID

ALL_TRACKS = ("V1", "V2", "A1", "A2")
COLOUR_EFFECTS = frozenset({"lift_gamma_gain", "avfilter.lut3d", "frei0r.colgate", "avfilter.eq", "brightness", "avfilter.colorbalance", "avfilter.curves", "frei0r.saturat0r", "avfilter.colortemperature"})
VFX_EFFECTS = frozenset({"qtblend", "affine", "avfilter.boxblur", "avfilter.gblur", "boxblur", "frei0r.squareblur", "crop", "avfilter.crop", "mask_start", "mask_apply", "rotoscoping", "avfilter.vignette", "frei0r.vignette", "obscure", "avfilter.delogo"})
GRAPHICS_EFFECTS = frozenset({"qtblend", "affine", "brightness", "crop"})
AUDIO_EFFECTS = frozenset({"volume", "avfilter.highpass", "avfilter.lowpass", "avfilter.afftdn", "avfilter.arnndn", "avfilter.acompressor", "avfilter.loudnorm",
                           "avfilter.agate", "avfilter.equalizer", "avfilter.alimiter", "avfilter.deesser", "panner", "mute", "fadein", "fadeout"})
READ_TOOLS = ("bop_status", "bop_project", "bop_media", "bop_transcript", "bop_search", "bop_frames", "bop_audio", "bop_ops", "bop_agents", "bop_qc")

ROLES: dict[str, dict[str, Any]] = {
    "executive": {
        "brief": "Read the creative brief like a lead editor: decide what the finished video must achieve, break it into specialist tasks, "
                 "send each to the agent that owns it, resolve dependencies (structure before finishing, finishing before QC), and only report done "
                 "after QC has verified the actual draft. Never edit the timeline yourself; delegate.",
        "ops": (), "tracks": (), "tools": READ_TOOLS + ("bop_task", "bop_run_agent", "bop_revise", "bop_preview"),
        "done_when": ["every planned task is completed or explained", "QC reports no unresolved high-confidence defect introduced by the task"],
    },
    "learning": {
        "brief": "Bring the series' approved preferences (pacing, camera habits, caption style, loudness) into every task and record accepted "
                 "agent edits as corrections so the next episode starts closer. Never change rules without human approval.",
        "ops": (), "tracks": (), "tools": READ_TOOLS + ("bop_learn",),
        "done_when": ["approved preferences for the series are listed in the task context"],
    },
    "media_ingest": {
        "brief": "Know every file: role (wide, host close-up, guest close-up, mic, B-roll, graphic), duration, frame rate, audio channels. "
                 "Register supplied B-roll and graphics so other agents can place them by id.",
        "ops": ("import_media",), "tracks": (), "tools": READ_TOOLS,
        "done_when": ["every supplied asset has a media_ref", "missing or unreadable media are reported"],
    },
    "synchronisation": {
        "brief": "Keep picture and sound in sync. After any edit, every V1 shot must show the moment A1 plays (within one frame). Fix drift by "
                 "slipping the source in point, never by moving dialogue.",
        "ops": ("trim_clip", "move_clip", "sync_clips"), "tracks": ALL_TRACKS, "tools": READ_TOOLS,
        "done_when": ["no av_desync defect"],
    },
    "editorial_intelligence": {
        "brief": "Understand the conversation: who is speaking, the hook in the first 30 seconds, topic changes, jokes and reactions, the "
                 "strongest standalone moments for Shorts. Mark them with evidence; say when evidence is only speech activity without words.",
        "ops": ("add_marker",), "tracks": ("V1",), "tools": READ_TOOLS,
        "done_when": ["findings cite timeline times and their evidence"],
    },
    "multicam_director": {
        "brief": "Cut cameras with intent: the speaker's close-up when one person talks, the listener when the reaction is the story, the wide "
                 "for crosstalk, topic changes and resets. No shot under the profile minimum, no flash frames, no cutting away mid-sentence "
                 "without a reason. Audio stays continuous: you switch pictures, not sound.",
        "ops": ("split_clip", "delete_clip", "place_clip", "select_camera", "trim_clip"), "tracks": ("V1",), "tools": READ_TOOLS,
        "done_when": ["no wrong_camera defect", "no shot shorter than the profile minimum it created"],
    },
    "rough_cut": {
        "brief": "Shape the story: remove dead air, false starts and repeated takes, tighten without losing natural beats, keep the hook early. "
                 "When you remove a range, remove it from every track and close the gap so picture, sound, graphics and captions stay together.",
        "ops": ("split_clip", "trim_clip", "move_clip", "delete_clip", "place_clip", "add_transition", "update_caption", "delete_caption"),
        "tracks": ALL_TRACKS, "tools": READ_TOOLS,
        "done_when": ["no gap_in_picture defect", "no av_desync defect", "the requested duration or structure is met"],
    },
    "audio_engineer": {
        "brief": "Make dialogue clear and even: consistent level between mics, no clipping, gentle noise reduction, -14 LUFS integrated and "
                 "-1 dBTP for YouTube unless the series says otherwise. Fade music under speech.",
        "ops": ("set_audio_level", "add_audio_filter", "add_effect", "set_effect_param", "set_keyframe", "add_transition"), "tracks": ("A1", "A2"),
        "effects": AUDIO_EFFECTS, "tools": READ_TOOLS,
        "done_when": ["no audio_level_jump defect", "rendered loudness within 1 LU of target when measured"],
    },
    "colourist": {
        "brief": "Match cameras so cuts are invisible: the same skin tone and exposure on every angle, the series look applied consistently, "
                 "using the native colour effects only.",
        "ops": ("add_effect", "set_effect_param", "set_keyframe"), "tracks": ("V1",), "effects": COLOUR_EFFECTS, "tools": READ_TOOLS,
        "done_when": ["no inconsistent_colour defect"],
    },
    "captions": {
        "brief": "Accurate, readable, branded captions: at most two balanced lines, on screen long enough to read, timed to the words, names "
                 "spelt as the series spells them. Never publish placeholder text from speech activity alone.",
        "ops": ("add_caption", "update_caption", "delete_caption"), "tracks": ("V1", "V2"), "tools": READ_TOOLS,
        "done_when": ["no caption_unreadable defect", "captions are inside the program duration"],
    },
    "graphics": {
        "brief": "Lower thirds when a person first speaks, score cards and headline images from supplied assets, placed where they do not cover "
                 "faces or captions and held long enough to read. Only supplied or template graphics, never invented logos.",
        "ops": ("add_lower_third", "place_clip", "move_clip", "trim_clip", "delete_clip", "add_effect", "set_effect_param", "set_keyframe", "add_transition", "import_media"),
        "tracks": ("V2",), "effects": GRAPHICS_EFFECTS, "tools": READ_TOOLS,
        "done_when": ["no graphic_wrong_speaker defect", "no graphic_too_short defect"],
    },
    "vfx": {
        "brief": "Use the native effects, masks, transforms and keyframes to fix framing (punch-in, reposition), blur what must be hidden and "
                 "build simple composites. Keyframe moves smoothly; never distort faces.",
        "ops": ("apply_mask", "add_effect", "set_effect_param", "set_keyframe", "add_transition"), "tracks": ("V1", "V2"), "effects": VFX_EFFECTS | COLOUR_EFFECTS,
        "tools": READ_TOOLS,
        "done_when": ["requested effect is present on the named clips", "no poor_framing defect it was asked to fix"],
    },
    "quality_control": {
        "brief": "Inspect the actual draft and rendered output independently: wrong cameras, flash frames, gaps, sync, misplaced graphics, "
                 "inconsistent colour, unreadable captions, loudness. Report actionable defects with time, evidence, confidence and the agent "
                 "that fixes it. You never edit the cut.",
        "ops": ("add_marker",), "tracks": ("V1",), "tools": READ_TOOLS + ("bop_preview",),
        "done_when": ["every defect has an owner, a time and evidence"],
    },
    "distribution": {
        "brief": "Deliver: the 16:9 master, coherent vertical Shorts that stand alone (hook first, subject framed), and highlight cuts, each as an "
                 "editable project with a checked render. Never publish or upload.",
        "ops": ("create_sequence", "export_sequence"), "tracks": (), "tools": READ_TOOLS + ("bop_vertical", "bop_preview", "bop_export"),
        "done_when": ["every deliverable has an editable project and a checked render"],
    },
}

assert tuple(ROLES) == AGENT_ORDER, "every production agent needs a V1.4 role, in registry order"


def role(agent_id: str) -> dict[str, Any]:
    if agent_id not in ROLES:
        raise KeyError(f"Unknown production agent '{agent_id}'. Known: {', '.join(AGENT_ORDER)}")
    r = dict(ROLES[agent_id])
    d = AGENTS_BY_ID[agent_id]
    r.update({"agent_id": agent_id, "display_name": d.display_name, "responsibilities": d.responsibilities, "engine": d.engine})
    return r


def _effect_id(op: dict[str, Any]) -> str:
    p = op.get("params") or {}
    return str(p.get("kdenlive_id") or p.get("mlt_service") or p.get("effect_id") or p.get("filter_id") or "")


def permission_violations(agent_id: str, ops: list[dict[str, Any]], clip_tracks: dict[str, str]) -> list[str]:
    """Why ``agent_id`` may not perform ``ops`` (empty when allowed). ``clip_tracks`` maps clip id -> track."""
    r = ROLES.get(agent_id)
    if r is None:
        return [f"Unknown agent '{agent_id}'"]
    out: list[str] = []
    allowed_effects = r.get("effects")
    for op in ops:
        t = op["type"]
        if t not in r["ops"]:
            out.append(f"{agent_id} may not perform {t} (allowed: {', '.join(r['ops']) or 'no timeline edits'})")
            continue
        track = op.get("track_id") or clip_tracks.get(str(op.get("clip_id") or ""), "")
        if track == "V1b":
            track = "V1"
        if r["tracks"] and track and track not in r["tracks"] and t not in ("import_media", "add_caption", "update_caption", "delete_caption", "add_marker"):
            out.append(f"{agent_id} may not change track {track} (allowed: {', '.join(r['tracks'])})")
        if allowed_effects is not None and t in ("add_effect", "add_audio_filter"):
            eid = _effect_id(op)
            if eid and eid not in allowed_effects:
                out.append(f"{agent_id} may not apply effect '{eid}'")
    return out


def roster() -> list[dict[str, Any]]:
    return [{"agent_id": a, "display_name": AGENTS_BY_ID[a].display_name, "ops": list(ROLES[a]["ops"]), "tracks": list(ROLES[a]["tracks"]), "done_when": ROLES[a]["done_when"]} for a in AGENT_ORDER]


__all__ = ["ROLES", "permission_violations", "role", "roster"]
