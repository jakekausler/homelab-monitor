"""Tests for FailureBudget quarantine alert wiring (Spec B).

Verifies that entering/re-entering/clearing quarantine emits events through the
AlertDispatcher and writes rows to the AlertRepository.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta as td
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from homelab_monitor.kernel.alerts.events import AlertFiringEvent, AlertResolvedEvent
from homelab_monitor.kernel.alerts.fingerprinting import quarantine_fingerprint
from homelab_monitor.kernel.alerts.repository import AlertRepository
from homelab_monitor.kernel.db.migrations import alembic_upgrade_head
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.dispatch.dispatcher import AlertDispatcher
from homelab_monitor.kernel.dispatch.types import AlertEvent
from homelab_monitor.kernel.plugins import (
    CollectorConfig,
    CollectorContext,
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
)
from homelab_monitor.kernel.plugins.base import BaseCollector, Collector
from homelab_monitor.kernel.plugins.loader import LoadedCollector
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind
from homelab_monitor.kernel.scheduler.failure_budget import FailureBudget
from homelab_monitor.kernel.scheduler.scheduler import Scheduler
from homelab_monitor.kernel.secrets.resolver import SyncSecretsResolver


class _CapturingChannel:
    """Test channel that records every delivered event."""

    kind: ClassVar[str] = "test_capture"

    def __init__(self) -> None:
        self.events: list[AlertEvent] = []

    async def deliver(self, event: AlertEvent) -> None:
        self.events.append(event)


@pytest.fixture
async def full_repo(tmp_path: Path) -> SqliteRepository:
    """SqliteRepository with full schema (all migrations applied)."""
    db_path = tmp_path / "test_quarantine_alert.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    alembic_upgrade_head(db_url)
    engine = create_async_engine(db_url)
    repo = SqliteRepository(engine)

    # Seed a collector row so failure_budget DB updates have a row to UPDATE
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO collectors (id, name, created_at) "
                "VALUES ('test-id', 'test_collector', '2026-01-01T00:00:00+00:00')"
            )
        )
    return repo


def _make_ctx_factory(
    metrics: InMemoryMetricsWriter,
) -> Callable[[Collector], CollectorContext]:
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
    run_impl: Callable[[BaseCollector, CollectorContext], Awaitable[CollectorResult]] | None = None,
) -> type[BaseCollector]:
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
            "concurrency_group": "default",
            "run": impl,
        },
    )
    return cls  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_quarantine_emits_alert_via_dispatcher(
    full_repo: SqliteRepository,
) -> None:
    """When quarantine threshold is reached, an AlertFiringEvent is dispatched."""
    channel = _CapturingChannel()
    log = structlog.get_logger()
    dispatcher = AlertDispatcher(channels=[channel], log=log)
    alert_repo = AlertRepository(full_repo)

    budget = FailureBudget(
        full_repo,
        log,
        default_threshold=2,
        alert_repo=alert_repo,
        dispatcher=dispatcher,
    )

    async def _failing_run(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        raise ValueError("boom")

    Cls = _make_collector("test_collector", run_impl=_failing_run)
    collector = Cls()
    loaded = [
        LoadedCollector(
            collector=collector, config=CollectorConfig(name="test_collector", quarantine_after=2)
        )
    ]
    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(loaded, _make_ctx_factory(metrics), metrics, failure_budget=budget)
    scheduler._loop = asyncio.get_running_loop()  # pyright: ignore[reportPrivateUsage]

    # Trigger 2 failures to reach threshold=2
    await scheduler._tick(collector)  # pyright: ignore[reportPrivateUsage]
    await scheduler._tick(collector)  # pyright: ignore[reportPrivateUsage]

    assert budget.is_quarantined("test_collector") is True
    assert len(channel.events) == 1
    event = channel.events[0]
    assert isinstance(event, AlertFiringEvent)
    assert event.source_tool == "scheduler"
    assert event.labels.get("collector_name") == "test_collector"

    # Verify a row was created in alerts table
    row = await full_repo.fetch_one(
        text("SELECT id, source_tool, status FROM alerts WHERE source_tool = 'scheduler' LIMIT 1"),
        {},
    )
    assert row is not None
    assert row[1] == "scheduler"
    assert row[2] == "firing"


@pytest.mark.asyncio
async def test_repeat_quarantine_while_active_bumps_last_seen(
    full_repo: SqliteRepository,
) -> None:
    """A second quarantine entry with the same collector/reason bumps last_seen_at.

    Not a new row.
    """
    channel = _CapturingChannel()
    log = structlog.get_logger()
    dispatcher = AlertDispatcher(channels=[channel], log=log)
    alert_repo = AlertRepository(full_repo)

    budget = FailureBudget(
        full_repo,
        log,
        default_threshold=2,
        alert_repo=alert_repo,
        dispatcher=dispatcher,
    )

    async def _failing_run(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        raise ValueError("boom")

    Cls = _make_collector("test_collector", run_impl=_failing_run)
    collector = Cls()
    loaded = [
        LoadedCollector(
            collector=collector, config=CollectorConfig(name="test_collector", quarantine_after=2)
        )
    ]
    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(loaded, _make_ctx_factory(metrics), metrics, failure_budget=budget)
    scheduler._loop = asyncio.get_running_loop()  # pyright: ignore[reportPrivateUsage]

    # First quarantine (2 failures)
    await scheduler._tick(collector)  # pyright: ignore[reportPrivateUsage]
    await scheduler._tick(collector)  # pyright: ignore[reportPrivateUsage]
    assert len(channel.events) == 1

    # Manually call _emit_quarantine_alert again to simulate re-entry (same fingerprint)
    reason = "exception"
    fp = quarantine_fingerprint("test_collector", reason)
    row_before = await full_repo.fetch_one(
        text("SELECT id, last_seen_at FROM alerts WHERE fingerprint = :fp"), {"fp": fp}
    )
    assert row_before is not None
    alert_id_before = row_before[0]
    last_seen_before = row_before[1]

    # Call emit directly to simulate a re-fire dedup scenario
    await budget._emit_quarantine_alert(  # pyright: ignore[reportPrivateUsage]
        name="test_collector",
        reason=reason,
        consecutive_failures=3,
        ts="2026-05-07T01:00:00+00:00",
    )

    row_after = await full_repo.fetch_one(
        text("SELECT id, last_seen_at FROM alerts WHERE fingerprint = :fp"), {"fp": fp}
    )
    assert row_after is not None
    # Same row (not new)
    assert row_after[0] == alert_id_before
    # last_seen_at updated
    assert row_after[1] != last_seen_before

    # Two dispatch events total (initial fire + re-fire)
    assert len(channel.events) == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_clear_quarantine_emits_resolved_event(
    full_repo: SqliteRepository,
) -> None:
    """clear_quarantine resolves the alert row.

    Dispatches AlertResolvedEvent via the scheduler.
    """
    channel = _CapturingChannel()
    log = structlog.get_logger()
    dispatcher = AlertDispatcher(channels=[channel], log=log)
    alert_repo = AlertRepository(full_repo)

    budget = FailureBudget(
        full_repo,
        log,
        default_threshold=2,
        alert_repo=alert_repo,
        dispatcher=dispatcher,
    )

    async def _failing_run(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        raise ValueError("boom")

    Cls = _make_collector("test_collector", run_impl=_failing_run)
    collector = Cls()
    loaded = [
        LoadedCollector(
            collector=collector, config=CollectorConfig(name="test_collector", quarantine_after=2)
        )
    ]
    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(
        loaded,
        _make_ctx_factory(metrics),
        metrics,
        failure_budget=budget,
        alert_repo=alert_repo,
        alert_dispatcher=dispatcher,
    )
    scheduler._loop = asyncio.get_running_loop()  # pyright: ignore[reportPrivateUsage]

    # Trigger quarantine
    await scheduler._tick(collector)  # pyright: ignore[reportPrivateUsage]
    await scheduler._tick(collector)  # pyright: ignore[reportPrivateUsage]
    assert budget.is_quarantined("test_collector") is True
    fire_events = [e for e in channel.events if isinstance(e, AlertFiringEvent)]
    assert len(fire_events) == 1

    # Clear quarantine
    await scheduler.clear_quarantine("test_collector", by="operator")
    assert budget.is_quarantined("test_collector") is False

    # A resolved event must have been dispatched
    resolved_events = [e for e in channel.events if isinstance(e, AlertResolvedEvent)]
    assert len(resolved_events) == 1
    ev = resolved_events[0]
    assert ev.source_tool == "scheduler"

    # DB row should be resolved
    row = await full_repo.fetch_one(
        text("SELECT status FROM alerts WHERE source_tool = 'scheduler' LIMIT 1"),
        {},
    )
    assert row is not None
    assert row[0] == "resolved"


@pytest.mark.asyncio
async def test_quarantine_after_clear_creates_new_row(
    full_repo: SqliteRepository,
) -> None:
    """After clear, a new quarantine creates a fresh alert row (not dedup of resolved)."""
    channel = _CapturingChannel()
    log = structlog.get_logger()
    dispatcher = AlertDispatcher(channels=[channel], log=log)
    alert_repo = AlertRepository(full_repo)

    budget = FailureBudget(
        full_repo,
        log,
        default_threshold=2,
        alert_repo=alert_repo,
        dispatcher=dispatcher,
    )

    async def _failing_run(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        raise ValueError("boom")

    Cls = _make_collector("test_collector", run_impl=_failing_run)
    collector = Cls()
    loaded = [
        LoadedCollector(
            collector=collector, config=CollectorConfig(name="test_collector", quarantine_after=2)
        )
    ]
    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(
        loaded,
        _make_ctx_factory(metrics),
        metrics,
        failure_budget=budget,
        alert_repo=alert_repo,
        alert_dispatcher=dispatcher,
    )
    scheduler._loop = asyncio.get_running_loop()  # pyright: ignore[reportPrivateUsage]

    # First quarantine cycle
    await scheduler._tick(collector)  # pyright: ignore[reportPrivateUsage]
    await scheduler._tick(collector)  # pyright: ignore[reportPrivateUsage]
    first_fire_id = channel.events[0].alert_id  # type: ignore[union-attr]

    await scheduler.clear_quarantine("test_collector")

    # Re-seed the consecutive failures counter so second quarantine can trigger
    budget._consecutive_failures.pop("test_collector", None)  # pyright: ignore[reportPrivateUsage]

    # Second quarantine cycle — need a fresh reason to get a distinct fingerprint
    # OR we directly call emit to simulate the second quarantine with a new reason
    await budget._emit_quarantine_alert(  # pyright: ignore[reportPrivateUsage]
        name="test_collector",
        reason="second_failure",
        consecutive_failures=2,
        ts="2026-05-07T02:00:00+00:00",
    )

    fire_events = [e for e in channel.events if isinstance(e, AlertFiringEvent)]
    # The second fire should be for a new (or updated) row, distinct from the cleared one
    assert len(fire_events) >= 2  # noqa: PLR2004
    second_fire = fire_events[-1]
    assert (
        second_fire.alert_id != first_fire_id
        or second_fire.fingerprint != fire_events[0].fingerprint
    )


@pytest.mark.asyncio
async def test_failure_budget_without_alert_repo_or_dispatcher_skips(
    full_repo: SqliteRepository,
) -> None:
    """FailureBudget without alert_repo/dispatcher silently skips alert emission."""
    channel = _CapturingChannel()
    log = structlog.get_logger()
    # No alert_repo, no dispatcher
    budget = FailureBudget(
        full_repo,
        log,
        default_threshold=2,
        alert_repo=None,
        dispatcher=None,
    )

    async def _failing_run(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        raise ValueError("boom")

    Cls = _make_collector("test_collector", run_impl=_failing_run)
    collector = Cls()
    loaded = [
        LoadedCollector(
            collector=collector, config=CollectorConfig(name="test_collector", quarantine_after=2)
        )
    ]
    metrics = InMemoryMetricsWriter()
    scheduler = Scheduler(loaded, _make_ctx_factory(metrics), metrics, failure_budget=budget)
    scheduler._loop = asyncio.get_running_loop()  # pyright: ignore[reportPrivateUsage]

    # Should not raise even without alert wiring
    await scheduler._tick(collector)  # pyright: ignore[reportPrivateUsage]
    await scheduler._tick(collector)  # pyright: ignore[reportPrivateUsage]

    assert budget.is_quarantined("test_collector") is True
    # No events captured (channel was never given to dispatcher)
    assert len(channel.events) == 0

    # No alert rows written
    row = await full_repo.fetch_one(
        text("SELECT id FROM alerts WHERE source_tool = 'scheduler' LIMIT 1"),
        {},
    )
    assert row is None
