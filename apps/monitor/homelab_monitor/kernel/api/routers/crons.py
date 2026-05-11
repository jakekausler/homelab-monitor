"""GET/POST/PATCH/DELETE /api/crons — cron registry CRUD (session-auth).

Authentication: session-only (operator dashboard surface). State-changing
methods (POST/PATCH/DELETE) inherit CSRF enforcement from
``require_session()`` automatically — there is no separate
``require_session_with_csrf`` factory in this codebase.

Audit verbs (recorded by ``CronRepo``):
- ``crons.create`` (POST)
- ``crons.update`` (PATCH that changes a non-archive field)
- ``crons.delete`` (PATCH archived_at non-null OR DELETE)
- ``crons.restore`` (PATCH archived_at -> null)

Preview endpoints (read-only, GET):
- ``GET /api/crons/{id}/preview-runs?count=N`` — saved cron
- ``GET /api/crons/preview-runs?expr=<cron>&count=N`` — unsaved input from
  add-modal. Both go through the SAME croniter helper so the UI's preview
  cannot drift from server-side validation.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from starlette.responses import JSONResponse, Response

from homelab_monitor.kernel.api.dependencies import get_cron_repo, require_session
from homelab_monitor.kernel.api.errors import NotFoundProblem
from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.cron.repository import CronRecord, CronRepo, CronWithState
from homelab_monitor.kernel.cron.schedule import (
    InvalidCronExpression,
    compute_next_runs,
)
from homelab_monitor.kernel.cron.schemas import (
    CronCreate,
    CronListQuery,
    CronListResponse,
    CronOut,
    CronUpdate,
    CronWithStateOut,
    HeartbeatStateOut,
    PreviewRunsQuery,
    PreviewRunsResponse,
)
from homelab_monitor.kernel.heartbeat.schemas import query_model

router = APIRouter(prefix="/crons", tags=["crons"])


def _client_ip(request: Request) -> str | None:
    if request.client is not None:
        return request.client.host
    return None  # pragma: no cover -- defensive


def _record_to_out(rec: CronRecord) -> CronOut:
    return CronOut(
        id=rec.id,
        name=rec.name,
        host=rec.host,
        command=rec.command,
        # schedule is stored as '' for cadence-only rows; surface as None publicly.
        schedule=None if rec.schedule == "" else rec.schedule,
        schedule_canonical=rec.schedule_canonical,
        cadence_seconds=rec.cadence_seconds,
        expected_grace_seconds=rec.expected_grace_seconds,
        integration_mode=rec.integration_mode,  # type: ignore[arg-type]
        enabled=rec.enabled,
        last_seen_state=rec.last_seen_state,  # type: ignore[arg-type]
        created_at=rec.created_at,
        updated_at=rec.updated_at,
        archived_at=rec.archived_at,
    )


def _with_state_to_out(joined: CronWithState) -> CronWithStateOut:
    state_out: HeartbeatStateOut | None = None
    if joined.state is not None:  # pragma: no cover
        s = joined.state
        state_out = HeartbeatStateOut(
            cron_id=s.cron_id,
            current_state=s.current_state,  # type: ignore[arg-type]
            last_start_at=s.last_start_at,
            last_ok_at=s.last_ok_at,
            last_fail_at=s.last_fail_at,
            current_streak=s.current_streak,
            expected_next_at=s.expected_next_at,
            last_duration_seconds=s.last_duration_seconds,
            last_exit_code=s.last_exit_code,
            updated_at=s.updated_at,
        )
    return CronWithStateOut(cron=_record_to_out(joined.cron), state=state_out)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=CronListResponse)
async def list_crons(
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[CronRepo, Depends(get_cron_repo)],
    query: Annotated[CronListQuery, Depends(query_model(CronListQuery))],
) -> CronListResponse:
    """Paginated list of crons with optional filters."""
    page = await repo.list_crons(
        page=query.page,
        page_size=query.page_size,
        host=query.host,
        integration_mode=query.integration_mode,
        enabled=query.enabled,
        state=query.state,
        q=query.q,
        include_archived=query.include_archived,
    )
    return CronListResponse(
        items=[_record_to_out(r) for r in page.items],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


# Preview endpoints come BEFORE the {cron_id} routes so FastAPI matches
# `/preview-runs` as a literal segment instead of trying to bind it as cron_id.


@router.get("/preview-runs", response_model=PreviewRunsResponse)
async def preview_runs_unsaved(
    _user: Annotated[User, Depends(require_session())],
    query: Annotated[PreviewRunsQuery, Depends(query_model(PreviewRunsQuery))],
) -> PreviewRunsResponse:
    """Preview the next N fire times for an unsaved cron expression.

    Used by the add-cron modal to preview the schedule before save. ``expr``
    is REQUIRED for this endpoint (the saved-cron variant omits it).
    """
    if query.expr is None:
        # query_model + Field validator rejects only if expr is supplied AND
        # invalid. Missing expr is a routing error here.
        raise NotFoundProblem(
            message="missing required query parameter: expr",
        )
    try:
        runs = compute_next_runs(query.expr, count=query.count)
    except (
        InvalidCronExpression
    ) as exc:  # pragma: no cover -- defense in depth, validator rejects bad expr first
        # Defense in depth: PreviewRunsQuery already validates, but if a
        # caller bypasses that path we surface 422-shaped error.
        raise NotFoundProblem(message=str(exc)) from exc
    return PreviewRunsResponse(runs=runs)


@router.get("/{cron_id}/preview-runs", response_model=PreviewRunsResponse)
async def preview_runs_saved(
    cron_id: str,
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[CronRepo, Depends(get_cron_repo)],
    query: Annotated[PreviewRunsQuery, Depends(query_model(PreviewRunsQuery))],
) -> PreviewRunsResponse:
    """Preview the next N fire times for a saved cron's schedule.

    404 if the cron is missing OR has no schedule (cadence-only cron has no
    schedule to preview)."""
    cron = await repo.get_cron(cron_id, include_archived=False)
    if cron is None:
        raise NotFoundProblem(message=f"cron not found: {cron_id}")
    if not cron.schedule:
        raise NotFoundProblem(
            message=f"cron has no schedule (cadence-only): {cron_id}",
        )
    runs = compute_next_runs(cron.schedule, count=query.count)
    return PreviewRunsResponse(runs=runs)


@router.get("/{cron_id}", response_model=CronWithStateOut)
async def get_cron(
    cron_id: str,
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[CronRepo, Depends(get_cron_repo)],
    include_archived: bool = False,
) -> CronWithStateOut:
    """Return a single cron + its joined heartbeat state.

    By default archived crons return 404; pass ``?include_archived=true``
    for admin recovery flows.
    """
    joined = await repo.get_cron_with_state(cron_id, include_archived=include_archived)
    if joined is None:
        raise NotFoundProblem(message=f"cron not found: {cron_id}")
    return _with_state_to_out(joined)


@router.post("", response_model=CronOut, status_code=201)
async def create_cron(
    payload: CronCreate,
    request: Request,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[CronRepo, Depends(get_cron_repo)],
) -> JSONResponse:
    """Create a new cron registry row. CSRF enforced by require_session()."""
    rec = await repo.create_cron(payload, who=user.username, ip=_client_ip(request))
    return JSONResponse(
        status_code=201,
        content=_record_to_out(rec).model_dump(mode="json"),
    )


@router.patch("/{cron_id}", response_model=CronOut)
async def update_cron(
    cron_id: str,
    payload: CronUpdate,
    request: Request,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[CronRepo, Depends(get_cron_repo)],
) -> CronOut:
    """Partial update of a cron. Empty diff returns 200 with no audit row."""
    try:
        rec = await repo.update_cron(cron_id, payload, who=user.username, ip=_client_ip(request))
    except LookupError as exc:
        raise NotFoundProblem(message=str(exc)) from exc
    return _record_to_out(rec)


@router.delete("/{cron_id}", status_code=204)
async def delete_cron(
    cron_id: str,
    request: Request,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[CronRepo, Depends(get_cron_repo)],
) -> Response:
    """Soft-delete (archive) a cron. 404 if missing OR already archived."""
    try:
        await repo.soft_delete_cron(cron_id, who=user.username, ip=_client_ip(request))
    except LookupError as exc:
        raise NotFoundProblem(message=str(exc)) from exc
    return Response(status_code=204)
