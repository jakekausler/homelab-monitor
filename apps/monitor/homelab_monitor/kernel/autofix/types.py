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
    CLAIM_ERROR = "claim_error"
    APPROVAL_NOT_PENDING = "approval_not_pending"
    RUNBOOK_CHANGED = "runbook_changed"
    RUNBOOK_MISSING = "runbook_missing"


class RunOutcome(StrEnum):
    """Terminal classification of a completed/denied handle_alert call."""

    RAN = "ran"  # exec actually fired (exit code captured; may be non-zero)
    DENIED = "denied"  # a gate denied before exec
    DRY_RUN_STORED = "dry_run_stored"  # risky runbook: plan captured, approval pending, HALT


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
    approval_id: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedGrants:
    """Runtime-resolved scoped capabilities for a single exec (STAGE-009-008).

    Re-read fresh from the runbook's YAML config at exec-start (file-
    authoritative per Decision 1B) rather than cached from the DB
    RunbookRecord. Docker capability is POLICY-ONLY this stage (Decision
    2E-lite) — no actual docker access is granted to the fixer. Egress is
    declared-but-unenforced (Decision 3E).
    """

    docker_container: str | None
    docker_allowed_actions: tuple[str, ...]
    ssh_target_id: str | None
    egress: tuple[str, ...]


class GrantResolutionError(Exception):
    """Raised by ``_resolve_grants`` when scoped capabilities cannot be
    resolved or validated for a runbook at exec-start.

    ``reason`` is a short machine-stable token used as the audit ``after``
    payload's ``reason`` field (mirrors ``DenialReason`` string-token style,
    but kept as a plain str since this isn't a gate-denial enum member).
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
