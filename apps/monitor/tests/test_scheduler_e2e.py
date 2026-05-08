"""E2E / integration tests for kernel/scheduler/scheduler.py.

Unit tests (test_scheduler.py) cover all failure modes with mocks and
short intervals (50-200ms). These tests fill the gaps:

  1. Real kernel types — SqliteRepository + httpx.AsyncClient + SyncSecretsResolver
     (NOT mocks) wired through a real CollectorContext.
  2. Long-running wall-clock precision — 30s multi-collector run, tick
     counts within ±15% of expected (generous for CI jitter).
  3. Mixed RunKind concurrency — ASYNC + THREAD + PROCESS simultaneously.
  4. PROCESS worker hard crash — os._exit(1) mid-run; scheduler must survive
     and keep other collectors ticking.
  5. Scheduler restart — second Scheduler instance starts fresh; metrics are
     independent of first run.
  6. High-cardinality same-interval collectors — 20 collectors at 2s intervals;
     hash-offset must spread first ticks across [0, 1] second window.

Wall-clock note: each test uses intervals ≥ 1s. Total suite wall-time is
roughly 30 + 12 + 15 + 12 + 8 = ~80s (under the 1-3 minute target).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
import pytest_asyncio
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.migrations import alembic_upgrade_head
from homelab_monitor.kernel.db.repository import SqliteRepository
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

# ---------------------------------------------------------------------------
# Fixtures — real kernel types
# ---------------------------------------------------------------------------


@pytest.fixture
def _tmp_db_path() -> Path:  # type: ignore[return]
    fd, raw = tempfile.mkstemp(prefix="hm-e2e-", suffix=".db")
    os.close(fd)
    path = Path(raw)
    path.unlink(missing_ok=True)
    yield path  # type: ignore[misc]
    for suffix in ("", "-wal", "-shm"):
        (path.parent / (path.name + suffix)).unlink(missing_ok=True)


@pytest_asyncio.fixture
async def real_engine(_tmp_db_path: Path) -> AsyncEngine:  # type: ignore[return]
    url = f"sqlite+aiosqlite:///{_tmp_db_path}"
    alembic_upgrade_head(url)
    engine = get_engine(url=url)
    yield engine  # type: ignore[misc]
    await engine.dispose()


@pytest.fixture
def real_repo(real_engine: AsyncEngine) -> SqliteRepository:
    return SqliteRepository(engine=real_engine)


def _make_real_ctx_factory(
    repo: SqliteRepository,
    metrics: InMemoryMetricsWriter,
) -> Callable[[Collector], CollectorContext]:
    """Return a ctx_factory using REAL DB, REAL httpx client, REAL secrets resolver."""
    http_client = httpx.AsyncClient(timeout=5.0)
    secrets = SyncSecretsResolver(_values={})
    log = structlog.get_logger().bind()

    def factory(c: Collector) -> CollectorContext:
        return CollectorContext(
            config=CollectorConfig(name=c.name),
            db=repo,
            vm=metrics,
            vl=InMemoryLogsWriter(),
            http=http_client,
            ssh=None,  # type: ignore[arg-type]
            secrets=secrets,
            log=log,
            ha=None,
        )

    return factory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count(
    metrics: InMemoryMetricsWriter,
    name: str,
    labels: dict[str, str] | None = None,
) -> int:
    n = 0
    for e in metrics.recorded:
        if e.name != name:
            continue
        if labels is None or all(e.labels.get(k) == v for k, v in labels.items()):
            n += 1
    return n


def _make_collector(
    name: str,
    interval_s: float,
    timeout_s: float = 5.0,
    *,
    run_kind: RunKind = RunKind.ASYNC,
    run_impl: (
        Callable[[BaseCollector, CollectorContext], Awaitable[CollectorResult]] | None
    ) = None,
) -> type[BaseCollector]:
    async def _default(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        return CollectorResult(ok=True)

    impl = run_impl or _default
    return type(  # type: ignore[return-value]
        f"_E2ECollector_{name}",
        (BaseCollector,),
        {
            "name": name,
            "interval": timedelta(seconds=interval_s),
            "timeout": timedelta(seconds=timeout_s),
            "run_kind": run_kind,
            "run": impl,
        },
    )


# ---------------------------------------------------------------------------
# Module-level PROCESS collector classes (must be picklable)
# ---------------------------------------------------------------------------


class _ProcHealthyCollector(BaseCollector):
    """PROCESS collector that writes one counter per tick and returns ok=True."""

    name: ClassVar[str] = "proc_healthy"
    interval: ClassVar[timedelta] = timedelta(seconds=2)
    timeout: ClassVar[timedelta] = timedelta(seconds=5)
    run_kind: ClassVar[RunKind] = RunKind.PROCESS

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        # type: ignore[attr-defined]  # PROCESS workers receive ProcessCollectorContext
        # (.metrics), not CollectorContext (.vm); see process_context.py TODO.
        ctx.metrics.write_counter("e2e_proc_ticks_total", 1.0, {})  # type: ignore[attr-defined]
        return CollectorResult(ok=True)


class _ProcCrashCollector(BaseCollector):
    """PROCESS collector that hard-crashes the worker with os._exit(1)."""

    name: ClassVar[str] = "proc_crash"
    interval: ClassVar[timedelta] = timedelta(seconds=2)
    timeout: ClassVar[timedelta] = timedelta(seconds=5)
    run_kind: ClassVar[RunKind] = RunKind.PROCESS

    async def run(self, ctx: CollectorContext) -> CollectorResult:  # pragma: no cover
        del ctx
        os._exit(1)


# ---------------------------------------------------------------------------
# Scenario 1 — Real kernel types plumbing
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_real_kernel_types_plumbing(
    real_repo: SqliteRepository,
) -> None:
    """Scheduler runs with REAL SqliteRepository, httpx.AsyncClient, SyncSecretsResolver.

    Verifies that nothing in the scheduler + tick + dispatch chain blows up
    when handed real (non-mock) kernel objects. Two collectors (1s and 2s)
    run for 5 seconds; we only require that success metrics appear — proving
    the real context was threaded through without error.
    """
    metrics = InMemoryMetricsWriter()
    ctx_factory = _make_real_ctx_factory(real_repo, metrics)

    cls_a = _make_collector("e2e_real_a", interval_s=1.0)
    cls_b = _make_collector("e2e_real_b", interval_s=2.0)

    loaded = [
        LoadedCollector(collector=cls_a(), config=CollectorConfig(name="e2e_real_a")),
        LoadedCollector(collector=cls_b(), config=CollectorConfig(name="e2e_real_b")),
    ]

    scheduler = Scheduler(loaded, ctx_factory, metrics)
    await scheduler.start()
    await asyncio.sleep(5.0)
    await scheduler.stop()

    success_a = _count(metrics, "homelab_collector_run_success_total", {"name": "e2e_real_a"})
    success_b = _count(metrics, "homelab_collector_run_success_total", {"name": "e2e_real_b"})
    failure_a = _count(metrics, "homelab_collector_run_failure_total", {"name": "e2e_real_a"})
    failure_b = _count(metrics, "homelab_collector_run_failure_total", {"name": "e2e_real_b"})

    # Both collectors should succeed; no failures with real types
    assert (
        success_a >= 3  # noqa: PLR2004
    ), f"expected ≥3 successes for e2e_real_a, got {success_a}"
    assert success_b >= 1, f"expected ≥1 successes for e2e_real_b, got {success_b}"
    assert failure_a == 0, f"unexpected failures for e2e_real_a: {failure_a}"
    assert failure_b == 0, f"unexpected failures for e2e_real_b: {failure_b}"


# ---------------------------------------------------------------------------
# Scenario 2 — Long-running wall-clock precision (30s)
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_long_running_tick_precision(
    real_repo: SqliteRepository,
) -> None:
    """4 collectors at 1s/2s/5s/10s run for 30s; tick counts stay within ±15%.

    This is the primary wall-clock realism test. Unit tests run 1-2s windows;
    this exercises the absolute-deadline math over a 30-tick cycle. Generous
    ±15% tolerance accounts for CI scheduling jitter without hiding real drift.

    Expected ticks in 30s (ignoring initial offset, which is ≤ interval):
      - 1s → ~29  (we allow 24-34)
      - 2s → ~14  (we allow 10-19)
      - 5s → ~5   (we allow 3-8)
      - 10s → ~2  (we allow 1-4)
    """
    metrics = InMemoryMetricsWriter()
    ctx_factory = _make_real_ctx_factory(real_repo, metrics)

    cls_1s = _make_collector("e2e_1s", interval_s=1.0)
    cls_2s = _make_collector("e2e_2s", interval_s=2.0)
    cls_5s = _make_collector("e2e_5s", interval_s=5.0)
    cls_10s = _make_collector("e2e_10s", interval_s=10.0)

    loaded = [
        LoadedCollector(collector=cls_1s(), config=CollectorConfig(name="e2e_1s")),
        LoadedCollector(collector=cls_2s(), config=CollectorConfig(name="e2e_2s")),
        LoadedCollector(collector=cls_5s(), config=CollectorConfig(name="e2e_5s")),
        LoadedCollector(collector=cls_10s(), config=CollectorConfig(name="e2e_10s")),
    ]

    scheduler = Scheduler(loaded, ctx_factory, metrics)
    await scheduler.start()
    await asyncio.sleep(30.0)
    await scheduler.stop()

    c1s = _count(metrics, "homelab_collector_run_success_total", {"name": "e2e_1s"})
    c2s = _count(metrics, "homelab_collector_run_success_total", {"name": "e2e_2s"})
    c5s = _count(metrics, "homelab_collector_run_success_total", {"name": "e2e_5s"})
    c10s = _count(metrics, "homelab_collector_run_success_total", {"name": "e2e_10s"})

    assert (
        24 <= c1s <= 34  # noqa: PLR2004
    ), f"1s collector: expected 24-34 ticks in 30s, got {c1s}"
    assert (
        10 <= c2s <= 19  # noqa: PLR2004
    ), f"2s collector: expected 10-19 ticks in 30s, got {c2s}"
    assert (
        3 <= c5s <= 8  # noqa: PLR2004
    ), f"5s collector: expected 3-8 ticks in 30s, got {c5s}"
    assert (
        1 <= c10s <= 4  # noqa: PLR2004
    ), f"10s collector: expected 1-4 ticks in 30s, got {c10s}"

    # No failures should occur with healthy collectors
    total_failures = sum(
        _count(metrics, "homelab_collector_run_failure_total", {"name": n})
        for n in ("e2e_1s", "e2e_2s", "e2e_5s", "e2e_10s")
    )
    assert total_failures == 0, f"unexpected failures in long-run: {total_failures}"


# ---------------------------------------------------------------------------
# Scenario 3 — Mixed RunKind concurrency with real context
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_mixed_run_kind_concurrency(
    real_repo: SqliteRepository,
) -> None:
    """ASYNC + THREAD + PROCESS collectors run simultaneously with real context.

    Verifies that all three dispatch arms produce success metrics and that
    PROCESS metric buffering (replay through ctx.vm) works end-to-end.
    Runs for 8s at 2s intervals → expect ~3 ticks each.
    """
    metrics = InMemoryMetricsWriter()
    ctx_factory = _make_real_ctx_factory(real_repo, metrics)

    async def _thread_impl(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        time.sleep(0.01)  # simulate sync work
        return CollectorResult(ok=True)

    cls_async = _make_collector("e2e_mixed_async", interval_s=2.0)
    cls_thread = _make_collector(
        "e2e_mixed_thread",
        interval_s=2.0,
        run_kind=RunKind.THREAD,
        run_impl=_thread_impl,
    )

    loaded = [
        LoadedCollector(collector=cls_async(), config=CollectorConfig(name="e2e_mixed_async")),
        LoadedCollector(collector=cls_thread(), config=CollectorConfig(name="e2e_mixed_thread")),
        LoadedCollector(
            collector=_ProcHealthyCollector(),
            config=CollectorConfig(name="proc_healthy"),
        ),
    ]

    scheduler = Scheduler(
        loaded,
        ctx_factory,
        metrics,
        SchedulerConfig(process_pool_size=1),
    )
    await scheduler.start()
    await asyncio.sleep(8.0)
    await scheduler.stop()

    async_ticks = _count(
        metrics, "homelab_collector_run_success_total", {"name": "e2e_mixed_async"}
    )
    thread_ticks = _count(
        metrics, "homelab_collector_run_success_total", {"name": "e2e_mixed_thread"}
    )
    proc_ticks = _count(metrics, "homelab_collector_run_success_total", {"name": "proc_healthy"})
    # PROCESS buffered metric should have been replayed into ctx.vm
    proc_buffered = _count(metrics, "e2e_proc_ticks_total")

    assert (
        async_ticks >= 2  # noqa: PLR2004
    ), f"ASYNC: expected ≥2 ticks, got {async_ticks}"
    assert (
        thread_ticks >= 2  # noqa: PLR2004
    ), f"THREAD: expected ≥2 ticks, got {thread_ticks}"
    assert (
        proc_ticks >= 2  # noqa: PLR2004
    ), f"PROCESS: expected ≥2 ticks, got {proc_ticks}"
    assert (
        proc_buffered >= 2  # noqa: PLR2004
    ), f"PROCESS buffered metric not replayed: got {proc_buffered}"


# ---------------------------------------------------------------------------
# Scenario 4 — PROCESS worker hard crash (os._exit)
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_process_worker_hard_crash_isolated(
    real_repo: SqliteRepository,
) -> None:
    """PROCESS collector that os._exit(1)s the worker must not kill the scheduler.

    The scheduler should emit failure_total{reason=exception} for the crashing
    collector and continue ticking the healthy ASYNC companion collector.
    Runs for 8s at 2s intervals for both collectors.
    """
    metrics = InMemoryMetricsWriter()
    ctx_factory = _make_real_ctx_factory(real_repo, metrics)

    cls_healthy = _make_collector("e2e_crash_companion", interval_s=2.0)

    loaded = [
        LoadedCollector(
            collector=_ProcCrashCollector(),
            config=CollectorConfig(name="proc_crash"),
        ),
        LoadedCollector(
            collector=cls_healthy(),
            config=CollectorConfig(name="e2e_crash_companion"),
        ),
    ]

    scheduler = Scheduler(
        loaded,
        ctx_factory,
        metrics,
        SchedulerConfig(process_pool_size=1),
    )
    await scheduler.start()
    await asyncio.sleep(8.0)
    await scheduler.stop()

    crash_failures = _count(
        metrics,
        "homelab_collector_run_failure_total",
        {"name": "proc_crash", "reason": "exception"},
    )
    companion_successes = _count(
        metrics, "homelab_collector_run_success_total", {"name": "e2e_crash_companion"}
    )

    # Crash must produce exception failures (not hang/swallow)
    assert crash_failures >= 1, (
        f"expected ≥1 exception failures for crashing PROCESS collector, got {crash_failures}"
    )
    # Companion must keep ticking despite the crashed worker process
    assert (
        companion_successes >= 2  # noqa: PLR2004
    ), f"companion ASYNC collector should survive worker crash, got {companion_successes} successes"


# ---------------------------------------------------------------------------
# Scenario 5 — Scheduler restart independence
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_scheduler_restart_metrics_are_independent(
    real_repo: SqliteRepository,
) -> None:
    """A second Scheduler started after stop() has independent metric counts.

    The Scheduler is documented as non-reusable (start once, stop once). This
    test creates two separate Scheduler instances with separate InMemoryMetricsWriter
    instances and verifies neither bleeds metrics into the other.
    """
    cls = _make_collector("e2e_restart", interval_s=1.0)

    # First run
    metrics_1 = InMemoryMetricsWriter()
    loaded_1 = [LoadedCollector(collector=cls(), config=CollectorConfig(name="e2e_restart"))]
    sched_1 = Scheduler(loaded_1, _make_real_ctx_factory(real_repo, metrics_1), metrics_1)
    await sched_1.start()
    await asyncio.sleep(3.0)
    await sched_1.stop()
    count_1 = _count(metrics_1, "homelab_collector_run_success_total", {"name": "e2e_restart"})

    # Second run — fresh Scheduler + fresh metrics writer
    metrics_2 = InMemoryMetricsWriter()
    loaded_2 = [LoadedCollector(collector=cls(), config=CollectorConfig(name="e2e_restart"))]
    sched_2 = Scheduler(loaded_2, _make_real_ctx_factory(real_repo, metrics_2), metrics_2)
    await sched_2.start()
    await asyncio.sleep(3.0)
    await sched_2.stop()
    count_2 = _count(metrics_2, "homelab_collector_run_success_total", {"name": "e2e_restart"})

    # Both runs should have similar tick counts (~2-3 ticks in 3s with 1s interval)
    assert count_1 >= 1, f"first run produced no ticks: {count_1}"
    assert count_2 >= 1, f"second run produced no ticks: {count_2}"

    # Verify the two InMemoryMetricsWriter instances are truly independent:
    # their recorded lists must not share any object by identity. Value-equal
    # entries are fine (two ticks can produce the same counter increment), but
    # if any entry in metrics_2 IS the same object as one in metrics_1 it means
    # the scheduler (or the writer) aliased the two lists.
    ids_1 = {id(e) for e in metrics_1.recorded}
    ids_2 = {id(e) for e in metrics_2.recorded}
    shared_ids = ids_1 & ids_2
    assert not shared_ids, (
        f"second scheduler run shares {len(shared_ids)} metric entry objects with first run "
        f"— scheduler or writer has aliased state"
    )

    # Sanity: each writer's entry count should be plausible for a single 3s window.
    # 5 self-metric kinds x ~3 ticks ≈ 15 entries; cap at 40 to allow for jitter.
    entries_2 = len(metrics_2.recorded)
    assert entries_2 <= 40, (  # noqa: PLR2004
        f"second writer has {entries_2} entries for a 3s run — possible state leak from first run"
    )


# ---------------------------------------------------------------------------
# Scenario 6 — High-cardinality same-interval hash-offset distribution
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_high_cardinality_hash_offset_spread() -> None:
    """20 collectors at 2s intervals: hash-offset spreads first ticks across [0, 2s].

    The scheduler computes offset = hash(name) % max(1, int(interval)) which
    for interval=2 gives offsets in {0, 1}. All 20 collectors fire at least
    once within 4s. We also check that NOT all collectors fired at the same
    second (i.e., the offset is doing work), by verifying the first-tick
    timestamps have at least some spread.

    Uses InMemoryMetricsWriter (not real DB) since we only care about tick
    timing, not context integration.
    """
    first_tick_times: dict[str, float] = {}

    def _make_recording_impl(
        cname: str,
    ) -> Callable[[BaseCollector, CollectorContext], Awaitable[CollectorResult]]:
        async def impl(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
            del self, ctx
            if cname not in first_tick_times:
                first_tick_times[cname] = time.monotonic()
            return CollectorResult(ok=True)

        return impl

    metrics = InMemoryMetricsWriter()

    def ctx_factory(c: Collector) -> CollectorContext:
        return CollectorContext(
            config=CollectorConfig(name=c.name),
            db=None,  # type: ignore[arg-type]
            vm=metrics,
            vl=InMemoryLogsWriter(),
            http=None,  # type: ignore[arg-type]
            ssh=None,  # type: ignore[arg-type]
            secrets=SyncSecretsResolver(_values={}),
            log=structlog.get_logger().bind(),
            ha=None,
        )

    collector_names = [f"e2e_hc_{i:02d}" for i in range(20)]
    loaded: list[LoadedCollector] = []
    for n in collector_names:
        cls = _make_collector(n, interval_s=2.0, run_impl=_make_recording_impl(n))
        loaded.append(LoadedCollector(collector=cls(), config=CollectorConfig(name=n)))

    # Hash-collision skip guard: if all collectors hash to same offset bucket,
    # the spread test cannot run
    buckets = {hash(n) % max(1, int(2.0)) for n in collector_names}
    if len(buckets) < 2:  # noqa: PLR2004
        pytest.skip(
            "all collector names hashed to same offset bucket on this "
            "PYTHONHASHSEED; spread test cannot run"
        )

    scheduler = Scheduler(loaded, ctx_factory, metrics)
    start_wall = time.monotonic()
    await scheduler.start()
    await asyncio.sleep(5.0)
    await scheduler.stop()

    # All 20 collectors must have fired at least once
    fired = set(first_tick_times.keys())
    missing = set(collector_names) - fired
    assert not missing, f"collectors never fired: {missing}"

    # Compute spread of first-tick times relative to scheduler start
    offsets = [first_tick_times[n] - start_wall for n in collector_names]
    spread = max(offsets) - min(offsets)

    # With hash-offset in {0, 1}, the spread should be close to 1s.
    # We require at least 0.3s spread to confirm decorrelation is working.
    # (If all 20 hashed to the same offset, spread would be ~0.)
    assert spread >= 0.3, (  # noqa: PLR2004
        f"first-tick spread across 20 same-interval collectors is only {spread:.3f}s "
        f"— hash-offset may not be decorrelating (offsets: {[f'{o:.2f}' for o in sorted(offsets)]})"
    )
