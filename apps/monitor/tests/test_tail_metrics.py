"""Tests for TailMetricsCollector — both branches of line 40 (registry / no-registry)."""

from __future__ import annotations

import pytest
import structlog

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.tail_service import TailRegistry
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter, MemoryRetainingMetricsWriter
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.builtin.tail_metrics import (
    _GAUGE,  # pyright: ignore[reportPrivateUsage]
    TailMetricsCollector,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COLLECTOR_NAME = "tail_metrics"


def _ctx(writer: MemoryRetainingMetricsWriter, repo: SqliteRepository) -> CollectorContext:
    """Minimal CollectorContext for TailMetricsCollector (no http/ssh/secrets/ha needed)."""
    return CollectorContext(
        config=CollectorConfig(name=_COLLECTOR_NAME),
        db=repo,
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector=_COLLECTOR_NAME),  # pyright: ignore[reportArgumentType]
        ha=None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tail_metrics_run_with_registry(repo: SqliteRepository) -> None:
    """Registry present: active_count is read and written as gauge (line 40 left branch)."""
    registry = TailRegistry(max_connections=5)
    # Acquire one slot so active_count > 0 — makes the assertion non-trivial.
    registry.try_acquire()

    collector = TailMetricsCollector(registry=registry)
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    result = await collector.run(ctx)

    assert result.ok
    assert result.metrics_emitted == 1
    gauge_value = writer.last_gauge(_GAUGE)
    assert gauge_value is not None
    assert gauge_value == float(registry.active_count)


@pytest.mark.asyncio
async def test_tail_metrics_run_without_registry(repo: SqliteRepository) -> None:
    """Registry None: else-branch of line 40 executes, gauge emitted as 0.0."""
    collector = TailMetricsCollector(registry=None)
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    result = await collector.run(ctx)

    assert result.ok
    assert result.metrics_emitted == 1
    gauge_value = writer.last_gauge(_GAUGE)
    assert gauge_value == 0.0
