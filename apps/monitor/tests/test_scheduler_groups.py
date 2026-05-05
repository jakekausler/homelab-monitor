"""Tests for Scheduler concurrency groups and skip-on-busy."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta as td
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.plugins import (
    BaseCollector,
    CollectorConfig,
    CollectorContext,
    CollectorResult,
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
    LoadedCollector,
    RunKind,
)
from homelab_monitor.kernel.plugins.base import Collector
from homelab_monitor.kernel.scheduler.scheduler import Scheduler
from homelab_monitor.kernel.secrets.resolver import SyncSecretsResolver


@pytest.fixture
async def repo_with_migrations(tmp_path: Path) -> SqliteRepository:
    """Create a repo with migrations applied through 0003."""
    db_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    repo = SqliteRepository(engine)

    async with repo.transaction() as conn:
        await conn.execute(
            text("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id TEXT PRIMARY KEY,
                who TEXT NOT NULL,
                what TEXT NOT NULL,
                "when" TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                ip TEXT
            )
            """)
        )
        await conn.execute(
            text("""
            CREATE TABLE IF NOT EXISTS collectors (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                config TEXT,
                created_at TEXT NOT NULL,
                quarantined_at TEXT,
                quarantine_reason TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0
            )
            """)
        )
        # Insert test collector rows
        await conn.execute(
            text("""
            INSERT INTO collectors (id, name, created_at)
            VALUES ('test-id-1', 'collector_1', '2026-01-01T00:00:00Z'),
                   ('test-id-2', 'collector_2', '2026-01-01T00:00:00Z')
            """)
        )

    return repo


@pytest.fixture
def logger() -> structlog.stdlib.BoundLogger:
    """Return a structlog BoundLogger for testing."""
    return structlog.get_logger()


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


def _make_collector(
    name: str,
    interval_ms: int = 1000,
    timeout_ms: int = 1000,
    *,
    concurrency_group: str = "default",
    run_impl: Callable[[BaseCollector, CollectorContext], Awaitable[CollectorResult]] | None = None,
) -> type[BaseCollector]:
    """Programmatically build a BaseCollector subclass with the given config."""

    async def _default_run(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        return CollectorResult(ok=True)

    impl = run_impl or _default_run
    cls = type(
        f"_TestCollector_{name}",
        (BaseCollector,),
        {
            "name": name,
            "interval": td(milliseconds=interval_ms),
            "timeout": td(milliseconds=timeout_ms),
            "run_kind": RunKind.ASYNC,
            "concurrency_group": concurrency_group,
            "run": impl,
        },
    )
    return cls  # type: ignore[return-value]


# Default group tests


@pytest.mark.asyncio
async def test_default_group_resolves_to_per_collector_lock(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Collectors with default group get solo locks (can run concurrently)."""
    Cls1 = _make_collector("collector_1")
    Cls2 = _make_collector("collector_2")
    collector1 = Cls1()
    collector2 = Cls2()

    config1 = CollectorConfig(name="collector_1")
    config2 = CollectorConfig(name="collector_2")
    loaded = [
        LoadedCollector(collector=collector1, config=config1),
        LoadedCollector(collector=collector2, config=config2),
    ]

    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(loaded, _make_ctx_factory(metrics), metrics)
    scheduler._loop = asyncio.get_running_loop()  # pyright: ignore[reportPrivateUsage]

    # Both collectors should have different lock keys
    # collector_1 -> "collector_1" (default resolves to name)
    # collector_2 -> "collector_2" (default resolves to name)
    # So they should not block each other
    await scheduler._tick(collector1)  # pyright: ignore[reportPrivateUsage]
    await scheduler._tick(collector2)  # pyright: ignore[reportPrivateUsage]

    # Both should have dispatched (no skip metrics)
    skipped = [e for e in metrics.recorded if e.name == "homelab_collector_run_skipped_total"]
    assert len(skipped) == 0


# Named group serialization tests


@pytest.mark.asyncio
async def test_named_group_serializes_collectors(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Collectors in same named group share a lock."""
    Cls1 = _make_collector("collector_1", concurrency_group="api")
    Cls2 = _make_collector("collector_2", concurrency_group="api")
    collector1 = Cls1()
    collector2 = Cls2()

    config1 = CollectorConfig(name="collector_1")
    config2 = CollectorConfig(name="collector_2")
    loaded = [
        LoadedCollector(collector=collector1, config=config1),
        LoadedCollector(collector=collector2, config=config2),
    ]

    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(loaded, _make_ctx_factory(metrics), metrics)
    scheduler._loop = asyncio.get_running_loop()  # pyright: ignore[reportPrivateUsage]

    # Manually hold the lock for the "api" group
    api_lock = scheduler._group_locks.setdefault("api", asyncio.Lock())  # pyright: ignore[reportPrivateUsage]
    await api_lock.acquire()

    # Now tick collector2 (which shares the group)
    # It should timeout waiting for the lock and skip
    await scheduler._tick(collector2)  # pyright: ignore[reportPrivateUsage]

    # Release lock
    api_lock.release()

    # Should have a skip metric for group_busy
    skipped = [
        e
        for e in metrics.recorded
        if e.name == "homelab_collector_run_skipped_total"
        and e.labels.get("reason") == "group_busy"
    ]
    assert len(skipped) == 1


# Skip-on-busy tests


@pytest.mark.asyncio
async def test_skip_on_busy_emits_metric(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Scheduler emits skipped metric when group lock is busy."""
    Cls = _make_collector("collector_1", concurrency_group="api")
    collector = Cls()
    config = CollectorConfig(name="collector_1")
    loaded = [LoadedCollector(collector=collector, config=config)]

    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(loaded, _make_ctx_factory(metrics), metrics)
    scheduler._loop = asyncio.get_running_loop()  # pyright: ignore[reportPrivateUsage]

    # Acquire the group lock
    api_lock = scheduler._group_locks.setdefault("api", asyncio.Lock())  # pyright: ignore[reportPrivateUsage]
    await api_lock.acquire()

    # Tick should timeout and skip
    await scheduler._tick(collector)  # pyright: ignore[reportPrivateUsage]

    api_lock.release()

    # Check for skipped metric
    skipped = [
        e
        for e in metrics.recorded
        if e.name == "homelab_collector_run_skipped_total"
        and e.labels.get("reason") == "group_busy"
    ]
    assert len(skipped) == 1


@pytest.mark.asyncio
async def test_skip_on_busy_does_not_dispatch(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Scheduler does not call dispatch when group lock times out."""
    Cls = _make_collector("collector_1", concurrency_group="api")
    collector = Cls()
    config = CollectorConfig(name="collector_1")
    loaded = [LoadedCollector(collector=collector, config=config)]

    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(loaded, _make_ctx_factory(metrics), metrics)
    scheduler._loop = asyncio.get_running_loop()  # pyright: ignore[reportPrivateUsage]

    # Acquire lock
    api_lock = scheduler._group_locks.setdefault("api", asyncio.Lock())  # pyright: ignore[reportPrivateUsage]
    await api_lock.acquire()

    # Mock dispatch to track calls
    dispatch_called = False

    async def mock_dispatch(c: Collector, ctx: CollectorContext) -> CollectorResult:
        nonlocal dispatch_called
        dispatch_called = True
        return CollectorResult(ok=True)

    scheduler._dispatch = mock_dispatch  # pyright: ignore[reportPrivateUsage]

    await scheduler._tick(collector)  # pyright: ignore[reportPrivateUsage]
    api_lock.release()

    assert dispatch_called is False


# Lock initialization tests


@pytest.mark.asyncio
async def test_group_lock_lazy_initialization(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Group locks are created lazily on first tick."""
    Cls = _make_collector("collector_1", concurrency_group="api")
    collector = Cls()
    config = CollectorConfig(name="collector_1")
    loaded = [LoadedCollector(collector=collector, config=config)]

    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(loaded, _make_ctx_factory(metrics), metrics)
    scheduler._loop = asyncio.get_running_loop()  # pyright: ignore[reportPrivateUsage]

    # Before tick, no locks
    assert len(scheduler._group_locks) == 0  # pyright: ignore[reportPrivateUsage]

    await scheduler._tick(collector)  # pyright: ignore[reportPrivateUsage]

    # After tick, "api" lock should exist
    assert "api" in scheduler._group_locks  # pyright: ignore[reportPrivateUsage]


# Lock release tests


@pytest.mark.asyncio
async def test_group_lock_released_on_failure(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Group lock is released even when collector fails."""

    async def _failing_run(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        raise ValueError("test failure")

    Cls = _make_collector("collector_1", concurrency_group="api", run_impl=_failing_run)
    collector = Cls()
    config = CollectorConfig(name="collector_1")
    loaded = [LoadedCollector(collector=collector, config=config)]

    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(loaded, _make_ctx_factory(metrics), metrics)
    scheduler._loop = asyncio.get_running_loop()  # pyright: ignore[reportPrivateUsage]

    await scheduler._tick(collector)  # pyright: ignore[reportPrivateUsage]

    # Lock should be released, so we should be able to acquire it
    api_lock = scheduler._group_locks["api"]  # pyright: ignore[reportPrivateUsage]
    acquired = api_lock.locked() is False  # Not locked = was released
    assert acquired is True


@pytest.mark.asyncio
async def test_group_lock_released_on_timeout(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Group lock is released even when collector times out."""

    async def _timeout_run(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        await asyncio.sleep(1000)  # Will timeout
        return CollectorResult(ok=True)

    Cls = _make_collector(
        "collector_1", timeout_ms=10, concurrency_group="api", run_impl=_timeout_run
    )
    collector = Cls()

    config = CollectorConfig(name="collector_1")
    loaded = [LoadedCollector(collector=collector, config=config)]

    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(loaded, _make_ctx_factory(metrics), metrics)
    scheduler._loop = asyncio.get_running_loop()  # pyright: ignore[reportPrivateUsage]

    await scheduler._tick(collector)  # pyright: ignore[reportPrivateUsage]

    # Lock should be released
    api_lock = scheduler._group_locks["api"]  # pyright: ignore[reportPrivateUsage]
    assert api_lock.locked() is False
