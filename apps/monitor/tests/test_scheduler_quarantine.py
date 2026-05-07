"""Tests for Scheduler + FailureBudget quarantine integration."""

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

from homelab_monitor.kernel.alerts.repository import AlertRepository
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.plugins import (
    CollectorConfig,
    CollectorContext,
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
)
from homelab_monitor.kernel.plugins.base import BaseCollector, Collector
from homelab_monitor.kernel.plugins.loader import LoadedCollector
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind
from homelab_monitor.kernel.scheduler.failure_budget import FailureBudget, QuarantineState
from homelab_monitor.kernel.scheduler.scheduler import (
    Scheduler,
    _extract_last_reason,  # pyright: ignore[reportPrivateUsage]
)
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
        await conn.execute(
            text("""
            INSERT INTO collectors (id, name, created_at)
            VALUES ('test-id', 'test_collector', '2026-01-01T00:00:00Z')
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


# Quarantine gate tests


@pytest.mark.asyncio
async def test_quarantine_gate_skips_tick(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Scheduler skips dispatch when collector is quarantined."""
    Cls = _make_collector("test_collector")
    fake_collector = Cls()
    config = CollectorConfig(name="test_collector")
    loaded = [LoadedCollector(collector=fake_collector, config=config)]

    budget = FailureBudget(repo_with_migrations, logger)
    # Manually quarantine
    budget._consecutive_failures["test_collector"] = 5  # pyright: ignore[reportPrivateUsage]
    budget._quarantined["test_collector"] = QuarantineState(  # pyright: ignore[reportPrivateUsage]
        consecutive_failures=5,
        quarantined_at="2026-05-05T00:00:00Z",
        quarantine_reason="test quarantine",
    )

    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(
        loaded,
        _make_ctx_factory(metrics),
        metrics,
        failure_budget=budget,
    )
    scheduler._loop = asyncio.get_running_loop()  # pyright: ignore[reportPrivateUsage]

    # Dispatch should be mocked to track if it was called
    original_dispatch = scheduler._dispatch  # pyright: ignore[reportPrivateUsage]
    dispatch_called = False

    async def mock_dispatch(c: Collector, ctx: CollectorContext) -> CollectorResult:
        nonlocal dispatch_called
        dispatch_called = True
        return await original_dispatch(c, ctx)

    scheduler._dispatch = mock_dispatch  # pyright: ignore[reportPrivateUsage]

    # Run tick directly
    await scheduler._tick(fake_collector)  # pyright: ignore[reportPrivateUsage]

    assert dispatch_called is False


@pytest.mark.asyncio
async def test_quarantine_gate_emits_skipped_metric(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Scheduler emits skipped metric when quarantined."""
    Cls = _make_collector("test_collector")
    fake_collector = Cls()
    config = CollectorConfig(name="test_collector")
    loaded = [LoadedCollector(collector=fake_collector, config=config)]

    budget = FailureBudget(repo_with_migrations, logger)
    budget._consecutive_failures["test_collector"] = 5  # pyright: ignore[reportPrivateUsage]
    budget._quarantined["test_collector"] = QuarantineState(  # pyright: ignore[reportPrivateUsage]
        consecutive_failures=5,
        quarantined_at="2026-05-05T00:00:00Z",
        quarantine_reason="test quarantine",
    )

    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(
        loaded,
        _make_ctx_factory(metrics),
        metrics,
        failure_budget=budget,
    )
    scheduler._loop = asyncio.get_running_loop()  # pyright: ignore[reportPrivateUsage]

    await scheduler._tick(fake_collector)  # pyright: ignore[reportPrivateUsage]

    # Check metrics for skipped counter
    skipped_metrics = [
        m
        for m in metrics.recorded
        if m.name == "homelab_collector_run_skipped_total"
        and m.labels.get("reason") == "quarantined"
    ]
    assert len(skipped_metrics) == 1


@pytest.mark.asyncio
async def test_clear_quarantine_resumes_ticking(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Scheduler resumes dispatch after clear_quarantine."""
    Cls = _make_collector("test_collector")
    fake_collector = Cls()
    config = CollectorConfig(name="test_collector")
    loaded = [LoadedCollector(collector=fake_collector, config=config)]

    budget = FailureBudget(repo_with_migrations, logger)
    budget._consecutive_failures["test_collector"] = 5  # pyright: ignore[reportPrivateUsage]
    budget._quarantined["test_collector"] = QuarantineState(  # pyright: ignore[reportPrivateUsage]
        consecutive_failures=5,
        quarantined_at="2026-05-05T00:00:00Z",
        quarantine_reason="test quarantine",
    )

    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(
        loaded,
        _make_ctx_factory(metrics),
        metrics,
        failure_budget=budget,
    )
    scheduler._loop = asyncio.get_running_loop()  # pyright: ignore[reportPrivateUsage]

    await scheduler.clear_quarantine("test_collector")
    assert budget.is_quarantined("test_collector") is False

    # Now tick should dispatch
    await scheduler._tick(fake_collector)  # pyright: ignore[reportPrivateUsage]

    # Should have a success metric
    success_metrics = [
        m for m in metrics.recorded if m.name == "homelab_collector_run_success_total"
    ]
    assert len(success_metrics) == 1


# Failure budget recording tests


@pytest.mark.asyncio
async def test_failure_budget_increments_on_tick_failure(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Scheduler calls record_failure when tick fails."""

    async def _failing_run(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        raise ValueError("test failure")

    Cls = _make_collector("test_collector", run_impl=_failing_run)
    fake_collector = Cls()
    config = CollectorConfig(name="test_collector")
    loaded = [LoadedCollector(collector=fake_collector, config=config)]

    budget = FailureBudget(repo_with_migrations, logger)

    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(
        loaded,
        _make_ctx_factory(metrics),
        metrics,
        failure_budget=budget,
    )
    scheduler._loop = asyncio.get_running_loop()  # pyright: ignore[reportPrivateUsage]

    await scheduler._tick(fake_collector)  # pyright: ignore[reportPrivateUsage]
    assert budget.consecutive_failures("test_collector") == 1


@pytest.mark.asyncio
async def test_quarantine_after_5_consecutive_failures(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Scheduler quarantines after default 5 failures."""

    async def _failing_run(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        raise ValueError("test failure")

    Cls = _make_collector("test_collector", run_impl=_failing_run)
    fake_collector = Cls()
    config = CollectorConfig(name="test_collector")
    loaded = [LoadedCollector(collector=fake_collector, config=config)]

    budget = FailureBudget(repo_with_migrations, logger)

    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(
        loaded,
        _make_ctx_factory(metrics),
        metrics,
        failure_budget=budget,
    )
    scheduler._loop = asyncio.get_running_loop()  # pyright: ignore[reportPrivateUsage]

    for _ in range(5):
        await scheduler._tick(fake_collector)  # pyright: ignore[reportPrivateUsage]

    assert budget.is_quarantined("test_collector") is True


@pytest.mark.asyncio
async def test_success_resets_in_memory_counter(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Scheduler calls record_success on successful tick."""
    Cls = _make_collector("test_collector")
    fake_collector = Cls()
    config = CollectorConfig(name="test_collector")
    loaded = [LoadedCollector(collector=fake_collector, config=config)]

    budget = FailureBudget(repo_with_migrations, logger)

    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(
        loaded,
        _make_ctx_factory(metrics),
        metrics,
        failure_budget=budget,
    )
    scheduler._loop = asyncio.get_running_loop()  # pyright: ignore[reportPrivateUsage]

    # Fail 3 times - create a failing version
    async def _failing_run(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        raise ValueError("test failure")

    FailingCls = _make_collector("test_collector", run_impl=_failing_run)
    failing_collector = FailingCls()
    for _ in range(3):
        await scheduler._tick(failing_collector)  # pyright: ignore[reportPrivateUsage]

    assert budget.consecutive_failures("test_collector") == 3  # noqa: PLR2004

    # Succeed once with normal collector
    await scheduler._tick(fake_collector)  # pyright: ignore[reportPrivateUsage]

    assert budget.consecutive_failures("test_collector") == 0


@pytest.mark.asyncio
async def test_per_collector_quarantine_after_override(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Scheduler respects per-collector quarantine_after override."""

    async def _failing_run(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        raise ValueError("test failure")

    Cls = _make_collector("test_collector", run_impl=_failing_run)
    fake_collector = Cls()
    config = CollectorConfig(name="test_collector", quarantine_after=2)
    loaded = [LoadedCollector(collector=fake_collector, config=config)]

    budget = FailureBudget(repo_with_migrations, logger)

    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(
        loaded,
        _make_ctx_factory(metrics),
        metrics,
        failure_budget=budget,
    )
    scheduler._loop = asyncio.get_running_loop()  # pyright: ignore[reportPrivateUsage]

    await scheduler._tick(fake_collector)  # pyright: ignore[reportPrivateUsage]
    assert budget.is_quarantined("test_collector") is False

    await scheduler._tick(fake_collector)  # pyright: ignore[reportPrivateUsage]
    assert budget.is_quarantined("test_collector") is True


@pytest.mark.asyncio
async def test_scheduler_load_state_called_on_start(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Scheduler calls load_state during start."""
    # Preload DB with counter=3
    async with repo_with_migrations.transaction() as conn:
        await conn.execute(
            text("UPDATE collectors SET consecutive_failures = 3 WHERE name = :name"),
            {"name": "test_collector"},
        )

    Cls = _make_collector("test_collector")
    fake_collector = Cls()
    config = CollectorConfig(name="test_collector")
    loaded = [LoadedCollector(collector=fake_collector, config=config)]

    budget = FailureBudget(repo_with_migrations, logger)

    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(
        loaded,
        _make_ctx_factory(metrics),
        metrics,
        failure_budget=budget,
    )

    await scheduler.start()
    # After start, budget should have loaded state
    assert budget.consecutive_failures("test_collector") == 3  # noqa: PLR2004
    await scheduler.stop()


@pytest.mark.asyncio
async def test_clear_quarantine_method_delegates(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Scheduler.clear_quarantine delegates to FailureBudget."""
    Cls = _make_collector("test_collector")
    fake_collector = Cls()
    config = CollectorConfig(name="test_collector")
    loaded = [LoadedCollector(collector=fake_collector, config=config)]

    budget = FailureBudget(repo_with_migrations, logger)
    # Manually quarantine
    budget._consecutive_failures["test_collector"] = 5  # pyright: ignore[reportPrivateUsage]
    budget._quarantined["test_collector"] = QuarantineState(  # pyright: ignore[reportPrivateUsage]
        consecutive_failures=5,
        quarantined_at="2026-05-05T00:00:00Z",
        quarantine_reason="test quarantine",
    )

    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(
        loaded,
        _make_ctx_factory(metrics),
        metrics,
        failure_budget=budget,
    )

    await scheduler.clear_quarantine("test_collector", by="alice")
    assert budget.is_quarantined("test_collector") is False


@pytest.mark.asyncio
async def test_clear_quarantine_without_failure_budget_raises(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Scheduler.clear_quarantine raises if no FailureBudget."""
    Cls = _make_collector("test_collector")
    fake_collector = Cls()
    config = CollectorConfig(name="test_collector")
    loaded = [LoadedCollector(collector=fake_collector, config=config)]

    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(
        loaded,
        _make_ctx_factory(metrics),
        metrics,
        failure_budget=None,
    )

    with pytest.raises(RuntimeError, match="without a FailureBudget"):
        await scheduler.clear_quarantine("test_collector")


@pytest.mark.asyncio
async def test_scheduler_without_failure_budget_skips_quarantine_logic(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Scheduler without FailureBudget skips all quarantine logic."""

    async def _failing_run(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        raise ValueError("test failure")

    Cls = _make_collector("test_collector", run_impl=_failing_run)
    fake_collector = Cls()
    config = CollectorConfig(name="test_collector")
    loaded = [LoadedCollector(collector=fake_collector, config=config)]

    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(
        loaded,
        _make_ctx_factory(metrics),
        metrics,
        failure_budget=None,
    )
    scheduler._loop = asyncio.get_running_loop()  # pyright: ignore[reportPrivateUsage]

    # Run failing tick
    await scheduler._tick(fake_collector)  # pyright: ignore[reportPrivateUsage]

    # No DB updates should occur (no failure_budget to update)
    # Verify by checking DB is still at 0
    async with repo_with_migrations.transaction() as conn:
        result = await conn.execute(
            text("SELECT consecutive_failures FROM collectors WHERE name = :name"),
            {"name": "test_collector"},
        )
        row = result.fetchone()

    assert row is not None
    assert row[0] == 0


# ----- Tests for _extract_last_reason branches -----


def test_extract_last_reason_valid_format() -> None:
    """_extract_last_reason extracts kind from properly formatted quarantine reason."""
    reason = "consecutive failures: 3 (last reason: timeout)"
    result = _extract_last_reason(reason)
    assert result == "timeout"


def test_extract_last_reason_missing_prefix() -> None:
    """_extract_last_reason returns None if prefix not found (line 140)."""
    reason = "consecutive failures: 3"
    result = _extract_last_reason(reason)
    assert result is None


def test_extract_last_reason_missing_suffix() -> None:
    """_extract_last_reason returns None if suffix not found (line 143)."""
    reason = "consecutive failures: 3 (last reason: timeout"
    result = _extract_last_reason(reason)
    assert result is None


def test_extract_last_reason_empty_inner() -> None:
    """_extract_last_reason returns None if inner content is empty (line 146)."""
    reason = "consecutive failures: 3 (last reason: )"
    result = _extract_last_reason(reason)
    assert result is None


# ----- Tests for Scheduler.clear_quarantine branch coverage -----


@pytest.mark.asyncio
async def test_clear_quarantine_without_alert_repo_or_dispatcher_skips(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Scheduler.clear_quarantine skips alert resolution if alert_repo or dispatcher is None.

    Tests the branch at scheduler.py:320 (if self._alert_repo is not None and ...)
    """
    Cls = _make_collector("test_collector")
    fake_collector = Cls()
    config = CollectorConfig(name="test_collector")
    loaded = [LoadedCollector(collector=fake_collector, config=config)]

    budget = FailureBudget(repo_with_migrations, logger)
    # Manually quarantine
    budget._consecutive_failures["test_collector"] = 5  # pyright: ignore[reportPrivateUsage]
    budget._quarantined["test_collector"] = QuarantineState(  # pyright: ignore[reportPrivateUsage]
        consecutive_failures=5,
        quarantined_at="2026-05-05T00:00:00Z",
        quarantine_reason="consecutive failures: 5 (last reason: timeout)",
    )

    metrics = InMemoryMetricsWriter()
    # Scheduler with alert_repo=None (alert_dispatcher is also None)
    scheduler = Scheduler(
        loaded,
        _make_ctx_factory(metrics),
        metrics,
        failure_budget=budget,
        alert_repo=None,
        alert_dispatcher=None,
    )

    # This should not raise even though alert_repo is None
    await scheduler.clear_quarantine("test_collector", by="alice")
    assert budget.is_quarantined("test_collector") is False


@pytest.mark.asyncio
async def test_clear_quarantine_extract_last_reason_returns_none(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Scheduler.clear_quarantine skips resolution if _extract_last_reason returns None.

    Tests the branch at scheduler.py:328 (if last_reason is not None)
    """
    from unittest.mock import AsyncMock  # noqa: PLC0415

    Cls = _make_collector("test_collector")
    fake_collector = Cls()
    config = CollectorConfig(name="test_collector")
    loaded = [LoadedCollector(collector=fake_collector, config=config)]

    budget = FailureBudget(repo_with_migrations, logger)
    budget._consecutive_failures["test_collector"] = 5  # pyright: ignore[reportPrivateUsage]
    budget._quarantined["test_collector"] = QuarantineState(  # pyright: ignore[reportPrivateUsage]
        consecutive_failures=5,
        quarantined_at="2026-05-05T00:00:00Z",
        quarantine_reason="malformed reason (no extraction possible)",
    )

    metrics = InMemoryMetricsWriter()
    mock_repo = AsyncMock(spec=AlertRepository)
    mock_dispatcher = AsyncMock()

    scheduler = Scheduler(
        loaded,
        _make_ctx_factory(metrics),
        metrics,
        failure_budget=budget,
        alert_repo=mock_repo,
        alert_dispatcher=mock_dispatcher,
    )

    # clear_quarantine should complete without calling alert_repo.find_active_by_fingerprint
    await scheduler.clear_quarantine("test_collector", by="alice")

    # Verify find_active_by_fingerprint was NOT called (because last_reason was None)
    mock_repo.find_active_by_fingerprint.assert_not_called()
    assert budget.is_quarantined("test_collector") is False


@pytest.mark.asyncio
async def test_clear_quarantine_no_active_alert_found(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Scheduler.clear_quarantine skips resolution if no active alert is found.

    Tests the branch at scheduler.py:331 (if active is not None)
    """
    from unittest.mock import AsyncMock  # noqa: PLC0415

    Cls = _make_collector("test_collector")
    fake_collector = Cls()
    config = CollectorConfig(name="test_collector")
    loaded = [LoadedCollector(collector=fake_collector, config=config)]

    budget = FailureBudget(repo_with_migrations, logger)
    budget._consecutive_failures["test_collector"] = 5  # pyright: ignore[reportPrivateUsage]
    budget._quarantined["test_collector"] = QuarantineState(  # pyright: ignore[reportPrivateUsage]
        consecutive_failures=5,
        quarantined_at="2026-05-05T00:00:00Z",
        quarantine_reason="consecutive failures: 5 (last reason: timeout)",
    )

    metrics = InMemoryMetricsWriter()
    mock_repo = AsyncMock(spec=AlertRepository)
    mock_repo.find_active_by_fingerprint.return_value = None  # No alert found
    mock_dispatcher = AsyncMock()

    scheduler = Scheduler(
        loaded,
        _make_ctx_factory(metrics),
        metrics,
        failure_budget=budget,
        alert_repo=mock_repo,
        alert_dispatcher=mock_dispatcher,
    )

    # clear_quarantine should complete without calling mark_resolved
    await scheduler.clear_quarantine("test_collector", by="alice")

    # Verify find_active_by_fingerprint was called but mark_resolved was NOT
    mock_repo.find_active_by_fingerprint.assert_called_once()
    mock_repo.mark_resolved.assert_not_called()
    assert budget.is_quarantined("test_collector") is False
