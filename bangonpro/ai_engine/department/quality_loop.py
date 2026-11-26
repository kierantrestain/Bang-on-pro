"""Bounded quality feedback loop and department extension hooks (V1.2.2).

Plan -> execute -> **assess -> correct -> verify** -> human approval -> approved
preference learning.

After an agent runs, a registered *assessor* scores its output with an objective,
agent-specific metric and may propose a bounded correction (a validated profile
overlay, e.g. a longer minimum shot or a gentler silence rule). The job reruns the
agent **once per attempt** on a fresh copy of the pre-run state in an isolated
attempt directory and keeps the correction **only** if the score improves by at
least ``MIN_IMPROVEMENT`` without introducing new error-severity defects. Otherwise
the original output is kept and the defects go to human review. Attempts are
capped per agent and per job, so there are no endless retries, and expensive
analysis is served from the shared cache, so a correction never repeats inference.

Specialist modules register their hooks here instead of editing the job engine:

* ``register_assessor(agent_id)``      - score an agent's outcome and propose a correction
* ``register_qc_check(name)``          - extra Quality Control checks (``st -> list[QCIssue]``)
* ``register_final_verifier(name)``    - Executive final verification (``(st, job_doc) -> dict``)

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

MAX_ATTEMPTS_PER_AGENT = 2
MAX_ATTEMPTS_PER_JOB = 6
MIN_IMPROVEMENT = 0.02
LOW_CONFIDENCE_REVIEW = 0.6
_log = logging.getLogger(__name__)
CRITICAL_SEVERITIES = frozenset({"error", "critical"})
DEFECT_MATCH_TOLERANCE_SEC = 0.5

# Modules that register hooks on import. Each is owned by one specialist domain.
EXTENSION_MODULES: tuple[str, ...] = (
    "bangonpro.ai_engine.department.editorial_intel",
    "bangonpro.ai_engine.department.media_intel",
)


@dataclass
class Defect:
    code: str
    severity: str  # "info" | "warning" | "error"
    message: str
    at_sec: float | None = None
    subjective: bool = False


@dataclass
class Assessment:
    """Objective score for one agent's output (0..1, higher is better)."""

    agent_id: str
    score: float
    metric: str
    defects: list[Defect] = field(default_factory=list)
    correction: dict[str, Any] | None = None  # learnable profile overlay to try
    correction_reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def error_count(self) -> int:
        return sum(1 for d in self.defects if d.severity == "error")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


Assessor = Callable[[Any, Any], Assessment | None]  # (AgentContext, AgentOutcome) -> Assessment
QCCheck = Callable[[Any], list[Any]]  # ProjectAnalysisState -> list[QCIssue]
FinalVerifier = Callable[[Any, dict[str, Any]], dict[str, Any]]

ASSESSORS: dict[str, Assessor] = {}
QC_CHECKS: dict[str, QCCheck] = {}
FINAL_VERIFIERS: dict[str, FinalVerifier] = {}
_LOADED = False


def register_assessor(agent_id: str) -> Callable[[Assessor], Assessor]:
    def deco(fn: Assessor) -> Assessor:
        ASSESSORS[agent_id] = fn
        return fn

    return deco


def register_qc_check(name: str) -> Callable[[QCCheck], QCCheck]:
    def deco(fn: QCCheck) -> QCCheck:
        QC_CHECKS[name] = fn
        return fn

    return deco


def register_final_verifier(name: str) -> Callable[[FinalVerifier], FinalVerifier]:
    def deco(fn: FinalVerifier) -> FinalVerifier:
        FINAL_VERIFIERS[name] = fn
        return fn

    return deco


def ensure_extensions_loaded() -> None:
    """Import every specialist extension module once (they register hooks on import)."""
    global _LOADED
    if _LOADED:
        return
    for mod in EXTENSION_MODULES:
        importlib.import_module(mod)
    _LOADED = True


def assess(agent_id: str, ctx: Any, outcome: Any) -> Assessment | None:
    ensure_extensions_loaded()
    fn = ASSESSORS.get(agent_id)
    if fn is None:
        return None
    return fn(ctx, outcome)


def _same_defect(a: Defect, b: Defect, tol: float = DEFECT_MATCH_TOLERANCE_SEC) -> bool:
    if a.code != b.code or a.severity != b.severity:
        return False
    if a.at_sec is None or b.at_sec is None:
        return a.at_sec is None and b.at_sec is None
    return abs(float(a.at_sec) - float(b.at_sec)) <= tol


def new_critical_defects(before: Assessment, after: Assessment) -> list[Defect]:
    """Critical defects present after a correction that did not exist before it.

    Matched by code, severity and master-clock location (+/- 0.5 s), so swapping one
    critical defect for another is detected even when the error count is unchanged.
    """
    unmatched = list(before.defects)
    new: list[Defect] = []
    for d in after.defects:
        hit = next((b for b in unmatched if _same_defect(b, d)), None)
        if hit is not None:
            unmatched.remove(hit)
        elif d.severity in CRITICAL_SEVERITIES:
            new.append(d)
    return new


def guard_issues(st: Any) -> list[Any]:
    """Independent cheap QC guard: built-in department checks plus registered QC checks.

    Runs on any intermediate state (checks must tolerate agents that have not run yet).
    """
    from bangonpro.ai_engine.department.agents import department_qc_checks

    issues: list[Any] = []
    try:
        issues.extend(department_qc_checks(st))
    except Exception as exc:  # noqa: BLE001 - a guard must never crash the job, but it must not fail silently
        _log.warning("QC guard: built-in department checks failed: %s: %s", type(exc).__name__, exc)
    hooked, errors = run_qc_checks(st)
    for name, err in errors.items():
        _log.warning("QC guard: check %s failed: %s", name, err)
    return issues + hooked


def new_critical_guard_issues(before: list[Any], after: list[Any]) -> list[str]:
    """Per-code increase of critical QC findings (timeline positions move with a re-cut)."""
    def _counts(items: list[Any]) -> dict[str, int]:
        out: dict[str, int] = {}
        for i in items:
            if getattr(i, "severity", "") in CRITICAL_SEVERITIES:
                out[i.code] = out.get(i.code, 0) + 1
        return out

    b, a = _counts(before), _counts(after)
    return sorted(f"{code} {b.get(code, 0)}->{n}" for code, n in a.items() if n > b.get(code, 0))


def accept_correction(
    before: Assessment,
    after: Assessment,
    min_improvement: float = MIN_IMPROVEMENT,
    *,
    guard_before: list[Any] | None = None,
    guard_after: list[Any] | None = None,
) -> tuple[bool, str]:
    """Keep a correction only when it is measurably better and introduces no new critical defect.

    Three independent conditions, all required:
    1. no new critical defect in the agent's own assessment (identity, not count);
    2. no increase of any critical finding in the independent QC guard;
    3. the agent's objective score improves by at least ``min_improvement``.
    """
    new = new_critical_defects(before, after)
    if new:
        return False, "rejected: new critical defect(s) " + ", ".join(f"{d.code}@{d.at_sec}" for d in new)
    if guard_before is not None and guard_after is not None:
        worse = new_critical_guard_issues(guard_before, guard_after)
        if worse:
            return False, "rejected: independent QC guard found new critical issue(s) " + ", ".join(worse)
    gain = round(after.score - before.score, 4)
    if gain < min_improvement:
        return False, f"rejected: {before.metric} {before.score:.3f} -> {after.score:.3f} (gain {gain:+.3f} < {min_improvement})"
    return True, f"accepted: {before.metric} {before.score:.3f} -> {after.score:.3f} (gain {gain:+.3f})"


def run_qc_checks(st: Any) -> tuple[list[Any], dict[str, str]]:
    """Run every registered QC check; a failing check is reported, never fatal."""
    ensure_extensions_loaded()
    issues: list[Any] = []
    errors: dict[str, str] = {}
    for name, fn in QC_CHECKS.items():
        try:
            issues.extend(fn(st) or [])
        except Exception as exc:  # noqa: BLE001 - one broken check must not hide the others
            errors[name] = f"{type(exc).__name__}: {exc}"
    return issues, errors


def run_final_verifiers(st: Any, job_doc: dict[str, Any]) -> dict[str, Any]:
    ensure_extensions_loaded()
    out: dict[str, Any] = {}
    for name, fn in FINAL_VERIFIERS.items():
        try:
            out[name] = fn(st, job_doc)
        except Exception as exc:  # noqa: BLE001
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


__all__ = [
    "ASSESSORS",
    "CRITICAL_SEVERITIES",
    "EXTENSION_MODULES",
    "FINAL_VERIFIERS",
    "LOW_CONFIDENCE_REVIEW",
    "MAX_ATTEMPTS_PER_AGENT",
    "MAX_ATTEMPTS_PER_JOB",
    "MIN_IMPROVEMENT",
    "QC_CHECKS",
    "Assessment",
    "Defect",
    "accept_correction",
    "assess",
    "ensure_extensions_loaded",
    "guard_issues",
    "new_critical_defects",
    "new_critical_guard_issues",
    "register_assessor",
    "register_final_verifier",
    "register_qc_check",
    "run_final_verifiers",
    "run_qc_checks",
]
