"""Logs endpoints — query proxy to VictoriaLogs + in-process streams panel."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import structlog
from fastapi import APIRouter, Depends, Query
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.api.dependencies import (
    get_http_client,
    get_log_stream_state,
    get_vl_url,
    require_session,
)
from homelab_monitor.kernel.api.errors import HttpProblem
from homelab_monitor.kernel.api.schemas import (
    LogsQueryResponse,
    LogsStreamsResponse,
)
from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.config import load_vl_query_limits
from homelab_monitor.kernel.logs.models import from_victorialogs_line
from homelab_monitor.kernel.logs.pagination import (
    InvalidCursorError,
    paginate_older,
)
from homelab_monitor.kernel.logs.victorialogs_client import (
    VictoriaLogsClient,
    VictoriaLogsClientError,
)
from homelab_monitor.plugins.collectors.builtin.log_stream_budget import LogStreamState

router = APIRouter()

_MAX_EXPR_LEN = 4096
_DEFAULT_LIMIT = 500
_MAX_LIMIT = 5000
_MAX_RANGE_DAYS = 30


@router.get("/logs/query", response_model=LogsQueryResponse)
async def logs_query(  # noqa: PLR0913
    expr: str = Query(..., description="LogsQL expression"),
    start: str = Query(..., description="ISO-8601 UTC start time"),
    end: str = Query(..., description="ISO-8601 UTC end time"),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    cursor: str | None = Query(None, description="Opaque pagination cursor"),
    _user: User = Depends(require_session()),  # noqa: B008
    vl_url: str = Depends(get_vl_url),
    http_client: httpx.AsyncClient = Depends(get_http_client),  # noqa: B008
) -> LogsQueryResponse:
    """Proxy a LogsQL query to VictoriaLogs.

    Auth: cookie session required. CSRF NOT enforced on GET.

    Surfaces transport / non-200 errors as 502 ``upstream_unavailable`` so the
    frontend can fall back to its synthetic baseline.
    """
    log: BoundLogger = cast(
        BoundLogger,
        structlog.get_logger().bind(component="logs_query"),
    )

    if len(expr) > _MAX_EXPR_LEN:
        raise HttpProblem(
            status_code=400,
            code="invalid_expr",
            message="expression too long",
        )

    # Validate ISO-8601 start/end
    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except ValueError as exc:
        raise HttpProblem(
            status_code=400,
            code="invalid_time_format",
            message="start and end must be ISO-8601 timestamps",
        ) from exc

    # Normalize tzinfo to avoid TypeError on naive vs aware mix.
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=UTC)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=UTC)

    if start_dt >= end_dt:
        raise HttpProblem(
            status_code=400,
            code="invalid_range",
            message="end must be after start",
        )

    if (end_dt - start_dt) > timedelta(days=_MAX_RANGE_DAYS):
        raise HttpProblem(
            status_code=400,
            code="range_too_wide",
            message=f"time range cannot exceed {_MAX_RANGE_DAYS} days",
        )

    base_limits = load_vl_query_limits()
    client = VictoriaLogsClient(vl_url=vl_url, http_client=http_client, limits=base_limits)
    try:
        page = await paginate_older(
            client=client,
            expr=expr,
            window_start=start,
            window_end=end,
            page_size=limit,
            base_limits=base_limits,
            cursor=cursor,
        )
    except InvalidCursorError as exc:
        raise HttpProblem(
            status_code=400,
            code="invalid_cursor",
            message=str(exc),
        ) from exc
    except VictoriaLogsClientError as exc:
        log.warning("logs_query.upstream_error", error=str(exc), expr=expr)
        raise HttpProblem(
            status_code=502,
            code="upstream_unavailable",
            message="victorialogs query failed",
        ) from exc

    lines = [from_victorialogs_line(line) for line in page.lines]
    return LogsQueryResponse(
        lines=lines,
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.get("/logs/streams", response_model=LogsStreamsResponse)
async def logs_streams(
    _user: User = Depends(require_session()),  # noqa: B008
    state: LogStreamState = Depends(get_log_stream_state),  # noqa: B008
) -> LogsStreamsResponse:
    """Return the in-process per-stream summary updated by the budget collector.

    Auth: cookie session required. CSRF NOT enforced on GET.
    """
    # Snapshot the dict to avoid race with collector mid-iteration.
    return LogsStreamsResponse(streams=list(dict(state).values()))
