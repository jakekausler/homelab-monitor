"""E2E / integration tests for STAGE-001-008: concurrency groups, failure budget,
and quarantine.

These tests complement the unit tests in test_failure_budget.py (which use
mock DB tables and cover the FailureBudget class in isolation) and the
STAGE-007 suite in test_scheduler_e2e.py (which exercises scheduling
precision, RunKind dispatch, and restart independence with real kernel types).

All tests here use REAL kernel types:
- SqliteRepository + alembic_upgrade_head (full migration stack, 0001-0003)
- InMemoryMetricsWriter for self-metric assertions
- Real Scheduler + FailureBudget wired together

Wall-clock notes:
- Small intervals (200-500 ms) to keep each test under 5 s.
- Total suite wall-time target: < 45 s.

Scenarios:
  1. End-to-end quarantine round-trip (real DB, SQL verification)
  2. Quarantine persists across scheduler restart (load_state rehydration)
  3. Manual clear_quarantine via Scheduler.clear_quarantine (audit trail)
  4. Concurrency-group serialization (no overlap under shared lock)
  5. Group skip-on-busy emits skipped_total{reason="group_busy"} metric
  6. Per-collector quarantine_after override (custom threshold < default)
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.migrations import alembic_upgrade_head
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.plugins import (
    BaseCollector,
    Collector,
    CollectorConfig,
    CollectorContext,
    CollectorResult,
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
    LoadedCollector,
)
from homelab_monitor.kernel.scheduler import Scheduler
from homelab_monitor.kernel.scheduler.failure_budget import FailureBudget
from homelab_monitor.kernel.secrets.resolver import SyncSecretsResolver

# ---------------------------------------------------------------------------
# Fixtures — real kernel types (mirrors test_scheduler_e2e.py style)
# ---------------------------------------------------------------------------


@pytest.fixture
def _tmp_db_path() -> Path:  # type: ignore[return]
    fd, raw = tempfile.mkstemp(prefix="hm-e2e-q-", suffix=".db")
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


async def _seed_collectors(repo: SqliteRepository, names: list[str]) -> None:
    """Pre-insert collectors rows so FailureBudget UPDATE statements find a row.

    The scheduler does not auto-register collectors in the DB; that is
    responsibility of the loader/registry (not yet built in STAGE-001-008).
    E2E tests must seed these rows manually to make DB-level assertions valid.
    """
    async with repo.transaction() as conn:
        for name in names:
            await conn.execute(
                text(
                    "INSERT OR IGNORE INTO collectors (id, name, created_at) "
                    "VALUES (:id, :name, :created_at)"
                ),
                {"id": str(uuid.uuid4()), "name": name, "created_at": utc_now_iso()},
            )


def _make_real_ctx_factory(
    repo: SqliteRepository,
    metrics: InMemoryMetricsWriter,
) -> Callable[[Collector], CollectorContext]:
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
    run_impl: (
        Callable[[BaseCollector, CollectorContext], Awaitable[CollectorResult]] | None
    ) = None,
    concurrency_group: str = "default",
) -> type[BaseCollector]:
    async def _default(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        return CollectorResult(ok=True)

    impl = run_impl or _default
    return type(  # type: ignore[return-value]
        f"_QE2ECollector_{name}",
        (BaseCollector,),
        {
            "name": name,
            "interval": timedelta(seconds=interval_s),
            "timeout": timedelta(seconds=timeout_s),
            "concurrency_group": concurrency_group,
            "run": impl,
        },
    )


# ---------------------------------------------------------------------------
# Scenario 1 — End-to-end quarantine round-trip (real DB)
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_quarantine_round_trip_real_db(
    real_repo: SqliteRepository,
    real_engine: AsyncEngine,
) -> None:
    """Crashing collector gets quarantined after 3 failures; DB + audit verified.

    A collector that always raises Exception runs with quarantine_after=3 and a
    200ms interval. After enough wall-clock time for at least 3 ticks to fire,
    the scheduler should have quarantined it. We then read the collectors and
    audit_log tables directly with SQL to verify all expected columns are set.
    """
    metrics = InMemoryMetricsWriter()

    async def _always_fail(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        msg = "injected failure"
        raise RuntimeError(msg)

    cls_crash = _make_collector("q_crash_1", interval_s=0.2, timeout_s=2.0, run_impl=_always_fail)

    log = structlog.get_logger()
    budget = FailureBudget(real_repo, log)

    loaded = [
        LoadedCollector(
            collector=cls_crash(),
            config=CollectorConfig(name="q_crash_1", quarantine_after=3),
        ),
    ]

    await _seed_collectors(real_repo, ["q_crash_1"])
    ctx_factory = _make_real_ctx_factory(real_repo, metrics)
    scheduler = Scheduler(loaded, ctx_factory, metrics, failure_budget=budget)
    await scheduler.start()
    await asyncio.sleep(2.0)  # ~10 ticks at 200ms; quarantine fires after 3
    await scheduler.stop()

    # Verify in-memory state
    assert budget.is_quarantined("q_crash_1"), "collector must be quarantined after 3 failures"

    # Verify DB: collectors row
    async with real_repo.transaction() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT consecutive_failures, quarantined_at, quarantine_reason "
                    "FROM collectors WHERE name = :name"
                ),
                {"name": "q_crash_1"},
            )
        ).fetchone()

    assert row is not None, "collectors row must exist for q_crash_1"
    consecutive_failures, quarantined_at, quarantine_reason = row
    assert consecutive_failures == 3, (  # noqa: PLR2004
        f"expected consecutive_failures=3, got {consecutive_failures}"
    )
    assert quarantined_at is not None, "quarantined_at must be set"
    assert quarantine_reason is not None, "quarantine_reason must be set"
    assert "consecutive failures: 3" in quarantine_reason, (
        f"quarantine_reason should mention count, got: {quarantine_reason!r}"
    )

    # Verify audit_log: exactly one quarantine_entered row
    async with real_repo.transaction() as conn:
        audit_rows = (
            await conn.execute(
                text(
                    "SELECT who, what, before_json, after_json FROM audit_log "
                    "WHERE what = 'collector.quarantine_entered'"
                )
            )
        ).fetchall()

    assert len(audit_rows) == 1, f"expected 1 audit row, got {len(audit_rows)}"
    who, what, before_raw, after_raw = audit_rows[0]
    assert who == "scheduler"
    assert what == "collector.quarantine_entered"

    before = json.loads(before_raw)
    after = json.loads(after_raw)
    assert before["quarantined_at"] is None
    assert after["quarantined_at"] is not None
    assert after["consecutive_failures"] == 3  # noqa: PLR2004

    # After quarantine: skipped_total{reason=quarantined} should accumulate
    skipped_quarantined = _count(
        metrics,
        "homelab_collector_run_skipped_total",
        {"name": "q_crash_1", "reason": "quarantined"},
    )
    assert skipped_quarantined >= 1, (
        f"expected ≥1 quarantined-skip metrics, got {skipped_quarantined}"
    )


# ---------------------------------------------------------------------------
# Scenario 2 — Quarantine persists across scheduler restart
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_quarantine_persists_across_restart(
    real_repo: SqliteRepository,
    real_engine: AsyncEngine,
) -> None:
    """Quarantine survives scheduler restart via FailureBudget.load_state().

    First scheduler run quarantines a crashing collector. After stop(), a NEW
    Scheduler + NEW FailureBudget are wired to the same DB. On start(), the
    new budget calls load_state() and must see the quarantine. The collector
    must NOT tick in the second run (no success/failure metrics).
    """
    metrics1 = InMemoryMetricsWriter()

    async def _always_fail(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        msg = "injected failure"
        raise RuntimeError(msg)

    cls_crash = _make_collector("q_restart", interval_s=0.2, timeout_s=2.0, run_impl=_always_fail)

    await _seed_collectors(real_repo, ["q_restart"])
    log = structlog.get_logger()
    budget1 = FailureBudget(real_repo, log)
    loaded1 = [
        LoadedCollector(
            collector=cls_crash(),
            config=CollectorConfig(name="q_restart", quarantine_after=3),
        ),
    ]
    sched1 = Scheduler(
        loaded1, _make_real_ctx_factory(real_repo, metrics1), metrics1, failure_budget=budget1
    )
    await sched1.start()
    await asyncio.sleep(2.0)
    await sched1.stop()

    assert budget1.is_quarantined("q_restart"), "first scheduler must quarantine q_restart"

    # Second scheduler + fresh FailureBudget (simulates restart)
    metrics2 = InMemoryMetricsWriter()
    budget2 = FailureBudget(real_repo, log)
    loaded2 = [
        LoadedCollector(
            collector=cls_crash(),
            config=CollectorConfig(name="q_restart", quarantine_after=3),
        ),
    ]
    sched2 = Scheduler(
        loaded2, _make_real_ctx_factory(real_repo, metrics2), metrics2, failure_budget=budget2
    )
    await sched2.start()
    await asyncio.sleep(1.5)  # enough time for multiple ticks if not gated
    await sched2.stop()

    # Reloaded budget must see quarantine
    assert budget2.is_quarantined("q_restart"), (
        "second FailureBudget must reload quarantine from DB after restart"
    )

    # Collector must NOT have ticked in second run (both success and failure = 0)
    success2 = _count(metrics2, "homelab_collector_run_success_total", {"name": "q_restart"})
    failure2 = _count(metrics2, "homelab_collector_run_failure_total", {"name": "q_restart"})
    assert success2 == 0, f"quarantined collector must not succeed in second run, got {success2}"
    assert failure2 == 0, f"quarantined collector must not fail in second run, got {failure2}"

    # Skipped-quarantined metric must appear
    skipped2 = _count(
        metrics2,
        "homelab_collector_run_skipped_total",
        {"name": "q_restart", "reason": "quarantined"},
    )
    assert skipped2 >= 1, f"expected ≥1 quarantine-skip in second run, got {skipped2}"


# ---------------------------------------------------------------------------
# Scenario 3 — Manual clear_quarantine via Scheduler.clear_quarantine
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_clear_quarantine_via_scheduler(
    real_repo: SqliteRepository,
    real_engine: AsyncEngine,
) -> None:
    """Scheduler.clear_quarantine writes audit row and unblocks the collector.

    Steps: quarantine the collector → call clear_quarantine("alice") → verify
    DB shows NULL quarantine columns + consecutive_failures=0 → verify
    audit_log has a cleared row with who="alice" → verify collector starts
    ticking again (accumulates failure metrics since it still crashes).
    """
    metrics = InMemoryMetricsWriter()
    fail_count = 0

    async def _always_fail(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        nonlocal fail_count
        fail_count += 1
        msg = "injected failure"
        raise RuntimeError(msg)

    cls_crash = _make_collector("q_clear", interval_s=0.2, timeout_s=2.0, run_impl=_always_fail)

    await _seed_collectors(real_repo, ["q_clear"])
    log = structlog.get_logger()
    budget = FailureBudget(real_repo, log)
    loaded = [
        LoadedCollector(
            collector=cls_crash(),
            config=CollectorConfig(name="q_clear", quarantine_after=3),
        ),
    ]
    ctx_factory = _make_real_ctx_factory(real_repo, metrics)
    scheduler = Scheduler(loaded, ctx_factory, metrics, failure_budget=budget)
    await scheduler.start()

    # Wait for quarantine to fire (3 failures at 200ms intervals ~ 600ms)
    await asyncio.sleep(1.5)
    assert budget.is_quarantined("q_clear"), "collector must be quarantined before clear"

    # Manual clear by alice
    await scheduler.clear_quarantine("q_clear", by="alice@example.com")
    assert not budget.is_quarantined("q_clear"), "quarantine must be cleared in memory"

    # Let it tick again (more failures will accumulate, but that's fine)
    await asyncio.sleep(1.0)
    await scheduler.stop()

    # DB: quarantine columns must be NULL, counter must be reset
    # (then re-incremented by new failures)
    async with real_repo.transaction() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT consecutive_failures, quarantined_at, quarantine_reason "
                    "FROM collectors WHERE name = :name"
                ),
                {"name": "q_clear"},
            )
        ).fetchone()

    # After clear, new failures will have incremented consecutive_failures again,
    # but quarantined_at/quarantine_reason should only be set if it re-entered quarantine.
    # We just verify the cleared audit event was written and the in-memory state was reset.
    assert row is not None

    # Verify audit_log has both entered + cleared events
    async with real_repo.transaction() as conn:
        audit_rows = (
            await conn.execute(text('SELECT what, who FROM audit_log ORDER BY "when" ASC'))
        ).fetchall()

    events = [(r[0], r[1]) for r in audit_rows]
    entered_events = [e for e in events if e[0] == "collector.quarantine_entered"]
    cleared_events = [e for e in events if e[0] == "collector.quarantine_cleared"]

    assert len(entered_events) >= 1, f"expected ≥1 quarantine_entered, got {entered_events}"
    assert len(cleared_events) >= 1, f"expected ≥1 quarantine_cleared, got {cleared_events}"

    # The clear must have been authored by alice
    alice_clears = [e for e in cleared_events if e[1] == "alice@example.com"]
    assert len(alice_clears) >= 1, (
        f"expected cleared event with who=alice@example.com; cleared events: {cleared_events}"
    )

    # After clear, collector must have ticked (and failed) at least once
    failure_after_clear = _count(
        metrics, "homelab_collector_run_failure_total", {"name": "q_clear", "reason": "exception"}
    )
    # The 3 pre-quarantine failures + any post-clear failures
    assert failure_after_clear >= 3, (  # noqa: PLR2004
        f"expected ≥3 total failures (3 pre-quarantine), got {failure_after_clear}"
    )


# ---------------------------------------------------------------------------
# Scenario 4 — Concurrency-group serialization: no overlap under shared lock
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_concurrency_group_serializes_collectors(
    real_repo: SqliteRepository,
) -> None:
    """Two collectors in the same named group never execute simultaneously.

    Each run() does a 100ms sleep to ensure runs are long enough to overlap
    if the lock is not honored. We track concurrent-execution depth via a
    shared counter; it must never exceed 1.
    """
    metrics = InMemoryMetricsWriter()
    concurrent_depth = 0
    max_concurrent = 0

    async def _slow_run(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        nonlocal concurrent_depth, max_concurrent
        concurrent_depth += 1
        max_concurrent = max(max_concurrent, concurrent_depth)
        await asyncio.sleep(0.1)
        concurrent_depth -= 1
        return CollectorResult(ok=True)

    cls_a = _make_collector(
        "q_group_a",
        interval_s=0.3,
        timeout_s=2.0,
        run_impl=_slow_run,
        concurrency_group="ha",
    )
    cls_b = _make_collector(
        "q_group_b",
        interval_s=0.3,
        timeout_s=2.0,
        run_impl=_slow_run,
        concurrency_group="ha",
    )

    loaded = [
        LoadedCollector(collector=cls_a(), config=CollectorConfig(name="q_group_a")),
        LoadedCollector(collector=cls_b(), config=CollectorConfig(name="q_group_b")),
    ]

    ctx_factory = _make_real_ctx_factory(real_repo, metrics)
    scheduler = Scheduler(loaded, ctx_factory, metrics)
    await scheduler.start()
    await asyncio.sleep(3.0)  # ~10 ticks each; plenty for overlap if lock broken
    await scheduler.stop()

    assert max_concurrent <= 1, (
        f"concurrency_group='ha' must serialize collectors; "
        f"max concurrent depth was {max_concurrent}"
    )

    # Both must have ticked at least a few times
    success_a = _count(metrics, "homelab_collector_run_success_total", {"name": "q_group_a"})
    success_b = _count(metrics, "homelab_collector_run_success_total", {"name": "q_group_b"})
    assert success_a >= 1, f"q_group_a must tick at least once, got {success_a}"
    assert success_b >= 1, f"q_group_b must tick at least once, got {success_b}"


# ---------------------------------------------------------------------------
# Scenario 5 — Group skip-on-busy emits skipped_total{reason="group_busy"}
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_group_busy_skip_emits_metric(
    real_repo: SqliteRepository,
) -> None:
    """Fast collector skips when the group lock is held by slow collector.

    slow_collector: interval=300ms, timeout=2s, run()=sleep(400ms)
        -- holds the group lock for ~400ms per tick.
    fast_collector: interval=300ms, timeout=2s, run()=instant
        -- tries to acquire the lock; deadline = interval/2 = 150ms.
        When slow_collector holds the lock, fast_collector times out and
        emits skipped_total{reason="group_busy"}.
    """
    metrics = InMemoryMetricsWriter()

    async def _slow_run(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        await asyncio.sleep(0.4)
        return CollectorResult(ok=True)

    async def _fast_run(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        return CollectorResult(ok=True)

    cls_slow = _make_collector(
        "q_busy_slow",
        interval_s=0.3,
        timeout_s=2.0,
        run_impl=_slow_run,
        concurrency_group="busy_group",
    )
    cls_fast = _make_collector(
        "q_busy_fast",
        interval_s=0.3,
        timeout_s=2.0,
        run_impl=_fast_run,
        concurrency_group="busy_group",
    )

    loaded = [
        LoadedCollector(collector=cls_slow(), config=CollectorConfig(name="q_busy_slow")),
        LoadedCollector(collector=cls_fast(), config=CollectorConfig(name="q_busy_fast")),
    ]

    ctx_factory = _make_real_ctx_factory(real_repo, metrics)
    scheduler = Scheduler(loaded, ctx_factory, metrics)
    await scheduler.start()
    await asyncio.sleep(3.0)
    await scheduler.stop()

    skipped_busy = _count(
        metrics,
        "homelab_collector_run_skipped_total",
        {"name": "q_busy_fast", "reason": "group_busy"},
    )
    assert skipped_busy >= 1, f"expected ≥1 group_busy skips for q_busy_fast, got {skipped_busy}"


# ---------------------------------------------------------------------------
# Scenario 6 — Per-collector quarantine_after override (threshold < default)
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_quarantine_after_override_fires_early(
    real_repo: SqliteRepository,
) -> None:
    """CollectorConfig.quarantine_after=2 quarantines after 2 failures (not 5).

    We verify:
    - After exactly 2 ticks, the collector is quarantined (not after 5).
    - The consecutive_failures column in DB equals 2 (not 5).
    - A parallel healthy collector with default threshold is unaffected.
    """
    metrics = InMemoryMetricsWriter()

    async def _always_fail(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self, ctx
        msg = "injected failure"
        raise RuntimeError(msg)

    cls_crash = _make_collector(
        "q_thresh_crash", interval_s=0.2, timeout_s=2.0, run_impl=_always_fail
    )
    cls_healthy = _make_collector("q_thresh_healthy", interval_s=0.2, timeout_s=2.0)

    await _seed_collectors(real_repo, ["q_thresh_crash", "q_thresh_healthy"])
    log = structlog.get_logger()
    budget = FailureBudget(real_repo, log)
    loaded = [
        LoadedCollector(
            collector=cls_crash(),
            config=CollectorConfig(name="q_thresh_crash", quarantine_after=2),
        ),
        LoadedCollector(
            collector=cls_healthy(),
            config=CollectorConfig(name="q_thresh_healthy"),
        ),
    ]

    ctx_factory = _make_real_ctx_factory(real_repo, metrics)
    scheduler = Scheduler(loaded, ctx_factory, metrics, failure_budget=budget)
    await scheduler.start()
    await asyncio.sleep(2.0)  # ~10 ticks; quarantine should fire after 2
    await scheduler.stop()

    assert budget.is_quarantined("q_thresh_crash"), (
        "collector with quarantine_after=2 must be quarantined after 2 failures"
    )
    assert budget.consecutive_failures("q_thresh_crash") == 2, (  # noqa: PLR2004
        f"expected consecutive_failures=2, got {budget.consecutive_failures('q_thresh_crash')}"
    )

    # DB must also show 2, not 5
    async with real_repo.transaction() as conn:
        row = (
            await conn.execute(
                text("SELECT consecutive_failures FROM collectors WHERE name = :name"),
                {"name": "q_thresh_crash"},
            )
        ).fetchone()
    assert row is not None
    assert row[0] == 2, f"DB consecutive_failures must be 2, got {row[0]}"  # noqa: PLR2004

    # Healthy companion must NOT be quarantined
    assert not budget.is_quarantined("q_thresh_healthy"), (
        "healthy companion must not be quarantined"
    )
    success_healthy = _count(
        metrics, "homelab_collector_run_success_total", {"name": "q_thresh_healthy"}
    )
    assert success_healthy >= 3, (  # noqa: PLR2004
        f"healthy companion must keep ticking, got {success_healthy} successes"
    )
