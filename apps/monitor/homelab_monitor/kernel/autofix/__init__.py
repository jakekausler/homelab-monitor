"""Auto-fix orchestrator (STAGE-009-005).

Alert -> match (<=1 runbook) -> gate sequence -> durable claim ->
docker exec claude as homelab-fixer -> capture -> persist.

Built and tested against the FAKE claude script only; the real Claude API is
never called in CI.
"""

from __future__ import annotations

from homelab_monitor.kernel.autofix.orchestrator import AutoFixOrchestrator
from homelab_monitor.kernel.autofix.types import (
    DenialReason,
    RunOutcome,
    RunResult,
)

__all__ = [
    "AutoFixOrchestrator",
    "DenialReason",
    "RunOutcome",
    "RunResult",
]
