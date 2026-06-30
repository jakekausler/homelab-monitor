"""Result + denial types for the auto-fix orchestrator (STAGE-009-005)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DenialReason(StrEnum):
    """Why a gate denied an auto-fix attempt (audit ``gate`` value)."""

    KILL_SWITCH = "kill_switch"
    ALLOW_LIST = "allow_list"
    RATE_LIMIT = "rate_limit"
    COOLDOWN = "cooldown"
    AMBIGUOUS_MATCH = "ambiguous_match"
    ALREADY_RUNNING = "already_running"
    RISKY_BLOCKED = "risky_blocked"
    CLAIM_ERROR = "claim_error"


class RunOutcome(StrEnum):
    """Terminal classification of a completed/denied handle_alert call."""

    RAN = "ran"  # exec actually fired (exit code captured; may be non-zero)
    DENIED = "denied"  # a gate denied before exec


class RunMode(StrEnum):
    """Execution mode of an auto-fix run (runbook_runs.mode column)."""

    REAL = "real"  # real claude --dangerously-skip-permissions exec
    DRY_RUN = "dry_run"  # STAGE-009-006 will use this for dry-run/ack flow


@dataclass(frozen=True, slots=True)
class RunResult:
    """Outcome of ``AutoFixOrchestrator.handle_alert``.

    ``handle_alert`` returns ``None`` ONLY for a no-match (nothing recorded).
    Every other path returns a populated ``RunResult``.
    """

    ran: bool
    outcome: RunOutcome
    runbook_id: str | None
    run_id: str | None
    exit_code: int | None
    denial_reason: DenialReason | None
