"""VlHealthCollector — probes VictoriaLogs /health and emits up + latency gauges.

Runs every 30s. Makes a single GET to {vl_url}/health with a configurable timeout.

Emits two gauges:
  homelab_vl_up — 1.0 if HTTP 200, 0.0 on non-200 / timeout / transport error.
  homelab_vl_response_time_seconds — probe latency in seconds (always emitted,
    including on failure; the elapsed-until-timeout is operationally useful).

The probe is a SUCCESS even when VL is down (homelab_vl_up=0.0): the collector
did its job of reporting VL's health. ok=False is reserved for genuine collector
malfunction (no http_client available). The homelab_collector_run_vl_health
self-metric is emitted automatically by the BaseCollector run-wrapper.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar

import httpx

from homelab_monitor.kernel.config import load_vl_health_config
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import (
    CollectorResult,
    RunKind,
    TrustLevel,
)

_HTTP_OK = 200


class VlHealthCollector(BaseCollector):
    """Probe VictoriaLogs /health; emit homelab_vl_up + response_time_seconds."""

    name: ClassVar[str] = "vl_health"
    interval: ClassVar[timedelta] = timedelta(seconds=30)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "vl_health"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN

    def __init__(
        self,
        *,
        vl_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout_s: float | None = None,
    ) -> None:
        super().__init__()
        self._vl_url = (vl_url or "http://victorialogs:9428").rstrip("/")
        self._http_client = http_client
        self._timeout_s: float = (
            timeout_s if timeout_s is not None else load_vl_health_config().timeout_s
        )

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Probe GET {vl_url}/health; emit homelab_vl_up + response_time gauges."""
        start = time.monotonic()

        client = self._http_client if self._http_client is not None else ctx.http
        if client is None:  # pyright: ignore[reportUnnecessaryComparison]
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["http_client_unavailable"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        up: float = 0.0
        elapsed: float = 0.0
        try:
            resp = await client.get(
                f"{self._vl_url}/health",
                timeout=self._timeout_s,
            )
            elapsed = time.monotonic() - start
            if resp.status_code == _HTTP_OK:
                up = 1.0
        except (httpx.TimeoutException, httpx.RequestError):
            elapsed = time.monotonic() - start

        ctx.vm.write_gauge("homelab_vl_up", up, {})
        ctx.vm.write_gauge("homelab_vl_response_time_seconds", elapsed, {})

        return CollectorResult(
            ok=True,
            metrics_emitted=2,
            errors=[],
            events=[],
            duration_seconds=time.monotonic() - start,
        )
