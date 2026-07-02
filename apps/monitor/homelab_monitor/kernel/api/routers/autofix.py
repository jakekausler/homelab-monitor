"""Auto-fix approval API: list pending approvals, read a dry-run plan, approve
(confirm-on-destructive) or reject.

All routes require a session (Depends(require_session()); 401 unauth; CSRF on
mutating methods). Approve/reject audit in the same txn as the state change
(via the orchestrator / approvals repo). The orchestrator is provided from
app.state (built in lifespan); 503 if auto-fix is not initialized.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict

from homelab_monitor.kernel.api.dependencies import get_repo, require_session
from homelab_monitor.kernel.api.errors import (
    ConflictProblem,
    DependencyUnavailableProblem,
    NotFoundProblem,
)
from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.autofix.approvals_repository import (
    ApprovalRecord,
    RunbookRunApprovalsRepository,
)
from homelab_monitor.kernel.autofix.orchestrator import AutoFixOrchestrator
from homelab_monitor.kernel.autofix.types import DenialReason, RunOutcome
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.runbooks.repository import RunbookRepo

router = APIRouter(prefix="/autofix", tags=["autofix"])

# Confirm-on-destructive phrase for approving a real run (mirrors docker's idiom).
_APPROVE_CONFIRM_PHRASE: Literal["approve"] = "approve"


def get_approvals_repo(
    db: Annotated[SqliteRepository, Depends(get_repo)],
) -> RunbookRunApprovalsRepository:
    return RunbookRunApprovalsRepository(db)


def get_runbooks_repo(
    db: Annotated[SqliteRepository, Depends(get_repo)],
) -> RunbookRepo:
    return RunbookRepo(db)


def get_orchestrator(request: Request) -> AutoFixOrchestrator:
    """Fetch the orchestrator from app.state (built in lifespan when docker is
    enabled). 503 if auto-fix is not initialized on this instance.
    """
    orch = getattr(request.app.state, "autofix_orchestrator", None)
    if not isinstance(orch, AutoFixOrchestrator):
        raise DependencyUnavailableProblem(
            message="auto-fix is not enabled on this instance",
            code="autofix_unavailable",
        )
    return orch


def _client_ip(request: Request) -> str | None:
    if request.client is not None:
        return request.client.host
    return None  # pragma: no cover -- defensive


# ---- wire models ----


class ApprovalOut(BaseModel):
    id: str
    dry_run_id: str
    runbook_id: str
    alert_id: str | None
    pinned_runbook_hash: str | None
    status: str
    approved_by: str | None
    decided_at: str | None
    real_run_id: str | None
    created_at: str
    drift_detected: bool


class ApprovalListResponse(BaseModel):
    items: list[ApprovalOut]


class PlanResponse(BaseModel):
    approval_id: str
    dry_run_id: str
    runbook_id: str
    transcript_path: str
    plan_text: str
    exit_code: int | None


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_phrase: str


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApproveResponse(BaseModel):
    approval_id: str
    ran: bool
    outcome: str
    real_run_id: str | None
    exit_code: int | None
    denial_reason: str | None


class RejectResponse(BaseModel):
    approval_id: str
    status: str


async def _drift_for(repo: RunbookRepo, approval: ApprovalRecord) -> bool:
    """True if the runbook is gone OR its current content_hash != the pinned hash."""
    record = await repo.get_runbook(approval.runbook_id)
    if record is None:
        return True
    return record.content_hash != approval.pinned_runbook_hash


def _approval_to_out(approval: ApprovalRecord, *, drift_detected: bool) -> ApprovalOut:
    return ApprovalOut(
        id=approval.id,
        dry_run_id=approval.dry_run_id,
        runbook_id=approval.runbook_id,
        alert_id=approval.alert_id,
        pinned_runbook_hash=approval.pinned_runbook_hash,
        status=approval.status,
        approved_by=approval.approved_by,
        decided_at=approval.decided_at,
        real_run_id=approval.real_run_id,
        created_at=approval.created_at,
        drift_detected=drift_detected,
    )


# ---- routes ----


@router.get("/approvals", response_model=ApprovalListResponse)
async def list_approvals(
    _user: Annotated[User, Depends(require_session())],
    approvals: Annotated[RunbookRunApprovalsRepository, Depends(get_approvals_repo)],
    runbooks: Annotated[RunbookRepo, Depends(get_runbooks_repo)],
    status_filter: Annotated[Literal["pending", "approved", "rejected"], Query()] = "pending",
) -> ApprovalListResponse:
    rows = await approvals.list_by_status(status_filter)
    items: list[ApprovalOut] = []
    for approval in rows:
        drift = await _drift_for(runbooks, approval)
        items.append(_approval_to_out(approval, drift_detected=drift))
    return ApprovalListResponse(items=items)


@router.get("/approvals/{approval_id}/plan", response_model=PlanResponse)
async def get_plan(
    approval_id: str,
    _user: Annotated[User, Depends(require_session())],
    approvals: Annotated[RunbookRunApprovalsRepository, Depends(get_approvals_repo)],
    orchestrator: Annotated[AutoFixOrchestrator, Depends(get_orchestrator)],
) -> PlanResponse:
    approval = await approvals.get(approval_id)
    if approval is None:
        raise NotFoundProblem(message=f"approval {approval_id} not found")
    plan = await orchestrator.read_dry_plan(approval.dry_run_id)
    if plan is None:
        raise NotFoundProblem(message="plan transcript not found")
    return PlanResponse(
        approval_id=approval.id,
        dry_run_id=approval.dry_run_id,
        runbook_id=approval.runbook_id,
        transcript_path=plan.transcript_path,
        plan_text=plan.plan_text,
        exit_code=plan.exit_code,
    )


@router.post("/approvals/{approval_id}/approve", response_model=ApproveResponse)
async def approve(  # noqa: PLR0913 -- FastAPI Depends parameters
    approval_id: str,
    payload: ApproveRequest,
    request: Request,
    user: Annotated[User, Depends(require_session())],
    approvals: Annotated[RunbookRunApprovalsRepository, Depends(get_approvals_repo)],
    orchestrator: Annotated[AutoFixOrchestrator, Depends(get_orchestrator)],
) -> ApproveResponse:
    # N1: exact string equality (no strip / no case-fold). The confirm phrase is
    # a deliberate friction gate on a destructive action — variants like
    # 'APPROVE' or ' approve ' are rejected.
    if payload.confirm_phrase != _APPROVE_CONFIRM_PHRASE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"confirm_phrase must equal '{_APPROVE_CONFIRM_PHRASE}'",
        )
    approval = await approvals.get(approval_id)
    if approval is None:
        raise NotFoundProblem(message=f"approval {approval_id} not found")
    if approval.status != "pending":
        raise ConflictProblem(
            message="approval is not pending",
            code="approval_not_pending",
        )
    # I3: drift + operational-gate re-checks (and their rejection + audits) are
    # owned by the orchestrator. The router only maps the denial_reason of the
    # returned RunResult to the right HTTP response.
    result = await orchestrator.execute_approved(
        approval_id, principal=user.username, ip=_client_ip(request)
    )
    if result.outcome == RunOutcome.DENIED:
        reason = result.denial_reason
        if reason == DenialReason.RUNBOOK_CHANGED:
            # Orchestrator already wrote the enriched autofix.rejected audit +
            # marked the approval rejected.
            raise ConflictProblem(
                message="runbook changed since the plan was captured",
                code="runbook_changed_since_plan",
            )
        if reason == DenialReason.RUNBOOK_MISSING:
            # Fix M2: distinguish deleted-runbook from mutated-runbook. Both are
            # 409 conflicts but the operator response differs (delete = the
            # runbook was removed; you must re-author or approve nothing;
            # changed = you may re-plan against the new content). The
            # orchestrator already wrote the autofix.rejected audit +
            # marked the approval rejected.
            raise ConflictProblem(
                message="runbook was deleted after the plan was captured",
                code="runbook_missing",
            )
        if reason == DenialReason.APPROVAL_NOT_PENDING:
            raise ConflictProblem(
                message="approval is not pending",
                code="approval_not_pending",
            )
        # Any other operational-gate denial (kill switch, allow list, rate
        # limit, cooldown, already-running, claim error) surfaces as 409 with
        # the gate name as the code, so the UI can distinguish.
        code = reason.value if reason is not None else "denied"
        raise ConflictProblem(
            message=f"auto-fix denied: {code}",
            code=code,
        )
    return ApproveResponse(
        approval_id=approval_id,
        ran=result.ran,
        outcome=result.outcome.value,
        real_run_id=result.run_id if result.outcome == RunOutcome.RAN else None,
        exit_code=result.exit_code,
        denial_reason=(result.denial_reason.value if result.denial_reason is not None else None),
    )


@router.post("/approvals/{approval_id}/reject", response_model=RejectResponse)
async def reject(
    approval_id: str,
    _payload: RejectRequest,
    request: Request,
    user: Annotated[User, Depends(require_session())],
    approvals: Annotated[RunbookRunApprovalsRepository, Depends(get_approvals_repo)],
) -> RejectResponse:
    approval = await approvals.get(approval_id)
    if approval is None:
        raise NotFoundProblem(message=f"approval {approval_id} not found")
    if approval.status != "pending":
        raise ConflictProblem(
            message="approval is not pending",
            code="approval_not_pending",
        )
    rowcount = await approvals.mark_rejected(
        approval_id=approval_id,
        approved_by=user.username,
        when=None,
        ip=_client_ip(request),
    )
    if rowcount == 0:
        # Concurrent caller already decided the approval between our pre-check
        # read and the UPDATE (race safety net; the pre-check catches the
        # common case).
        raise ConflictProblem(
            message="approval is not pending",
            code="approval_not_pending",
        )
    return RejectResponse(approval_id=approval_id, status="rejected")
