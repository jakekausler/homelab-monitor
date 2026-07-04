"""Runs-history read API for the auto-fix subsystem (STAGE-009-011).

Sibling router to ``autofix.py`` (same ``/autofix`` prefix, distinct
``autofix-runs`` tag). Read-only endpoints for the UI's Runs History page:
list runs w/ filters + offset pagination, run detail, feedback list, and a
bounded transcript reader (tail-truncated at 512 KiB with path-traversal
defense-in-depth).

Every endpoint requires a valid session (Depends(require_session())).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from homelab_monitor.kernel import config as fixer_config_module
from homelab_monitor.kernel.api.dependencies import (
    get_repo,
    require_session,
)
from homelab_monitor.kernel.api.errors import HttpProblem, NotFoundProblem
from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.autofix.feedback_repository import (
    RunbookRunFeedbackRepository,
)
from homelab_monitor.kernel.autofix.runs_repository import (
    RunbookRunsRepository,
    RunRow,
    RunsFilter,
)
from homelab_monitor.kernel.autofix.transcript_reader import read_transcript
from homelab_monitor.kernel.db.repository import SqliteRepository

router = APIRouter(prefix="/autofix", tags=["autofix-runs"])


# ---- literal types ----

RunModeLiteral = Literal["dry_run", "real"]
OutcomeLiteral = Literal["in_flight", "success", "failure", "killed", "dry_run"]
OutcomeFilterLiteral = Literal["in_flight", "success", "failure", "killed"]
InitiatorLiteral = Literal["alert", "operator"]
FeedbackKindLiteral = Literal[
    "missing_capability",
    "config_change",
    "runbook_gap",
    "blocked",
    "worked_around",
    "other",
    "parse_error",
]


# ---- wire models (colocated per runbooks.py convention) ----


class RunOut(BaseModel):
    id: str
    runbook_id: str
    runbook_path: str
    mode: RunModeLiteral
    outcome: OutcomeLiteral
    exit_code: int | None
    started_at: str | None
    ended_at: str | None
    duration_ms: int | None
    alert_id: str | None
    initiated_by: InitiatorLiteral
    killed_at: str | None


class RunDetailOut(RunOut):
    prompt: str | None
    transcript_path: str | None
    runbook_hash: str | None
    fixer_user: str | None
    host: str | None
    created_at: str


class RunListResponse(BaseModel):
    items: list[RunOut]
    total_count: int
    limit: int
    offset: int


class RunFeedbackOut(BaseModel):
    id: str
    runbook_run_id: str
    kind: FeedbackKindLiteral
    suggestion_text: str
    structured_hint: dict[str, Any] | None
    created_at: str


class RunFeedbackListResponse(BaseModel):
    items: list[RunFeedbackOut]


class TranscriptOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    truncated: bool
    size_bytes: int


# ---- helpers ----


_MAX_LIMIT = 500


def _derive_outcome(
    *, mode: str, ended_at: str | None, exit_code: int | None, killed_at: str | None
) -> OutcomeLiteral:
    """Derive an outcome literal from a run's fields.

    Precedence (top wins): killed_at > in_flight > dry_run > success > failure.
    Intentional: a killed run is 'killed' regardless of mode, matching the
    `outcome=killed` filter semantics in RunbookRunsRepository.list_paged.
    Locked by tests in tests/kernel/autofix/test_runs_repository_stats.py:
    test_status_matches_router_derivation and test_dry_run_killed_at_precedence.
    """
    if killed_at is not None:
        return "killed"
    if ended_at is None:
        return "in_flight"
    if mode == "dry_run":
        return "dry_run"
    if exit_code == 0:
        return "success"
    return "failure"


def _duration_ms(started_at: str | None, ended_at: str | None) -> int | None:
    if started_at is None or ended_at is None:
        return None
    s = datetime.fromisoformat(started_at)
    e = datetime.fromisoformat(ended_at)
    delta = e - s
    ms = int(delta.total_seconds() * 1000)
    return max(ms, 0)


def _row_to_run_out(row: RunRow) -> RunOut:
    outcome = _derive_outcome(
        mode=row.mode,
        ended_at=row.ended_at,
        exit_code=row.exit_code,
        killed_at=row.killed_at,
    )
    return RunOut(
        id=row.id,
        runbook_id=row.runbook_id,
        runbook_path=row.runbook_path,
        mode=row.mode,  # type: ignore[arg-type]  # validated by RunMode StrEnum on write
        outcome=outcome,
        exit_code=row.exit_code,
        started_at=row.started_at,
        ended_at=row.ended_at,
        duration_ms=_duration_ms(row.started_at, row.ended_at),
        alert_id=row.alert_id,
        initiated_by=row.initiated_by,  # type: ignore[arg-type]
        killed_at=row.killed_at,
    )


def _row_to_run_detail_out(row: RunRow) -> RunDetailOut:
    base = _row_to_run_out(row).model_dump()
    return RunDetailOut(
        **base,
        prompt=row.prompt,
        transcript_path=row.transcript_path,
        runbook_hash=row.runbook_hash,
        fixer_user=row.fixer_user,
        host=row.host,
        created_at=row.created_at,
    )


# ---- DI ----


def get_runs_repo(
    db: Annotated[SqliteRepository, Depends(get_repo)],
) -> RunbookRunsRepository:
    return RunbookRunsRepository(db)


def get_feedback_repo(
    db: Annotated[SqliteRepository, Depends(get_repo)],
) -> RunbookRunFeedbackRepository:
    return RunbookRunFeedbackRepository(db)


# ---- routes ----


@router.get("/runs", response_model=RunListResponse)
async def list_runs(  # noqa: PLR0913 -- FastAPI query params
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[RunbookRunsRepository, Depends(get_runs_repo)],
    runbook_id: str | None = None,
    mode: RunModeLiteral | None = None,
    outcome: OutcomeFilterLiteral | None = None,
    initiator: InitiatorLiteral | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> RunListResponse:
    filt = RunsFilter(
        runbook_id=runbook_id,
        mode=mode,
        outcome=outcome,
        initiator=initiator,
        since=since,
        until=until,
    )
    rows, total = await repo.list_paged(filt, limit=limit, offset=offset)
    return RunListResponse(
        items=[_row_to_run_out(r) for r in rows],
        total_count=total,
        limit=limit,
        offset=offset,
    )


@router.get("/runs/{run_id}", response_model=RunDetailOut)
async def get_run(
    run_id: str,
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[RunbookRunsRepository, Depends(get_runs_repo)],
) -> RunDetailOut:
    row = await repo.get_by_id(run_id)
    if row is None:
        raise NotFoundProblem(message=f"run {run_id} not found")
    return _row_to_run_detail_out(row)


@router.get("/runs/{run_id}/feedback", response_model=RunFeedbackListResponse)
async def get_run_feedback(
    run_id: str,
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[RunbookRunsRepository, Depends(get_runs_repo)],
    feedback_repo: Annotated[RunbookRunFeedbackRepository, Depends(get_feedback_repo)],
) -> RunFeedbackListResponse:
    # Enforce run existence (spec: 404 if run doesn't exist; list may be empty).
    row = await repo.get_by_id(run_id)
    if row is None:
        raise NotFoundProblem(message=f"run {run_id} not found")
    feedback = await feedback_repo.list_by_run(run_id)
    items = [
        RunFeedbackOut(
            id=f.id,
            runbook_run_id=f.runbook_run_id,
            kind=f.kind.value,  # type: ignore[arg-type]  # StrEnum value ∈ literal set
            suggestion_text=f.suggestion_text,
            structured_hint=(dict(f.structured_hint) if f.structured_hint is not None else None),
            created_at=f.created_at,
        )
        for f in feedback
    ]
    return RunFeedbackListResponse(items=items)


@router.get("/runs/{run_id}/transcript", response_model=TranscriptOut)
async def get_run_transcript(
    run_id: str,
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[RunbookRunsRepository, Depends(get_runs_repo)],
) -> TranscriptOut:
    row = await repo.get_by_id(run_id)
    if row is None:
        raise NotFoundProblem(message=f"run {run_id} not found")
    if row.transcript_path is None or row.transcript_path == "":
        raise NotFoundProblem(message=f"run {run_id} has no transcript")

    # Base dir from config; MUST be resolved before containment check.
    fixer_config = fixer_config_module.load_fixer_runner_config()
    if not fixer_config.transcript_dir:
        # Defensive: config default is '/data/runbook-transcripts'; if a
        # deployment misconfigures it to empty, refuse rather than allowing
        # unbounded reads. See "Rollback notes" below.
        raise HttpProblem(
            status_code=500,
            code="transcript_dir_not_configured",
            message="transcript base dir is not configured",
        )
    base_dir = Path(fixer_config.transcript_dir).resolve()

    result = read_transcript(row.transcript_path, base_dir=base_dir)
    if result is None:
        # File missing OR outside base dir (both look like "not found" to the caller).
        raise NotFoundProblem(message=f"transcript for run {run_id} not available")
    return TranscriptOut(
        text=result.text,
        truncated=result.truncated,
        size_bytes=result.size_bytes,
    )


__all__ = ["router"]
