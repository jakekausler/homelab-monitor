"""Tests for SynologyPlaceholderCollector — static homelab_synology_bundle_loaded gauge."""

from __future__ import annotations

import structlog

from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.integrations.synology.placeholder import (
    SynologyPlaceholderCollector,
)

_EXPECTED_INTERVAL = 60.0
_EXPECTED_TIMEOUT = 5.0


def _ctx(writer: InMemoryMetricsWriter) -> CollectorContext:
    """Minimal CollectorContext — only vm is real; synology is None (run never reads it)."""
    return CollectorContext(
        config=CollectorConfig(name="synology_placeholder", interval_seconds=60, timeout_seconds=5),
        db=None,  # pyright: ignore[reportArgumentType]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(  # pyright: ignore[reportArgumentType]
            collector="synology_placeholder"
        ),
        synology=None,
    )


def test_synology_placeholder_classvars() -> None:
    """ClassVars match the locked cadence + the default concurrency group."""
    assert SynologyPlaceholderCollector.name == "synology_placeholder"
    assert SynologyPlaceholderCollector.interval.total_seconds() == _EXPECTED_INTERVAL
    assert SynologyPlaceholderCollector.timeout.total_seconds() == _EXPECTED_TIMEOUT
    # Must stay in "default" — it makes no DSM calls, so it must NOT consume
    # a "synology" concurrency group's serialization budget.
    assert SynologyPlaceholderCollector.concurrency_group == "default"


async def test_synology_placeholder_emits_bundle_loaded_gauge() -> None:
    """run() emits homelab_synology_bundle_loaded == 1.0, ok=True, metrics_emitted=1."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer)
    result = await SynologyPlaceholderCollector().run(ctx)
    assert result.ok is True
    assert result.metrics_emitted == 1
    assert result.errors == []
    assert result.events == []
    gauges = [e for e in writer.recorded if e.name == "homelab_synology_bundle_loaded"]
    assert len(gauges) == 1
    assert gauges[0].value == 1.0
    assert gauges[0].labels == {}


async def test_synology_placeholder_succeeds_with_synology_none() -> None:
    """run() never touches ctx.synology — succeeds even when synology is None."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer)
    assert ctx.synology is None
    result = await SynologyPlaceholderCollector().run(ctx)
    assert result.ok is True
    assert result.duration_seconds >= 0.0
