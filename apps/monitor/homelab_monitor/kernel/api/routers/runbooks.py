"""Runbook registry API: list, refresh (scan+reconcile), patch operator gates.

All routes require a session (Depends(require_session()), 401 on unauth; CSRF on
mutating methods enforced by require_session). PATCH and refresh audit in the same
transaction as the data write (via the repository).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from homelab_monitor.kernel.api.dependencies import get_repo, require_session
from homelab_monitor.kernel.api.errors import NotFoundProblem
from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.config import get_runbooks_dir
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.runbooks.loader import scan_runbooks
from homelab_monitor.kernel.runbooks.repository import (
    RunbookRecord,
    RunbookRepo,
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


class RunbookGatesPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    auto_trigger: bool | None = None


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


# ---- routes ----


@router.get("", response_model=RunbookListResponse)
async def list_runbooks(
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[RunbookRepo, Depends(get_runbooks_repo)],
) -> RunbookListResponse:
    records = await repo.list_runbooks()
    return RunbookListResponse(items=[_record_to_out(r) for r in records])


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
    )


@router.patch("/{runbook_id}", response_model=RunbookOut)
async def patch_runbook_gates(
    runbook_id: str,
    payload: RunbookGatesPatch,
    request: Request,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[RunbookRepo, Depends(get_runbooks_repo)],
) -> RunbookOut:
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
