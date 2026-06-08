"""LogStreamBudgetCollector — observes per-stream byte + rate budget in VL.

Runs every 60s. Makes two VictoriaLogs ``/select/logsql/stats_query`` instant
queries:

  1. Today's cumulative bytes + lines per (host, service):
     ``* | stats by (host, service) count() as lines, sum_len(_msg) as bytes_today``
     scoped to ``start=<today>T00:00:00Z``. Each (host, service) yields two
     instant-vector rows, one per ``__name__`` (``lines`` and ``bytes_today``).
  2. The last-5-minutes line count for the current rate:
     ``_time:5m | stats by (host, service) count() as lines``; ``lines_per_sec``
     is ``count / 300.0``.

Emits three gauges per stream (``homelab_log_stream_bytes_today``,
``homelab_log_stream_lines_per_sec``, ``homelab_log_stream_bytes_budget``) and
updates a shared in-process :class:`LogStreamState` map that the
``/api/logs/streams`` endpoint reads.

Failures (transport, non-2xx, JSON parse) on EITHER query produce a
CollectorResult with ``ok=False`` + populated ``errors``; the state map is left
untouched.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime, timedelta
from typing import ClassVar, cast

import httpx

from homelab_monitor.kernel.api.schemas import LogsStreamSummary
from homelab_monitor.kernel.config import load_log_stream_budget_config
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind, TrustLevel

# Shared state map: (host, service) -> LogsStreamSummary.
LogStreamState = dict[tuple[str, str], LogsStreamSummary]

_VL_TIMEOUT_S = 5.0
_HTTP_OK = 200
_RATE_WINDOW_S = 300.0  # 5-minute window for lines_per_sec


class LogStreamBudgetCollector(BaseCollector):
    """Observe per-stream log bytes + rate; emit gauges and refresh state."""

    name: ClassVar[str] = "log_stream_budget"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=30)
    concurrency_group: ClassVar[str] = "log_stream_budget"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN

    def __init__(
        self,
        *,
        state: LogStreamState | None = None,
        vl_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        budget_bytes_per_day: int | None = None,
    ) -> None:
        super().__init__()
        self._state = state if state is not None else {}
        self._vl_url = (vl_url or "http://victorialogs:9428").rstrip("/")
        self._http_client = http_client
        self._budget_bytes_per_day: int = (
            budget_bytes_per_day
            if budget_bytes_per_day is not None
            else load_log_stream_budget_config().bytes_per_day_per_stream
        )

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Run a single tick: two VL stats_query calls -> three gauges/stream."""
        start = time.monotonic()
        errors: list[str] = []
        emitted = 0

        client = self._http_client if self._http_client is not None else ctx.http
        if client is None:  # pyright: ignore[reportUnnecessaryComparison]
            errors.append("http_client_unavailable")
            return self._error_result(errors, start)

        start_ts = (
            datetime.now(UTC)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )

        # --- Call 1: today's cumulative bytes + lines per (host, service). ---
        rows_today, err1 = await self._query_stats(
            client,
            {
                "query": (
                    "* | stats by (host, service) count() as lines, sum_len(_msg) as bytes_today"
                ),
                "start": start_ts,
            },
        )
        if err1 is not None:
            errors.append(err1)
            return self._error_result(errors, start)

        # --- Call 2: last-5-minute line count for the current rate. ---
        rows_rate, err2 = await self._query_stats(
            client,
            {"query": "_time:5m | stats by (host, service) count() as lines"},
        )
        if err2 is not None:
            errors.append(err2)
            return self._error_result(errors, start)

        bytes_by_key: dict[tuple[str, str], float] = {}
        for row in rows_today:
            key, name, value = self._row_fields(row)
            if key is None or value is None:
                continue
            if name == "bytes_today":
                bytes_by_key[key] = value

        rate_by_key: dict[tuple[str, str], float] = {}
        for row in rows_rate:
            key, name, value = self._row_fields(row)
            if key is None or value is None:
                continue
            if name == "lines":
                rate_by_key[key] = value / _RATE_WINDOW_S

        now_iso = utc_now_iso()
        # Drive off bytes_by_key: a stream with no bytes-today row is silent today;
        # emitting budget/rate for it would be noise. rate-only keys are intentionally dropped.
        for key, bytes_today in bytes_by_key.items():
            host, service = key
            lines_per_sec = rate_by_key.get(key, 0.0)

            self._state[(host, service)] = LogsStreamSummary(
                host=host,
                service=service,
                last_seen=now_iso,
                lines_per_sec=lines_per_sec,
                bytes_today=int(bytes_today),  # sum_len is integral; int() is exact here
            )

            ctx.vm.write_gauge(
                "homelab_log_stream_bytes_today",
                bytes_today,
                {"host": host, "service": service},
            )
            ctx.vm.write_gauge(
                "homelab_log_stream_lines_per_sec",
                lines_per_sec,
                {"host": host, "service": service},
            )
            ctx.vm.write_gauge(
                "homelab_log_stream_bytes_budget",
                float(self._budget_bytes_per_day),
                {"host": host, "service": service},
            )
            emitted += 3

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted,
            errors=errors,
            events=[],
            duration_seconds=time.monotonic() - start,
        )

    def _error_result(self, errors: list[str], start: float) -> CollectorResult:
        """Build an ok=False result; leaves the state map untouched."""
        return CollectorResult(
            ok=False,
            metrics_emitted=0,
            errors=errors,
            events=[],
            duration_seconds=time.monotonic() - start,
        )

    async def _query_stats(
        self,
        client: httpx.AsyncClient,
        params: dict[str, str],
    ) -> tuple[list[dict[str, object]], str | None]:
        """One stats_query GET; returns (result rows, error string or None).

        On transport error -> ([], "vl_transport: ...").
        On non-200       -> ([], "vl_status: <code>").
        On JSON parse err -> ([], "vl_json: ...").
        On status != success -> ([], None) with empty rows (treated as no data).
        """
        try:
            resp = await client.get(
                f"{self._vl_url}/select/logsql/stats_query",
                params=params,
                timeout=_VL_TIMEOUT_S,
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            return [], f"vl_transport: {exc}"

        if resp.status_code != _HTTP_OK:
            return [], f"vl_status: {resp.status_code}"

        try:
            body_raw: object = resp.json()
        except ValueError as exc:
            return [], f"vl_json: {exc}"

        body = cast(dict[str, object], body_raw) if isinstance(body_raw, dict) else {}
        if body.get("status") != "success":
            return [], None
        data_raw = body.get("data")
        data = cast(dict[str, object], data_raw) if isinstance(data_raw, dict) else {}
        result_raw = data.get("result")
        result_list = cast(list[object], result_raw) if isinstance(result_raw, list) else []
        rows: list[dict[str, object]] = [
            cast(dict[str, object], r) for r in result_list if isinstance(r, dict)
        ]
        return rows, None

    @staticmethod
    def _row_fields(
        row: dict[str, object],
    ) -> tuple[tuple[str, str] | None, str | None, float | None]:
        """Extract ((host, service), __name__, value) from one instant-vector row.

        Returns (None, *, *) when host/service is missing/empty, and (*, *, None)
        when the value is absent, non-numeric, or non-finite (NaN/inf).
        """
        metric_raw = row.get("metric")
        metric = cast(dict[str, object], metric_raw) if isinstance(metric_raw, dict) else {}
        host = str(metric.get("host", ""))
        service = str(metric.get("service", ""))
        name_raw = metric.get("__name__")
        name = name_raw if isinstance(name_raw, str) else None
        if not host or not service:
            return None, name, None

        value_raw = row.get("value")
        if not isinstance(value_raw, list):
            return (host, service), name, None
        value_list = cast(list[object], value_raw)
        if len(value_list) < 2:  # noqa: PLR2004
            return (host, service), name, None
        try:
            value = float(str(value_list[1]))
        except (TypeError, ValueError):
            return (host, service), name, None
        if not math.isfinite(value):
            return (host, service), name, None
        return (host, service), name, value
