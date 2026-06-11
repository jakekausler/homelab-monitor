"""Metrics endpoints — snapshot (in-memory latest) + range (VictoriaMetrics proxy)."""

from __future__ import annotations

import contextlib
import re
from typing import Literal, cast

import httpx
import structlog
from fastapi import APIRouter, Depends, Query
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.api.dependencies import (
    get_http_client,
    get_in_memory_metrics_writer,
    get_vm_url,
    require_session,
)
from homelab_monitor.kernel.api.errors import HttpProblem
from homelab_monitor.kernel.api.schemas import (
    MetricNamesResponse,
    MetricsRangeResponse,
    MetricsSnapshotEntry,
    MetricsSnapshotResponse,
    VMRangeData,
    VMRangeResult,
)
from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.plugins.io import MemoryRetainingMetricsWriter

router = APIRouter()

_VM_TIMEOUT_S = 5.0
_HTTP_OK = 200
_PAIR_MIN_LEN = 2
_MAX_EXPR_LEN = 4096
_MAX_RANGE_POINTS = 11000  # Prometheus convention
_STEP_RE = re.compile(r"^\d+(ms|s|m|h|d)$")


@router.get("/metrics/snapshot", response_model=MetricsSnapshotResponse)
async def metrics_snapshot(
    _user: User = Depends(require_session()),  # noqa: B008
    writer: MemoryRetainingMetricsWriter = Depends(get_in_memory_metrics_writer),  # noqa: B008
) -> MetricsSnapshotResponse:
    """Return the in-memory writer's latest-value snapshot.

    Auth: cookie session required. CSRF NOT enforced on GET.

    Backed by ``MemoryRetainingMetricsWriter`` (one half of the production
    multiplex). The VM-backed scrape path is exposed through ``/metrics/range``.
    """
    entries = [
        MetricsSnapshotEntry(
            name=e.name,
            value=e.value,
            labels=e.labels,
            kind=e.kind,
            ts=e.ts,
        )
        for e in writer.snapshot()
    ]
    return MetricsSnapshotResponse(ts=utc_now_iso(), entries=entries)


@router.get("/metrics/range", response_model=MetricsRangeResponse)
async def metrics_range(  # noqa: PLR0913
    expr: str = Query(..., description="PromQL expression"),
    start: str = Query(..., description="ISO-8601 UTC start time"),
    end: str = Query(..., description="ISO-8601 UTC end time"),
    step: str = Query("10s", description="Resolution step (e.g. '10s', '1m')"),
    _user: User = Depends(require_session()),  # noqa: B008
    vm_url: str = Depends(get_vm_url),
    http_client: httpx.AsyncClient = Depends(get_http_client),  # noqa: B008
) -> MetricsRangeResponse:
    """Proxy a PromQL range query to VictoriaMetrics.

    Auth: cookie session required. CSRF NOT enforced on GET.

    On any non-200 response from VM, on a transport error, or on a timeout,
    surfaces as 502 ``upstream_unavailable`` so the frontend can fall back
    to its synthetic baseline.
    """
    log: BoundLogger = cast(
        BoundLogger,
        structlog.get_logger().bind(component="metrics_range"),
    )

    # Validate expr length
    if len(expr) > _MAX_EXPR_LEN:
        raise HttpProblem(
            status_code=400,
            code="invalid_expr",
            message="expression too long",
        )

    # Validate step format
    if not _STEP_RE.match(step):
        raise HttpProblem(
            status_code=400,
            code="invalid_step",
            message="step must match /^\\d+(ms|s|m|h|d)$/",
        )

    params = {"query": expr, "start": start, "end": end, "step": step}
    try:
        resp = await http_client.get(
            f"{vm_url}/api/v1/query_range",
            params=params,
            timeout=_VM_TIMEOUT_S,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning("metrics_range.upstream_error", error=str(exc), expr=expr)
        raise HttpProblem(
            status_code=502,
            code="upstream_unavailable",
            message="victoriametrics query failed",
        ) from exc

    if resp.status_code != _HTTP_OK:
        log.warning(
            "metrics_range.upstream_status",
            status=resp.status_code,
            body=resp.text[:200],
        )
        raise HttpProblem(
            status_code=502,
            code="upstream_unavailable",
            message=f"victoriametrics returned status {resp.status_code}",
        )

    body_raw = resp.json()  # pyright: ignore[reportAssignmentType, reportReturnType]
    # VM shape: {"status": "success", "data": {"resultType": "matrix",
    #            "result": [{"metric": {...}, "values": [[ts, "str"], ...]}]}}
    body = cast(dict[str, object], body_raw) if isinstance(body_raw, dict) else {}

    # Check for VM error response (HTTP 200 but status != "success")
    if body.get("status") != "success":
        error_type = body.get("errorType", "unknown")
        error_msg = body.get("error", "")
        log.warning(
            "metrics_range.vm_error_response",
            error_type=error_type,
            error_msg=str(error_msg)[:200],
        )
        raise HttpProblem(
            status_code=502,
            code="upstream_unavailable",
            message=f"VictoriaMetrics returned error: {error_type}",
        )
    body_data = body.get("data")
    data = cast(dict[str, object], body_data) if isinstance(body_data, dict) else {}
    result_raw = data.get("result")
    result_list = cast(list[object], result_raw) if isinstance(result_raw, list) else []
    parsed_results: list[VMRangeResult] = []
    for item in result_list:
        if not isinstance(item, dict):
            continue
        item_dict = cast(dict[str, object], item)
        metric_dict = item_dict.get("metric", {})
        metric = cast(dict[str, str], metric_dict) if isinstance(metric_dict, dict) else {}
        values_raw = item_dict.get("values", [])
        values: list[list[float | str]] = []
        if isinstance(values_raw, list):
            for pair in values_raw:  # pyright: ignore[reportUnknownVariableType]
                if isinstance(pair, (list, tuple)) and len(pair) >= _PAIR_MIN_LEN:  # pyright: ignore[reportUnknownArgumentType]
                    with contextlib.suppress(ValueError, TypeError, IndexError):
                        values.append([float(pair[0]), str(pair[1])])  # pyright: ignore[reportUnknownArgumentType]
        parsed_results.append(VMRangeResult(metric=metric, values=values))
    status_raw = str(body.get("status", "success"))
    status: Literal["success", "error"] = "success" if status_raw == "success" else "error"
    return MetricsRangeResponse(
        status=status,
        data=VMRangeData(
            resultType=str(data.get("resultType", "matrix")),
            result=parsed_results,
        ),
    )


@router.get("/metrics/metric-names", response_model=MetricNamesResponse)
async def metrics_metric_names(
    _user: User = Depends(require_session()),  # noqa: B008
    vm_url: str = Depends(get_vm_url),
    http_client: httpx.AsyncClient = Depends(get_http_client),  # noqa: B008
) -> MetricNamesResponse:
    """Proxy VictoriaMetrics' ``__name__`` label-values list (metric-name discovery).

    Auth: cookie session required. CSRF NOT enforced on GET.

    Powers the MetricsQL Simple-mode authoring autocomplete. On any non-200
    response from VM, a transport error, or a timeout, surfaces as 502
    ``upstream_unavailable`` (mirrors ``metrics_range``). VM's response shape is
    ``{"status": "success", "data": ["metric1", "metric2", ...]}``; we parse
    ``data`` (a list of strings) into ``names``. A 200 with ``status != "success"``
    or a missing/non-list ``data`` yields an empty ``names`` list rather than an
    error (best-effort discovery aid).
    """
    log: BoundLogger = cast(
        BoundLogger,
        structlog.get_logger().bind(component="metrics_metric_names"),
    )

    try:
        resp = await http_client.get(
            f"{vm_url}/api/v1/label/__name__/values",
            timeout=_VM_TIMEOUT_S,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning("metrics_metric_names.upstream_error", error=str(exc))
        raise HttpProblem(
            status_code=502,
            code="upstream_unavailable",
            message="victoriametrics label-values query failed",
        ) from exc

    if resp.status_code != _HTTP_OK:
        log.warning(
            "metrics_metric_names.upstream_status",
            status=resp.status_code,
            body=resp.text[:200],
        )
        raise HttpProblem(
            status_code=502,
            code="upstream_unavailable",
            message=f"victoriametrics returned status {resp.status_code}",
        )

    body_raw = resp.json()  # pyright: ignore[reportAssignmentType, reportReturnType]
    body = cast(dict[str, object], body_raw) if isinstance(body_raw, dict) else {}

    # Best-effort: a non-success status or a non-list `data` yields an empty list
    # (the autocomplete is advisory; a custom-typed metric name is always allowed).
    names: list[str] = []
    if body.get("status") == "success":
        data_raw = body.get("data")
        if isinstance(data_raw, list):
            for item in data_raw:  # pyright: ignore[reportUnknownVariableType]
                if isinstance(item, str):
                    names.append(item)

    return MetricNamesResponse(names=names)
