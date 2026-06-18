"""Tests for UnifiPlaceholderCollector — static homelab_unifi_bundle_loaded gauge."""

from __future__ import annotations

import structlog

from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.integrations.unifi.placeholder import (
    UnifiPlaceholderCollector,
)

_EXPECTED_INTERVAL = 60.0
_EXPECTED_TIMEOUT = 5.0


def _ctx(writer: InMemoryMetricsWriter) -> CollectorContext:
    """Minimal CollectorContext — only vm is real; unifi is None (run never reads it)."""
    return CollectorContext(
        config=CollectorConfig(name="unifi_placeholder", interval_seconds=60, timeout_seconds=5),
        db=None,  # pyright: ignore[reportArgumentType]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(  # pyright: ignore[reportArgumentType]
            collector="unifi_placeholder"
        ),
        unifi=None,
    )


def test_unifi_placeholder_classvars() -> None:
    """ClassVars match the locked cadence + the default concurrency group."""
    assert UnifiPlaceholderCollector.name == "unifi_placeholder"
    assert UnifiPlaceholderCollector.interval.total_seconds() == _EXPECTED_INTERVAL
    assert UnifiPlaceholderCollector.timeout.total_seconds() == _EXPECTED_TIMEOUT
    # Must stay in "default" — it makes no controller calls, so it must NOT consume
    # the "unifi" concurrency group's serialization budget.
    assert UnifiPlaceholderCollector.concurrency_group == "default"


async def test_unifi_placeholder_emits_bundle_loaded_gauge() -> None:
    """run() emits homelab_unifi_bundle_loaded == 1.0, ok=True, metrics_emitted=1."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer)
    result = await UnifiPlaceholderCollector().run(ctx)
    assert result.ok is True
    assert result.metrics_emitted == 1
    assert result.errors == []
    assert result.events == []
    gauges = [e for e in writer.recorded if e.name == "homelab_unifi_bundle_loaded"]
    assert len(gauges) == 1
    assert gauges[0].value == 1.0
    assert gauges[0].labels == {}


async def test_unifi_placeholder_succeeds_with_unifi_none() -> None:
    """run() never touches ctx.unifi — succeeds even when unifi is None."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer)
    assert ctx.unifi is None
    result = await UnifiPlaceholderCollector().run(ctx)
    assert result.ok is True
    assert result.duration_seconds >= 0.0
