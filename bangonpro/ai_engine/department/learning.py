"""Learning Agent: versioned, human-approved production preferences.

Records editor decisions (accepted / rejected agent edits, camera overrides,
caption corrections) and compares AI drafts against human-finished projects to
*suggest* profile improvements. Suggestions are never applied automatically:
an editor approves them, which writes a new immutable preference version that
can be reset or rolled back. Only whitelisted production-profile keys may be
changed; application code, model weights and approval rules are out of scope
by construction.

Storage is local by default: ``$BANGONPRO_PREFS_DIR`` or
``~/.local/share/bangonpro/preferences/<profile_id>/<series_id>/``.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bangonpro.ai_engine.department.checkpoint import write_json_atomic

# Only these profile keys can be tuned by learned preferences.
LEARNABLE_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "reaction_shot_frequency",
        "wide_shot_interval_sec",
        "min_shot_duration_sec",
        "max_shot_duration_sec",
        "silence_threshold_db",
        "loudness_target_lufs",
        "speaker_camera_map",
        "camera_preferences",
        "editing_rules",
        "audio_rules",
        "caption_style",
        "graphics_rules",
        "graphics_templates",
        "colour_rules",
        "distribution_rules",
    }
)
_FORBIDDEN_KEY_RE = re.compile(r"(approval|consent|source_code|application_code|model_weights|weights_path|provider)", re.IGNORECASE)

# Numeric guard rails so a learned value can never leave a safe editorial range.
NUMERIC_BOUNDS: dict[str, tuple[float, float]] = {
    "reaction_shot_frequency": (0.0, 1.0),
    "wide_shot_interval_sec": (4.0, 120.0),
    "min_shot_duration_sec": (0.5, 30.0),
    "max_shot_duration_sec": (2.0, 120.0),
    "silence_threshold_db": (-70.0, -10.0),
    "loudness_target_lufs": (-31.0, -9.0),
    "editing_rules.min_silence_sec": (0.2, 10.0),
    "editing_rules.min_shot_sec": (0.5, 30.0),
    "editing_rules.max_shot_sec": (2.0, 120.0),
    "editing_rules.min_shot_duration_sec": (0.5, 30.0),
    "editing_rules.max_shot_duration_sec": (2.0, 120.0),
    "editing_rules.reaction_shot_frequency": (0.0, 1.0),
    "editing_rules.wide_shot_interval_sec": (4.0, 120.0),
}


class PreferenceError(ValueError):
    pass


def default_prefs_dir() -> Path:
    env = os.environ.get("BANGONPRO_PREFS_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".local" / "share" / "bangonpro" / "preferences").resolve()


def _utc() -> str:
    return datetime.now(UTC).isoformat()


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip()) or "default"
    return cleaned[:96]


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict) and k not in ("speaker_camera_map",):
            flat.update(_flatten(v, key))
        else:
            flat[key] = v
    return flat


def validate_overlay(overlay: dict[str, Any]) -> dict[str, Any]:
    """Reject non-whitelisted keys and clamp numeric values to safe bounds."""
    cleaned: dict[str, Any] = {}
    for top, value in overlay.items():
        if top not in LEARNABLE_TOP_LEVEL_KEYS:
            raise PreferenceError(f"Preference key '{top}' is not a learnable production setting")
        for flat_key in _flatten({top: value}):
            if _FORBIDDEN_KEY_RE.search(flat_key):
                raise PreferenceError(f"Preference key '{flat_key}' may not be changed by learning")
        cleaned[top] = copy.deepcopy(value)
    for flat_key, (lo, hi) in NUMERIC_BOUNDS.items():
        parts = flat_key.split(".")
        cur: Any = cleaned
        for p in parts[:-1]:
            cur = cur.get(p) if isinstance(cur, dict) else None
        if isinstance(cur, dict) and isinstance(cur.get(parts[-1]), (int, float)):
            cur[parts[-1]] = float(min(hi, max(lo, float(cur[parts[-1]]))))
    return cleaned


class ProfilePreferenceStore:
    """Versioned preference overlay per (profile_id, series_id)."""

    def __init__(self, profile_id: str, series_id: str = "default", root: str | Path | None = None) -> None:
        self.profile_id = _safe_name(profile_id)
        self.series_id = _safe_name(series_id or "default")
        base = Path(root).expanduser().resolve() if root else default_prefs_dir()
        self.dir = base / self.profile_id / self.series_id
        self.versions_dir = self.dir / "versions"
        self.suggestions_path = self.dir / "suggestions.json"
        self.corrections_path = self.dir / "corrections.jsonl"
        self.head_path = self.dir / "HEAD.json"

    # ---- versions -------------------------------------------------------
    def _read_head(self) -> dict[str, Any]:
        try:
            return json.loads(self.head_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"active_version": 0}

    def active_version(self) -> int:
        return int(self._read_head().get("active_version", 0))

    def list_versions(self) -> list[dict[str, Any]]:
        if not self.versions_dir.exists():
            return []
        out = []
        for p in sorted(self.versions_dir.glob("v*.json")):
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
                out.append({k: doc.get(k) for k in ("version", "created_at_utc", "reason", "source_suggestions")})
            except (OSError, ValueError):
                continue
        return out

    def load_version(self, version: int) -> dict[str, Any]:
        if version <= 0:
            return {"version": 0, "overlay": {}}
        path = self.versions_dir / f"v{version:04d}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PreferenceError(f"Preference version {version} does not exist") from exc

    def active_overlay(self) -> dict[str, Any]:
        return dict(self.load_version(self.active_version()).get("overlay", {}))

    def _next_version(self) -> int:
        existing = [int(p.stem[1:]) for p in self.versions_dir.glob("v*.json")] if self.versions_dir.exists() else []
        return (max(existing) if existing else 0) + 1

    def commit_overlay(self, overlay: dict[str, Any], *, reason: str, source_suggestions: list[str] | None = None) -> int:
        cleaned = validate_overlay(overlay)
        version = self._next_version()
        write_json_atomic(
            self.versions_dir / f"v{version:04d}.json",
            {
                "version": version,
                "profile_id": self.profile_id,
                "series_id": self.series_id,
                "created_at_utc": _utc(),
                "reason": reason,
                "source_suggestions": list(source_suggestions or []),
                "parent_version": self.active_version(),
                "overlay": cleaned,
            },
        )
        write_json_atomic(self.head_path, {"active_version": version, "updated_at_utc": _utc()})
        return version

    def rollback(self, version: int) -> int:
        if version != 0:
            self.load_version(version)  # existence check
        write_json_atomic(self.head_path, {"active_version": int(version), "updated_at_utc": _utc()})
        return int(version)

    def reset(self) -> int:
        """Return to the unmodified base profile (history is preserved)."""
        return self.rollback(0)

    def apply_to_profile(self, profile: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        overlay = self.active_overlay()
        return (deep_merge(profile, overlay) if overlay else copy.deepcopy(profile)), overlay

    # ---- corrections & suggestions ---------------------------------------
    def record_correction(self, correction: dict[str, Any]) -> dict[str, Any]:
        entry = dict(correction)
        entry.setdefault("correction_id", f"corr_{uuid.uuid4().hex[:10]}")
        entry["recorded_at_utc"] = _utc()
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.corrections_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
        try:  # V1.5: the same human feedback becomes evidence in the department's shared intelligence
            from bangonpro.ai_engine.department.intelligence import capture_correction

            root = os.environ.get("BANGONPRO_INTEL_DIR") or str(self.dir.parent.parent.parent / "intelligence")
            capture_correction(self.profile_id, self.series_id, entry, root=root)
        except Exception:  # noqa: BLE001, S110 - learning is advisory
            pass
        return entry

    def corrections(self) -> list[dict[str, Any]]:
        if not self.corrections_path.exists():
            return []
        out = []
        for line in self.corrections_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out

    def suggestions(self) -> list[dict[str, Any]]:
        try:
            return list(json.loads(self.suggestions_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return []

    def _save_suggestions(self, items: list[dict[str, Any]]) -> None:
        write_json_atomic(self.suggestions_path, items)

    def propose(self, suggestions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        existing = [s for s in self.suggestions() if s.get("status") == "pending"]
        keys = {json.dumps(s.get("overlay"), sort_keys=True) for s in existing}
        for s in suggestions:
            k = json.dumps(s.get("overlay"), sort_keys=True)
            if k in keys:
                continue
            validate_overlay(s["overlay"])
            s = dict(s)
            s.setdefault("suggestion_id", f"sug_{uuid.uuid4().hex[:10]}")
            s["status"] = "pending"
            s["created_at_utc"] = _utc()
            existing.append(s)
            keys.add(k)
        resolved = [s for s in self.suggestions() if s.get("status") != "pending"]
        self._save_suggestions(resolved + existing)
        return existing

    def decide_suggestion(self, suggestion_id: str, *, approve: bool) -> int | None:
        items = self.suggestions()
        target = next((s for s in items if s.get("suggestion_id") == suggestion_id), None)
        if target is None:
            raise PreferenceError(f"Unknown suggestion '{suggestion_id}'")
        if target.get("status") != "pending":
            raise PreferenceError(f"Suggestion '{suggestion_id}' already {target.get('status')}")
        version: int | None = None
        if approve:
            merged = deep_merge(self.active_overlay(), target["overlay"])
            version = self.commit_overlay(merged, reason=target.get("reason", "approved suggestion"), source_suggestions=[suggestion_id])
            target["status"] = "approved"
            target["applied_version"] = version
        else:
            target["status"] = "rejected"
        target["decided_at_utc"] = _utc()
        self._save_suggestions(items)
        return version


def _get_num(profile: dict[str, Any], key: str, default: float) -> float:
    rules = profile.get("editing_rules", {}) or {}
    return float(profile.get(key, rules.get(key, default)))


def suggest_from_decisions(profile: dict[str, Any], decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive bounded profile suggestions from recorded editor decisions.

    Each decision dict may contain ``rejected_ops``/``accepted_ops`` lists of
    ``{"agent_id", "type", "params"}`` and ``camera_overrides`` lists of
    ``{"from_camera", "to_camera", "from_role", "to_role"}``. A suggestion is
    only produced when a pattern recurs (>= 3 observations and >= 60 % share).
    """
    rejected: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    overrides: list[dict[str, Any]] = []
    for d in decisions:
        rejected.extend(d.get("rejected_ops", []) or [])
        accepted.extend(d.get("accepted_ops", []) or [])
        overrides.extend(d.get("camera_overrides", []) or [])

    out: list[dict[str, Any]] = []

    def _share(items: list[dict[str, Any]], pred: Any) -> tuple[int, int]:
        hits = sum(1 for i in items if pred(i))
        return hits, len(items)

    # Reaction shots repeatedly replaced by editors -> reduce reaction frequency
    react_rej, _ = _share(overrides, lambda o: o.get("from_role") == "reaction")
    react_acc = sum(1 for o in accepted if (o.get("params") or {}).get("camera_role") == "reaction")
    if react_rej >= 3 and react_rej >= 0.6 * max(1, react_rej + react_acc):
        cur = _get_num(profile, "reaction_shot_frequency", 0.45)
        new = round(max(0.0, cur - 0.10), 3)
        out.append(
            {
                "category": "camera_choice",
                "reason": f"Editors replaced {react_rej} reaction shots; reduce reaction frequency {cur} -> {new}",
                "evidence": {"reaction_overrides": react_rej, "reaction_accepted": react_acc},
                "overlay": {"reaction_shot_frequency": new, "editing_rules": {"reaction_shot_frequency": new}},
            }
        )

    # Wide shots repeatedly replaced -> lengthen wide reset interval
    wide_rej, _ = _share(overrides, lambda o: o.get("from_role") == "wide")
    if wide_rej >= 3:
        cur = _get_num(profile, "wide_shot_interval_sec", 18.0)
        new = round(cur * 1.25, 2)
        out.append(
            {
                "category": "camera_choice",
                "reason": f"Editors replaced {wide_rej} wide resets; lengthen wide interval {cur}s -> {new}s",
                "evidence": {"wide_overrides": wide_rej},
                "overlay": {"wide_shot_interval_sec": new, "editing_rules": {"wide_shot_interval_sec": new}},
            }
        )

    # Silence/pacing cuts repeatedly rejected -> raise minimum silence to cut
    cut_types = {"delete_clip", "trim_clip", "split_clip"}
    pace_rej = [o for o in rejected if o.get("type") in cut_types and o.get("agent_id") in ("rough_cut", "rough_cut_multicam_agent")]
    pace_acc = [o for o in accepted if o.get("type") in cut_types and o.get("agent_id") in ("rough_cut", "rough_cut_multicam_agent")]
    if len(pace_rej) >= 3 and len(pace_rej) >= 0.6 * (len(pace_rej) + len(pace_acc)):
        rules = profile.get("editing_rules", {}) or {}
        cur = float(rules.get("min_silence_sec", 0.8))
        new = round(cur + 0.3, 2)
        out.append(
            {
                "category": "pacing",
                "reason": f"Editors rejected {len(pace_rej)} silence/pacing cuts; cut only pauses >= {new}s",
                "evidence": {"rejected_cuts": len(pace_rej), "accepted_cuts": len(pace_acc)},
                "overlay": {"editing_rules": {"min_silence_sec": new}},
            }
        )

    # Level changes repeatedly rejected -> adjust loudness target toward editor choice
    loud = [o for o in rejected if o.get("type") == "set_audio_level" and "editor_target_lufs" in (o.get("params") or {})]
    if len(loud) >= 3:
        vals = sorted(float(o["params"]["editor_target_lufs"]) for o in loud)
        med = vals[len(vals) // 2]
        out.append(
            {
                "category": "audio",
                "reason": f"Editors consistently re-targeted loudness to {med} LUFS",
                "evidence": {"observations": len(loud)},
                "overlay": {"loudness_target_lufs": med, "audio_rules": {"target_lufs": med, "loudness_target_lufs": med}},
            }
        )

    # Captions: repeated manual line-length corrections
    cap_edits = [o for o in rejected if o.get("type") in ("add_caption", "update_caption") and (o.get("params") or {}).get("editor_max_chars_per_line")]
    if len(cap_edits) >= 3:
        vals = sorted(int(o["params"]["editor_max_chars_per_line"]) for o in cap_edits)
        out.append(
            {
                "category": "captions",
                "reason": "Editors consistently re-wrapped captions",
                "evidence": {"observations": len(cap_edits)},
                "overlay": {"caption_style": {"max_chars_per_line": int(vals[len(vals) // 2])}},
            }
        )

    # Agents whose output is rejected wholesale -> propose disabling for this series
    by_agent: dict[str, list[int]] = {}
    for o in rejected:
        by_agent.setdefault(str(o.get("agent_id")), [0, 0])[0] += 1
    for o in accepted:
        by_agent.setdefault(str(o.get("agent_id")), [0, 0])[1] += 1
    for agent_id, (rej, acc) in sorted(by_agent.items()):
        if agent_id in ("graphics", "colourist", "vfx") and rej >= 5 and acc == 0:
            out.append(
                {
                    "category": "agent_activation",
                    "reason": f"All {rej} {agent_id} edits were rejected; disable {agent_id} for this series",
                    "evidence": {"rejected": rej, "accepted": acc},
                    "overlay": {"distribution_rules": {"disabled_agents_hint": [agent_id]}},
                }
            )
    return out


def suggest_from_project_diff(
    profile: dict[str, Any],
    ai_shots: list[dict[str, Any]],
    final_shots: list[dict[str, Any]],
    camera_roles: dict[str, str],
) -> list[dict[str, Any]]:
    """Compare AI draft vs editor-finished V1 shot lists (``camera_id``, ``duration_sec``)."""
    if len(ai_shots) < 4 or len(final_shots) < 4:
        return []

    def _avg(shots: list[dict[str, Any]]) -> float:
        return sum(float(s["duration_sec"]) for s in shots) / len(shots)

    def _role_share(shots: list[dict[str, Any]], role: str) -> float:
        total = sum(float(s["duration_sec"]) for s in shots) or 1.0
        return sum(float(s["duration_sec"]) for s in shots if camera_roles.get(s.get("camera_id", "")) == role) / total

    out: list[dict[str, Any]] = []
    ai_avg, fin_avg = _avg(ai_shots), _avg(final_shots)
    if fin_avg > ai_avg * 1.2:
        cur = _get_num(profile, "min_shot_duration_sec", 2.0)
        new = round(min(fin_avg * 0.6, cur * 1.5), 2)
        if new > cur:
            out.append(
                {
                    "category": "pacing",
                    "reason": f"Editor's final cut holds shots longer (avg {fin_avg:.1f}s vs AI {ai_avg:.1f}s)",
                    "evidence": {"ai_avg_shot_sec": round(ai_avg, 2), "final_avg_shot_sec": round(fin_avg, 2)},
                    "overlay": {"min_shot_duration_sec": new, "editing_rules": {"min_shot_duration_sec": new, "min_shot_sec": new}},
                }
            )
    elif fin_avg < ai_avg * 0.8:
        cur = _get_num(profile, "max_shot_duration_sec", 12.0)
        new = round(max(fin_avg * 1.8, cur * 0.75), 2)
        if new < cur:
            out.append(
                {
                    "category": "pacing",
                    "reason": f"Editor's final cut is faster (avg {fin_avg:.1f}s vs AI {ai_avg:.1f}s)",
                    "evidence": {"ai_avg_shot_sec": round(ai_avg, 2), "final_avg_shot_sec": round(fin_avg, 2)},
                    "overlay": {"max_shot_duration_sec": new, "editing_rules": {"max_shot_duration_sec": new, "max_shot_sec": new}},
                }
            )
    ai_r, fin_r = _role_share(ai_shots, "reaction"), _role_share(final_shots, "reaction")
    if abs(fin_r - ai_r) >= 0.08:
        cur = _get_num(profile, "reaction_shot_frequency", 0.45)
        new = round(min(1.0, max(0.0, cur * (fin_r / ai_r if ai_r > 0 else 1.0))), 3)
        if abs(new - cur) >= 0.02:
            out.append(
                {
                    "category": "camera_choice",
                    "reason": f"Reaction coverage changed from {ai_r:.0%} (AI) to {fin_r:.0%} (editor)",
                    "evidence": {"ai_reaction_share": round(ai_r, 3), "final_reaction_share": round(fin_r, 3)},
                    "overlay": {"reaction_shot_frequency": new, "editing_rules": {"reaction_shot_frequency": new}},
                }
            )
    return out


# ---------------------------------------------------------------------------
# V1.2.2: draft -> final comparison from a job, recurring preferences, bundles
# ---------------------------------------------------------------------------
BUNDLE_FORMAT = "bangonpro.preference_bundle"
BUNDLE_VERSION = 1
MAX_BUNDLE_BYTES = 256 * 1024
_BUNDLE_KEYS = frozenset({"bundle_format", "bundle_version", "app_version", "created_at_utc", "profile_id", "series_id", "editor", "source_version", "items", "manifest"})
_ITEM_KEYS = frozenset({"category", "reason", "overlay", "evidence"})
_WEIGHT_OR_CODE_RE = re.compile(r"(\.(onnx|pt|pth|bin|safetensors|ckpt|gguf|mlmodel|py|sh|so|dylib)\b|__\w+__|\bimport\s|\blambda\b|\beval\(|\bexec\()", re.IGNORECASE)


def shots_from_kdenlive(path: str | Path, path_to_camera: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """V1 (video) shots of a Kdenlive project as ``{camera_id, duration_sec}``."""
    from bangonpro.ai_engine.planner import ingest_kdenlive_project

    mapping = {str(Path(k).resolve()): v for k, v in (path_to_camera or {}).items()}
    ctx = ingest_kdenlive_project(path)
    out = []
    for c in ctx.clips:
        if not c.track_id.upper().startswith("V") or c.clip_kind == "kdenlivetitle":
            continue
        fp = str(Path(c.file_path).resolve()) if c.file_path else ""
        out.append({"camera_id": mapping.get(fp) or Path(c.file_path or c.clip_name).stem, "duration_sec": c.duration_sec})
    return out


def diff_job_against_final(job_dir: str | Path, final_project: str | Path, *, prefs_root: str | Path | None = None, record: bool = True) -> dict[str, Any]:
    """Compare a job's agent draft with the editor's finished ``.kdenlive`` and propose suggestions.

    The observation is recorded against the job, so recurring preferences can be detected
    across jobs; suggestions are proposed as *pending* and still need approval.
    """
    from bangonpro.ai_engine.department.jobs import ProductionJob

    job = ProductionJob(job_dir)
    if not job.doc:
        raise PreferenceError(f"No production job at {job_dir}")
    draft = (job.doc.get("artifacts") or {}).get("kdenlive_project")
    if not draft or not Path(draft).is_file():
        raise PreferenceError("The job has no draft timeline to compare")
    st = job.state or job._latest_state()
    path_to_cam = {v: k for k, v in st.input_camera_files.items()}
    path_to_cam.update({a.file_path: cam for cam, a in st.media_assets.items()})
    store = ProfilePreferenceStore(str(job.doc["profile_id"]), str(job.doc.get("series_id") or "default"), prefs_root or (job.doc.get("options") or {}).get("prefs_root"))
    profile, _ = store.apply_to_profile(st.profile)
    roles = dict(profile.get("camera_roles") or {})
    ai_shots = shots_from_kdenlive(draft, path_to_cam)
    final_shots = shots_from_kdenlive(final_project, path_to_cam)
    suggestions = suggest_from_project_diff(profile, ai_shots, final_shots, roles)
    observation = {
        "kind": "draft_final_diff",
        "job_id": job.doc["job_id"],
        "agent_id": "multicam_director",
        "ai_shot_count": len(ai_shots),
        "final_shot_count": len(final_shots),
        "parameters": {k: v for s in suggestions for k, v in _flatten(s["overlay"]).items() if not isinstance(v, (dict, list))},
        "evidence": [s.get("evidence") for s in suggestions],
    }
    if record:
        store.record_correction(observation)
    return {"job_id": job.doc["job_id"], "ai_shots": len(ai_shots), "final_shots": len(final_shots), "suggestions": store.propose(suggestions) if suggestions else [], "observation": observation}


def _parameter_observations(corrections: list[dict[str, Any]]) -> dict[str, list[tuple[str, float]]]:
    """Flat parameter -> [(job_id, value)] from rerun overrides and draft/final comparisons."""
    obs: dict[str, list[tuple[str, float]]] = {}
    for c in corrections:
        params: dict[str, Any] = {}
        if c.get("kind") == "parameter_override":
            params = _flatten(c.get("overrides") or {})
        elif c.get("kind") == "draft_final_diff":
            params = dict(c.get("parameters") or {})
        for k, v in params.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                obs.setdefault(k, []).append((str(c.get("job_id")), float(v)))
    return obs


def detect_recurring_preferences(profile: dict[str, Any], corrections: list[dict[str, Any]], *, min_jobs: int = 2) -> list[dict[str, Any]]:
    """Suggest a parameter change when editors made the same correction in >= ``min_jobs`` jobs.

    A parameter qualifies when it was overridden on a rerun, or changed by a draft/final
    comparison, in at least ``min_jobs`` distinct jobs, and every observation moves it in the
    same direction relative to the current profile. The suggestion carries evidence counts
    (jobs, observations, values) and still needs approval.
    """
    flat_profile = _flatten(profile)
    out: list[dict[str, Any]] = []
    for key, items in sorted(_parameter_observations(corrections).items()):
        jobs = sorted({j for j, _ in items})
        if len(jobs) < min_jobs or key.split(".")[0] not in LEARNABLE_TOP_LEVEL_KEYS:
            continue
        cur = flat_profile.get(key)
        values = sorted(v for _, v in items)
        if isinstance(cur, (int, float)) and not isinstance(cur, bool):
            ups, downs = sum(1 for v in values if v > cur), sum(1 for v in values if v < cur)
            if (ups and downs) or not (ups or downs):
                continue  # editors disagree, or the profile already has this value
        median = values[len(values) // 2]
        overlay: dict[str, Any] = {}
        node = overlay
        parts = key.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = round(median, 3)
        try:
            overlay = validate_overlay(overlay)
        except PreferenceError:
            continue
        out.append(
            {
                "category": "recurring_preference",
                "reason": f"{key} was changed in {len(jobs)} jobs ({cur} -> {round(median, 3)})",
                "evidence": {"jobs": len(jobs), "observations": len(items), "job_ids": jobs, "values": values, "current_value": cur},
                "overlay": overlay,
            }
        )
    rejected_jobs: dict[str, set[str]] = {}
    for c in corrections:
        if c.get("decision") == "rejected" and c.get("agent_id") in ("graphics", "colourist", "vfx"):
            rejected_jobs.setdefault(str(c["agent_id"]), set()).add(str(c.get("job_id")))
    for agent_id, jobs_set in sorted(rejected_jobs.items()):
        if len(jobs_set) >= max(min_jobs, 2):
            out.append(
                {
                    "category": "agent_activation",
                    "reason": f"{agent_id} was rejected in {len(jobs_set)} jobs; consider leaving it out for this series",
                    "evidence": {"jobs": len(jobs_set), "job_ids": sorted(jobs_set)},
                    "overlay": {"distribution_rules": {"disabled_agents_hint": [agent_id]}},
                }
            )
    return out


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _check_values(value: Any, path: str = "", depth: int = 0) -> None:
    if depth > 5:
        raise PreferenceError(f"Bundle value '{path}' is nested too deeply")
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise PreferenceError(f"Bundle value '{path}' is not a finite number")
    if isinstance(value, str):
        if len(value) > 200 or "\n" in value or _WEIGHT_OR_CODE_RE.search(value):
            raise PreferenceError(f"Bundle value '{path}' is not a production setting")
    elif isinstance(value, list):
        if len(value) > 50:
            raise PreferenceError(f"Bundle list '{path}' is too long")
        for i, v in enumerate(value):
            _check_values(v, f"{path}[{i}]", depth + 1)
    elif isinstance(value, dict):
        for k, v in value.items():
            _check_values(v, f"{path}.{k}" if path else str(k), depth + 1)
    elif not isinstance(value, (int, float, bool, type(None))):
        raise PreferenceError(f"Bundle value '{path}' has an unsupported type")


def export_preference_bundle(store: ProfilePreferenceStore, out_path: str | Path, *, editor: str = "", app_version: str = "") -> dict[str, Any]:
    """Write the series' approved preferences as a versioned, checksummed JSON bundle."""
    overlay = validate_overlay(store.active_overlay())
    if not overlay:
        raise PreferenceError("There are no approved preferences to export for this profile and series")
    approved = [s for s in store.suggestions() if s.get("status") == "approved"]
    items = []
    for top, value in sorted(overlay.items()):
        reasons = [s.get("reason", "") for s in approved if top in (s.get("overlay") or {})]
        items.append({"category": "approved_preference", "reason": "; ".join(r for r in reasons if r) or f"Approved {top}", "overlay": {top: value}, "evidence": {"source_version": store.active_version(), "approved_suggestions": len(reasons)}})
    payload: dict[str, Any] = {
        "bundle_format": BUNDLE_FORMAT,
        "bundle_version": BUNDLE_VERSION,
        "app_version": app_version,
        "created_at_utc": _utc(),
        "profile_id": store.profile_id,
        "series_id": store.series_id,
        "editor": editor[:80],
        "source_version": store.active_version(),
        "items": items,
    }
    payload["manifest"] = {"algorithm": "sha256", "payload_sha256": hashlib.sha256(_canonical(payload)).hexdigest(), "item_sha256": [hashlib.sha256(_canonical(i)).hexdigest() for i in items]}
    write_json_atomic(out_path, payload)
    return payload


def load_preference_bundle(path: str | Path) -> dict[str, Any]:
    """Parse and fully validate a bundle; raises ``PreferenceError`` on any problem."""
    p = Path(path).expanduser()
    if p.stat().st_size > MAX_BUNDLE_BYTES:
        raise PreferenceError("Preference bundle is too large")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PreferenceError(f"Preference bundle is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("bundle_format") != BUNDLE_FORMAT:
        raise PreferenceError("Not a Bang On Pro preference bundle")
    if doc.get("bundle_version") != BUNDLE_VERSION:
        raise PreferenceError(f"Unsupported preference bundle version {doc.get('bundle_version')}")
    extra = set(doc) - _BUNDLE_KEYS
    if extra:
        raise PreferenceError(f"Unexpected bundle fields: {', '.join(sorted(extra))}")
    manifest = doc.get("manifest") or {}
    body = {k: v for k, v in doc.items() if k != "manifest"}
    if manifest.get("algorithm") != "sha256" or manifest.get("payload_sha256") != hashlib.sha256(_canonical(body)).hexdigest():
        raise PreferenceError("Preference bundle checksum does not match (the file was changed or damaged)")
    items = doc.get("items")
    if not isinstance(items, list) or not items or len(items) > 50:
        raise PreferenceError("Preference bundle has no valid items")
    cleaned = []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or set(item) - _ITEM_KEYS or not isinstance(item.get("overlay"), dict):
            raise PreferenceError(f"Bundle item {i} is malformed")
        _check_values(item["overlay"], "overlay")
        cleaned.append({**item, "overlay": validate_overlay(item["overlay"])})
    doc["items"] = cleaned
    return doc


def import_preference_bundle(store: ProfilePreferenceStore, path: str | Path, *, allow_other_profile: bool = False) -> list[dict[str, Any]]:
    """Import a bundle as *pending* suggestions: nothing is applied until an editor approves it."""
    doc = load_preference_bundle(path)
    if doc.get("profile_id") != store.profile_id and not allow_other_profile:
        raise PreferenceError(f"Bundle is for profile '{doc.get('profile_id')}', not '{store.profile_id}'")
    checksum = doc["manifest"]["payload_sha256"]
    sugg = [
        {
            "category": "imported",
            "reason": f"Imported from {doc.get('editor') or 'another editor'}: {item.get('reason', '')}".strip(),
            "overlay": item["overlay"],
            "evidence": {"bundle_sha256": checksum, "source_profile": doc.get("profile_id"), "source_series": doc.get("series_id"), "source_version": doc.get("source_version"), **(item.get("evidence") or {})},
        }
        for item in doc["items"]
    ]
    before = {s.get("suggestion_id") for s in store.suggestions()}
    return [s for s in store.propose(sugg) if s.get("suggestion_id") not in before]
