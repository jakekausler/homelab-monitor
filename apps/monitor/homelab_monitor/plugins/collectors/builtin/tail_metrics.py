"""TailMetricsCollector — publishes the live-tail active-connections gauge.

Each tick reads TailRegistry.active_count and writes
homelab_log_tail_active_connections. The registry is injected post-construction
in lifespan.py (mirrors how LogStreamBudgetCollector gets its state). When the
registry is not wired (degraded / test minimal set), the collector emits 0.0.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar

from homelab_monitor.kernel.logs.tail_service import TailRegistry
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind, TrustLevel

_GAUGE = "homelab_log_tail_active_connections"


class TailMetricsCollector(BaseCollector):
    """Publish the live-tail active-connections gauge from the registry."""

    name: ClassVar[str] = "tail_metrics"
    interval: ClassVar[timedelta] = timedelta(seconds=15)
    timeout: ClassVar[timedelta] = timedelta(seconds=5)
    concurrency_group: ClassVar[str] = "tail_metrics"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN

    def __init__(self, *, registry: TailRegistry | None = None) -> None:
        super().__init__()
        self._registry = registry

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Read the registry's active count and write the gauge."""
        start = time.monotonic()
        count = self._registry.active_count if self._registry is not None else 0
        ctx.vm.write_gauge(_GAUGE, float(count), {})
        return CollectorResult(
            ok=True,
            metrics_emitted=1,
            errors=[],
            events=[],
            duration_seconds=time.monotonic() - start,
        )
