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
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

MAX_ATTEMPTS_PER_AGENT = 2
MAX_ATTEMPTS_PER_JOB = 6
MIN_IMPROVEMENT = 0.02
LOW_CONFIDENCE_REVIEW = 0.6

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


def accept_correction(before: Assessment, after: Assessment, min_improvement: float = MIN_IMPROVEMENT) -> tuple[bool, str]:
    """A correction is kept only when it is measurably better and introduces no new errors."""
    gain = round(after.score - before.score, 4)
    if after.error_count > before.error_count:
        return False, f"rejected: errors {before.error_count} -> {after.error_count}"
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
    "register_assessor",
    "register_final_verifier",
    "register_qc_check",
    "run_final_verifiers",
    "run_qc_checks",
]
