"""LogStreamBudgetCollector — observes per-stream byte + rate budget in VL.

Runs every 60s. Queries VictoriaLogs ``/select/logsql/stats`` for per-stream
byte counts (today) + lines/sec, emits two gauges, and updates a shared
in-process :class:`LogStreamState` map that the :class:`/api/logs/streams`
endpoint reads.

Failures (transport, non-2xx, JSON parse) produce a CollectorResult with
``ok=False`` + populated ``errors``; the state map is left untouched.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any, ClassVar, cast

import httpx

from homelab_monitor.kernel.api.schemas import LogsStreamSummary
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind, TrustLevel

# Shared state map: (host, service) -> LogsStreamSummary.
LogStreamState = dict[tuple[str, str], LogsStreamSummary]

_VL_TIMEOUT_S = 5.0
_HTTP_OK = 200


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
    ) -> None:
        super().__init__()
        self._state = state if state is not None else {}
        self._vl_url = (vl_url or "http://victorialogs:9428").rstrip("/")
        self._http_client = http_client

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Run a single tick."""
        start = time.monotonic()
        errors: list[str] = []
        emitted = 0

        client = self._http_client if self._http_client is not None else ctx.http
        if client is None:  # pyright: ignore[reportUnnecessaryComparison]
            errors.append("http_client_unavailable")
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=errors,
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        try:
            resp = await client.get(
                f"{self._vl_url}/select/logsql/stats",
                timeout=_VL_TIMEOUT_S,
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            errors.append(f"vl_transport: {exc}")
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=errors,
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        if resp.status_code != _HTTP_OK:
            errors.append(f"vl_status: {resp.status_code}")
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=errors,
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        try:
            body_raw: object = resp.json()
        except ValueError as exc:
            errors.append(f"vl_json: {exc}")
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=errors,
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        body = cast(dict[str, Any], body_raw) if isinstance(body_raw, dict) else {}
        streams_raw = body.get("streams", [])
        streams = cast(list[Any], streams_raw) if isinstance(streams_raw, list) else []

        now_iso = utc_now_iso()
        for item in streams:
            if not isinstance(item, dict):
                continue
            item_dict = cast(dict[str, Any], item)
            host = str(item_dict.get("host", ""))
            service = str(item_dict.get("service", ""))
            if not host or not service:
                continue
            try:
                bytes_today = int(item_dict.get("bytes_today", 0))
                lines_per_sec = float(item_dict.get("lines_per_sec", 0.0))
            except (TypeError, ValueError):
                continue

            self._state[(host, service)] = LogsStreamSummary(
                host=host,
                service=service,
                last_seen=now_iso,
                lines_per_sec=lines_per_sec,
                bytes_today=bytes_today,
            )

            ctx.vm.write_gauge(
                "homelab_log_stream_bytes_today",
                float(bytes_today),
                {"host": host, "service": service},
            )
            ctx.vm.write_gauge(
                "homelab_log_stream_lines_per_sec",
                float(lines_per_sec),
                {"host": host, "service": service},
            )
            emitted += 2

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted,
            errors=errors,
            events=[],
            duration_seconds=time.monotonic() - start,
        )
