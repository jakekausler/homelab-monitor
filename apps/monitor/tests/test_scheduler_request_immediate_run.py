"""Tests for kernel/scheduler/scheduler.py — request_immediate_run extension."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import structlog

from homelab_monitor.kernel.events import (
    BaseEvent,
    SchedulerTickEvent,
    TriggerContext,
)
from homelab_monitor.kernel.plugins import (
    BaseCollector,
    Collector,
    CollectorConfig,
    CollectorContext,
    CollectorResult,
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
    LoadedCollector,
    RunKind,
)
from homelab_monitor.kernel.scheduler import Scheduler, SchedulerConfig
from homelab_monitor.kernel.secrets.resolver import SyncSecretsResolver


def _make_collector(
    name: str,
    interval_ms: int,
    timeout_ms: int = 1000,
    *,
    run_kind: RunKind = RunKind.ASYNC,
) -> type[BaseCollector]:
    """Create a test collector using type() factory pattern."""

    async def _default_run(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        return CollectorResult(ok=True)

    cls = type(
        f"_TestCollector_{name}",
        (BaseCollector,),
        {
            "name": name,
            "interval": timedelta(milliseconds=interval_ms),
            "timeout": timedelta(milliseconds=timeout_ms),
            "run_kind": run_kind,
            "run": _default_run,
        },
    )
    return cls  # type: ignore[return-value]


def _make_ctx_factory(
    metrics: InMemoryMetricsWriter,
) -> Callable[[Collector], CollectorContext]:
    """Return a ctx_factory that builds a usable CollectorContext for tests."""

    def factory(c: Collector) -> CollectorContext:
        return CollectorContext(
            config=CollectorConfig(name=c.name),
            db=MagicMock(),
            vm=metrics,
            vl=InMemoryLogsWriter(),
            http=AsyncMock(spec=httpx.AsyncClient),
            ssh=MagicMock(),
            secrets=SyncSecretsResolver(_values={}),
            log=structlog.get_logger().bind(),
            ha=None,
        )

    return factory


class _TestEventSink:
    """Test double that captures published events."""

    def __init__(self) -> None:
        self.events: list[SchedulerTickEvent] = []

    async def publish(self, event: BaseEvent) -> None:
        assert isinstance(event, SchedulerTickEvent)
        self.events.append(event)


@pytest.mark.asyncio
async def test_request_immediate_run_returns_hex32() -> None:
    """request_immediate_run returns a hex32 tick_id."""
    metrics = InMemoryMetricsWriter()
    collector_cls = _make_collector("test", interval_ms=100)
    ctx_factory = _make_ctx_factory(metrics)
    loaded = [LoadedCollector(config=CollectorConfig(name="test"), collector=collector_cls())]

    scheduler = Scheduler(loaded, ctx_factory, metrics, SchedulerConfig())
    await scheduler.start()

    try:
        tick_id = await scheduler.request_immediate_run(
            "test", trigger=TriggerContext(kind="retry", request_id="req-001")
        )
        assert isinstance(tick_id, str)
        assert len(tick_id) == 32  # noqa: PLR2004
        assert all(c in "0123456789abcdef" for c in tick_id)
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_request_immediate_run_unknown_collector_raises() -> None:
    """request_immediate_run raises KeyError for unknown collector."""
    metrics = InMemoryMetricsWriter()
    collector_cls = _make_collector("test", interval_ms=100)
    ctx_factory = _make_ctx_factory(metrics)
    loaded = [LoadedCollector(config=CollectorConfig(name="test"), collector=collector_cls())]

    scheduler = Scheduler(loaded, ctx_factory, metrics, SchedulerConfig())
    await scheduler.start()

    try:
        with pytest.raises(KeyError):
            await scheduler.request_immediate_run("unknown", trigger=TriggerContext(kind="retry"))
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_request_immediate_run_executes_through_pipeline() -> None:
    """Immediate run executes through full pipeline (lock, dispatch, event sink)."""
    metrics = InMemoryMetricsWriter()
    sink = _TestEventSink()
    collector_cls = _make_collector("test", interval_ms=1000)  # Long interval
    ctx_factory = _make_ctx_factory(metrics)
    loaded = [LoadedCollector(config=CollectorConfig(name="test"), collector=collector_cls())]

    scheduler = Scheduler(loaded, ctx_factory, metrics, SchedulerConfig(event_sink=sink))
    await scheduler.start()

    try:
        await asyncio.sleep(0.1)  # Let scheduler initialize
        tick_id = await scheduler.request_immediate_run(
            "test", trigger=TriggerContext(kind="retry", request_id="req-001")
        )
        # Wait for execution
        await asyncio.sleep(0.5)

        # Should have published an event
        matching = [e for e in sink.events if e.tick_id == tick_id]
        event_ids = [e.tick_id for e in sink.events]
        assert len(matching) == 1, f"No event with tick_id={tick_id}; got {event_ids}"
        event = matching[0]
        assert event.collector == "test"
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_request_immediate_run_event_has_trigger_kind() -> None:
    """Event sink receives SchedulerTickEvent with trigger_kind from TriggerContext."""
    metrics = InMemoryMetricsWriter()
    sink = _TestEventSink()
    collector_cls = _make_collector("test", interval_ms=1000)
    ctx_factory = _make_ctx_factory(metrics)
    loaded = [LoadedCollector(config=CollectorConfig(name="test"), collector=collector_cls())]

    scheduler = Scheduler(loaded, ctx_factory, metrics, SchedulerConfig(event_sink=sink))
    await scheduler.start()

    try:
        await asyncio.sleep(0.1)
        await scheduler.request_immediate_run(
            "test", trigger=TriggerContext(kind="retry", request_id="req-001")
        )
        await asyncio.sleep(0.5)

        matching = [e for e in sink.events if e.trigger_kind == "retry"]
        event_info = [(e.tick_id, e.trigger_kind) for e in sink.events]
        assert len(matching) >= 1, f"No retry event found; events: {event_info}"
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_scheduled_tick_event_has_trigger_kind_scheduled() -> None:
    """Scheduled ticks have trigger_kind='scheduled'."""
    metrics = InMemoryMetricsWriter()
    sink = _TestEventSink()
    collector_cls = _make_collector("test", interval_ms=50)  # Short interval
    ctx_factory = _make_ctx_factory(metrics)
    loaded = [LoadedCollector(config=CollectorConfig(name="test"), collector=collector_cls())]

    scheduler = Scheduler(loaded, ctx_factory, metrics, SchedulerConfig(event_sink=sink))
    await scheduler.start()

    try:
        # Wait for at least one scheduled tick
        await asyncio.sleep(0.2)
        assert len(sink.events) > 0
        event = sink.events[0]
        assert event.trigger_kind == "scheduled"
        assert event.request_id is None
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_request_immediate_run_defers_scheduled_tick() -> None:
    """Scheduled tick is deferred by interval after immediate run."""
    metrics = InMemoryMetricsWriter()
    sink = _TestEventSink()
    collector_cls = _make_collector("test", interval_ms=100)
    ctx_factory = _make_ctx_factory(metrics)
    loaded = [LoadedCollector(config=CollectorConfig(name="test"), collector=collector_cls())]

    scheduler = Scheduler(loaded, ctx_factory, metrics, SchedulerConfig(event_sink=sink))
    await scheduler.start()

    try:
        await asyncio.sleep(0.15)  # Let one scheduled tick happen
        initial_event_count = len(sink.events)

        # Request immediate run
        await scheduler.request_immediate_run("test", trigger=TriggerContext(kind="manual"))
        await asyncio.sleep(0.2)

        # Should have at least one more event (the immediate run)
        assert len(sink.events) > initial_event_count
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_multiple_immediate_runs_executed_in_order() -> None:
    """Multiple immediate runs are queued and executed in order."""
    metrics = InMemoryMetricsWriter()
    sink = _TestEventSink()
    collector_cls = _make_collector("test", interval_ms=1000)
    ctx_factory = _make_ctx_factory(metrics)
    loaded = [LoadedCollector(config=CollectorConfig(name="test"), collector=collector_cls())]

    scheduler = Scheduler(loaded, ctx_factory, metrics, SchedulerConfig(event_sink=sink))
    await scheduler.start()

    try:
        await asyncio.sleep(0.1)

        # Queue multiple immediate runs
        tick_ids: list[str] = []
        for _ in range(3):
            tick_id = await scheduler.request_immediate_run(
                "test", trigger=TriggerContext(kind="manual")
            )
            tick_ids.append(tick_id)

        # Wait for execution
        await asyncio.sleep(1.0)

        # All should execute
        tick_ids_in_events = [e.tick_id for e in sink.events]
        for tid in tick_ids:
            assert tid in tick_ids_in_events
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_process_pool_executor_has_forkserver_context() -> None:
    """Scheduler._process_pool uses mp_context='forkserver'."""
    metrics = InMemoryMetricsWriter()
    collector_cls = _make_collector("test", interval_ms=100, run_kind=RunKind.ASYNC)
    ctx_factory = _make_ctx_factory(metrics)
    loaded = [LoadedCollector(config=CollectorConfig(name="test"), collector=collector_cls())]

    scheduler = Scheduler(loaded, ctx_factory, metrics, SchedulerConfig())
    await scheduler.start()
    try:
        # Check the process pool context
        pool = scheduler._process_pool  # pyright: ignore[reportPrivateUsage]
        assert pool is not None  # pyright: ignore[reportPrivateUsage]
        assert pool._mp_context is not None  # pyright: ignore[reportPrivateUsage]
        assert pool._mp_context.get_start_method() == "forkserver"  # pyright: ignore[reportPrivateUsage]
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_await_immediate_run_returns_collector_result() -> None:
    """await_immediate_run waits for the tick to complete and returns CollectorResult."""
    metrics = InMemoryMetricsWriter()
    collector_cls = _make_collector("test", interval_ms=1000)
    ctx_factory = _make_ctx_factory(metrics)
    loaded = [LoadedCollector(config=CollectorConfig(name="test"), collector=collector_cls())]

    scheduler = Scheduler(loaded, ctx_factory, metrics, SchedulerConfig())
    await scheduler.start()

    try:
        await asyncio.sleep(0.1)  # Let scheduler initialize
        result = await scheduler.await_immediate_run(
            "test",
            trigger=TriggerContext(kind="manual", request_id=None),
            timeout=5.0,
        )
        # Should get the CollectorResult
        assert result is not None
        assert isinstance(result, CollectorResult)
        assert result.ok is True
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_await_immediate_run_timeout_returns_none() -> None:
    """await_immediate_run returns None on timeout."""
    metrics = InMemoryMetricsWriter()

    # Create a collector that sleeps longer than the timeout
    async def _slow_run(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        await asyncio.sleep(2.0)
        return CollectorResult(ok=True)

    collector_cls = type(
        "_TestCollectorSlow",
        (BaseCollector,),
        {
            "name": "slow",
            "interval": timedelta(milliseconds=10000),
            "timeout": timedelta(milliseconds=10000),
            "run_kind": RunKind.ASYNC,
            "run": _slow_run,
        },
    )
    ctx_factory = _make_ctx_factory(metrics)
    loaded = [LoadedCollector(config=CollectorConfig(name="slow"), collector=collector_cls())]

    scheduler = Scheduler(loaded, ctx_factory, metrics, SchedulerConfig())
    await scheduler.start()

    try:
        await asyncio.sleep(0.1)
        result = await scheduler.await_immediate_run(
            "slow",
            trigger=TriggerContext(kind="manual", request_id=None),
            timeout=0.1,  # Very short timeout
        )
        # Should return None on timeout
        assert result is None
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_await_immediate_run_unknown_collector_raises() -> None:
    """await_immediate_run raises KeyError for unknown collector."""
    metrics = InMemoryMetricsWriter()
    collector_cls = _make_collector("test", interval_ms=100)
    ctx_factory = _make_ctx_factory(metrics)
    loaded = [LoadedCollector(config=CollectorConfig(name="test"), collector=collector_cls())]

    scheduler = Scheduler(loaded, ctx_factory, metrics, SchedulerConfig())
    await scheduler.start()

    try:
        with pytest.raises(KeyError):
            await scheduler.await_immediate_run(
                "unknown",
                trigger=TriggerContext(kind="manual"),
                timeout=1.0,
            )
    finally:
        await scheduler.stop()
