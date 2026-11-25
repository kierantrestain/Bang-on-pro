"""Deterministic editorial evaluation (V1.2.2): synthetic scenarios with known ground truth.

Evidence class: **unit/synthetic**. Every scenario is a constructed transcript state
(segments, timed words, pauses, reaction events) with hand-labelled ground truth; no real
footage is used and no neural inference is executed. The same metric functions measure
V1.2.1 and V1.2.2 decisions on identical inputs, so the comparison is like for like.

Scenarios: podcast question / punchline / laugh / stutter; daily news with dead air;
gameshow suspense and applause; crosstalk, rapid exchange and an interjection; false
starts and repeats (also under a "keep every word" brief).

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from bangonpro.ai_engine.department import editorial_text as et
from bangonpro.ai_engine.state import (
    AsrBackendReport,
    MediaAsset,
    ProductionIntent,
    ProjectAnalysisState,
    SyncResult,
    TimeInterval,
    TranscriptSegment,
)

EVIDENCE_CLASS = "unit/synthetic (constructed transcript states with hand-labelled ground truth; no real footage; no neural inference executed)"


# ---------------------------------------------------------------------------
# Scenario construction
# ---------------------------------------------------------------------------
def _words(text: str, start: float, end: float) -> list[dict[str, Any]]:
    toks = text.split()
    if not toks:
        return []
    weights = [max(1, len(et.norm_token(t))) + 1 for t in toks]
    total = float(sum(weights))
    dur = end - start
    out, cur = [], start
    for t, w in zip(toks, weights):
        span = dur * w / total
        out.append({"word": t, "start_sec": round(cur, 4), "end_sec": round(cur + span * 0.86, 4), "confidence": 0.93, "timing": "asr"})
        cur += span
    return out


def make_state(profile_id: str, turns: list[tuple[float, float, str, str]], *, duration: float, brief: str = "Build a first cut", cameras: dict[str, tuple[str, str | None]] | None = None, nonspeech: list[dict[str, Any]] | None = None, overlaps: set[int] | None = None, neural: bool = True) -> ProjectAnalysisState:
    from bangonpro.ai_engine.department.editorial_analysis import segment_topics
    from bangonpro.ai_engine.state import StorySection
    from bangonpro.show_profiles.loader import load_show_profile

    prof = load_show_profile(profile_id)
    intent = ProductionIntent.full_production(brief)
    st = ProjectAnalysisState(f"eval_{profile_id}", "seq_eval", prof, tempfile.mkdtemp(prefix="bop_eval_"), instruction=brief, intent=intent)
    cams = cameras or {c["camera_id"]: (c["role"], c.get("speaker_label")) for c in prof["cameras"]}
    for cid, (role, spk) in cams.items():
        st.media_assets[cid] = MediaAsset(f"media_{cid}", cid, role, spk, f"/synthetic/{cid}.mp4", "0" * 64, 1, duration, 1920, 1080, 25, 1, "h264", "aac", 48000, 2)
    ref = next((c for c, (r, _s) in cams.items() if r in ("wide", "desk")), next(iter(cams)))
    st.sync_result = SyncResult(ref, "synthetic", 16000, offsets_sec={c: 0.0 for c in cams}, confidences={c: 1.0 for c in cams}, common_duration_sec=duration)
    spk_map = prof.get("speaker_camera_map") or {}
    overlaps = overlaps or set()
    for i, (s, e, spk, text) in enumerate(turns):
        is_nn = neural and not text.startswith("[")
        st.transcript_segments.append(
            TranscriptSegment(
                segment_id=f"seg_{i + 1:04d}", start_sec=s, end_sec=e, speaker_id=spk, camera_id=spk_map.get(spk, ref),
                text=text if is_nn else f"[VAD speech segment: {spk}, {e - s:.1f}s]", confidence=0.9, is_low_confidence=False,
                energy_db=-20.0 - (i % 3), is_neural_inference=is_nn, transcript_quality="neural_asr" if is_nn else "vad_speech_segments_only",
                is_overlap=i in overlaps, overlap_speakers=["Host", "Guest"] if i in overlaps else [], words=_words(text, s, e) if is_nn else [],
            )
        )
    st.asr_backend_report = AsrBackendReport(backend_id="synthetic_asr" if neural else "deterministic_multimic_vad", is_neural_inference=neural, transcript_quality="neural_asr" if neural else "vad_speech_segments_only")
    min_sil = float(prof["editing_rules"]["min_silence_sec"])
    segs = sorted(st.transcript_segments, key=lambda x: x.start_sec)
    if segs[0].start_sec >= 0.4:
        st.silence_intervals.append(TimeInterval(0.0, segs[0].start_sec, "setup_delay"))
        st.setup_delay_sec = segs[0].start_sec
    end_so_far = segs[0].end_sec
    for a in segs[1:]:
        if a.start_sec - end_so_far >= min_sil:
            st.silence_intervals.append(TimeInterval(round(end_so_far, 4), round(a.start_sec, 4), "silence"))
        end_so_far = max(end_so_far, a.end_sec)
    if duration - end_so_far >= min_sil:
        st.silence_intervals.append(TimeInterval(round(end_so_far, 4), duration, "trailing_silence"))
    st.metadata["nonspeech_activity"] = list(nonspeech or [])
    rows = segment_topics(st.transcript_segments, prof, min_silence_sec=min_sil)
    st.story_sections = [StorySection(r["section_id"], r["label"], r["start_sec"], r["end_sec"], r["dominant_speaker"], r["confidence"]) for r in rows]
    return st


def scenarios() -> dict[str, dict[str, Any]]:
    """Scenario definitions with ground truth (master clock, seconds)."""
    podcast = [
        (1.0, 6.0, "Host", "Welcome back to the show. So tell me, what happened when you finally met the producers?"),
        (7.8, 13.0, "Guest", "Well, it was a disaster from the start. They had lost all of the tapes."),
        (16.0, 20.0, "Guest", "We waited for hours. Then the director walked in."),
        (21.2, 24.0, "Guest", "And he said, we start again tomorrow."),
        (24.5, 26.5, "Host", "Ha ha ha."),
        (27.5, 32.0, "Host", "That is brilliant. I I think we we need a sequel."),
        (34.4, 38.0, "Guest", "Absolutely, as long as the coffee is good."),
    ]
    news = [
        (0.5, 8.0, "Host", "Good evening. Breaking news tonight: the council has confirmed a two million pound deal for the new bridge."),
        (10.5, 16.0, "Host", "Work will start in the spring. In other news, the city marathon set a new record this morning."),
        (17.0, 24.0, "Reporter", "Thousands lined the streets as the winner crossed the line in record time."),
        (26.0, 30.0, "Host", "That is all from us tonight. Good night."),
    ]
    gameshow = [
        (1.0, 5.0, "Host", "For one million pounds, is that your final answer?"),
        (8.0, 8.6, "Contestant", "Yes."),
        (14.0, 18.0, "Host", "You have just won one million pounds!"),
        (20.5, 24.0, "Host", "We will be right back after the break."),
    ]
    crosstalk = [
        (1.0, 4.9, "Host", "I want to talk about the final scene. It was shot in one take."),
        (5.0, 5.4, "Guest", "Yeah."),
        (5.5, 12.0, "Host", "The crew had one chance before the sun went down, and they nailed it."),
        (12.5, 13.3, "Host", "Really?"),
        (13.4, 14.3, "Guest", "Totally."),
        (14.4, 15.2, "Host", "No way."),
        (15.3, 16.1, "Guest", "Yes way."),
        (16.2, 17.0, "Host", "Wow."),
        (17.5, 19.5, "Host", "But hang on, that is not what you told me before."),
        (20.0, 25.9, "Guest", "Let me explain how it really happened on the day. Nobody believed us at first."),
        (26.0, 26.8, "Host", "Ha ha."),
        (26.9, 34.0, "Guest", "We had the camera on a bicycle. The director was pedalling while shouting directions."),
    ]
    false_start = [
        (1.0, 7.0, "Guest", "We were going to- we went to the studio at the the end of the day."),
        (8.0, 12.0, "Guest", "I think I think it worked out in the end."),
    ]
    return {
        "podcast_question_punchline_laugh": {
            "profile": "podcast_multicam", "duration": 39.5, "turns": podcast,
            "truth": {
                "meaningful_pauses": [(6.0, 7.8, "after question"), (20.0, 21.2, "beat before punchline"), (26.5, 27.5, "laugh settling")],
                "dead_air": [(13.0, 16.0), (32.0, 34.4)],
                "disfluencies": [("I", 27.5, 32.0, 0), ("we", 27.5, 32.0, 0)],
                "highlight_payoff": (24.5, 26.5),
            },
        },
        "news_dead_air": {
            "profile": "daily_news", "duration": 31.0, "turns": news,
            "cameras": {"cam_desk": ("desk", "Host"), "cam_broll": ("broll", None)},
            "truth": {"meaningful_pauses": [], "dead_air": [(8.0, 10.5), (16.0, 17.0), (24.0, 26.0)], "disfluencies": []},
        },
        "gameshow_suspense_applause": {
            "profile": "gameshow_multicam", "duration": 25.0, "turns": gameshow,
            "nonspeech": [{"start_sec": 9.2, "end_sec": 13.2, "where": "pause", "kind_hint": "applause", "dominant_camera": "cam_wide", "dominant_speaker": None}],
            "truth": {"meaningful_pauses": [(5.0, 8.0, "suspense after question"), (8.6, 9.2, "beat before applause"), (13.2, 14.0, "applause settling")], "reaction_audio": [(9.2, 13.2)], "dead_air": [(18.0, 20.5)], "disfluencies": []},
        },
        "crosstalk_exchange_interjection": {
            "profile": "podcast_multicam", "duration": 34.5, "turns": crosstalk, "overlaps": {8},
            "truth": {"interjection": (5.0, 5.4), "rapid_exchange": (12.5, 17.0), "crosstalk": (17.5, 19.5), "listener_reaction": (26.0, 26.8, "cam_host"), "meaningful_pauses": [], "dead_air": [], "disfluencies": []},
        },
        "false_start_repeat": {
            "profile": "podcast_multicam", "duration": 13.0, "turns": false_start,
            "truth": {"meaningful_pauses": [], "dead_air": [], "disfluencies": [("We were going to-", 1.0, 7.0, 0), ("the", 1.0, 7.0, 8), ("I think", 8.0, 12.0, 0)]},
        },
    }


def build(name: str, *, brief: str = "Build a first cut") -> ProjectAnalysisState:
    sc = scenarios()[name]
    return make_state(sc["profile"], sc["turns"], duration=sc["duration"], brief=brief, cameras=sc.get("cameras"), nonspeech=sc.get("nonspeech"), overlaps=sc.get("overlaps"))


# ---------------------------------------------------------------------------
# Version-independent ground-truth metrics
# ---------------------------------------------------------------------------
def _kept_len(a: float, b: float, kept: list[Any]) -> float:
    return sum(max(0.0, min(b, k.end_sec) - max(a, k.start_sec)) for k in kept)


def _disfluency_words(st: ProjectAnalysisState, truth: dict[str, Any]) -> list[tuple[dict[str, Any], bool]]:
    """(word, is_ground_truth_disfluency) for every timed word."""
    gt_idx: set[tuple[str, int]] = set()
    for text, s, _e, first in truth.get("disfluencies", []):
        seg = next(x for x in st.transcript_segments if abs(x.start_sec - s) < 1e-6)
        for k in range(len(text.split())):
            gt_idx.add((seg.segment_id, first + k))
    out = []
    for seg in st.transcript_segments:
        for k, w in enumerate(et.segment_words(seg)):
            out.append((w, (seg.segment_id, k) in gt_idx))
    return out


def ground_truth_metrics(st: ProjectAnalysisState, kept: list[Any], truth: dict[str, Any]) -> dict[str, Any]:
    from bangonpro.ai_engine.department.editorial_pacing import REF_NATURAL_CAP, REF_PROTECTED_FLOOR, format_key, pacing_metrics

    fmt = format_key(st.profile)
    floor, cap = REF_PROTECTED_FLOOR[fmt], REF_NATURAL_CAP[fmt]
    mp = truth.get("meaningful_pauses", [])
    preserved = [p for p in mp if _kept_len(p[0], p[1], kept) >= min(p[1] - p[0], floor) - 0.05]
    da = truth.get("dead_air", [])
    removable = sum(max(0.0, (b - a) - cap) for a, b in da)
    left = sum(max(0.0, _kept_len(a, b, kept) - cap) for a, b in da)
    react = truth.get("reaction_audio", [])
    react_kept = sum(_kept_len(a, b, kept) for a, b in react) / max(1e-6, sum(b - a for a, b in react)) if react else None
    words = _disfluency_words(st, truth)

    def removed(w: dict[str, Any]) -> bool:
        mid = (w["start_sec"] + w["end_sec"]) / 2.0
        return not any(k.start_sec - 1e-3 <= mid <= k.end_sec + 1e-3 for k in kept)

    gt_d = [w for w, is_d in words if is_d]
    content = [w for w, is_d in words if not is_d]
    pm = pacing_metrics(st, kept)
    return {
        "meaningful_pauses": len(mp),
        "meaningful_pauses_preserved": len(preserved),
        "meaningful_pause_preservation": round(len(preserved) / len(mp), 4) if mp else None,
        "dead_air_removable_sec": round(removable, 3),
        "dead_air_left_sec": round(left, 3),
        "dead_air_removed_ratio": round(1.0 - left / removable, 4) if removable > 1e-6 else None,
        "reaction_audio_kept_ratio": round(react_kept, 4) if react_kept is not None else None,
        "disfluent_words": len(gt_d),
        "disfluent_words_removed": sum(1 for w in gt_d if removed(w)),
        "content_words_lost": sum(1 for w in content if removed(w)),
        "cuts_inside_words": len(pm["cuts_inside_words"]),
        "cuts_inside_segments": len(pm["cuts_inside_segments"]),
        "interior_cuts": pm["interior_cuts"],
        "edited_sec": pm["edited_sec"],
    }


def shot_truth_metrics(st: ProjectAnalysisState, shots: list[dict[str, Any]], kept: list[Any], truth: dict[str, Any]) -> dict[str, Any]:
    from bangonpro.ai_engine.department.editorial_director import shot_plan_metrics

    m = shot_plan_metrics(st, shots, kept)
    roles = {c: a.camera_role for c, a in st.media_assets.items()}
    spk_map = st.profile.get("speaker_camera_map") or {}
    out = {k: m[k] for k in ("shots", "camera_cuts", "camera_cuts_per_min", "unnecessary_cuts", "speaker_coverage", "overlap_on_wide")}
    out["sub_minimum_shots"] = len(m["sub_minimum_shots"])
    if "interjection" in truth:
        a, b = truth["interjection"]
        guest_cam = spk_map.get("Guest")
        out["cut_to_interjection"] = any(s["camera_id"] == guest_cam and s["start"] < b and s["end"] > a and (s["end"] - s["start"]) < 3.0 for s in shots)
    for key in ("rapid_exchange", "crosstalk"):
        if key in truth:
            a, b = truth[key]
            on_wide = sum(max(0.0, min(b, s["end"]) - max(a, s["start"])) for s in shots if roles.get(s["camera_id"]) == "wide")
            out[f"{key}_on_wide"] = round(on_wide / (b - a), 4)
    if "listener_reaction" in truth:
        a, _b, cam = truth["listener_reaction"]
        out["reaction_shot_on_laugh"] = any(s["camera_id"] == cam and abs(s["start"] - a) <= 0.3 for s in shots)
    return out


def highlight_boundary_metrics(st: ProjectAnalysisState, highlights: list[dict[str, Any]], payoff: tuple[float, float] | None) -> dict[str, Any]:
    words = sorted((w for s in st.transcript_segments for w in et.segment_words(s)), key=lambda w: w["start_sec"])
    starts = set(et.sentence_starts(st.transcript_segments))
    ends = set(et.sentence_boundaries(st.transcript_segments))
    ok = 0
    inside = 0
    for h in highlights:
        a, b = h["start_sec"], h["end_sec"]
        if et.inside_word(a, words) or et.inside_word(b, words):
            inside += 1
        nxt = next((w for w in words if w["start_sec"] >= a - 1e-3), None)
        prv = next((w for w in reversed(words) if w["end_sec"] <= b + 1e-3), None)
        s_ok = nxt is not None and any(abs(nxt["start_sec"] - t) < 1e-3 for t in starts)
        e_ok = prv is not None and any(abs(prv["end_sec"] - t) < 1e-3 for t in ends)
        ok += s_ok and e_ok
    n = len(highlights)
    best = highlights and max(highlights, key=lambda h: h["score"])
    return {
        "highlights": n,
        "on_sentence_boundaries": round(ok / n, 4) if n else None,
        "boundaries_inside_words": inside,
        "top_highlight_contains_payoff": bool(best and payoff and best["start_sec"] <= payoff[0] and best["end_sec"] >= payoff[1]),
    }


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------
def run_v122(st: ProjectAnalysisState) -> dict[str, Any]:
    from bangonpro.ai_engine.agents import rough_cut_multicam as rcm
    from bangonpro.ai_engine.department.editorial_analysis import build_editorial_context, score_highlights
    from bangonpro.ai_engine.department.editorial_pacing import compute_pacing_plan

    st.metadata["editorial_context"] = build_editorial_context(st)
    plan = compute_pacing_plan(st, st.sync_result.common_duration_sec)
    wide, reaction, spk = rcm._resolve_camera_roles(st)
    shots = rcm.plan_rough_cut_shots(st, plan.kept, wide_cam=wide, reaction_cam=reaction, speaker_to_cam=spk, applause_times=rcm._detect_high_energy_applause_times(st, st.sync_result.common_duration_sec))
    hl = score_highlights(st, max_items=4, moments=st.metadata["editorial_context"]["moments"])
    return {"kept": plan.kept, "shots": shots, "highlights": hl, "disfluencies": plan.disfluencies}


def load_v121_modules(repo_root: str | Path, rev: str = "v1.2.1") -> dict[str, Any] | None:
    """Import the V1.2.1 rough cut, captions and highlight code from git (comparison only)."""
    out: dict[str, Any] = {}
    tmp = Path(tempfile.mkdtemp(prefix="bop_v121_"))
    for key, rel in (("rough_cut", "bangonpro/ai_engine/agents/rough_cut_multicam.py"), ("agq", "bangonpro/ai_engine/agents/audio_graphics_qc.py"), ("agents", "bangonpro/ai_engine/department/agents.py")):
        src = None
        for r in (rev, "dbaf0dc"):
            try:
                res = subprocess.run(["git", "-C", str(repo_root), "show", f"{r}:{rel}"], capture_output=True, text=True, timeout=30, check=False)
            except (OSError, subprocess.TimeoutExpired):
                return None
            if res.returncode == 0:
                src = res.stdout
                break
        if src is None:
            return None
        path = tmp / f"_v121_{key}.py"
        path.write_text(src, encoding="utf-8")
        spec = importlib.util.spec_from_file_location(f"_bop_v121_{key}", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        out[key] = mod
    return out


def run_v121(st: ProjectAnalysisState, v121: dict[str, Any]) -> dict[str, Any]:
    rcm = v121["rough_cut"]
    dur = st.sync_result.common_duration_sec
    kept = rcm._compute_kept_master_intervals(st, dur)
    wide, reaction, spk = rcm._resolve_camera_roles(st)
    shots: list[dict[str, Any]] = []
    prev = None
    for iv in kept:
        s = rcm._plan_shots_for_kept_interval(iv, state=st, wide_cam=wide, reaction_cam=reaction, speaker_to_cam=spk, applause_times=[], prev_end_camera=prev)
        shots.extend(s)
        if s:
            prev = s[-1]["camera_id"]
    shots = rcm._apply_intro_outro_rules(shots, state=st, wide_cam=wide)
    hl = v121["agents"].score_highlights(st, max_items=4)
    return {"kept": kept, "shots": shots, "highlights": hl, "disfluencies": []}


def edit_shots(st: ProjectAnalysisState, shots: list[dict[str, Any]]) -> list[Any]:
    """Planned shot dicts -> timeline ``EditShot``s (same frame maths as the engine, no media access)."""
    from bangonpro.ai_engine.state import EditShot

    fps = st.fps
    out, cursor = [], 0
    for i, sp in enumerate(shots, start=1):
        n = max(1, round((sp["end"] - sp["start"]) * fps))
        f_in = round(sp["start"] * fps)
        out.append(EditShot(f"shot_{i:04d}", sp["camera_id"], f"media_{sp['camera_id']}", f"media_{sp['camera_id']}", sp["start"], sp["end"], sp["start"], sp["end"], cursor / fps, (cursor + n) / fps, f_in, f_in + n - 1, f_in, f_in + n - 1, cursor, cursor + n - 1, sp["start"], sp["end"], sp["speaker_id"], sp["reason"], float(sp["confidence"])))
        cursor += n
    return out


def kendall_tau(a: list[float], b: list[float]) -> float:
    n = len(a)
    conc = disc = 0
    for i in range(n):
        for j in range(i + 1, n):
            s = (a[i] - a[j]) * (b[i] - b[j])
            if s > 0:
                conc += 1
            elif s < 0:
                disc += 1
    pairs = n * (n - 1) / 2
    return round((conc - disc) / pairs, 4) if pairs else 1.0


__all__ = [
    "EVIDENCE_CLASS",
    "build",
    "edit_shots",
    "ground_truth_metrics",
    "highlight_boundary_metrics",
    "kendall_tau",
    "load_v121_modules",
    "make_state",
    "run_v121",
    "run_v122",
    "scenarios",
    "shot_truth_metrics",
]
