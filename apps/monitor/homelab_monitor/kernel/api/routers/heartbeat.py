"""POST /api/hb/{cron_id}/{start|ok|fail} -- heartbeat receiver.

Auth: API token with ``Scope.HEARTBEAT_WRITE`` (single global token per
Decision 3). Per-cron rate-limiting (Decision 3a) defends against spoofed
ping floods if the global token leaks.

State recorded atomically into ``heartbeats_state`` + mirrored to
``crons.last_seen_state`` + audit row in ONE transaction (Decision 4 mirror,
dual-write through ``HeartbeatRepo._record_state_transition``).

observe-mode crons receiving any of /start, /ok, /fail get the same
recording as heartbeat-mode crons PLUS a structlog warning
``heartbeat.received_in_observe_mode`` (Decision 1a). The receiver does NOT
auto-promote ``integration_mode``.

404 (cron unknown) does NOT write an audit row -- no state change occurred.
429 (rate-limited) does NOT write an audit row -- request never executed.

Prometheus metric emission is DEFERRED to STAGE-002-006: the router does not
touch the prometheus registry in this stage. STAGE-002-006 will add a
collector that reads ``heartbeats_state`` on each scrape and emits
``homelab_heartbeat_seconds_since_last_ok`` alongside the vmalert rules.
"""

from __future__ import annotations

from typing import Annotated, cast

import structlog
from fastapi import APIRouter, Depends, Request
from starlette.responses import Response
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.api.dependencies import (
    get_heartbeat_repo,
    require_token_scope,
)
from homelab_monitor.kernel.api.errors import NotFoundProblem
from homelab_monitor.kernel.auth.models import ApiToken
from homelab_monitor.kernel.auth.scopes import Scope
from homelab_monitor.kernel.heartbeat.rate_limiter import cron_rate_limiter
from homelab_monitor.kernel.heartbeat.repository import HeartbeatRepo
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
    cron_id: str,
) -> CronRecordLike:
    cron = await repo.get_cron(cron_id)
    if cron is None:
        raise NotFoundProblem(message=f"cron not found: {cron_id}")
    return cron


def _enforce_rate_limit(cron_id: str) -> None:
    """Raise a Response-shaped 429 by setting headers on a dedicated exception path.

    We use FastAPI's HTTPException via ``HttpProblem``-equivalent to attach
    ``Retry-After``. Returning a Response from a dependency wouldn't compose;
    raising surfaces through the registered HTTP problem handler.
    """
    allowed, retry_after = cron_rate_limiter.try_acquire(cron_id)
    if allowed:
        return
    # Round retry_after up to the nearest integer second per RFC 7231 §7.1.3
    # (Retry-After is delta-seconds as an integer).
    retry_after_seconds = max(1, int(retry_after) + (1 if retry_after % 1 else 0))
    raise _RateLimitedError(retry_after_seconds=retry_after_seconds)


class _RateLimitedError(Exception):
    """Internal sentinel: raised by the rate limiter, caught by an endpoint-local handler."""

    def __init__(self, *, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("rate limited")


def _log() -> BoundLogger:
    return cast(BoundLogger, structlog.get_logger().bind(component="heartbeat.receiver"))


def _maybe_log_observe_warning(cron: CronRecordLike, endpoint: str) -> None:
    if cron.integration_mode == "observe":
        _log().warning(
            "heartbeat.received_in_observe_mode",
            cron_id=cron.id,
            endpoint=endpoint,
        )


# ---- routes -----------------------------------------------------------------


@router.post("/{cron_id}/start", status_code=204)
async def receive_start(
    cron_id: str,
    request: Request,
    token: Annotated[ApiToken, Depends(require_token_scope(Scope.HEARTBEAT_WRITE))],
    repo: Annotated[HeartbeatRepo, Depends(get_heartbeat_repo)],
    query: Annotated[HeartbeatStartQuery, Depends(query_model(HeartbeatStartQuery))],
) -> Response:
    """Record a ``/start`` ping for ``cron_id``."""
    del query
    cron = await _resolve_cron_or_404(repo, cron_id)
    try:
        _enforce_rate_limit(cron_id)
    except _RateLimitedError as exc:
        return _rate_limited_response(exc)
    _maybe_log_observe_warning(cron, endpoint="start")
    state = await repo.record_start(
        cron_id,
        who=token.name,
        ip=_client_ip(request),
    )
    _log().info(
        "heartbeat.start.received",
        cron_id=cron_id,
        streak=state.current_streak,
    )
    return Response(status_code=204)


@router.post("/{cron_id}/ok", status_code=204)
async def receive_ok(
    cron_id: str,
    request: Request,
    token: Annotated[ApiToken, Depends(require_token_scope(Scope.HEARTBEAT_WRITE))],
    repo: Annotated[HeartbeatRepo, Depends(get_heartbeat_repo)],
    query: Annotated[HeartbeatOkQuery, Depends(query_model(HeartbeatOkQuery))],
) -> Response:
    """Record an ``/ok`` ping for ``cron_id``. Optional ``?duration=<seconds>``.

    NOTE: Prometheus metric emission is deferred to STAGE-002-006 — this
    handler does not touch the prometheus registry. The state write to
    ``heartbeats_state`` is sufficient; the future metric collector reads
    from that table on each scrape.
    """
    cron = await _resolve_cron_or_404(repo, cron_id)
    try:
        _enforce_rate_limit(cron_id)
    except _RateLimitedError as exc:
        return _rate_limited_response(exc)
    _maybe_log_observe_warning(cron, endpoint="ok")
    state = await repo.record_ok(
        cron_id,
        duration_seconds=query.duration,
        who=token.name,
        ip=_client_ip(request),
    )
    _log().info(
        "heartbeat.ok.received",
        cron_id=cron_id,
        duration_seconds=query.duration,
        streak=state.current_streak,
    )
    return Response(status_code=204)


@router.post("/{cron_id}/fail", status_code=204)
async def receive_fail(
    cron_id: str,
    request: Request,
    token: Annotated[ApiToken, Depends(require_token_scope(Scope.HEARTBEAT_WRITE))],
    repo: Annotated[HeartbeatRepo, Depends(get_heartbeat_repo)],
    query: Annotated[HeartbeatFailQuery, Depends(query_model(HeartbeatFailQuery))],
) -> Response:
    """Record a ``/fail`` ping for ``cron_id``. Optional ``?duration``, ``?exit_code``."""
    cron = await _resolve_cron_or_404(repo, cron_id)
    try:
        _enforce_rate_limit(cron_id)
    except _RateLimitedError as exc:
        return _rate_limited_response(exc)
    _maybe_log_observe_warning(cron, endpoint="fail")
    state = await repo.record_fail(
        cron_id,
        duration_seconds=query.duration,
        exit_code=query.exit_code,
        who=token.name,
        ip=_client_ip(request),
    )
    _log().info(
        "heartbeat.fail.received",
        cron_id=cron_id,
        exit_code=query.exit_code,
        streak=state.current_streak,
    )
    return Response(status_code=204)


def _rate_limited_response(exc: _RateLimitedError) -> Response:
    """Return a 429 with ``Retry-After`` header."""
    return Response(
        status_code=429,
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )


# Forward-reference shim. ``CronRecordLike`` is just ``CronRecord`` from the
# repository module; declared here so the helper signatures above stay
# compact without yet another import-order import.
from homelab_monitor.kernel.heartbeat.repository import CronRecord as CronRecordLike  # noqa: E402
