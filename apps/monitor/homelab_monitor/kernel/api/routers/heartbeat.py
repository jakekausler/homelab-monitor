"""POST /api/hb/{fingerprint}/{start|ok|fail} -- heartbeat receiver.

Auth: API token with ``Scope.HEARTBEAT_WRITE`` (single global token per
Decision 3). Per-cron rate-limiting (Decision 3a) defends against spoofed
ping floods if the global token leaks.

State recorded atomically into ``heartbeats_state`` + mirrored to
``crons.last_seen_state`` + audit row in ONE transaction (Decision 4 mirror,
dual-write through ``HeartbeatRepo._record_state_transition``).

404 (cron unknown) does NOT write an audit row -- no state change occurred.
429 (rate-limited) does NOT write an audit row -- request never executed.

Prometheus metric emission is DEFERRED to STAGE-002-006: the router does not
touch the prometheus registry in this stage. STAGE-002-006 will add a
collector that reads ``heartbeats_state`` on each scrape and emits
``homelab_heartbeat_seconds_since_last_ok`` alongside the vmalert rules.
"""

from __future__ import annotations

import math
from typing import Annotated, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import JSONResponse, Response
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.api.dependencies import (
    get_cron_repo,
    get_cron_run_repo,
    get_heartbeat_repo,
    require_token_scope,
)
from homelab_monitor.kernel.api.errors import NotFoundProblem, envelope_response
from homelab_monitor.kernel.auth.models import ApiToken
from homelab_monitor.kernel.auth.scopes import Scope
from homelab_monitor.kernel.cron.fingerprint import compute_fingerprint
from homelab_monitor.kernel.cron.repository import CronRepo
from homelab_monitor.kernel.cron.run_repository import CronRunRepository
from homelab_monitor.kernel.cron.schedule import (
    InvalidCronExpression,
    canonicalize_schedule,
)
from homelab_monitor.kernel.cron.schemas import (
    CronOut,
    RegisterCronBody,
    cron_record_to_out,
)
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.heartbeat.rate_limiter import cron_rate_limiter
from homelab_monitor.kernel.heartbeat.repository import CronRecord, HeartbeatRepo
from homelab_monitor.kernel.heartbeat.schemas import (
    HeartbeatFailQuery,
    HeartbeatOkQuery,
    HeartbeatStartQuery,
    query_model,
)

router = APIRouter(prefix="/hb", tags=["heartbeat"])


def _client_ip(request: Request) -> str | None:
    """Return the request peer IP, or None when starlette omits request.client."""
    if request.client is not None:
        return request.client.host
    return None  # pragma: no cover -- defensive


async def _resolve_cron_or_404(
    repo: HeartbeatRepo,
    fingerprint: str,
) -> CronRecord:
    cron = await repo.get_cron(fingerprint)
    if cron is None:
        raise NotFoundProblem(message=f"cron not found: {fingerprint}")
    return cron


def _enforce_rate_limit(fingerprint: str) -> None:
    """Raise a Response-shaped 429 by setting headers on a dedicated exception path.

    We use FastAPI's HTTPException via ``HttpProblem``-equivalent to attach
    ``Retry-After``. Returning a Response from a dependency wouldn't compose;
    raising surfaces through the registered HTTP problem handler.
    """
    allowed, retry_after = cron_rate_limiter.try_acquire(fingerprint)
    if allowed:
        return
    # Round retry_after up to the nearest integer second per RFC 7231 §7.1.3
    # (Retry-After is delta-seconds as an integer).
    retry_after_seconds = max(1, math.ceil(retry_after))
    raise _RateLimitedError(retry_after_seconds=retry_after_seconds)


class _RateLimitedError(Exception):
    """Internal sentinel: raised by the rate limiter, caught by an endpoint-local handler."""

    def __init__(self, *, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("rate limited")


_log: BoundLogger = cast(BoundLogger, structlog.get_logger().bind(component="heartbeat.receiver"))


# ---- routes -----------------------------------------------------------------


@router.post("/{fingerprint}/start", status_code=204)
async def receive_start(  # noqa: PLR0913
    fingerprint: str,
    request: Request,
    token: Annotated[ApiToken, Depends(require_token_scope(Scope.HEARTBEAT_WRITE))],
    repo: Annotated[HeartbeatRepo, Depends(get_heartbeat_repo)],
    run_repo: Annotated[CronRunRepository, Depends(get_cron_run_repo)],
    query: Annotated[HeartbeatStartQuery, Depends(query_model(HeartbeatStartQuery))],
) -> Response:
    """Record a ``/start`` ping for ``fingerprint``."""
    await _resolve_cron_or_404(repo, fingerprint)
    try:
        _enforce_rate_limit(fingerprint)
    except _RateLimitedError as exc:
        return _rate_limited_response(exc)
    state = await repo.record_start(
        fingerprint,
        who=token.name,
        ip=_client_ip(request),
    )
    # The cron_runs write is a separate transaction from the heartbeats_state
    # write above (intentional, consistent with the existing at-most-once
    # heartbeat contract — a crash between the two is an accepted loss).
    if query.run_id is not None:
        now = utc_now_iso()
        await run_repo.insert_run(
            run_id=query.run_id,
            cron_fingerprint=fingerprint,
            source="wrapper",
            started_at=now,
            vl_window_start=now,
        )
    _log.info(
        "heartbeat.start.received",
        cron_fingerprint=fingerprint,
        run_id=query.run_id,
        streak=state.current_streak,
    )
    return Response(status_code=204)


@router.post("/{fingerprint}/ok", status_code=204)
async def receive_ok(  # noqa: PLR0913
    fingerprint: str,
    request: Request,
    token: Annotated[ApiToken, Depends(require_token_scope(Scope.HEARTBEAT_WRITE))],
    repo: Annotated[HeartbeatRepo, Depends(get_heartbeat_repo)],
    run_repo: Annotated[CronRunRepository, Depends(get_cron_run_repo)],
    query: Annotated[HeartbeatOkQuery, Depends(query_model(HeartbeatOkQuery))],
) -> Response:
    """Record an ``/ok`` ping for ``fingerprint``. Optional ``?duration=<seconds>``.

    NOTE: Prometheus metric emission is deferred to STAGE-002-006 — this
    handler does not touch the prometheus registry. The state write to
    ``heartbeats_state`` is sufficient; the future metric collector reads
    from that table on each scrape.
    """
    await _resolve_cron_or_404(repo, fingerprint)
    try:
        _enforce_rate_limit(fingerprint)
    except _RateLimitedError as exc:
        return _rate_limited_response(exc)
    state = await repo.record_ok(
        fingerprint,
        duration_seconds=query.duration,
        who=token.name,
        ip=_client_ip(request),
    )
    # The cron_runs write is a separate transaction from the heartbeats_state
    # write above (intentional, consistent with the existing at-most-once
    # heartbeat contract — a crash between the two is an accepted loss).
    if query.run_id is not None:
        now = utc_now_iso()
        await run_repo.close_run(
            run_id=query.run_id,
            cron_fingerprint=fingerprint,
            source="wrapper",
            state="ok",
            ended_at=now,
            duration_seconds=query.duration,
            exit_code=query.exit_code if query.exit_code is not None else 0,
            vl_window_end=now,
        )
    _log.info(
        "heartbeat.ok.received",
        cron_fingerprint=fingerprint,
        run_id=query.run_id,
        duration_seconds=query.duration,
        streak=state.current_streak,
    )
    return Response(status_code=204)


@router.post("/{fingerprint}/fail", status_code=204)
async def receive_fail(  # noqa: PLR0913
    fingerprint: str,
    request: Request,
    token: Annotated[ApiToken, Depends(require_token_scope(Scope.HEARTBEAT_WRITE))],
    repo: Annotated[HeartbeatRepo, Depends(get_heartbeat_repo)],
    run_repo: Annotated[CronRunRepository, Depends(get_cron_run_repo)],
    query: Annotated[HeartbeatFailQuery, Depends(query_model(HeartbeatFailQuery))],
) -> Response:
    """Record a ``/fail`` ping for ``fingerprint``. Optional ``?duration``, ``?exit_code``."""
    await _resolve_cron_or_404(repo, fingerprint)
    try:
        _enforce_rate_limit(fingerprint)
    except _RateLimitedError as exc:
        return _rate_limited_response(exc)
    state = await repo.record_fail(
        fingerprint,
        duration_seconds=query.duration,
        exit_code=query.exit_code,
        who=token.name,
        ip=_client_ip(request),
    )
    # The cron_runs write is a separate transaction from the heartbeats_state
    # write above (intentional, consistent with the existing at-most-once
    # heartbeat contract — a crash between the two is an accepted loss).
    if query.run_id is not None:
        now = utc_now_iso()
        await run_repo.close_run(
            run_id=query.run_id,
            cron_fingerprint=fingerprint,
            source="wrapper",
            state="fail",
            ended_at=now,
            duration_seconds=query.duration,
            exit_code=query.exit_code,
            vl_window_end=now,
        )
    _log.info(
        "heartbeat.fail.received",
        cron_fingerprint=fingerprint,
        run_id=query.run_id,
        exit_code=query.exit_code,
        streak=state.current_streak,
    )
    return Response(status_code=204)


@router.post(
    "/{fingerprint}/register",
    response_model=CronOut,
    responses={
        201: {"model": CronOut, "description": "Cron registered"},
        200: {
            "model": CronOut,
            "description": "Cron already registered; wrapper_last_seen_at may be refreshed",
        },
        401: {"description": "Missing or invalid bearer token"},
        403: {"description": "Token lacks HEARTBEAT_WRITE scope"},
        422: {"description": "Body validation failed, fingerprint mismatch, or invalid schedule"},
        429: {"description": "Rate limit exceeded"},
    },
)
async def receive_register(
    fingerprint: str,
    request: Request,
    body: RegisterCronBody,
    token: Annotated[ApiToken, Depends(require_token_scope(Scope.HEARTBEAT_WRITE))],
    cron_repo: Annotated[CronRepo, Depends(get_cron_repo)],
) -> Response:
    """Idempotent wrapper handshake.

    See ``docs/architecture/cron-identity.md`` §2 for the full contract.
    Status code matrix:
    - 201 — new row inserted
    - 200 — row already existed (with or without wrapper_last_seen_at refresh)
    - 422 — fingerprint mismatch OR invalid schedule (detail flag carried)
    - 401 / 403 — auth (handled by ``require_token_scope``)
    - 429 — rate limited
    """
    # 1. URL-vs-body fingerprint check.
    expected = compute_fingerprint(
        host=body.host,
        source_path=body.source_path,
        schedule=body.schedule,
        command=body.command,
    )
    if expected != fingerprint:
        raise HTTPException(
            status_code=422,
            detail={
                "fingerprint_mismatch": True,
                "message": "URL fingerprint does not match body-computed fingerprint",
            },
        )

    # 2. Schedule validation (croniter).
    try:
        canonicalize_schedule(body.schedule)
    except InvalidCronExpression as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "invalid_schedule": True,
                "reason": str(exc),
            },
        ) from exc

    # 3. Rate-limit (shared 60/min per-fingerprint bucket).
    try:
        _enforce_rate_limit(fingerprint)
    except _RateLimitedError as exc:
        return _rate_limited_response(exc)

    # 4. Idempotent upsert.
    record, created = await cron_repo.register_cron(
        body,
        url_fingerprint=fingerprint,
        who=token.name,
        ip=_client_ip(request),
    )

    _log.info(
        "heartbeat.register.received",
        cron_fingerprint=fingerprint,
        created=created,
        wrapper=body.wrapper,
    )

    return _cron_json_response(
        status=201 if created else 200,
        record=record,
    )


def _cron_json_response(*, status: int, record: CronRecord) -> Response:
    """Build a CronOut JSON response with the given HTTP status code."""
    out = cron_record_to_out(record)
    return JSONResponse(status_code=status, content=out.model_dump(mode="json"))


def _rate_limited_response(exc: _RateLimitedError) -> Response:
    """Return a 429 with ``Retry-After`` header and an ErrorEnvelope body."""
    resp = envelope_response(
        status=429,
        code="rate_limited",
        message="too many heartbeat pings for this cron; back off",
        details={"retry_after_seconds": exc.retry_after_seconds},
    )
    resp.headers["Retry-After"] = str(exc.retry_after_seconds)
    return resp
