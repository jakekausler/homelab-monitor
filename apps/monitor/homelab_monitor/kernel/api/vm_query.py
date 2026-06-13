"""Reusable VictoriaMetrics INSTANT-query helper for the API layer.

Sibling to the VM range read path in ``routers/metrics.py``. Performs a single
``/api/v1/query`` (instant) request, parses the ``vector`` result, and surfaces
upstream failures as ``HttpProblem(502, "upstream_unavailable")`` — matching the
convention used by ``metrics_range``.

Public surface (importable by tests — no leading underscore):
  - ``VmInstantSample`` — one parsed vector sample (labels + unix ts + raw value str).
  - ``vm_instant_query`` — run a query, return ``list[VmInstantSample]``.
  - ``vm_count`` — run a ``count(...)``-style query, return the single int (default 0).
  - ``first_sample`` — return the first sample of a vector, or ``None``.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import cast

import httpx
import structlog
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.api.errors import HttpProblem

_VM_TIMEOUT_S = 5.0
_HTTP_OK = 200
_VALUE_PAIR_LEN = 2


@dataclass(frozen=True, slots=True)
class VmInstantSample:
    """One parsed sample from a VictoriaMetrics instant (vector) query.

    ``value_str`` is the raw string value VM returns (e.g. ``"5"``); ``ts`` is the
    unix timestamp of the sample.
    """

    labels: dict[str, str]
    ts: float
    value_str: str


def _upstream_unavailable(message: str) -> HttpProblem:
    return HttpProblem(
        status_code=502,
        code="upstream_unavailable",
        message=message,
    )


async def vm_instant_query(
    http_client: httpx.AsyncClient,
    vm_url: str,
    query: str,
    *,
    timeout: float = _VM_TIMEOUT_S,
) -> list[VmInstantSample]:
    """Run a single instant query against VictoriaMetrics and parse the vector.

    On a transport error, a non-200 status, or an HTTP-200 body whose ``status``
    is not ``"success"``, raises ``HttpProblem(502, "upstream_unavailable")``.

    A successful query with an EMPTY ``result`` list returns ``[]`` (the caller
    is responsible for defaulting absent series to 0).
    """
    log: BoundLogger = cast(
        BoundLogger,
        structlog.get_logger().bind(component="vm_instant_query"),
    )
    try:
        resp = await http_client.get(
            f"{vm_url}/api/v1/query",
            params={"query": query},
            timeout=timeout,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning("vm_instant_query.upstream_error", error=str(exc), query=query)
        raise _upstream_unavailable("victoriametrics instant query failed") from exc

    if resp.status_code != _HTTP_OK:
        log.warning(
            "vm_instant_query.upstream_status",
            status=resp.status_code,
            body=resp.text[:200],
        )
        raise _upstream_unavailable(f"victoriametrics returned status {resp.status_code}")

    body_raw = resp.json()  # pyright: ignore[reportAssignmentType]
    body = cast(dict[str, object], body_raw) if isinstance(body_raw, dict) else {}

    if body.get("status") != "success":
        error_type = body.get("errorType", "unknown")
        log.warning("vm_instant_query.vm_error_response", error_type=str(error_type)[:200])
        raise _upstream_unavailable(f"VictoriaMetrics returned error: {error_type}")

    body_data = body.get("data")
    data = cast(dict[str, object], body_data) if isinstance(body_data, dict) else {}
    result_raw = data.get("result")
    result_list = cast(list[object], result_raw) if isinstance(result_raw, list) else []

    samples: list[VmInstantSample] = []
    for item in result_list:
        if not isinstance(item, dict):
            continue
        item_dict = cast(dict[str, object], item)
        metric_raw = item_dict.get("metric", {})
        metric = cast(dict[str, str], metric_raw) if isinstance(metric_raw, dict) else {}
        value_raw = item_dict.get("value")
        if not isinstance(value_raw, (list, tuple)) or len(value_raw) < _VALUE_PAIR_LEN:  # pyright: ignore[reportUnknownArgumentType]
            continue
        with contextlib.suppress(ValueError, TypeError, IndexError):
            ts = float(value_raw[0])  # pyright: ignore[reportUnknownArgumentType]
            value_str = str(value_raw[1])  # pyright: ignore[reportUnknownArgumentType]
            samples.append(VmInstantSample(labels=metric, ts=ts, value_str=value_str))
    return samples


def first_sample(samples: list[VmInstantSample]) -> VmInstantSample | None:
    """Return the first sample of a parsed instant vector, or ``None`` if empty."""
    return samples[0] if samples else None


async def vm_count(
    http_client: httpx.AsyncClient,
    vm_url: str,
    query: str,
    *,
    timeout: float = _VM_TIMEOUT_S,
) -> int:
    """Run a ``count(...)``-style instant query; return the single integer value.

    Defaults to 0 when VM returns an empty vector (which is what
    ``count(metric == 0)`` does when zero series match). A non-numeric value
    also defaults to 0. Upstream failures propagate as ``HttpProblem(502)``.
    """
    log: BoundLogger = cast(
        BoundLogger,
        structlog.get_logger().bind(component="vm_count"),
    )
    samples = await vm_instant_query(http_client, vm_url, query, timeout=timeout)
    sample = first_sample(samples)
    if sample is None:
        return 0
    try:
        return int(float(sample.value_str))
    except (ValueError, TypeError):
        log.warning("vm_count.non_numeric_value", value=sample.value_str[:50])
        return 0
