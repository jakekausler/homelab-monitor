"""Result + denial types for the auto-fix orchestrator (STAGE-009-005)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from homelab_monitor.kernel.autofix.feedback_parser import ParsedFeedbackItem
    from homelab_monitor.kernel.docker.socket_client import ExecResult


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


InitiatedBy = Literal["alert", "operator"]


class RunbookNotFoundError(Exception):
    """Raised by handle_operator_trigger when the runbook_id does not exist."""

    def __init__(self, runbook_id: str) -> None:
        super().__init__(f"runbook {runbook_id} not found")
        self.runbook_id = runbook_id


class DryRunRequiredForRiskyError(Exception):
    """Raised when an operator requests mode='real' on a risky runbook (dry_run_required=True).

    Server 400s this per Design Decision B — risky runbooks must go through the
    dry-run + approval flow, even for manual triggers.
    """

    def __init__(self, runbook_id: str) -> None:
        super().__init__(f"runbook {runbook_id} is risky; use dry_run mode")
        self.runbook_id = runbook_id


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


@dataclass(frozen=True, slots=True)
class ExecOutcome:
    """Bundled result of :meth:`AutoFixOrchestrator._exec_claude`.

    Promoted from a 5-tuple to a dataclass by STAGE-009-014 so the intent
    gateway can access ``grants`` (previously computed inside the exec's
    transcript-lock critical section but not returned) without re-reading the
    runbook config from disk.

    ``grants`` is ``None`` only when ``errored is True`` due to a grant-
    resolution failure (the caller must skip the intent gateway in that
    case — an envelope-less exec cannot have valid intents).
    """

    exec_result: ExecResult
    transcript_path: str | None
    error_msg: str | None
    errored: bool
    feedback_items: list[ParsedFeedbackItem] | None
    grants: ResolvedGrants | None


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


class EgressConfigurationError(Exception):
    """Raised when the fixer-runner's per-exec egress proxy config cannot be
    installed (allow-list write failure, Squid reconfigure timeout, Squid
    reconfigure non-zero exit, or invalid hostname in grants).

    ``reason`` is a short machine-stable token used as the audit ``after``
    payload's ``reason`` field. Mirrors :class:`GrantResolutionError`'s shape.

    Fail-closed policy (STAGE-009-015 Decision F): NO retry — a single failure
    causes the orchestrator to skip the real exec and emit
    ``autofix.egress_configuration_error``.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class FeedbackKind(StrEnum):
    """Category of a Claude→user improvement feedback row (STAGE-009-009).

    Locked value set (Design Decision 2B). Unknown wire values are down-
    graded to ``OTHER`` at parse time for forward-compat; ``PARSE_ERROR`` is
    reserved for the parser's synthetic-row fallback and is never emitted
    by claude itself.
    """

    MISSING_CAPABILITY = "missing_capability"
    CONFIG_CHANGE = "config_change"
    RUNBOOK_GAP = "runbook_gap"
    BLOCKED = "blocked"
    WORKED_AROUND = "worked_around"
    OTHER = "other"
    PARSE_ERROR = "parse_error"


# Bound at persist time; a single feedback item cannot exceed this many
# characters of suggestion_text. Longer input is truncated with
# ``TRUNCATION_SUFFIX`` appended (STAGE-009-009 Decision 2B / 3C).
SUGGESTION_TEXT_MAX: Final[int] = 4096
TRUNCATION_SUFFIX: Final[str] = "\n...[truncated]"


@dataclass(frozen=True, slots=True)
class RunbookRunFeedback:
    """A hydrated ``runbook_run_feedback`` row."""

    id: str
    runbook_run_id: str
    kind: FeedbackKind
    suggestion_text: str
    structured_hint: Mapping[str, object] | None
    created_at: str


class FeedbackParseError(Exception):
    """Raised inside ``feedback_parser`` when the sentinel file's content
    is invalid JSON, wrong top-level shape, or violates required-key rules.

    Caller (orchestrator) catches this and persists a synthetic
    ``FeedbackKind.PARSE_ERROR`` row + emits an
    ``autofix.feedback_parse_error`` audit event.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail
