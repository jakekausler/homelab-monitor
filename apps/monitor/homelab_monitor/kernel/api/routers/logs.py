"""Logs endpoints — query proxy to VictoriaLogs + in-process streams panel."""

from __future__ import annotations

from typing import Any, cast

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
    LogsQueryEntry,
    LogsQueryResponse,
    LogsStreamsResponse,
)
from homelab_monitor.kernel.auth.models import User
from homelab_monitor.plugins.collectors.builtin.log_stream_budget import LogStreamState

router = APIRouter()

_VL_TIMEOUT_S = 5.0
_HTTP_OK = 200
_MAX_EXPR_LEN = 4096
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 5000


@router.get("/logs/query", response_model=LogsQueryResponse)
async def logs_query(  # noqa: PLR0913
    expr: str = Query(..., description="LogsQL expression"),
    start: str = Query(..., description="ISO-8601 UTC start time"),
    end: str = Query(..., description="ISO-8601 UTC end time"),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
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

    params = {"query": expr, "start": start, "end": end, "limit": str(limit)}
    try:
        resp = await http_client.get(
            f"{vl_url}/select/logsql/query",
            params=params,
            timeout=_VL_TIMEOUT_S,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning("logs_query.upstream_error", error=str(exc), expr=expr)
        raise HttpProblem(
            status_code=502,
            code="upstream_unavailable",
            message="victorialogs query failed",
        ) from exc

    if resp.status_code != _HTTP_OK:
        log.warning(
            "logs_query.upstream_status",
            status=resp.status_code,
            body=resp.text[:200],
        )
        raise HttpProblem(
            status_code=502,
            code="upstream_unavailable",
            message=f"victorialogs returned status {resp.status_code}",
        )

    # VL /select/logsql/query returns NDJSON: one JSON object per line.
    # Parse each line; tolerate empty lines + malformed entries (skip them).
    entries: list[LogsQueryEntry] = []
    for raw_line in resp.text.splitlines():
        if not raw_line.strip():
            continue
        try:
            import json  # noqa: PLC0415

            obj_raw: object = json.loads(raw_line)
        except ValueError:
            continue
        if not isinstance(obj_raw, dict):
            continue
        obj = cast(dict[str, Any], obj_raw)
        stream = str(obj.get("_stream_id", ""))
        line = str(obj.get("_msg", ""))
        ts = str(obj.get("_time", ""))
        fields: dict[str, str] = {
            k: str(v) for k, v in obj.items() if k not in {"_stream_id", "_msg", "_time"}
        }
        entries.append(LogsQueryEntry(stream=stream, line=line, ts=ts, fields=fields))

    return LogsQueryResponse(entries=entries, next_cursor=None)


@router.get("/logs/streams", response_model=LogsStreamsResponse)
async def logs_streams(
    _user: User = Depends(require_session()),  # noqa: B008
    state: LogStreamState = Depends(get_log_stream_state),  # noqa: B008
) -> LogsStreamsResponse:
    """Return the in-process per-stream summary updated by the budget collector.

    Auth: cookie session required. CSRF NOT enforced on GET.
    """
    return LogsStreamsResponse(streams=list(state.values()))
