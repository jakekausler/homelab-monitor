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
from fastapi import APIRouter, Depends, Request
from starlette.responses import Response
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.api.dependencies import (
    get_heartbeat_repo,
    require_token_scope,
)
from homelab_monitor.kernel.api.errors import NotFoundProblem, envelope_response
from homelab_monitor.kernel.auth.models import ApiToken
from homelab_monitor.kernel.auth.scopes import Scope
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
async def receive_start(
    fingerprint: str,
    request: Request,
    token: Annotated[ApiToken, Depends(require_token_scope(Scope.HEARTBEAT_WRITE))],
    repo: Annotated[HeartbeatRepo, Depends(get_heartbeat_repo)],
    _query: Annotated[HeartbeatStartQuery, Depends(query_model(HeartbeatStartQuery))],
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
    _log.info(
        "heartbeat.start.received",
        cron_fingerprint=fingerprint,
        streak=state.current_streak,
    )
    return Response(status_code=204)


@router.post("/{fingerprint}/ok", status_code=204)
async def receive_ok(
    fingerprint: str,
    request: Request,
    token: Annotated[ApiToken, Depends(require_token_scope(Scope.HEARTBEAT_WRITE))],
    repo: Annotated[HeartbeatRepo, Depends(get_heartbeat_repo)],
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
    _log.info(
        "heartbeat.ok.received",
        cron_fingerprint=fingerprint,
        duration_seconds=query.duration,
        streak=state.current_streak,
    )
    return Response(status_code=204)


@router.post("/{fingerprint}/fail", status_code=204)
async def receive_fail(
    fingerprint: str,
    request: Request,
    token: Annotated[ApiToken, Depends(require_token_scope(Scope.HEARTBEAT_WRITE))],
    repo: Annotated[HeartbeatRepo, Depends(get_heartbeat_repo)],
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
    _log.info(
        "heartbeat.fail.received",
        cron_fingerprint=fingerprint,
        exit_code=query.exit_code,
        streak=state.current_streak,
    )
    return Response(status_code=204)


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
