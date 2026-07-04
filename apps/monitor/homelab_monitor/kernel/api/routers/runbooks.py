"""Runbook registry API: list, refresh (scan+reconcile), patch operator gates.

All routes require a session (Depends(require_session()), 401 on unauth; CSRF on
mutating methods enforced by require_session). PATCH and refresh audit in the same
transaction as the data write (via the repository).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from homelab_monitor.kernel.api.dependencies import (
    get_app_settings,
    get_pin_rate_limiter,
    get_repo,
    require_session,
)
from homelab_monitor.kernel.api.errors import ConflictProblem, HttpProblem, NotFoundProblem
from homelab_monitor.kernel.api.routers.autofix import get_orchestrator
from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.autofix.orchestrator import AutoFixOrchestrator
from homelab_monitor.kernel.autofix.runs_repository import (
    RunbookRunsRepository,
    RunbookStatsRow,
)
from homelab_monitor.kernel.autofix.types import (
    DenialReason,
    DryRunRequiredForRiskyError,
    RunbookNotFoundError,
    RunMode,
    RunOutcome,
)
from homelab_monitor.kernel.config import get_runbooks_dir
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.runbooks.loader import scan_runbooks
from homelab_monitor.kernel.runbooks.repository import (
    RunbookRecord,
    RunbookRepo,
)
from homelab_monitor.kernel.security.pin import (
    InProcessPinRateLimiter,
    PhraseMatchMode,
    verify_destructive_credential,
)

router = APIRouter(prefix="/runbooks", tags=["runbooks"])


def get_runbooks_repo(
    db: Annotated[SqliteRepository, Depends(get_repo)],
) -> RunbookRepo:
    return RunbookRepo(db)


def _client_ip(request: Request) -> str | None:
    if request.client is not None:
        return request.client.host
    return None  # pragma: no cover -- defensive


# ---- wire models ----


class RunbookOut(BaseModel):
    id: str
    path: str
    created_at: str
    # Any exception: matchers are opaque pre-validated JSON, echoed read-only.
    alert_match_patterns: list[dict[str, Any]]
    risk_tag: str
    dry_run_required: bool
    rate_limit_per_hour: int | None
    cooldown_seconds: int | None
    enabled: bool
    auto_trigger: bool
    content_hash: str | None


class RunbookListResponse(BaseModel):
    items: list[RunbookOut]


class LoadErrorOut(BaseModel):
    path: str
    message: str


class RefreshResponse(BaseModel):
    registered: list[str]
    refreshed: list[str]
    skipped: list[str]
    errors: list[LoadErrorOut]
    pruned: list[str] = Field(default_factory=list)
    prune_skipped: list[str] = Field(default_factory=list)


class RunbookGatesPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    auto_trigger: bool | None = None


class TriggerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["dry_run", "real"]
    confirm_phrase: str | None = None
    confirm_pin: str | None = None


class TriggerResponse(BaseModel):
    run_id: str | None
    outcome: Literal["ran", "denied", "dry_run_stored"]
    denial_reason: str | None = None
    approval_id: str | None = None


# ---- stats (STAGE-009-011) ----


class RunbookStatsOut(BaseModel):
    runbook_id: str
    last_run_at: str | None
    last_run_status: Literal["success", "failure", "killed", "in_flight", "dry_run"] | None
    success_rate_30d: float | None
    run_count_30d: int


class RunbookStatsResponse(BaseModel):
    items: list[RunbookStatsOut]


def _stats_row_to_out(row: RunbookStatsRow) -> RunbookStatsOut:
    return RunbookStatsOut(
        runbook_id=row.runbook_id,
        last_run_at=row.last_run_at,
        last_run_status=row.last_run_status,  # type: ignore[arg-type]
        success_rate_30d=row.success_rate_30d,
        run_count_30d=row.run_count_30d,
    )


def get_runs_repo(
    db: Annotated[SqliteRepository, Depends(get_repo)],
) -> RunbookRunsRepository:
    return RunbookRunsRepository(db)


def _record_to_out(rec: RunbookRecord) -> RunbookOut:
    return RunbookOut(
        id=rec.id,
        path=rec.path,
        created_at=rec.created_at,
        alert_match_patterns=rec.alert_match_patterns,
        risk_tag=rec.risk_tag,
        dry_run_required=rec.dry_run_required,
        rate_limit_per_hour=rec.rate_limit_per_hour,
        cooldown_seconds=rec.cooldown_seconds,
        enabled=rec.enabled,
        auto_trigger=rec.auto_trigger,
        content_hash=rec.content_hash,
    )


def _denial_reason_to_code(reason: DenialReason) -> str:
    """Map DenialReason -> HTTP error code string (409 Conflict payload)."""
    return {
        DenialReason.KILL_SWITCH: "kill_switch",
        DenialReason.ALLOW_LIST: "runbook_disabled",
        DenialReason.RATE_LIMIT: "rate_limit",
        DenialReason.COOLDOWN: "cooldown",
        DenialReason.ALREADY_RUNNING: "already_running",
        DenialReason.CLAIM_ERROR: "claim_error",
        DenialReason.APPROVAL_NOT_PENDING: "approval_not_pending",
        DenialReason.RUNBOOK_CHANGED: "runbook_changed",
        DenialReason.RUNBOOK_MISSING: "runbook_missing",
        DenialReason.AMBIGUOUS_MATCH: "ambiguous_match",
    }[reason]


# ---- routes ----


@router.get("", response_model=RunbookListResponse)
async def list_runbooks(
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[RunbookRepo, Depends(get_runbooks_repo)],
) -> RunbookListResponse:
    records = await repo.list_runbooks()
    return RunbookListResponse(items=[_record_to_out(r) for r in records])


@router.get("/stats", response_model=RunbookStatsResponse)
async def list_runbook_stats(
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[RunbookRunsRepository, Depends(get_runs_repo)],
) -> RunbookStatsResponse:
    """Per-runbook 30-day aggregates for the runbooks-list UI cards.

    All 30 registered runbooks appear (LEFT-JOIN semantics) — runbooks with
    zero runs return run_count_30d=0 and null stats.
    """
    now = datetime.fromisoformat(utc_now_iso())
    window_start = (now - timedelta(days=30)).isoformat()
    rows = await repo.stats_per_runbook(window_start_iso=window_start)
    return RunbookStatsResponse(items=[_stats_row_to_out(r) for r in rows])


@router.post("/refresh", response_model=RefreshResponse)
async def refresh_runbooks(
    request: Request,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[RunbookRepo, Depends(get_runbooks_repo)],
) -> RefreshResponse:
    scan = scan_runbooks(get_runbooks_dir())
    outcome = await repo.reconcile(scan, who_principal=user, ip=_client_ip(request))
    return RefreshResponse(
        registered=outcome.registered,
        refreshed=outcome.refreshed,
        skipped=outcome.skipped,
        errors=[LoadErrorOut(path=e.path, message=e.message) for e in outcome.errors],
        pruned=outcome.pruned,
        prune_skipped=outcome.prune_skipped,
    )


@router.patch("/{runbook_id}", response_model=RunbookOut)
async def patch_runbook_gates(
    runbook_id: str,
    payload: RunbookGatesPatch,
    request: Request,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[RunbookRepo, Depends(get_runbooks_repo)],
) -> RunbookOut:
    # Defense-in-depth: risky runbooks cannot be armed for auto-trigger via PATCH.
    # UI already hides the auto_trigger switch on risky cards, but a curl/API misuse
    # or a UI bug would otherwise silently arm a high-blast-radius runbook.
    if payload.auto_trigger is True:
        record = await repo.get_runbook(runbook_id)
        if record is None:
            raise NotFoundProblem(message=f"runbook {runbook_id} not found")
        if record.risk_tag == "risky":
            raise HttpProblem(
                status_code=400,
                code="risky_auto_trigger_denied",
                message=(
                    f"cannot enable auto_trigger on risky runbook {runbook_id}; "
                    "flip risk_tag first or use the approval flow"
                ),
                details={"runbook_id": runbook_id, "risk_tag": record.risk_tag},
            )

    try:
        rec = await repo.patch_gates(
            runbook_id,
            enabled=payload.enabled,
            auto_trigger=payload.auto_trigger,
            who_principal=user,
            ip=_client_ip(request),
        )
    except LookupError as exc:
        raise NotFoundProblem(message=str(exc)) from exc
    return _record_to_out(rec)


@router.post(
    "/{runbook_id}/trigger",
    response_model=TriggerResponse,
    responses={
        400: {"description": "Bad request (e.g., risky runbook rejected for real mode)"},
        404: {"description": "Runbook not found"},
        409: {"description": "Denied by an operational gate"},
    },
)
async def trigger_runbook(  # noqa: PLR0913 -- FastAPI route with injected dependencies
    runbook_id: str,
    payload: TriggerRequest,
    request: Request,
    user: Annotated[User, Depends(require_session())],
    orchestrator: Annotated[AutoFixOrchestrator, Depends(get_orchestrator)],
    repo: Annotated[RunbookRepo, Depends(get_runbooks_repo)],
    app_settings: Annotated[AppSettingsRepository, Depends(get_app_settings)],
    pin_rate_limiter: Annotated[InProcessPinRateLimiter, Depends(get_pin_rate_limiter)],
) -> TriggerResponse:
    """Manually trigger a runbook (operator-initiated).

    Skips MATCH + auto_trigger allow-list; enforces every other safety gate.
    """
    mode = RunMode(payload.mode)
    credential_type: Literal["pin", "phrase"] | None = None

    # For real mode, load runbook to derive basename for credential verification.
    if mode == RunMode.REAL:
        record = await repo.get_runbook(runbook_id)
        if record is None:
            raise NotFoundProblem(message=f"runbook {runbook_id} not found")
        expected_basename = Path(record.path).name
        credential_type = await verify_destructive_credential(
            confirm_pin=payload.confirm_pin,
            confirm_phrase=payload.confirm_phrase,
            expected_phrase=expected_basename,
            phrase_match_mode=PhraseMatchMode.CASE_FOLD,
            user=user,
            app_settings=app_settings,  # type: ignore[arg-type]
            rate_limiter=pin_rate_limiter,  # type: ignore[arg-type]
        )
    # For dry_run, skip credential verification; credential_type stays None.

    try:
        result = await orchestrator.handle_operator_trigger(
            runbook_id=runbook_id,
            mode=mode,
            principal=user.username,
            ip=_client_ip(request),
            credential_type=credential_type,
        )
    except RunbookNotFoundError as exc:
        raise NotFoundProblem(message=str(exc)) from exc
    except DryRunRequiredForRiskyError as exc:
        raise HttpProblem(
            status_code=400,
            code="dry_run_required_for_risky",
            message=str(exc),
            details={"runbook_id": runbook_id},
        ) from exc

    if result.outcome == RunOutcome.DENIED:
        assert result.denial_reason is not None
        code = _denial_reason_to_code(result.denial_reason)
        raise ConflictProblem(
            message=f"runbook {runbook_id} denied: {result.denial_reason.value}",
            code=code,
        )

    return TriggerResponse(
        run_id=result.run_id,
        outcome=result.outcome.value,
        denial_reason=None,
        approval_id=result.approval_id,
    )
