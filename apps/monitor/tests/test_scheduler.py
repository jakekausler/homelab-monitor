"""Tests for kernel/scheduler/scheduler.py — tick scheduling + dispatch + self-metrics."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import structlog

from homelab_monitor.kernel.plugins import (
    BaseCollector,
    Collector,
    CollectorConfig,
    CollectorContext,
    CollectorResult,
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
    LoadedCollector,
    ProcessCollectorContext,
    RunKind,
)
from homelab_monitor.kernel.scheduler import Scheduler, SchedulerConfig
from homelab_monitor.kernel.secrets.resolver import SyncSecretsResolver

# --- Test fixtures and helpers -----------------------------------------------


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


def _count_metric(
    metrics: InMemoryMetricsWriter,
    name: str,
    *,
    labels_subset: dict[str, str] | None = None,
) -> int:
    """Count entries in ``metrics.recorded`` matching ``name`` and a label subset."""
    n = 0
    for e in metrics.recorded:
        if e.name != name:
            continue
        if labels_subset is None or all(e.labels.get(k) == v for k, v in labels_subset.items()):
            n += 1
    return n


def _make_collector(
    name: str,
    interval_ms: int,
    timeout_ms: int = 1000,
    *,
    run_kind: RunKind = RunKind.ASYNC,
    run_impl: (
        Callable[[BaseCollector, CollectorContext], Awaitable[CollectorResult]] | None
    ) = None,
) -> type[BaseCollector]:
    """Programmatically build a BaseCollector subclass with the given config.

    Inherits ``concurrency_group = "default"`` and
    ``trust_level = TrustLevel.BUILTIN`` from :class:`BaseCollector`.
    STAGE-001-008 will introduce concurrency_group locks; tests that need
    parallelism across collectors will need to override these.
    """

    async def _default_run(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        return CollectorResult(ok=True)

    impl = run_impl or _default_run
    cls = type(
        f"_TestCollector_{name}",
        (BaseCollector,),
        {
            "name": name,
            "interval": timedelta(milliseconds=interval_ms),
            "timeout": timedelta(milliseconds=timeout_ms),
            "run_kind": run_kind,
            "run": impl,
        },
    )
    return cls  # type: ignore[return-value]


# --- PROCESS-mode test helpers (module-level for pickle) --------------------


async def _process_collector_impl(
    self: BaseCollector, ctx: ProcessCollectorContext
) -> CollectorResult:
    """Process-mode collector that writes a metric to its buffering writer."""
    ctx.metrics.write_counter("process_test_total", 1.0, {})
    ctx.metrics.write_gauge("process_test_gauge", 42.0, {})
    ctx.metrics.write_summary("process_test_summary", 0.5, {})
    return CollectorResult(ok=True, metrics_emitted=1)


# Module-level collector class for PROCESS tests — must use class syntax (not
# type()) so pickle can locate it via __module__.__qualname__ in the worker process.
class _ProcessTestCollector(BaseCollector):
    name: ClassVar[str] = "process_test"
    interval: ClassVar[timedelta] = timedelta(milliseconds=100)
    timeout: ClassVar[timedelta] = timedelta(milliseconds=1000)
    run_kind: ClassVar[RunKind] = RunKind.PROCESS

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        return await _process_collector_impl(self, ctx)  # type: ignore[arg-type]


# --- Test cases -----------------------------------------------


@pytest.mark.asyncio
async def test_tick_precision_short_interval() -> None:
    """Single collector at 100ms interval fires ~15 times in 1.5s."""
    metrics = InMemoryMetricsWriter()
    collector_cls = _make_collector("short", interval_ms=100)
    loader_collectors = [
        LoadedCollector(
            collector=collector_cls(),
            config=CollectorConfig(name="short"),
        )
    ]

    scheduler = Scheduler(
        loader_collectors,
        _make_ctx_factory(metrics),
        metrics,
    )
    await scheduler.start()
    await asyncio.sleep(1.5)
    await scheduler.stop()

    count = _count_metric(
        metrics, "homelab_collector_run_success_total", labels_subset={"name": "short"}
    )
    assert 14 <= count <= 16  # noqa: PLR2004


@pytest.mark.asyncio
async def test_multiple_collectors_different_intervals() -> None:
    """Two collectors at 50ms and 200ms run with expected frequency."""
    metrics = InMemoryMetricsWriter()
    collector_a_cls = _make_collector("collector_a", interval_ms=50)
    collector_b_cls = _make_collector("collector_b", interval_ms=200)
    loader_collectors = [
        LoadedCollector(collector=collector_a_cls(), config=CollectorConfig(name="collector_a")),
        LoadedCollector(collector=collector_b_cls(), config=CollectorConfig(name="collector_b")),
    ]

    scheduler = Scheduler(loader_collectors, _make_ctx_factory(metrics), metrics)
    await scheduler.start()
    await asyncio.sleep(1.0)
    await scheduler.stop()

    count_a = _count_metric(
        metrics,
        "homelab_collector_run_success_total",
        labels_subset={"name": "collector_a"},
    )
    count_b = _count_metric(
        metrics,
        "homelab_collector_run_success_total",
        labels_subset={"name": "collector_b"},
    )
    assert 16 <= count_a <= 24  # noqa: PLR2004
    assert 3 <= count_b <= 7  # noqa: PLR2004


@pytest.mark.asyncio
async def test_per_collector_timeout() -> None:
    """Timeout during long-running tick emits failure metric."""

    async def _slow_run(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        await asyncio.sleep(0.2)
        return CollectorResult(ok=True)

    metrics = InMemoryMetricsWriter()
    collector_cls = _make_collector(
        "slow",
        interval_ms=200,
        timeout_ms=50,
        run_impl=_slow_run,
    )
    loader_collectors = [
        LoadedCollector(collector=collector_cls(), config=CollectorConfig(name="slow"))
    ]

    scheduler = Scheduler(loader_collectors, _make_ctx_factory(metrics), metrics)
    await scheduler.start()
    await asyncio.sleep(0.25)
    await scheduler.stop()

    timeout_count = _count_metric(
        metrics,
        "homelab_collector_run_failure_total",
        labels_subset={"name": "slow", "reason": "timeout"},
    )
    error_age_count = _count_metric(
        metrics,
        "homelab_collector_run_last_error_age_seconds",
        labels_subset={"name": "slow"},
    )
    assert timeout_count >= 1
    assert error_age_count >= 1


@pytest.mark.asyncio
async def test_thread_run_kind() -> None:
    """THREAD run_kind does not block ASYNC collectors."""

    async def _thread_sync_sleep(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        time.sleep(0.05)
        return CollectorResult(ok=True)

    metrics = InMemoryMetricsWriter()
    thread_cls = _make_collector(
        "thread_sync",
        interval_ms=100,
        run_kind=RunKind.THREAD,
        run_impl=_thread_sync_sleep,
    )
    async_cls = _make_collector("async_check", interval_ms=100)
    loader_collectors = [
        LoadedCollector(collector=thread_cls(), config=CollectorConfig(name="thread_sync")),
        LoadedCollector(collector=async_cls(), config=CollectorConfig(name="async_check")),
    ]

    scheduler = Scheduler(loader_collectors, _make_ctx_factory(metrics), metrics)
    await scheduler.start()
    await asyncio.sleep(0.4)
    await scheduler.stop()

    thread_count = _count_metric(
        metrics,
        "homelab_collector_run_success_total",
        labels_subset={"name": "thread_sync"},
    )
    async_count = _count_metric(
        metrics,
        "homelab_collector_run_success_total",
        labels_subset={"name": "async_check"},
    )
    assert thread_count >= 1
    assert async_count >= 3  # noqa: PLR2004


@pytest.mark.asyncio
async def test_process_run_kind() -> None:
    """PROCESS run_kind buffers metrics and replays them in parent."""
    metrics = InMemoryMetricsWriter()
    loader_collectors = [
        LoadedCollector(
            collector=_ProcessTestCollector(),
            config=CollectorConfig(name="process_test"),
        )
    ]

    scheduler = Scheduler(
        loader_collectors,
        _make_ctx_factory(metrics),
        metrics,
        SchedulerConfig(process_pool_size=1),
    )
    await scheduler.start()
    await asyncio.sleep(0.6)
    await scheduler.stop()

    success_count = _count_metric(
        metrics,
        "homelab_collector_run_success_total",
        labels_subset={"name": "process_test"},
    )
    # Check that the buffered metric was replayed
    proc_counter = _count_metric(metrics, "process_test_total", labels_subset={})
    proc_gauge = _count_metric(metrics, "process_test_gauge", labels_subset={})
    proc_summary = _count_metric(metrics, "process_test_summary", labels_subset={})
    assert success_count >= 1
    assert proc_counter >= 1
    assert proc_gauge >= 1
    assert proc_summary >= 1


@pytest.mark.asyncio
async def test_initial_offset_decorrelates_ticks() -> None:
    """Initial offset from hash decorrelates same-interval collectors."""
    tick_times: dict[str, list[float]] = {"alpha": [], "beta": []}

    async def _record_tick(
        name: str,
    ) -> Callable[[BaseCollector, CollectorContext], Awaitable[CollectorResult]]:
        async def run_impl(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
            del self, ctx
            tick_times[name].append(time.time())
            return CollectorResult(ok=True)

        return run_impl

    metrics = InMemoryMetricsWriter()
    alpha_cls = _make_collector(
        "alpha",
        interval_ms=3000,
        run_impl=await _record_tick("alpha"),
    )
    beta_cls = _make_collector(
        "beta",
        interval_ms=3000,
        run_impl=await _record_tick("beta"),
    )
    loader_collectors = [
        LoadedCollector(collector=alpha_cls(), config=CollectorConfig(name="alpha")),
        LoadedCollector(collector=beta_cls(), config=CollectorConfig(name="beta")),
    ]

    scheduler = Scheduler(loader_collectors, _make_ctx_factory(metrics), metrics)
    await scheduler.start()
    await asyncio.sleep(4.0)
    await scheduler.stop()

    # Offset = hash(name) % max(1, int(interval)). Skip if hash collision on this interpreter.
    alpha_offset = hash("alpha") % max(1, int(3.0))
    beta_offset = hash("beta") % max(1, int(3.0))
    if alpha_offset == beta_offset:
        pytest.skip(
            "hash('alpha') % 3 == hash('beta') % 3 on this interpreter; "
            "offset decorrelation impossible by design",
        )
    if tick_times["alpha"] and tick_times["beta"]:
        diff = abs(tick_times["alpha"][0] - tick_times["beta"][0])
        assert diff > 0.01  # At least some separation  # noqa: PLR2004


@pytest.mark.asyncio
async def test_graceful_shutdown_cancels_tick_loops() -> None:
    """Graceful stop completes without exception and sets running=False."""
    metrics = InMemoryMetricsWriter()
    collector_cls = _make_collector("noop_test", interval_ms=100)
    loader_collectors = [
        LoadedCollector(collector=collector_cls(), config=CollectorConfig(name="noop_test"))
    ]

    scheduler = Scheduler(loader_collectors, _make_ctx_factory(metrics), metrics)
    await scheduler.start()
    assert scheduler.running
    await asyncio.sleep(0.2)
    await scheduler.stop()
    assert not scheduler.running


@pytest.mark.asyncio
async def test_in_flight_tick_during_shutdown_emits_shutdown_metric() -> None:
    """In-flight tick during graceful stop emits shutdown_total (not failure_total)."""

    async def _long_sleep(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        await asyncio.sleep(0.5)
        return CollectorResult(ok=True)

    metrics = InMemoryMetricsWriter()
    collector_cls = _make_collector(
        "long_sleep",
        interval_ms=100,
        timeout_ms=10000,
        run_impl=_long_sleep,
    )
    loader_collectors = [
        LoadedCollector(collector=collector_cls(), config=CollectorConfig(name="long_sleep"))
    ]

    scheduler = Scheduler(loader_collectors, _make_ctx_factory(metrics), metrics)
    await scheduler.start()
    await asyncio.sleep(0.05)
    await scheduler.stop()

    shutdown_count = _count_metric(
        metrics,
        "homelab_collector_run_shutdown_total",
        labels_subset={"name": "long_sleep"},
    )
    assert shutdown_count >= 1


@pytest.mark.asyncio
async def test_failure_metrics_emitted_on_exception() -> None:
    """Exception in collector emits failure_total{reason=exception}."""

    async def _raise_error(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        raise RuntimeError("boom")

    metrics = InMemoryMetricsWriter()
    collector_cls = _make_collector(
        "error_raiser",
        interval_ms=100,
        run_impl=_raise_error,
    )
    loader_collectors = [
        LoadedCollector(
            collector=collector_cls(),
            config=CollectorConfig(name="error_raiser"),
        )
    ]

    scheduler = Scheduler(loader_collectors, _make_ctx_factory(metrics), metrics)
    await scheduler.start()
    await asyncio.sleep(0.2)
    await scheduler.stop()

    failure_count = _count_metric(
        metrics,
        "homelab_collector_run_failure_total",
        labels_subset={"name": "error_raiser", "reason": "exception"},
    )
    error_age_count = _count_metric(
        metrics,
        "homelab_collector_run_last_error_age_seconds",
        labels_subset={"name": "error_raiser"},
    )
    assert failure_count >= 1
    assert error_age_count >= 1


@pytest.mark.asyncio
async def test_last_error_age_decays_after_recovery() -> None:
    """last_error_age_seconds emitted only after failure, decays on success."""

    class _StatefulCollector(BaseCollector):
        name: ClassVar[str] = "stateful"
        interval: ClassVar[timedelta] = timedelta(milliseconds=100)
        timeout: ClassVar[timedelta] = timedelta(seconds=5)
        tick_count = 0

        async def run(self, ctx: CollectorContext) -> CollectorResult:
            del ctx
            self.tick_count += 1
            if self.tick_count <= 2:  # noqa: PLR2004
                raise RuntimeError("fail")
            return CollectorResult(ok=True)

    metrics = InMemoryMetricsWriter()
    collector = _StatefulCollector()
    loader_collectors = [
        LoadedCollector(collector=collector, config=CollectorConfig(name="stateful"))
    ]

    scheduler = Scheduler(loader_collectors, _make_ctx_factory(metrics), metrics)
    await scheduler.start()
    await asyncio.sleep(0.6)
    await scheduler.stop()

    # Check that we have both zero and non-zero error-age gauges
    zero_age = [
        e
        for e in metrics.recorded
        if e.name == "homelab_collector_run_last_error_age_seconds" and e.value == 0.0
    ]
    positive_age = [
        e
        for e in metrics.recorded
        if e.name == "homelab_collector_run_last_error_age_seconds" and e.value > 0.0
    ]
    assert len(zero_age) >= 1
    assert len(positive_age) >= 1


@pytest.mark.asyncio
async def test_result_error_emits_failure_metric() -> None:
    """CollectorResult(ok=False) emits failure_total{reason=result_error}."""

    async def _return_error(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        return CollectorResult(ok=False, errors=["bad"])

    metrics = InMemoryMetricsWriter()
    collector_cls = _make_collector(
        "result_error",
        interval_ms=100,
        run_impl=_return_error,
    )
    loader_collectors = [
        LoadedCollector(
            collector=collector_cls(),
            config=CollectorConfig(name="result_error"),
        )
    ]

    scheduler = Scheduler(loader_collectors, _make_ctx_factory(metrics), metrics)
    await scheduler.start()
    await asyncio.sleep(0.2)
    await scheduler.stop()

    result_error_count = _count_metric(
        metrics,
        "homelab_collector_run_failure_total",
        labels_subset={"name": "result_error", "reason": "result_error"},
    )
    exception_count = _count_metric(
        metrics,
        "homelab_collector_run_failure_total",
        labels_subset={"name": "result_error", "reason": "exception"},
    )
    error_age_count = _count_metric(
        metrics,
        "homelab_collector_run_last_error_age_seconds",
        labels_subset={"name": "result_error"},
    )
    assert result_error_count >= 1
    assert exception_count == 0
    assert error_age_count >= 1


@pytest.mark.asyncio
async def test_drift_skip_forward_no_catchup() -> None:
    """Long tick followed by short interval doesn't cause catch-up storm."""

    class _DriftCollector(BaseCollector):
        name: ClassVar[str] = "drift_test"
        interval: ClassVar[timedelta] = timedelta(milliseconds=50)
        timeout: ClassVar[timedelta] = timedelta(seconds=5)
        tick_count: int = 0

        async def run(self, ctx: CollectorContext) -> CollectorResult:
            del ctx
            if self.tick_count == 0:
                await asyncio.sleep(0.3)
            self.tick_count += 1
            return CollectorResult(ok=True)

    metrics = InMemoryMetricsWriter()
    collector = _DriftCollector()
    loader_collectors = [
        LoadedCollector(collector=collector, config=CollectorConfig(name="drift_test"))
    ]

    scheduler = Scheduler(loader_collectors, _make_ctx_factory(metrics), metrics)
    await scheduler.start()
    await asyncio.sleep(0.8)
    await scheduler.stop()

    count = _count_metric(
        metrics,
        "homelab_collector_run_success_total",
        labels_subset={"name": "drift_test"},
    )
    assert count <= 16  # noqa: PLR2004


@pytest.mark.asyncio
async def test_scheduler_running_property() -> None:
    """running property reflects scheduler lifecycle."""
    metrics = InMemoryMetricsWriter()
    collector_cls = _make_collector("prop_test", interval_ms=100)
    loader_collectors = [
        LoadedCollector(collector=collector_cls(), config=CollectorConfig(name="prop_test"))
    ]

    scheduler = Scheduler(loader_collectors, _make_ctx_factory(metrics), metrics)
    assert not scheduler.running
    await scheduler.start()
    assert scheduler.running
    await scheduler.stop()
    assert not scheduler.running


@pytest.mark.asyncio
async def test_scheduler_double_start_raises() -> None:
    """Second start() without stop() raises RuntimeError."""
    metrics = InMemoryMetricsWriter()
    collector_cls = _make_collector("double_start", interval_ms=100)
    loader_collectors = [
        LoadedCollector(
            collector=collector_cls(),
            config=CollectorConfig(name="double_start"),
        )
    ]

    scheduler = Scheduler(loader_collectors, _make_ctx_factory(metrics), metrics)
    await scheduler.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            await scheduler.start()
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_scheduler_stop_with_no_inflight() -> None:
    """stop() completes cleanly when called before any tick fires."""
    metrics = InMemoryMetricsWriter()
    collector_cls = _make_collector("no_inflight", interval_ms=10000)
    loader_collectors = [
        LoadedCollector(
            collector=collector_cls(),
            config=CollectorConfig(name="no_inflight"),
        )
    ]

    scheduler = Scheduler(loader_collectors, _make_ctx_factory(metrics), metrics)
    await scheduler.start()
    await scheduler.stop()
    assert not scheduler.running


@pytest.mark.asyncio
async def test_scheduler_empty_loaded_list() -> None:
    """Scheduler with no collectors starts and stops cleanly."""
    metrics = InMemoryMetricsWriter()

    scheduler = Scheduler([], _make_ctx_factory(metrics), metrics)
    assert not scheduler.running
    await scheduler.start()
    assert not scheduler.running  # Still False because tick_tasks is empty
    await scheduler.stop()
    assert not scheduler.running


@pytest.mark.asyncio
async def test_last_error_age_not_emitted_before_any_failure() -> None:
    """Spec pattern (ii): never emit last_error_age_seconds before first failure.

    A collector that always succeeds should produce zero
    homelab_collector_run_last_error_age_seconds gauge entries.
    """
    metrics = InMemoryMetricsWriter()
    collector_cls = _make_collector("always_ok", interval_ms=50)
    loaded = [
        LoadedCollector(
            collector=collector_cls(),
            config=CollectorConfig(name="always_ok"),
        )
    ]
    sched = Scheduler(loaded, _make_ctx_factory(metrics), metrics)
    await sched.start()
    await asyncio.sleep(0.3)
    await sched.stop()
    age_count = sum(
        1 for e in metrics.recorded if e.name == "homelab_collector_run_last_error_age_seconds"
    )
    assert age_count == 0


@pytest.mark.asyncio
async def test_shutdown_grace_timeout_force_cancels() -> None:
    """In-flight ticks that outlive shutdown_grace_seconds are force-cancelled.

    Drives the TimeoutError branch in Scheduler.stop() — covers the defensive
    second-pass cancellation guard.
    """

    async def _ignore_cancel(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        # Shield against cancellation so the grace timeout actually fires.
        try:
            await asyncio.shield(asyncio.sleep(2.0))
        except asyncio.CancelledError:
            await asyncio.sleep(0.5)
            raise
        return CollectorResult(ok=True)

    metrics = InMemoryMetricsWriter()
    collector_cls = _make_collector(
        "grace_timeout",
        interval_ms=50,
        timeout_ms=20000,
        run_impl=_ignore_cancel,
    )
    loaded = [
        LoadedCollector(
            collector=collector_cls(),
            config=CollectorConfig(name="grace_timeout"),
        )
    ]
    scheduler = Scheduler(
        loaded,
        _make_ctx_factory(metrics),
        metrics,
        SchedulerConfig(shutdown_grace_seconds=0.05),
    )
    await scheduler.start()
    await asyncio.sleep(0.08)  # Let one tick start
    await scheduler.stop()
    assert not scheduler.running
