"""Logs endpoints — query proxy to VictoriaLogs + in-process streams panel."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Literal, cast
from uuid import uuid4

import httpx
import structlog
from fastapi import APIRouter, Depends, Query, status
from starlette.responses import StreamingResponse
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.api.dependencies import (
    get_cycle_status_store,
    get_drain_consumer,
    get_http_client,
    get_log_stream_state,
    get_log_window_fetcher,
    get_metrics_writer,
    get_repo,
    get_tail_registry,
    get_vl_url,
    require_session,
)
from homelab_monitor.kernel.api.errors import (
    ConflictProblem,
    HttpProblem,
    NotFoundProblem,
    ServiceUnavailableProblem,
)
from homelab_monitor.kernel.api.schemas import (
    AnnotationCreateRequest,
    AnnotationListResponse,
    AnnotationResponse,
    DrainCycleResultResponse,
    LastCycleResponse,
    LogsFieldsResponse,
    LogsHistogramResponse,
    LogsQueryResponse,
    LogsServicesResponse,
    LogsStreamsResponse,
    LogWindowResponse,
    ModelDetailResponse,
    ModelListResponse,
    ModelSummary,
    ModelTemplateEntry,
    RefreshCycleResponse,
    RefreshStatusResponse,
    SavedQueriesListResponse,
    SavedQueryResponse,
    SavedServiceIdentity,
    SaveQueryCreateRequest,
    SaveQueryRenameRequest,
    SignatureListResponse,
    SignaturePatchRequest,
    SignatureResponse,
    SignatureSamplesResponse,
    SilenceAllowlistCreateRequest,
    SilenceAllowlistListResponse,
    SilenceAllowlistResponse,
)
from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.config import load_tail_config, load_vl_query_limits
from homelab_monitor.kernel.db.repository import SqliteRepository

if TYPE_CHECKING:
    from homelab_monitor.kernel.plugins.io import MetricsWriter
from homelab_monitor.kernel.logs.cycle_status import CycleStatusStore
from homelab_monitor.kernel.logs.drain_consumer import (
    CycleInProgressError,
    DrainConsumer,
)
from homelab_monitor.kernel.logs.drain_persistence import ModelSummaryRow
from homelab_monitor.kernel.logs.export import stream_export
from homelab_monitor.kernel.logs.fields import (
    FieldsCache,
    fetch_fields,
)
from homelab_monitor.kernel.logs.histogram import (
    HistogramCache,
    fetch_histogram,
)
from homelab_monitor.kernel.logs.log_window_fetcher import LogWindowFetcher
from homelab_monitor.kernel.logs.models import LogLine, from_victorialogs_line
from homelab_monitor.kernel.logs.pagination import (
    InvalidCursorError,
    paginate_older,
)
from homelab_monitor.kernel.logs.saved_queries_repo import (
    DuplicateNameError,
    SavedQueriesRepository,
    SavedQueryRow,
)
from homelab_monitor.kernel.logs.services import (
    ServicesCache,
    fetch_services,
)
from homelab_monitor.kernel.logs.signature_annotations_repo import (
    Annotation,
    AnnotationsRepository,
)
from homelab_monitor.kernel.logs.signatures_repo import (
    Signature,
    SignatureFilter,
    SignaturesRepository,
)
from homelab_monitor.kernel.logs.silence_allowlist_repo import (
    SilenceAllowlistEntry,
    SilenceAllowlistRepository,
)
from homelab_monitor.kernel.logs.tail_service import (
    DroppedEvent,
    ErrorEvent,
    LineEvent,
    TailRegistry,
    TailSession,
)
from homelab_monitor.kernel.logs.time_window import parse_and_validate_window
from homelab_monitor.kernel.logs.victorialogs_client import (
    VictoriaLogsClient,
    VictoriaLogsClientError,
    logsql_quote_phrase,
)
from homelab_monitor.plugins.collectors.builtin.log_stream_budget import LogStreamState

router = APIRouter()

_MAX_EXPR_LEN = 4096
_DEFAULT_LIMIT = 500
_MAX_LIMIT = 5000
# Range validation (ISO parse, start<end, no-future, ≤30d) lives in
# kernel.logs.time_window.parse_and_validate_window, shared with the docker logs
# endpoint.

_SERVICES_DEFAULT_LIMIT = 100
_SERVICES_MIN_LIMIT = 1
_SERVICES_MAX_LIMIT = 1000

_FIELDS_DEFAULT_SAMPLE = 200
_FIELDS_MIN_SAMPLE = 1
_FIELDS_MAX_SAMPLE = 2000

_HISTOGRAM_DEFAULT_BUCKETS = 60
_HISTOGRAM_MIN_BUCKETS = 1
_HISTOGRAM_MAX_BUCKETS = 500

_SURROUNDING_WINDOW_S = 1800  # per-side window for /logs/window (D-B)
_SURROUNDING_DEFAULT_COUNT = 100
_SURROUNDING_MIN_COUNT = 1
_SURROUNDING_MAX_COUNT = 500

_EXPORT_DEFAULT_MAX = 10000
_EXPORT_MIN_MAX = 1
_EXPORT_MAX_MAX = 100000

_TAIL_PROBE_WINDOW_S = 1  # the pre-flight probe queries [now-1s, now]
_TAIL_RETRY_AFTER_S = 60  # Retry-After when the global cap is hit
_HTTP_CLIENT_4XX_LO = 400
_HTTP_CLIENT_4XX_HI = 500

# Process-wide 30s TTL cache keyed on (start, end, limit). Module-scoped so it
# survives across requests within a worker. Clock injectable only in tests via
# the module-level rebind (see test).
_services_cache = ServicesCache()

# Process-wide 30s TTL cache for /logs/fields, keyed on
# (sha256(effective_expr), start, end, sample_n). Module-scoped; rebind in tests.
_fields_cache = FieldsCache()

# Process-wide 30s TTL cache for /logs/histogram, keyed on
# (sha256(effective_expr), start, end, buckets). Module-scoped; rebind in tests.
_histogram_cache = HistogramCache()

# Strong refs to fire-and-forget refresh tasks so they are NOT garbage-collected
# mid-flight (asyncio only holds a weak ref to created tasks). The done-callback
# discards each task once it finishes.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


async def _run_and_record(
    consumer: DrainConsumer,
    store: CycleStatusStore,
    cycle_id: str,
    log: BoundLogger,
) -> None:
    """Run one drain cycle and record its outcome into the status store."""
    try:
        result = await consumer.run_once()
    except CycleInProgressError:
        store.fail(cycle_id, "cycle_in_progress")
    except Exception as exc:
        store.fail(cycle_id, str(exc))
        log.warning("logs_signatures_refresh.cycle_failed", cycle_id=cycle_id, error=str(exc))
    else:
        store.complete(cycle_id, result)


def _compose_services_expr(expr: str, services_csv: str | None) -> str:
    """AND an identity-qualified `(service:… AND source_type:…)` clause onto expr.

    STAGE-004-012A: `services_csv` is a CSV of `<source_type>:<service>` entries
    (e.g. ``docker:nginx,cron:hmrun``). Each entry is split on the FIRST ``:`` —
    the service name may itself contain ``:`` but source_type never does. Each
    half is escaped via the canonical ``logsql_quote_phrase``. Identities are
    OR'd; the OR-group is AND'd with the user's expr (passed through VERBATIM,
    wrapped in parens). Empty/absent/all-malformed `services_csv` returns `expr`
    unchanged (byte-identical). Malformed entries (no ``:``, empty source_type or
    empty service) are skipped.
    """
    if services_csv is None:
        return expr
    entries = [s for s in (part.strip() for part in services_csv.split(",")) if s]
    clauses: list[str] = []
    for entry in entries:
        source_type, sep, service = entry.partition(":")
        if not sep or not source_type or not service:
            continue  # malformed: no colon, or empty half
        svc_q = logsql_quote_phrase(service)
        st_q = logsql_quote_phrase(source_type)
        clauses.append(f"service:{svc_q} AND source_type:{st_q}")
        if len(clauses) >= _SERVICES_MAX_LIMIT:
            break
    if not clauses:
        return expr
    if len(clauses) == 1:
        return f"({clauses[0]}) AND ({expr})"
    or_clause = " OR ".join(f"({c})" for c in clauses)
    return f"({or_clause}) AND ({expr})"


@router.get("/logs/query", response_model=LogsQueryResponse)
async def logs_query(  # noqa: PLR0913
    expr: str = Query(..., description="LogsQL expression"),
    start: str = Query(..., description="ISO-8601 UTC start time"),
    end: str = Query(..., description="ISO-8601 UTC end time"),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    cursor: str | None = Query(None, description="Opaque pagination cursor"),
    services: str | None = Query(None, description="CSV of <source_type>:<service> identities"),
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

    # Validate ISO-8601 start/end via shared helper (STAGE-004-008 extraction).
    # Raises HttpProblem(400, ...) with identical code/message as before.
    parse_and_validate_window(start, end)

    effective_expr = _compose_services_expr(expr, services)

    base_limits = load_vl_query_limits()
    client = VictoriaLogsClient(vl_url=vl_url, http_client=http_client, limits=base_limits)
    try:
        page = await paginate_older(
            client=client,
            expr=effective_expr,
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


def _merge_window_lines(
    before: list[LogLine],
    after: list[LogLine],
) -> list[LogLine]:
    """Merge the before-side + after-side line lists.

    Dedup by (timestamp, stream, message) (D-D); a line present in both sides is
    kept once. Sort ascending by timestamp (D-E); ISO UTC strings sort
    lexicographically. `sorted` is stable, so equal-timestamp ties preserve
    before-then-after insertion order.
    """
    seen: set[tuple[str, str, str]] = set()
    merged: list[LogLine] = []
    for line in (*before, *after):
        key = (line.timestamp, line.stream, line.message)
        if key in seen:
            continue
        seen.add(key)
        merged.append(line)
    merged.sort(key=lambda ln: ln.timestamp)
    return merged


def _locate_anchor_index(
    lines: list[LogLine],
    anchor_ts_iso: str,
    anchor_stream: str | None,
    anchor_message: str | None,
) -> int | None:
    """Find the anchor's position in the merged+sorted lines (D-C).

    1. Exact match on (timestamp, stream, message) when stream+message given.
    2. Else first line with timestamp >= anchor_ts_iso (insertion point).
    3. None when no line qualifies (empty list, or all lines precede anchor).
    """
    if not lines:
        return None
    if anchor_stream is not None and anchor_message is not None:
        for i, line in enumerate(lines):
            if (
                line.timestamp == anchor_ts_iso
                and line.stream == anchor_stream
                and line.message == anchor_message
            ):
                return i
    for i, line in enumerate(lines):
        if line.timestamp >= anchor_ts_iso:
            return i
    return None


@router.get("/logs/window", response_model=LogWindowResponse)
async def logs_window(  # noqa: PLR0913
    anchor_ts: str = Query(..., description="ISO-8601 UTC anchor timestamp"),
    anchor_stream: str | None = Query(
        None, description="Anchor line stream (exact identification)"
    ),
    anchor_message: str | None = Query(
        None, description="Anchor line message (exact identification)"
    ),
    expr: str = Query("*", description="Base LogsQL expression"),
    service: str | None = Query(None, description="Scope to this service name"),
    source_type: str | None = Query(None, description="Scope source_type for `service`"),
    before: int = Query(
        _SURROUNDING_DEFAULT_COUNT, ge=_SURROUNDING_MIN_COUNT, le=_SURROUNDING_MAX_COUNT
    ),
    after: int = Query(
        _SURROUNDING_DEFAULT_COUNT, ge=_SURROUNDING_MIN_COUNT, le=_SURROUNDING_MAX_COUNT
    ),
    _user: User = Depends(require_session()),  # noqa: B008
    fetcher: LogWindowFetcher = Depends(get_log_window_fetcher),  # noqa: B008
) -> LogWindowResponse:
    """Anchor-centered surrounding-logs window (STAGE-004-031A).

    Calls LogWindowFetcher.fetch() TWICE: before-side (window_after_s=0, nearest N
    before) and after-side (window_before_s=0, nearest N after), then merges +
    dedupes + sorts ascending. Scope: when `service` is given, AND an identity
    clause via _compose_services_expr; otherwise base expr unchanged.

    Degraded (VL down) → 200 with degraded=true, lines=[] (NEVER 500). Auth:
    cookie session required. CSRF NOT enforced on GET.
    """
    if len(expr) > _MAX_EXPR_LEN:
        raise HttpProblem(
            status_code=400,
            code="invalid_expr",
            message="expression too long",
        )

    # Parse the anchor timestamp. Reuse the same ISO contract as the window
    # validator: a bad timestamp is a 400 invalid_time_format.
    try:
        anchor_dt = datetime.fromisoformat(anchor_ts.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HttpProblem(
            status_code=400,
            code="invalid_time_format",
            message="anchor_ts is not a valid ISO-8601 timestamp",
        ) from exc

    # Scope: build the services CSV only when a service name is given.
    if service is not None:
        st = source_type if source_type is not None else "unknown"
        services_csv = f"{st}:{service}"
        effective_expr = _compose_services_expr(expr, services_csv)
    else:
        effective_expr = expr

    before_result = await fetcher.fetch(
        effective_expr,
        anchor_dt,
        window_before_s=_SURROUNDING_WINDOW_S,
        window_after_s=0,
        limit=before,
    )
    after_result = await fetcher.fetch(
        effective_expr,
        anchor_dt,
        window_before_s=0,
        window_after_s=_SURROUNDING_WINDOW_S,
        limit=after,
    )

    merged = _merge_window_lines(before_result.lines, after_result.lines)

    # Find the anchor's position in the merged+sorted lines. Use the raw
    # anchor_ts param so exact-match comparisons match the strings from VL.
    anchor_index = _locate_anchor_index(merged, anchor_ts, anchor_stream, anchor_message)

    return LogWindowResponse(
        lines=merged,
        truncated_before=before_result.truncated,
        truncated_after=after_result.truncated,
        degraded=before_result.degraded or after_result.degraded,
        anchor_index=anchor_index,
        window_start=before_result.window_start,
        window_end=after_result.window_end,
        queried_at=after_result.queried_at,
    )


@router.get(
    "/logs/export",
    responses={200: {"content": {"text/plain": {}, "application/json": {}}}},
)
async def logs_export(  # noqa: PLR0913
    expr: str = Query(..., description="LogsQL expression"),
    start: str = Query(..., description="ISO-8601 UTC start time"),
    end: str = Query(..., description="ISO-8601 UTC end time"),
    fmt: Literal["txt", "json"] = Query("txt", alias="format", description="Export format"),
    # Out-of-range max is REJECTED with 422 (FastAPI ge/le), not clamped.
    max: int = Query(_EXPORT_DEFAULT_MAX, ge=_EXPORT_MIN_MAX, le=_EXPORT_MAX_MAX),
    services: str | None = Query(None, description="CSV of <source_type>:<service> identities"),
    _user: User = Depends(require_session()),  # noqa: B008
    vl_url: str = Depends(get_vl_url),
    http_client: httpx.AsyncClient = Depends(get_http_client),  # noqa: B008
) -> StreamingResponse:
    """Stream matching log lines to the browser as a download (STAGE-004-020).

    True streaming: opens a single VictoriaLogs streaming query and pipes lines
    out one at a time (O(1) memory). ``format`` is "txt" (human-readable) or
    "json" (a JSON array of LogLine objects). ``max`` caps the number of lines
    (default 10000, range [1, 100000]).

    A pre-flight pulls the FIRST line inside the handler so a VictoriaLogs error
    surfaces as HTTP 502 ``upstream_unavailable`` BEFORE the 200 StreamingResponse
    headers are committed (after headers are sent we can no longer change status).

    Auth: cookie session required. CSRF NOT enforced on GET. Same window-validation
    + scope-composition as /api/logs/query. Maps VictoriaLogsClientError -> 502.
    """
    log: BoundLogger = cast(
        BoundLogger,
        structlog.get_logger().bind(component="logs_export"),
    )

    if len(expr) > _MAX_EXPR_LEN:
        raise HttpProblem(
            status_code=400,
            code="invalid_expr",
            message="expression too long",
        )

    parse_and_validate_window(start, end)

    effective_expr = _compose_services_expr(expr, services)

    base_limits = load_vl_query_limits()
    client = VictoriaLogsClient(vl_url=vl_url, http_client=http_client, limits=base_limits)

    # Map VlLogLine -> LogLine lazily as lines arrive (keeps O(1) memory).
    async def _mapped() -> AsyncGenerator[LogLine, None]:
        async for vl_line in client.stream_query(
            expr=effective_expr, start=start, end=end, limit=max
        ):
            yield from_victorialogs_line(vl_line)

    source = _mapped()

    # Pre-flight: pull the first line INSIDE the handler so a VL error becomes a
    # 502 BEFORE we return the 200 StreamingResponse. A sentinel distinguishes
    # "no lines" (valid empty result) from "first line present".
    _SENTINEL = object()
    try:
        first: LogLine | object = await anext(source, _SENTINEL)
    except VictoriaLogsClientError as exc:
        await source.aclose()
        log.warning("logs_export.upstream_error", error=str(exc))
        raise HttpProblem(
            status_code=502,
            code="upstream_unavailable",
            message="victorialogs export query failed",
        ) from exc
    except BaseException:
        await source.aclose()
        raise

    # Re-chain the already-pulled first line in front of the remainder.
    async def _chained() -> AsyncIterator[LogLine]:
        if first is not _SENTINEL:
            yield cast(LogLine, first)
        async for line in source:
            yield line

    ext = "json" if fmt == "json" else "txt"
    media_type = "application/json" if fmt == "json" else "text/plain; charset=utf-8"
    filename = f"logs_{datetime.now(UTC).strftime('%Y-%m-%d_%H%M%S')}Z.{ext}"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        stream_export(_chained(), fmt),
        media_type=media_type,
        headers=headers,
    )


@router.get("/logs/tail")
async def logs_tail(  # noqa: PLR0913
    expr: str = Query(..., description="LogsQL expression"),
    services: str | None = Query(None, description="CSV of <source_type>:<service> identities"),
    _user: User = Depends(require_session()),  # noqa: B008
    vl_url: str = Depends(get_vl_url),
    http_client: httpx.AsyncClient = Depends(get_http_client),  # noqa: B008
    registry: TailRegistry = Depends(get_tail_registry),  # noqa: B008
    metrics_writer: MetricsWriter = Depends(get_metrics_writer),  # noqa: B008
) -> StreamingResponse:
    """Live-tail matching log lines as Server-Sent Events (STAGE-004-023).

    Polls VictoriaLogs ~1s and pushes NEW lines as `event: line` SSE events.
    Enforces a global connection cap (503 + Retry-After), per-second
    backpressure (`event: dropped`), and a per-connection duration cap.

    Strict ordering: cap-check (503) -> LogsQL probe (422 bad / 502 VL-down) ->
    200 stream. The registry slot is acquired BEFORE the probe and released on
    probe failure or in gen()'s finally (never both).

    Auth: cookie session required. CSRF NOT enforced on GET.
    """
    log: BoundLogger = cast(
        BoundLogger,
        structlog.get_logger().bind(component="logs_tail"),
    )

    if len(expr) > _MAX_EXPR_LEN:
        raise HttpProblem(status_code=400, code="invalid_expr", message="expression too long")

    effective_expr = _compose_services_expr(expr, services)
    base_limits = load_vl_query_limits()
    tail_config = load_tail_config()
    client = VictoriaLogsClient(vl_url=vl_url, http_client=http_client, limits=base_limits)

    # 1. Global cap (503 + Retry-After) — acquire BEFORE probing.
    if not registry.try_acquire():
        raise ServiceUnavailableProblem(
            message="tail connection limit reached",
            code="tail_capacity",
            details={"retry_after_seconds": _TAIL_RETRY_AFTER_S},
        )

    # 2. Pre-flight probe: one bounded query over [now-1s, now]. Maps VL 4xx ->
    #    422 invalid_logsql, VL 5xx/transport -> 502 upstream_unavailable. ALWAYS
    #    release the slot on any probe failure (prevents a slot leak).
    now = datetime.now(UTC)
    probe_start = (now - timedelta(seconds=_TAIL_PROBE_WINDOW_S)).isoformat()
    probe_end = now.isoformat()
    try:
        await client.query(expr=effective_expr, start=probe_start, end=probe_end)
    except VictoriaLogsClientError as exc:
        registry.release()
        sc = exc.status_code
        if sc is not None and _HTTP_CLIENT_4XX_LO <= sc < _HTTP_CLIENT_4XX_HI:
            raise HttpProblem(
                status_code=422,
                code="invalid_logsql",
                message="invalid LogsQL expression",
            ) from exc
        log.warning("logs_tail.upstream_error", error=str(exc))
        raise HttpProblem(
            status_code=502,
            code="upstream_unavailable",
            message="victorialogs tail probe failed",
        ) from exc
    except BaseException:
        registry.release()
        raise

    # 3. Build the session + StreamingResponse. Slot is released ONLY in gen()'s
    #    finally from here on (probe succeeded).
    session = TailSession(
        vl_client=client,
        expr=effective_expr,
        config=tail_config,
        metrics_writer=metrics_writer,
        clock=lambda: datetime.now(UTC),
    )

    async def gen() -> AsyncGenerator[bytes, None]:
        seq = 0
        try:
            async for ev in session.events():
                if isinstance(ev, LineEvent):
                    seq += 1
                    payload = ev.line.model_dump_json()
                    yield f"event: line\ndata: {payload}\nid: {seq}\n\n".encode()
                elif isinstance(ev, DroppedEvent):
                    yield f'event: dropped\ndata: {{"count":{ev.count}}}\n\n'.encode()
                elif isinstance(ev, ErrorEvent):
                    err = json.dumps(
                        {"code": ev.code, "message": ev.message},
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    yield f"event: error\ndata: {err}\n\n".encode()
                else:  # KeepaliveEvent
                    yield b": keepalive\n\n"
        finally:
            registry.release()

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Content-Type": "text/event-stream; charset=utf-8",
    }
    return StreamingResponse(gen(), headers=headers, media_type="text/event-stream")


@router.get("/logs/services", response_model=LogsServicesResponse)
async def logs_services(
    start: str = Query(..., description="ISO-8601 UTC start time"),
    end: str = Query(..., description="ISO-8601 UTC end time"),
    limit: int = Query(_SERVICES_DEFAULT_LIMIT, ge=_SERVICES_MIN_LIMIT, le=_SERVICES_MAX_LIMIT),
    _user: User = Depends(require_session()),  # noqa: B008
    vl_url: str = Depends(get_vl_url),
    http_client: httpx.AsyncClient = Depends(get_http_client),  # noqa: B008
) -> LogsServicesResponse:
    """Distinct `service` values + line counts over [start, end], for the
    stream-picker sidebar (STAGE-004-012).

    FORWARD-COMPAT: STAGE-004-018's /api/logs/fields will generalize distinct-
    value+count discovery and may absorb/replace this endpoint. Do not couple
    new callers beyond the stream picker.

    Auth: cookie session required. CSRF NOT enforced on GET. Same window-
    validation rules as /api/logs/query.
    """
    log: BoundLogger = cast(
        BoundLogger,
        structlog.get_logger().bind(component="logs_services"),
    )
    parse_and_validate_window(start, end)

    key = (start, end, limit)
    cached = _services_cache.get(key)
    if cached is not None:
        return cached

    base_limits = load_vl_query_limits()
    client = VictoriaLogsClient(vl_url=vl_url, http_client=http_client, limits=base_limits)
    try:
        response = await fetch_services(client=client, start=start, end=end, limit=limit)
    except VictoriaLogsClientError as exc:
        log.warning("logs_services.upstream_error", error=str(exc))
        raise HttpProblem(
            status_code=502,
            code="upstream_unavailable",
            message="victorialogs stats query failed",
        ) from exc

    _services_cache.put(key, response)
    return response


@router.get("/logs/fields", response_model=LogsFieldsResponse)
async def logs_fields(  # noqa: PLR0913
    expr: str = Query(..., description="LogsQL expression"),
    start: str = Query(..., description="ISO-8601 UTC start time"),
    end: str = Query(..., description="ISO-8601 UTC end time"),
    # Out-of-range sample_n is REJECTED with 422 (FastAPI Query ge/le validation),
    # not clamped. Valid range: [1, 2000].
    sample_n: int = Query(_FIELDS_DEFAULT_SAMPLE, ge=_FIELDS_MIN_SAMPLE, le=_FIELDS_MAX_SAMPLE),
    services: str | None = Query(None, description="CSV of <source_type>:<service> identities"),
    _user: User = Depends(require_session()),  # noqa: B008
    vl_url: str = Depends(get_vl_url),
    http_client: httpx.AsyncClient = Depends(get_http_client),  # noqa: B008
) -> LogsFieldsResponse:
    """Discover fields present in the current query scope (STAGE-004-018).

    Hybrid: VL ``field_names`` (authoritative names + exact coverage) + a bounded
    most-recent ``query`` sample (values + type hints). Same scope-composition +
    window-validation as /api/logs/query. Maps VictoriaLogsClientError → 502
    ``upstream_unavailable``.

    Auth: cookie session required. CSRF NOT enforced on GET.
    """
    log: BoundLogger = cast(
        BoundLogger,
        structlog.get_logger().bind(component="logs_fields"),
    )

    if len(expr) > _MAX_EXPR_LEN:
        raise HttpProblem(
            status_code=400,
            code="invalid_expr",
            message="expression too long",
        )

    parse_and_validate_window(start, end)

    effective_expr = _compose_services_expr(expr, services)

    key = FieldsCache.make_key(expr=effective_expr, start=start, end=end, sample_n=sample_n)
    cached = _fields_cache.get(key)
    if cached is not None:
        return cached

    base_limits = load_vl_query_limits()
    client = VictoriaLogsClient(vl_url=vl_url, http_client=http_client, limits=base_limits)
    try:
        response = await fetch_fields(
            client=client,
            expr=effective_expr,
            start=start,
            end=end,
            sample_n=sample_n,
        )
    except VictoriaLogsClientError as exc:
        log.warning("logs_fields.upstream_error", error=str(exc))
        raise HttpProblem(
            status_code=502,
            code="upstream_unavailable",
            message="victorialogs field discovery failed",
        ) from exc

    _fields_cache.put(key, response)
    return response


@router.get("/logs/histogram", response_model=LogsHistogramResponse)
async def logs_histogram(  # noqa: PLR0913
    expr: str = Query(..., description="LogsQL expression"),
    start: str = Query(..., description="ISO-8601 UTC start time"),
    end: str = Query(..., description="ISO-8601 UTC end time"),
    # Out-of-range buckets is REJECTED with 422 (FastAPI ge/le), not clamped.
    buckets: int = Query(
        _HISTOGRAM_DEFAULT_BUCKETS, ge=_HISTOGRAM_MIN_BUCKETS, le=_HISTOGRAM_MAX_BUCKETS
    ),
    services: str | None = Query(None, description="CSV of <source_type>:<service> identities"),
    _user: User = Depends(require_session()),  # noqa: B008
    vl_url: str = Depends(get_vl_url),
    http_client: httpx.AsyncClient = Depends(get_http_client),  # noqa: B008
) -> LogsHistogramResponse:
    """Severity-stacked log-density histogram over [start, end] (STAGE-004-019).

    ONE VictoriaLogs ``/select/logsql/hits?field=severity`` call, re-binned onto
    START-aligned buckets + coarse-mapped to error/warn/info. Same scope-
    composition + window-validation as /api/logs/query. Maps
    VictoriaLogsClientError -> 502 ``upstream_unavailable``.

    Auth: cookie session required. CSRF NOT enforced on GET.
    """
    log: BoundLogger = cast(
        BoundLogger,
        structlog.get_logger().bind(component="logs_histogram"),
    )

    if len(expr) > _MAX_EXPR_LEN:
        raise HttpProblem(
            status_code=400,
            code="invalid_expr",
            message="expression too long",
        )

    parse_and_validate_window(start, end)

    effective_expr = _compose_services_expr(expr, services)

    key = HistogramCache.make_key(expr=effective_expr, start=start, end=end, buckets=buckets)
    cached = _histogram_cache.get(key)
    if cached is not None:
        return cached

    base_limits = load_vl_query_limits()
    client = VictoriaLogsClient(vl_url=vl_url, http_client=http_client, limits=base_limits)
    try:
        response = await fetch_histogram(
            client=client,
            expr=effective_expr,
            start=start,
            end=end,
            buckets=buckets,
        )
    except VictoriaLogsClientError as exc:
        log.warning("logs_histogram.upstream_error", error=str(exc))
        raise HttpProblem(
            status_code=502,
            code="upstream_unavailable",
            message="victorialogs histogram query failed",
        ) from exc

    _histogram_cache.put(key, response)
    return response


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


@router.post(
    "/logs/signatures/refresh",
    response_model=RefreshCycleResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def refresh_signatures(
    _user: Annotated[User, Depends(require_session())],
    consumer: Annotated[DrainConsumer, Depends(get_drain_consumer)],
    store: Annotated[CycleStatusStore, Depends(get_cycle_status_store)],
) -> RefreshCycleResponse:
    """Trigger one drain cycle out of band; returns a cycle_id to poll.

    202 + cycle_id on accept. 409 if a cycle is already running. 503 if the drain
    consumer is not running (drain disabled). Auth: session required; CSRF enforced.
    """
    log: BoundLogger = cast(
        BoundLogger,
        structlog.get_logger().bind(component="logs_signatures_refresh"),
    )
    if consumer.is_cycle_running():
        raise ConflictProblem(
            message="a drain cycle is already running",
            details={"cycle_started_at": consumer.cycle_started_at},
        )
    cycle_id = uuid4().hex
    store.begin(cycle_id)
    task = asyncio.create_task(_run_and_record(consumer, store, cycle_id, log))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return RefreshCycleResponse(cycle_id=cycle_id)


@router.get(
    "/logs/signatures/refresh/{cycle_id}",
    response_model=RefreshStatusResponse,
)
async def get_refresh_status(
    cycle_id: str,
    _user: Annotated[User, Depends(require_session())],
    store: Annotated[CycleStatusStore, Depends(get_cycle_status_store)],
) -> RefreshStatusResponse:
    """Poll a manually-triggered drain cycle. 404 if unknown or expired.

    Auth: session required. CSRF NOT enforced on GET.
    """
    entry = store.get(cycle_id)
    if entry is None:
        raise NotFoundProblem(message=f"unknown or expired cycle: {cycle_id}")
    result: DrainCycleResultResponse | None = None
    if entry.result is not None:
        result = DrainCycleResultResponse(
            started_at=entry.result.started_at,
            finished_at=entry.result.finished_at,
            lines_processed=entry.result.lines_processed,
            new_templates=entry.result.new_templates,
            models_touched=entry.result.models_touched,
            cycle_status=entry.result.cycle_status,
            error=entry.result.error,
        )
    return RefreshStatusResponse(status=entry.status, result=result, error=entry.error)


_SAMPLES_WINDOW_HOURS = 24
_SAMPLES_DEFAULT_LIMIT = 10
_SAMPLES_MAX_LIMIT = 10
_SIG_LIST_DEFAULT_LIMIT = 100
_SIG_LIST_MAX_LIMIT = 500


def _get_signatures_repo(
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> SignaturesRepository:
    return SignaturesRepository(repo)


def _signature_to_response(sig: Signature) -> SignatureResponse:
    return SignatureResponse(
        template_hash=sig.template_hash,
        service_key=sig.service_key,
        template_str=sig.template_str,
        label=sig.label,
        status=cast(Literal["active", "suppressed", "expected"], sig.status),
        first_seen_at=sig.first_seen_at,
        last_seen_at=sig.last_seen_at,
        total_count=sig.total_count,
    )


def _get_annotations_repo(
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> AnnotationsRepository:
    return AnnotationsRepository(repo)


def _annotation_to_response(a: Annotation) -> AnnotationResponse:
    return AnnotationResponse(
        id=a.id,
        template_hash=a.template_hash,
        service_key=a.service_key,
        note=a.note,
        author=a.author,
        created_at=a.created_at,
    )


def _get_silence_allowlist_repo(
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> SilenceAllowlistRepository:
    return SilenceAllowlistRepository(repo)


def _silence_entry_to_response(e: SilenceAllowlistEntry) -> SilenceAllowlistResponse:
    return SilenceAllowlistResponse(
        id=e.id,
        template_hash=e.template_hash,
        service_key=e.service_key,
        schedule_kind=cast(Literal["always", "cron", "window"], e.schedule_kind),
        schedule_value=e.schedule_value,
        reason=e.reason,
        created_at=e.created_at,
        expires_at=e.expires_at,
    )


@router.get(
    "/logs/signatures/silence-allowlist",
    response_model=SilenceAllowlistListResponse,
)
async def list_silence_allowlist(
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[SilenceAllowlistRepository, Depends(_get_silence_allowlist_repo)],
) -> SilenceAllowlistListResponse:
    """List all expected-silence allowlist entries, newest first.

    Auth: session required; GET (no CSRF).
    """
    rows = await repo.list_all()
    return SilenceAllowlistListResponse(entries=[_silence_entry_to_response(r) for r in rows])


@router.post(
    "/logs/signatures/silence-allowlist",
    response_model=SilenceAllowlistResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_silence_allowlist_entry(
    body: SilenceAllowlistCreateRequest,
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[SilenceAllowlistRepository, Depends(_get_silence_allowlist_repo)],
) -> SilenceAllowlistResponse:
    """Create an expected-silence allowlist entry. 201 on success.

    schedule_value is canonicalized (cron) / range-validated (window) / empty-checked
    (always) by the request model — bad input -> 422. Auth: session required; CSRF
    enforced (POST) by require_session().
    """
    created = await repo.create(
        template_hash=body.template_hash,
        service_key=body.service_key,
        schedule_kind=body.schedule_kind,
        schedule_value=body.schedule_value,
        reason=body.reason,
        expires_at=body.expires_at,
    )
    return _silence_entry_to_response(created)


@router.delete(
    "/logs/signatures/silence-allowlist/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_silence_allowlist_entry(
    entry_id: int,
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[SilenceAllowlistRepository, Depends(_get_silence_allowlist_repo)],
) -> None:
    """Delete an allowlist entry by id. 204 on success, 404 if absent. CSRF enforced (DELETE)."""
    deleted = await repo.delete(entry_id)
    if not deleted:
        raise NotFoundProblem(message=f"silence allowlist entry not found: {entry_id}")


@router.get("/logs/signatures", response_model=SignatureListResponse)
async def list_signatures(  # noqa: PLR0913
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[SignaturesRepository, Depends(_get_signatures_repo)],
    service: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    label_q: str | None = Query(None),
    limit: int = Query(_SIG_LIST_DEFAULT_LIMIT, ge=1, le=_SIG_LIST_MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> SignatureListResponse:
    """List signatures with optional service/status/label filters + pagination.

    Auth: session required. CSRF NOT enforced on GET. Sorted by last_seen_at DESC.
    """
    rows, total = await repo.list(
        filter=SignatureFilter(service=service, status=status_filter, label_q=label_q),
        limit=limit,
        offset=offset,
    )
    return SignatureListResponse(
        signatures=[_signature_to_response(r) for r in rows],
        total=total,
    )


def _summary_to_response(row: ModelSummaryRow) -> ModelSummary:
    return ModelSummary(
        model_key=row.model_key,
        template_count=row.template_count,
        line_count=row.line_count,
        last_processed_ts=row.last_processed_ts,
        updated_at=row.updated_at,
    )


@router.get("/logs/signatures/models", response_model=ModelListResponse)
async def list_drain_models(
    _user: Annotated[User, Depends(require_session())],
    consumer: Annotated[DrainConsumer, Depends(get_drain_consumer)],
) -> ModelListResponse:
    """List all drain models (column-level summaries). 503 when drain disabled.

    Auth: session required; GET (no CSRF). Reads drain_models COLUMNS directly via
    the consumer's persistence — complete on fresh start, blob-corruption-immune.
    """
    rows = await consumer.get_persistence().list_all_summaries()
    return ModelListResponse(models=[_summary_to_response(r) for r in rows])


@router.get("/logs/signatures/cycle/last", response_model=LastCycleResponse)
async def get_last_cycle(
    _user: Annotated[User, Depends(require_session())],
    consumer: Annotated[DrainConsumer, Depends(get_drain_consumer)],
) -> LastCycleResponse:
    """Return the last drain cycle's stats. 503 when drain disabled.

    Returns has_run=False (empty) when no cycle has run yet — NOT an error.
    Auth: session required; GET (no CSRF).
    """
    r = await consumer.get_last_result()
    if r is None:
        return LastCycleResponse(has_run=False)
    return LastCycleResponse(
        has_run=True,
        started_at=r.started_at,
        finished_at=r.finished_at,
        lines_processed=r.lines_processed,
        new_templates=r.new_templates,
        models_touched=r.models_touched,
        cycle_status=r.cycle_status,
        error=r.error,
    )


@router.get("/logs/signatures/models/{model_key}", response_model=ModelDetailResponse)
async def get_drain_model(
    model_key: str,
    _user: Annotated[User, Depends(require_session())],
    consumer: Annotated[DrainConsumer, Depends(get_drain_consumer)],
) -> ModelDetailResponse:
    """Get one drain model's summary + its mined templates. 503 when drain disabled.

    The summary comes from the drain_models columns (authoritative count). The
    templates come from the engine's loaded state via get_model()+templates(), which
    has a corrupt-blob fallback (degrades to templates=[]). The stored vs live count
    mismatch is surfaced to the FE so corruption is visible. 404 if the model_key has
    no drain_models row. model_key may contain ':' (cron keys) — a single path
    segment handles colons fine. Auth: session required; GET (no CSRF).
    """
    rows = await consumer.get_persistence().list_all_summaries()
    summary_row = next((r for r in rows if r.model_key == model_key), None)
    if summary_row is None:
        raise NotFoundProblem(message=f"drain model not found: {model_key}")
    engine = consumer.get_engine()
    await engine.get_model(model_key)  # ensure loaded (idempotent; corrupt-blob fallback)
    templates = engine.templates(model_key)
    return ModelDetailResponse(
        model_key=model_key,
        summary=_summary_to_response(summary_row),
        templates=[
            ModelTemplateEntry(
                template_id=t.template_id,
                template_hash=t.template_hash,
                template_str=t.template_str,
                size=t.size,
                first_seen_ts=t.first_seen_ts,
                last_seen_ts=t.last_seen_ts,
            )
            for t in templates
        ],
    )


@router.get(
    "/logs/signatures/{template_hash}/{service_key}",
    response_model=SignatureResponse,
)
async def get_signature(
    template_hash: str,
    service_key: str,
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[SignaturesRepository, Depends(_get_signatures_repo)],
) -> SignatureResponse:
    """Get one signature by composite key. 404 if absent. GET, no CSRF."""
    sig = await repo.get(template_hash, service_key)
    if sig is None:
        raise NotFoundProblem(message=f"signature not found: {template_hash}/{service_key}")
    return _signature_to_response(sig)


@router.patch(
    "/logs/signatures/{template_hash}/{service_key}",
    response_model=SignatureResponse,
)
async def patch_signature(
    template_hash: str,
    service_key: str,
    body: SignaturePatchRequest,
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[SignaturesRepository, Depends(_get_signatures_repo)],
) -> SignatureResponse:
    """Update a signature's label and/or status. 404 if absent.

    Only fields present in the request body are written (model_fields_set). Auth:
    session required; CSRF enforced (PATCH).
    """
    set_fields = body.model_fields_set
    sig: Signature | None = await repo.get(template_hash, service_key)
    if sig is None:
        raise NotFoundProblem(message=f"signature not found: {template_hash}/{service_key}")
    if "label" in set_fields:
        sig = await repo.update_label(template_hash, service_key, body.label)
        if sig is None:  # pragma: no cover -- existed a line ago
            raise NotFoundProblem(message=f"signature not found: {template_hash}/{service_key}")
    if "status" in set_fields and body.status is not None:
        sig = await repo.set_status(template_hash, service_key, body.status)
        if sig is None:  # pragma: no cover -- existed a line ago
            raise NotFoundProblem(message=f"signature not found: {template_hash}/{service_key}")
    return _signature_to_response(sig)


@router.get(
    "/logs/signatures/{template_hash}/{service_key}/samples",
    response_model=SignatureSamplesResponse,
)
async def get_signature_samples(  # noqa: PLR0913
    template_hash: str,
    service_key: str,
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[SignaturesRepository, Depends(_get_signatures_repo)],
    vl_url: str = Depends(get_vl_url),
    http_client: httpx.AsyncClient = Depends(get_http_client),  # noqa: B008
    limit: int = Query(_SAMPLES_DEFAULT_LIMIT, ge=1, le=_SAMPLES_MAX_LIMIT),
) -> SignatureSamplesResponse:
    """Best-effort live sample lines for a signature, from VictoriaLogs (last 24h).

    404 if the signature is absent. Builds a LogsQL phrase-AND from the template's
    non-wildcard segments (Decision B1). Empty/generic template -> samples=[] with
    reason 'template_too_generic'. VL down -> samples=[] reason 'vl_unavailable'
    (never 500). Auth: session required; GET (no CSRF).
    """
    log: BoundLogger = cast(
        BoundLogger, structlog.get_logger().bind(component="logs_signature_samples")
    )
    sig = await repo.get(template_hash, service_key)
    if sig is None:
        raise NotFoundProblem(message=f"signature not found: {template_hash}/{service_key}")

    expr = _signature_samples_expr(sig.template_str, service_key)
    if expr is None:
        return SignatureSamplesResponse(lines=[], reason="template_too_generic")

    now = datetime.now(UTC)
    start_iso = (now - timedelta(hours=_SAMPLES_WINDOW_HOURS)).isoformat()
    end_iso = now.isoformat()
    base_limits = load_vl_query_limits()
    client = VictoriaLogsClient(vl_url=vl_url, http_client=http_client, limits=base_limits)
    lines: list[LogLine] = []
    try:
        async for vl_line in client.stream_query(
            expr=expr, start=start_iso, end=end_iso, limit=limit
        ):
            lines.append(from_victorialogs_line(vl_line))
    except VictoriaLogsClientError as exc:
        log.warning("logs_signature_samples.upstream_error", error=str(exc))
        return SignatureSamplesResponse(lines=[], reason="vl_unavailable")
    return SignatureSamplesResponse(lines=lines, reason=None)


def _signature_samples_expr(template_str: str, service_key: str) -> str | None:
    """Build the LogsQL samples query for a template (Decision B1).

    Splits the template on '<*>' and ANDs the quoted non-whitespace segments. When
    NO segment has non-whitespace content (template is just '<*>' / whitespace),
    returns None (caller -> 'template_too_generic'; never a match-all query). For a
    real service name (not 'cron:*' / '_unknown'), ANDs a `service:"..."` filter.
    """
    segments = [seg for seg in template_str.split("<*>") if seg.strip()]
    if not segments:
        return None
    expr = " AND ".join(logsql_quote_phrase(seg) for seg in segments)
    if not service_key.startswith("cron:") and service_key != "_unknown":
        expr = f"service:{logsql_quote_phrase(service_key)} AND {expr}"
    return expr


@router.get(
    "/logs/signatures/{template_hash}/{service_key}/annotations",
    response_model=AnnotationListResponse,
)
async def list_annotations(
    template_hash: str,
    service_key: str,
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[AnnotationsRepository, Depends(_get_annotations_repo)],
) -> AnnotationListResponse:
    """List a signature's annotations, newest first. Returns [] for an unknown
    signature (no 404). Auth: session required; GET (no CSRF)."""
    rows = await repo.list_for_signature(template_hash, service_key)
    return AnnotationListResponse(annotations=[_annotation_to_response(r) for r in rows])


@router.post(
    "/logs/signatures/{template_hash}/{service_key}/annotations",
    response_model=AnnotationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_annotation(  # noqa: PLR0913
    template_hash: str,
    service_key: str,
    body: AnnotationCreateRequest,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[AnnotationsRepository, Depends(_get_annotations_repo)],
    sig_repo: Annotated[SignaturesRepository, Depends(_get_signatures_repo)],
) -> AnnotationResponse:
    """Create an annotation. 201 on success, 404 if the parent signature is absent.

    `author` is the session username (Decision A2). Auth: session required;
    CSRF enforced (POST). The signature existence pre-check gives a clean 404
    instead of an opaque FK IntegrityError.
    """
    sig = await sig_repo.get(template_hash, service_key)
    if sig is None:
        raise NotFoundProblem(message=f"signature not found: {template_hash}/{service_key}")
    created = await repo.create(
        template_hash=template_hash,
        service_key=service_key,
        note=body.note,
        author=user.username,
    )
    return _annotation_to_response(created)


@router.delete(
    "/logs/signatures/{template_hash}/{service_key}/annotations/{annotation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_annotation(
    template_hash: str,
    service_key: str,
    annotation_id: int,
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[AnnotationsRepository, Depends(_get_annotations_repo)],
) -> None:
    """Delete an annotation scoped to its signature. 204 on success, 404 if
    absent or it belongs to a different signature. CSRF enforced (DELETE)."""
    deleted = await repo.delete(annotation_id, template_hash, service_key)
    if not deleted:
        raise NotFoundProblem(
            message=f"annotation not found: {annotation_id} for {template_hash}/{service_key}"
        )


# DI helper for saved queries repository
def _get_saved_queries_repo(
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> SavedQueriesRepository:
    return SavedQueriesRepository(repo)


def _row_to_response(row: SavedQueryRow) -> SavedQueryResponse:
    return SavedQueryResponse(
        id=row.id,
        name=row.name,
        logs_ql=row.logs_ql,
        selected_services=[
            SavedServiceIdentity(service=s["service"], source_type=s["source_type"])
            for s in row.selected_services
        ],
        since_preset=row.since_preset,
        range_start_iso=row.range_start_iso,
        range_end_iso=row.range_end_iso,
        advanced_mode=row.advanced_mode,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/logs/saved-queries", response_model=SavedQueriesListResponse)
async def list_saved_queries(
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[SavedQueriesRepository, Depends(_get_saved_queries_repo)],
) -> SavedQueriesListResponse:
    """List all saved queries, sorted by name. Auth: session required (GET, no CSRF)."""
    rows = await repo.list_sorted()
    return SavedQueriesListResponse(saved_queries=[_row_to_response(r) for r in rows])


@router.post(
    "/logs/saved-queries",
    response_model=SavedQueryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_saved_query(
    body: SaveQueryCreateRequest,
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[SavedQueriesRepository, Depends(_get_saved_queries_repo)],
) -> SavedQueryResponse:
    """Create a saved query. 201 on success, 409 on duplicate name.

    Auth: session required; CSRF enforced (POST) by require_session().
    """
    try:
        row = await repo.create(
            name=body.name,
            logs_ql=body.logs_ql,
            selected_services=[s.model_dump() for s in body.selected_services],
            since_preset=body.since_preset,
            range_start_iso=body.range_start_iso,
            range_end_iso=body.range_end_iso,
            advanced_mode=body.advanced_mode,
        )
    except DuplicateNameError as exc:
        raise ConflictProblem(message=f"saved query name already exists: {body.name}") from exc
    return _row_to_response(row)


@router.patch("/logs/saved-queries/{query_id}", response_model=SavedQueryResponse)
async def rename_saved_query(
    query_id: int,
    body: SaveQueryRenameRequest,
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[SavedQueriesRepository, Depends(_get_saved_queries_repo)],
) -> SavedQueryResponse:
    """Rename a saved query. 200 on success, 404 if absent, 409 on duplicate name.

    Auth: session required; CSRF enforced (PATCH).
    """
    try:
        row = await repo.rename(query_id=query_id, new_name=body.name)
    except DuplicateNameError as exc:
        raise ConflictProblem(message=f"saved query name already exists: {body.name}") from exc
    if row is None:
        raise NotFoundProblem(message=f"saved query not found: {query_id}")
    return _row_to_response(row)


@router.put("/logs/saved-queries/{query_id}", response_model=SavedQueryResponse)
async def update_saved_query(
    query_id: int,
    body: SaveQueryCreateRequest,
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[SavedQueriesRepository, Depends(_get_saved_queries_repo)],
) -> SavedQueryResponse:
    """Overwrite a saved query's PAYLOAD (full replace), keeping its name.

    The request body is SaveQueryCreateRequest for schema reuse, but ``body.name``
    is INTENTIONALLY IGNORED — the saved query keeps the name stored on the
    existing row. Only logs_ql / selected_services / range / advanced_mode are
    written. The body's range-invariant validation (exactly one of since_preset
    OR custom range) still applies.

    200 on success, 404 if absent. Auth: session required; CSRF enforced (PUT).
    """
    row = await repo.update(
        query_id=query_id,
        logs_ql=body.logs_ql,
        selected_services=[s.model_dump() for s in body.selected_services],
        since_preset=body.since_preset,
        range_start_iso=body.range_start_iso,
        range_end_iso=body.range_end_iso,
        advanced_mode=body.advanced_mode,
    )
    if row is None:
        raise NotFoundProblem(message=f"saved query not found: {query_id}")
    return _row_to_response(row)


@router.delete(
    "/logs/saved-queries/{query_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_saved_query(
    query_id: int,
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[SavedQueriesRepository, Depends(_get_saved_queries_repo)],
) -> None:
    """Delete a saved query. 204 on success, 404 if absent. CSRF enforced (DELETE)."""
    deleted = await repo.delete(query_id)
    if not deleted:
        raise NotFoundProblem(message=f"saved query not found: {query_id}")
