"""Auto-fix orchestrator (STAGE-009-005).

Alert -> match (<=1 runbook) -> gate sequence -> durable claim ->
docker exec claude as homelab-fixer -> capture -> persist.

Built and tested against the FAKE claude script only; the real Claude API is
never called in CI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homelab_monitor.kernel.autofix.feedback_repository import (
    RunbookRunFeedbackRepository,
)
from homelab_monitor.kernel.autofix.types import (
    DenialReason,
    FeedbackKind,
    RunbookRunFeedback,
    RunOutcome,
    RunResult,
)

if TYPE_CHECKING:
    from homelab_monitor.kernel.autofix.orchestrator import AutoFixOrchestrator


def __getattr__(name: str) -> object:
    if name == "AutoFixOrchestrator":
        from homelab_monitor.kernel.autofix.orchestrator import (  # noqa: PLC0415
            AutoFixOrchestrator as _AutoFixOrchestrator,
        )

        return _AutoFixOrchestrator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AutoFixOrchestrator",
    "DenialReason",
    "FeedbackKind",
    "RunOutcome",
    "RunResult",
    "RunbookRunFeedback",
    "RunbookRunFeedbackRepository",
]
