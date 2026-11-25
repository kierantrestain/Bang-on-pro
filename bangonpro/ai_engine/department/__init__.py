"""Bang On Pro AI Production Department (V1.2): coordinated specialist production agents.

V1.2.2 adds the production queue (``queue``), the system check (``readiness``),
sanitised diagnostics (``diagnostics``) and plain-language error help (``errors``).

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from bangonpro.ai_engine.department.agents import (
    JobOptions,
    compute_activation,
    parse_brief_flags,
)
from bangonpro.ai_engine.department.cache import AnalysisCache
from bangonpro.ai_engine.department.errors import explain
from bangonpro.ai_engine.department.jobs import ApprovalRequiredError, ProductionJob
from bangonpro.ai_engine.department.learning import ProfilePreferenceStore
from bangonpro.ai_engine.department.queue import JobQueue, job_history
from bangonpro.ai_engine.department.registry import (
    AGENT_DEFINITIONS,
    AGENT_ORDER,
    AUTOMATION_MODES,
    capability_matrix,
)
from bangonpro.ai_engine.department.version import APP_VERSION

__all__ = [
    "AGENT_DEFINITIONS",
    "AGENT_ORDER",
    "APP_VERSION",
    "AUTOMATION_MODES",
    "AnalysisCache",
    "ApprovalRequiredError",
    "JobOptions",
    "JobQueue",
    "ProductionJob",
    "ProfilePreferenceStore",
    "capability_matrix",
    "compute_activation",
    "explain",
    "job_history",
    "parse_brief_flags",
]
