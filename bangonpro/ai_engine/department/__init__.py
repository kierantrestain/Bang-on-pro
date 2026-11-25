"""Bang On Pro AI Production Department (V1.2): coordinated specialist production agents.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from bangonpro.ai_engine.department.agents import (
    JobOptions,
    compute_activation,
    parse_brief_flags,
)
from bangonpro.ai_engine.department.cache import AnalysisCache
from bangonpro.ai_engine.department.jobs import ApprovalRequiredError, ProductionJob
from bangonpro.ai_engine.department.learning import ProfilePreferenceStore
from bangonpro.ai_engine.department.registry import (
    AGENT_DEFINITIONS,
    AGENT_ORDER,
    AUTOMATION_MODES,
    capability_matrix,
)

__all__ = [
    "AGENT_DEFINITIONS",
    "AGENT_ORDER",
    "AUTOMATION_MODES",
    "AnalysisCache",
    "ApprovalRequiredError",
    "JobOptions",
    "ProductionJob",
    "ProfilePreferenceStore",
    "capability_matrix",
    "compute_activation",
    "parse_brief_flags",
]
