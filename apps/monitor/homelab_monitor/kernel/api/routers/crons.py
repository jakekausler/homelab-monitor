"""GET/PATCH/DELETE /api/crons — cron registry CRUD (session-auth).

Authentication: session-only (operator dashboard surface). State-changing
methods (PATCH/DELETE) inherit CSRF enforcement from
``require_session()`` automatically — there is no separate
``require_session_with_csrf`` factory in this codebase.

Audit verbs (recorded by ``CronRepo``):
- ``crons.update`` (PATCH that changes a non-hidden_at field)
- ``crons.hide`` (PATCH hidden_at non-null OR DELETE)
- ``crons.unhide`` (PATCH hidden_at -> null)

Preview endpoints (read-only, GET):
- ``GET /api/crons/{fingerprint}/preview-runs?count=N`` — saved cron
- ``GET /api/crons/preview-runs?expr=<cron>&count=N`` — unsaved expression.
  Both go through the SAME croniter helper so UI preview cannot drift from
  server-side validation.
"""

from __future__ import annotations

import asyncio
import time as _time
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request
from starlette.responses import Response as _FastApiResponse

from homelab_monitor.kernel.api.dependencies import get_cron_repo, require_session
from homelab_monitor.kernel.api.errors import (
    DependencyUnavailableProblem,
    NotFoundProblem,
    TooManyRequestsProblem,
)
from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.cron.discovery_types import CronScanResult
from homelab_monitor.kernel.cron.repository import CronRepo, CronWithState
from homelab_monitor.kernel.cron.schedule import (
    InvalidCronExpression,
    compute_next_runs,
)
from homelab_monitor.kernel.cron.schemas import (
    CronListQuery,
    CronListResponse,
    CronOut,
    CronUpdate,
    CronWithStateOut,
    HeartbeatStateOut,
    PreviewRunsQuery,
    PreviewRunsResponse,
    cron_record_to_out,
)
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.heartbeat.schemas import query_model

router = APIRouter(prefix="/crons", tags=["crons"])

# Throttle state for discover-now endpoint
_DISCOVER_NOW_THROTTLE_SECONDS = 10.0
_discover_now_lock = asyncio.Lock()
_discover_now_last_call: float = 0.0


def _client_ip(request: Request) -> str | None:
    if request.client is not None:
        return request.client.host
    return None  # pragma: no cover -- defensive


def _with_state_to_out(joined: CronWithState) -> CronWithStateOut:
    state_out: HeartbeatStateOut | None = None
    if joined.state is not None:  # pragma: no cover
        s = joined.state
        state_out = HeartbeatStateOut(
            cron_fingerprint=s.cron_fingerprint,
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
    return CronWithStateOut(cron=cron_record_to_out(joined.cron), state=state_out)


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
        enabled=query.enabled,
        state=query.state,
        q=query.q,
        include_hidden=query.include_hidden,
        include_soft_deleted=query.include_soft_deleted,
        wrapper_installed=query.wrapper_installed,
    )
    return CronListResponse(
        items=[cron_record_to_out(r) for r in page.items],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


# Preview endpoints come BEFORE the {fingerprint} routes so FastAPI matches
# `/preview-runs` as a literal segment instead of trying to bind it as fingerprint.


@router.get("/preview-runs", response_model=PreviewRunsResponse)
async def preview_runs_unsaved(
    _user: Annotated[User, Depends(require_session())],
    query: Annotated[PreviewRunsQuery, Depends(query_model(PreviewRunsQuery))],
) -> PreviewRunsResponse:
    """Preview the next N fire times for an unsaved cron expression.

    Used by UI clients to validate or preview an unsaved cron expression. ``expr``
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


@router.get("/{fingerprint}/preview-runs", response_model=PreviewRunsResponse)
async def preview_runs_saved(
    fingerprint: str,
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[CronRepo, Depends(get_cron_repo)],
    query: Annotated[PreviewRunsQuery, Depends(query_model(PreviewRunsQuery))],
) -> PreviewRunsResponse:
    """Preview the next N fire times for a saved cron's schedule.

    404 if the cron is missing OR has no schedule (cadence-only cron has no
    schedule to preview)."""
    cron = await repo.get_cron(fingerprint, include_hidden=False)
    if cron is None:
        raise NotFoundProblem(message=f"cron not found: {fingerprint}")
    if not cron.schedule:
        raise NotFoundProblem(
            message=f"cron has no schedule (cadence-only): {fingerprint}",
        )
    runs = compute_next_runs(cron.schedule, count=query.count)
    return PreviewRunsResponse(runs=runs)


@router.get("/{fingerprint}", response_model=CronWithStateOut)
async def get_cron(
    fingerprint: str,
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[CronRepo, Depends(get_cron_repo)],
    include_hidden: bool = False,
) -> CronWithStateOut:
    """Return a single cron + its joined heartbeat state.

    By default hidden crons return 404; pass ``?include_hidden=true``
    for admin recovery flows. Soft-deleted crons are ALWAYS returned by this
    endpoint (direct fetch is unfiltered for soft-delete; STAGE-002-007A).
    """
    joined = await repo.get_cron_with_state(fingerprint, include_hidden=include_hidden)
    if joined is None:
        raise NotFoundProblem(message=f"cron not found: {fingerprint}")
    return _with_state_to_out(joined)


@router.patch("/{fingerprint}", response_model=CronOut)
async def update_cron(
    fingerprint: str,
    payload: CronUpdate,
    request: Request,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[CronRepo, Depends(get_cron_repo)],
) -> CronOut:
    """Partial update of a cron. Empty diff returns 200 with no audit row."""
    try:
        rec = await repo.update_cron(
            fingerprint, payload, who=user.username, ip=_client_ip(request)
        )
    except LookupError as exc:
        raise NotFoundProblem(message=str(exc)) from exc
    return cron_record_to_out(rec)


@router.delete("/{fingerprint}", status_code=204)
async def delete_cron(
    fingerprint: str,
    request: Request,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[CronRepo, Depends(get_cron_repo)],
) -> None:
    """Soft-delete (hide) a cron. 404 if missing OR already hidden."""
    try:
        await repo.soft_delete_cron(fingerprint, who=user.username, ip=_client_ip(request))
    except LookupError as exc:
        raise NotFoundProblem(message=str(exc)) from exc


@router.post("/discover-now", status_code=202)
async def discover_now(
    request: Request,
    response: _FastApiResponse,
    user: Annotated[User, Depends(require_session())],
) -> dict[str, object]:
    """Trigger an ad-hoc cron discovery scan. Throttled to once per 10s.

    Admin-only via session auth. 429 with Retry-After when called within
    the throttle window. 202 Accepted on success, with a JSON summary of
    the scan result.
    """
    global _discover_now_last_call  # noqa: PLW0603
    async with _discover_now_lock:
        now = _time.monotonic()
        elapsed = now - _discover_now_last_call
        if elapsed < _DISCOVER_NOW_THROTTLE_SECONDS:
            retry_after = max(1, int(_DISCOVER_NOW_THROTTLE_SECONDS - elapsed))
            raise TooManyRequestsProblem(
                code="discover_now_throttled",
                message=f"discovery scan recently triggered; retry in {retry_after}s",
                details={"retry_after_seconds": retry_after},
            )
        _discover_now_last_call = now

        discoverer = getattr(request.app.state, "cron_discoverer", None)
        if discoverer is None:
            raise DependencyUnavailableProblem(
                code="cron_discoverer_unavailable",
                message="cron-discoverer plugin not registered",
            )
        cron_repo = getattr(request.app.state, "cron_repo", None)
        if cron_repo is None:
            raise DependencyUnavailableProblem(
                code="cron_repo_unavailable",
                message="cron-repo not registered",
            )
        # use the lifespan logger instead — fetch from app.state if available

        bound_log = structlog.stdlib.get_logger().bind(component="discover-now", who=user.username)
        result: CronScanResult = await discoverer.scan(cron_repo, log=bound_log)
        try:
            soft_deleted, restored = await cron_repo.reconcile_soft_deletes(
                host=result.host,
                clean_paths=result.clean_source_paths,
                found_by_path=result.found_by_source_path,
                now=utc_now_iso(),
            )
        except Exception as exc:
            soft_deleted, restored = 0, 0
            bound_log.warning("discover_now.reconcile_failed", error=str(exc))
        return {
            "found_count": len(result.found_fingerprints),
            "inserted_count": result.inserted_count,
            "updated_count": result.updated_count,
            "bump_only_count": result.bump_only_count,
            "soft_deleted_count": soft_deleted,
            "restored_count": restored,
            "partial": result.partial,
            "error_count": len(result.errors),
            "errors": [
                {"host_source_path": e.host_source_path, "error": e.error} for e in result.errors
            ],
        }
