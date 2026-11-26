"""Plain-language explanations for common production failures.

Editors in the internal beta have no terminal knowledge, so every failure the
department reports (agent errors, blocked jobs, failed exports, CLI errors) is
mapped to a short title, a plain sentence and one concrete fix. ``fix_action``
names the native control that performs the fix (the Agent Control Centre and the
System check dialog turn it into a button); it is ``None`` when the editor only
needs to read the advice.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from __future__ import annotations

import errno
import re
from typing import Any

TOOLS = ("ffmpeg", "ffprobe", "melt")

FIX_ACTIONS = (
    "run_system_check",
    "choose_job_folder",
    "relink_media",
    "open_model_manager",
    "resume_job",
    "rerun_agent",
    "export_diagnostics",
    "choose_other_file",
)


def _entry(code: str, title: str, message: str, fix: str, fix_action: str | None) -> dict[str, Any]:
    return {"code": code, "title": title, "message": message, "fix": fix, "fix_action": fix_action}


def _text(err: BaseException | str) -> str:
    if isinstance(err, BaseException):
        return f"{type(err).__name__}: {err}"
    return str(err)


def explain(err: BaseException | str, *, context: str = "") -> dict[str, Any]:
    """Map an exception (or its recorded text) to ``{code, title, message, fix, fix_action}``."""
    text = _text(err)
    low = text.lower()
    errnum = getattr(err, "errno", None) if isinstance(err, BaseException) else None
    filename = str(getattr(err, "filename", "") or "") if isinstance(err, BaseException) else ""
    tool = next((t for t in TOOLS if re.search(rf"(^|[/\\'\" ]){t}(['\" :]|$)", filename or low)), None)

    if type(err).__name__ == "PipelineCancelledError" or "pipelinecancellederror" in low:
        return _entry("cancelled", "Production paused", "The job was cancelled. Finished agents are kept.", "Press Resume to continue from the last checkpoint.", "resume_job")
    if type(err).__name__ == "InsufficientStorageError" or "insufficient storage" in low or errnum == errno.ENOSPC or "no space left" in low:
        return _entry(
            "no_space",
            "Not enough free space",
            "The job folder's drive is too full to continue safely.",
            "Free some space on that drive, or choose another job folder (an external SSD works) in System check, then press Resume.",
            "choose_job_folder",
        )
    if "refusing to overwrite original source media" in low:
        return _entry(
            "source_protected",
            "Camera originals are protected",
            "Bang On Pro never writes over source media.",
            "Choose a different file name for the project.",
            "choose_other_file",
        )
    if type(err).__name__ == "ApprovalRequiredError" or "requires explicit approval" in low:
        return _entry(
            "approval_required",
            "Approval needed",
            "Replacing an existing edit needs your confirmation.",
            "Confirm the replacement. A timestamped backup of the old file is kept.",
            None,
        )
    if (isinstance(err, FileNotFoundError) and tool) or (tool and ("not found" in low or "no such file" in low or "not installed" in low)):
        return _entry(
            "missing_tool",
            f"{tool} is missing",
            f"Bang On Pro needs {tool} to read and render media, and it could not be found.",
            "Reinstall Bang On Pro from the DMG (it includes ffmpeg, ffprobe and melt), then run System check again.",
            "run_system_check",
        )
    if "all source media are missing" in low or "missing media" in low or (isinstance(err, FileNotFoundError) and filename and not tool and not filename.endswith(".json")):
        return _entry(
            "missing_media",
            "Footage not found",
            "Some source files are no longer where the job expects them (a drive may be disconnected or the files were moved).",
            "Reconnect the drive, or use Relink media to find the moved files. Files are matched by their content, not their name.",
            "relink_media",
        )
    if type(err).__name__ in ("ModelNotFoundError",) or re.search(r"model .*not (installed|found|downloaded)|weights[_ ]missing|no runnable .*model", low):
        return _entry(
            "model_not_installed",
            "Local model not installed",
            "This step can use a local AI model that is not installed yet.",
            "Open Model Manager and install it, or carry on: the agent uses its deterministic fallback and tells you what it withheld.",
            "open_model_manager",
        )
    if isinstance(err, PermissionError) or errnum in (errno.EACCES, errno.EPERM) or "permission denied" in low or "operation not permitted" in low:
        return _entry(
            "permission_denied",
            "Permission denied",
            "Bang On Pro is not allowed to write to that folder.",
            "Choose a folder you own (for example in Movies or on an external SSD), or allow Bang On Pro in System Settings > Privacy & Security > Files and Folders.",
            "choose_job_folder",
        )
    if "consent" in low or ("budget" in low and "exceed" in low):
        return _entry(
            "external_ai_unavailable",
            "External AI not used",
            "External AI is switched off or over this job's budget, so the job stayed local.",
            "Nothing to fix. To use an external model, grant consent in AI providers and privacy and raise the job budget.",
            None,
        )
    if context in ("distribution", "export") or re.search(r"\b(render|export|encode|encoder|deliverable|loudnorm|melt|avformat)\b", low):
        return _entry(
            "export_failed",
            "Export failed",
            "A render or deliverable could not be produced. The editable draft is not affected.",
            "Check that the job folder has free space, then rerun the Distribution agent. If it fails again, export diagnostics and send them to the team.",
            "rerun_agent",
        )
    return _entry(
        "unexpected",
        "Something went wrong",
        "An agent stopped with an unexpected error. Work from other agents is kept.",
        "Rerun this agent. If it fails again, export diagnostics and send the zip to the team.",
        "export_diagnostics",
    )


__all__ = ["FIX_ACTIONS", "explain"]
