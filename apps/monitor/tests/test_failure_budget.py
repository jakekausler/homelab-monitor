"""Tests for FailureBudget and QuarantineState."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from structlog.testing import capture_logs

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.scheduler.failure_budget import FailureBudget, QuarantineState


@pytest.fixture
async def repo_with_migrations(tmp_path: Path) -> SqliteRepository:
    """Create a repo with migrations applied through 0003."""
    db_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    repo = SqliteRepository(engine)

    async with repo.transaction() as conn:
        # Create audit_log table
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
        # Create collectors table with quarantine columns
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
        # Insert a test collector row
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


# State queries


@pytest.mark.asyncio
async def test_initial_state_clean(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Fresh FailureBudget has no quarantines or failures."""
    budget = FailureBudget(repo_with_migrations, logger)
    assert budget.is_quarantined("test_collector") is False
    assert budget.consecutive_failures("test_collector") == 0


@pytest.mark.asyncio
async def test_consecutive_failures_starts_at_zero(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """New collector has zero failures."""
    budget = FailureBudget(repo_with_migrations, logger)
    assert budget.consecutive_failures("unknown_collector") == 0


# Counter behavior


@pytest.mark.asyncio
async def test_record_failure_increments_counter(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """record_failure increments in-memory counter."""
    budget = FailureBudget(repo_with_migrations, logger)
    await budget.record_failure("test_collector", "exception")
    assert budget.consecutive_failures("test_collector") == 1


@pytest.mark.asyncio
async def test_record_failure_persists_to_db(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """record_failure updates DB counter."""
    budget = FailureBudget(repo_with_migrations, logger)
    await budget.record_failure("test_collector", "exception")

    async with repo_with_migrations.transaction() as conn:
        result = await conn.execute(
            text("SELECT consecutive_failures FROM collectors WHERE name = :name"),
            {"name": "test_collector"},
        )
        row = result.fetchone()

    assert row is not None
    assert row[0] == 1


@pytest.mark.asyncio
async def test_record_success_resets_counter(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """record_success resets in-memory counter to 0."""
    budget = FailureBudget(repo_with_migrations, logger)
    await budget.record_failure("test_collector", "exception")
    await budget.record_failure("test_collector", "exception")
    await budget.record_failure("test_collector", "exception")
    assert budget.consecutive_failures("test_collector") == 3  # noqa: PLR2004

    await budget.record_success("test_collector")
    assert budget.consecutive_failures("test_collector") == 0


@pytest.mark.asyncio
async def test_record_success_no_db_write(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """record_success does NOT write to DB (counter stays persisted)."""
    budget = FailureBudget(repo_with_migrations, logger)
    # Manually set DB counter to 3
    async with repo_with_migrations.transaction() as conn:
        await conn.execute(
            text("UPDATE collectors SET consecutive_failures = 3 WHERE name = :name"),
            {"name": "test_collector"},
        )

    # Call record_success
    await budget.record_success("test_collector")

    # Verify DB still says 3
    async with repo_with_migrations.transaction() as conn:
        result = await conn.execute(
            text("SELECT consecutive_failures FROM collectors WHERE name = :name"),
            {"name": "test_collector"},
        )
        row = result.fetchone()

    assert row is not None
    assert row[0] == 3  # noqa: PLR2004


@pytest.mark.asyncio
async def test_record_success_does_not_clear_quarantine(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """record_success does not clear quarantine (defensive no-op)."""
    budget = FailureBudget(repo_with_migrations, logger)
    # Manually set quarantine
    budget._quarantined["test_collector"] = QuarantineState(  # pyright: ignore[reportPrivateUsage]
        consecutive_failures=5,
        quarantined_at="2026-01-01T00:00:00Z",
        quarantine_reason="test",
    )

    await budget.record_success("test_collector")

    assert budget.is_quarantined("test_collector") is True


@pytest.mark.asyncio
async def test_record_failure_is_noop_when_already_quarantined(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """C3 defensive guard: record_failure() on a quarantined collector is a no-op.

    The scheduler's quarantine gate prevents this in normal operation, but the
    FailureBudget API must defensively guard against future code paths that
    might bypass the gate. Without this guard, a quarantined collector could
    accumulate further failures and write duplicate quarantine_entered audit
    rows on each subsequent record_failure call.
    """
    # Pre-populate quarantine state (matches record_success_does_not_clear_quarantine).
    budget = FailureBudget(repo_with_migrations, logger)
    quarantine_failure_count = 5
    budget._quarantined["test_collector"] = QuarantineState(  # pyright: ignore[reportPrivateUsage]
        consecutive_failures=quarantine_failure_count,
        quarantined_at="2026-05-05T00:00:00",
        quarantine_reason="pre-populated",
    )
    budget._consecutive_failures["test_collector"] = quarantine_failure_count  # pyright: ignore[reportPrivateUsage]

    # Pre-seed the collectors row so any UPDATE would actually find a target.
    async with repo_with_migrations.transaction() as conn:
        await conn.execute(
            text(
                "UPDATE collectors SET consecutive_failures = :count, "
                "quarantined_at = '2026-05-05T00:00:00', "
                "quarantine_reason = 'pre-populated' WHERE name = :name"
            ),
            {"count": quarantine_failure_count, "name": "test_collector"},
        )

    # Call record_failure on the quarantined collector — should be a no-op.
    await budget.record_failure("test_collector", "exception")

    # Counter should NOT have incremented (defensive guard fired).
    assert budget.consecutive_failures("test_collector") == quarantine_failure_count

    # No additional quarantine_entered audit rows should exist beyond what's
    # in the DB (since this fixture's collectors row was pre-populated by
    # test setup, the audit_log shouldn't have any rows for this collector
    # since record_failure short-circuited before the transaction).
    async with repo_with_migrations.transaction() as conn:
        result = await conn.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE what = 'collector.quarantine_entered'")
        )
        count = result.scalar()
    assert count == 0


# Quarantine threshold


@pytest.mark.asyncio
async def test_quarantine_threshold_default_5(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Default threshold is 5 consecutive failures."""
    budget = FailureBudget(repo_with_migrations, logger)
    for _ in range(4):
        await budget.record_failure("test_collector", "exception")
    assert budget.is_quarantined("test_collector") is False

    await budget.record_failure("test_collector", "exception")
    assert budget.is_quarantined("test_collector") is True


@pytest.mark.asyncio
async def test_quarantine_threshold_per_collector_override(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Per-collector threshold override via threshold param."""
    budget = FailureBudget(repo_with_migrations, logger)
    await budget.record_failure("test_collector", "exception", threshold=2)
    assert budget.is_quarantined("test_collector") is False

    await budget.record_failure("test_collector", "exception", threshold=2)
    assert budget.is_quarantined("test_collector") is True


@pytest.mark.asyncio
async def test_quarantine_at_exactly_threshold(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Quarantine triggers when count == threshold."""
    budget = FailureBudget(repo_with_migrations, logger, default_threshold=3)
    await budget.record_failure("test_collector", "timeout", threshold=3)
    assert budget.is_quarantined("test_collector") is False
    assert budget.consecutive_failures("test_collector") == 1

    await budget.record_failure("test_collector", "timeout", threshold=3)
    assert budget.is_quarantined("test_collector") is False
    assert budget.consecutive_failures("test_collector") == 2  # noqa: PLR2004

    await budget.record_failure("test_collector", "timeout", threshold=3)
    assert budget.is_quarantined("test_collector") is True
    assert budget.consecutive_failures("test_collector") == 3  # noqa: PLR2004


# Persistence + audit


@pytest.mark.asyncio
async def test_quarantine_persists_to_db(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Quarantine entry updates all 3 DB columns."""
    budget = FailureBudget(repo_with_migrations, logger)
    for _ in range(5):
        await budget.record_failure("test_collector", "exception")

    async with repo_with_migrations.transaction() as conn:
        result = await conn.execute(
            text(
                "SELECT consecutive_failures, quarantined_at, quarantine_reason "
                "FROM collectors WHERE name = :name"
            ),
            {"name": "test_collector"},
        )
        row = result.fetchone()

    assert row is not None
    assert row[0] == 5  # noqa: PLR2004
    assert row[1] is not None  # quarantined_at is set
    assert row[2] is not None  # quarantine_reason is set


@pytest.mark.asyncio
async def test_quarantine_writes_audit_log_atomically(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Quarantine entry writes audit_log row in same transaction."""
    budget = FailureBudget(repo_with_migrations, logger)
    for _ in range(5):
        await budget.record_failure("test_collector", "exception")

    async with repo_with_migrations.transaction() as conn:
        result = await conn.execute(text("SELECT COUNT(*) FROM audit_log"))
        count = result.scalar()

    assert count == 1


@pytest.mark.asyncio
async def test_audit_event_name_is_collector_quarantine_entered(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Quarantine audit event is 'collector.quarantine_entered'."""
    budget = FailureBudget(repo_with_migrations, logger)
    for _ in range(5):
        await budget.record_failure("test_collector", "exception")

    async with repo_with_migrations.transaction() as conn:
        result = await conn.execute(
            text("SELECT what FROM audit_log WHERE what = :what"),
            {"what": "collector.quarantine_entered"},
        )
        row = result.fetchone()

    assert row is not None
    assert row[0] == "collector.quarantine_entered"


@pytest.mark.asyncio
async def test_audit_who_is_scheduler_for_system_initiated(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Quarantine audit who is 'scheduler'."""
    budget = FailureBudget(repo_with_migrations, logger)
    for _ in range(5):
        await budget.record_failure("test_collector", "exception")

    async with repo_with_migrations.transaction() as conn:
        result = await conn.execute(
            text("SELECT who FROM audit_log WHERE what = :what"),
            {"what": "collector.quarantine_entered"},
        )
        row = result.fetchone()

    assert row is not None
    assert row[0] == "scheduler"


@pytest.mark.asyncio
async def test_audit_before_and_after_contain_full_state(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Audit before/after JSON contain full quarantine state."""
    budget = FailureBudget(repo_with_migrations, logger)
    for _ in range(5):
        await budget.record_failure("test_collector", "timeout")

    async with repo_with_migrations.transaction() as conn:
        result = await conn.execute(
            text("SELECT before_json, after_json FROM audit_log WHERE what = :what"),
            {"what": "collector.quarantine_entered"},
        )
        row = result.fetchone()

    assert row is not None
    before = json.loads(row[0])
    after = json.loads(row[1])

    # Before should have no quarantine, after should have it
    assert before["quarantined_at"] is None
    assert before["quarantine_reason"] is None
    assert before["consecutive_failures"] == 4  # noqa: PLR2004

    assert after["quarantined_at"] is not None
    assert after["quarantine_reason"] is not None
    assert after["consecutive_failures"] == 5  # noqa: PLR2004


# Clear quarantine


@pytest.mark.asyncio
async def test_clear_quarantine_resets_db_state(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """clear_quarantine updates DB to NULL/0."""
    budget = FailureBudget(repo_with_migrations, logger)
    for _ in range(5):
        await budget.record_failure("test_collector", "exception")

    await budget.clear_quarantine("test_collector")

    async with repo_with_migrations.transaction() as conn:
        result = await conn.execute(
            text(
                "SELECT consecutive_failures, quarantined_at, quarantine_reason "
                "FROM collectors WHERE name = :name"
            ),
            {"name": "test_collector"},
        )
        row = result.fetchone()

    assert row is not None
    assert row[0] == 0
    assert row[1] is None
    assert row[2] is None


@pytest.mark.asyncio
async def test_clear_quarantine_writes_audit_log(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """clear_quarantine writes audit_log row."""
    budget = FailureBudget(repo_with_migrations, logger)
    for _ in range(5):
        await budget.record_failure("test_collector", "exception")

    await budget.clear_quarantine("test_collector")

    async with repo_with_migrations.transaction() as conn:
        result = await conn.execute(
            text("SELECT what FROM audit_log WHERE what = :what"),
            {"what": "collector.quarantine_cleared"},
        )
        row = result.fetchone()

    assert row is not None
    assert row[0] == "collector.quarantine_cleared"


@pytest.mark.asyncio
async def test_clear_quarantine_audit_who_default_operator(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """clear_quarantine defaults who to 'operator'."""
    budget = FailureBudget(repo_with_migrations, logger)
    for _ in range(5):
        await budget.record_failure("test_collector", "exception")

    await budget.clear_quarantine("test_collector")

    async with repo_with_migrations.transaction() as conn:
        result = await conn.execute(
            text("SELECT who FROM audit_log WHERE what = :what"),
            {"what": "collector.quarantine_cleared"},
        )
        row = result.fetchone()

    assert row is not None
    assert row[0] == "operator"


@pytest.mark.asyncio
async def test_clear_quarantine_audit_who_custom_by(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """clear_quarantine respects custom by parameter."""
    budget = FailureBudget(repo_with_migrations, logger)
    for _ in range(5):
        await budget.record_failure("test_collector", "exception")

    await budget.clear_quarantine("test_collector", by="alice")

    async with repo_with_migrations.transaction() as conn:
        result = await conn.execute(
            text("SELECT who FROM audit_log WHERE what = :what"),
            {"what": "collector.quarantine_cleared"},
        )
        row = result.fetchone()

    assert row is not None
    assert row[0] == "alice"


@pytest.mark.asyncio
async def test_clear_quarantine_idempotent_when_not_quarantined(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """clear_quarantine is idempotent when not quarantined."""
    budget = FailureBudget(repo_with_migrations, logger)
    await budget.clear_quarantine("test_collector")

    # Verify no audit row was written
    async with repo_with_migrations.transaction() as conn:
        result = await conn.execute(text("SELECT COUNT(*) FROM audit_log"))
        count = result.scalar()

    assert count == 0


@pytest.mark.asyncio
async def test_clear_quarantine_resets_in_memory_counter(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """clear_quarantine resets in-memory counter to 0."""
    budget = FailureBudget(repo_with_migrations, logger)
    for _ in range(5):
        await budget.record_failure("test_collector", "exception")

    await budget.clear_quarantine("test_collector")
    assert budget.consecutive_failures("test_collector") == 0


# Load state


@pytest.mark.asyncio
async def test_load_state_reads_persisted_quarantine(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """load_state rehydrates quarantine from DB."""
    # Manually insert a quarantined row
    async with repo_with_migrations.transaction() as conn:
        await conn.execute(
            text("""
            UPDATE collectors
            SET consecutive_failures = 5,
                quarantined_at = '2026-01-01T12:00:00Z',
                quarantine_reason = 'test'
            WHERE name = :name
            """),
            {"name": "test_collector"},
        )

    budget = FailureBudget(repo_with_migrations, logger)
    await budget.load_state()

    assert budget.is_quarantined("test_collector") is True


@pytest.mark.asyncio
async def test_load_state_reads_persisted_counter(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """load_state rehydrates counter from DB."""
    async with repo_with_migrations.transaction() as conn:
        await conn.execute(
            text("UPDATE collectors SET consecutive_failures = 3 WHERE name = :name"),
            {"name": "test_collector"},
        )

    budget = FailureBudget(repo_with_migrations, logger)
    await budget.load_state()

    assert budget.consecutive_failures("test_collector") == 3  # noqa: PLR2004


@pytest.mark.asyncio
async def test_load_state_skips_zero_counter_and_no_quarantine(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """load_state skips rows with counter=0 and no quarantine."""
    # DB row already has counter=0 and quarantine_at=NULL
    budget = FailureBudget(repo_with_migrations, logger)
    await budget.load_state()

    # In-memory dicts should be empty
    assert len(budget._consecutive_failures) == 0  # pyright: ignore[reportPrivateUsage]
    assert len(budget._quarantined) == 0  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_load_state_idempotent(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """load_state is idempotent; second call returns early."""
    async with repo_with_migrations.transaction() as conn:
        await conn.execute(
            text("UPDATE collectors SET consecutive_failures = 3 WHERE name = :name"),
            {"name": "test_collector"},
        )

    budget = FailureBudget(repo_with_migrations, logger)
    await budget.load_state()
    count1 = budget.consecutive_failures("test_collector")

    # Update DB to a different value
    async with repo_with_migrations.transaction() as conn:
        await conn.execute(
            text("UPDATE collectors SET consecutive_failures = 5 WHERE name = :name"),
            {"name": "test_collector"},
        )

    # Call load_state again
    await budget.load_state()
    count2 = budget.consecutive_failures("test_collector")

    # Should still be 3 (second load_state was no-op)
    assert count1 == 3  # noqa: PLR2004
    assert count2 == 3  # noqa: PLR2004


# Logging


@pytest.mark.asyncio
async def test_warning_log_emitted_on_quarantine(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Quarantine entry emits WARNING log."""
    budget = FailureBudget(repo_with_migrations, logger)

    with capture_logs() as cap_logs:
        for _ in range(5):
            await budget.record_failure("test_collector", "exception")

    # Find the log event
    warning_logs = [log for log in cap_logs if log.get("event") == "collector_quarantined"]
    assert len(warning_logs) == 1
    assert warning_logs[0]["name"] == "test_collector"
    assert warning_logs[0]["consecutive_failures"] == 5  # noqa: PLR2004


@pytest.mark.asyncio
async def test_info_log_emitted_on_clear(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Quarantine clear emits INFO log."""
    budget = FailureBudget(repo_with_migrations, logger)
    for _ in range(5):
        await budget.record_failure("test_collector", "exception")

    with capture_logs() as cap_logs:
        await budget.clear_quarantine("test_collector", by="alice")

    info_logs = [log for log in cap_logs if log.get("event") == "collector_quarantine_cleared"]
    assert len(info_logs) == 1
    assert info_logs[0]["name"] == "test_collector"
    assert info_logs[0]["cleared_by"] == "alice"


# Clock injection


@pytest.mark.asyncio
async def test_clock_injection_used_for_quarantined_at(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """FailureBudget uses injected clock for quarantined_at timestamp."""
    fixed_time = "2026-01-01T00:00:00Z"
    budget = FailureBudget(
        repo_with_migrations,
        logger,
        clock=lambda: fixed_time,
    )

    for _ in range(5):
        await budget.record_failure("test_collector", "exception")

    async with repo_with_migrations.transaction() as conn:
        result = await conn.execute(
            text("SELECT quarantined_at FROM collectors WHERE name = :name"),
            {"name": "test_collector"},
        )
        row = result.fetchone()

    assert row is not None
    assert row[0] == fixed_time


@pytest.mark.asyncio
async def test_load_state_quarantined_with_zero_counter(
    repo_with_migrations: SqliteRepository,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Persisted state: quarantined=True but counter=0 (edge case from manual DB fix-up).

    The load_state SELECT filters on (consecutive_failures > 0 OR quarantined_at IS NOT NULL).
    So it's possible for a row to have consecutive_failures=0 but quarantined_at IS NOT NULL.
    This tests the if count > 0 branch handles that case correctly.
    """
    # Pre-seed: insert a collector row with consecutive_failures=0 but quarantined_at set
    async with repo_with_migrations.transaction() as conn:
        await conn.execute(
            text(
                "UPDATE collectors SET consecutive_failures = 0, "
                "quarantined_at = '2026-05-05T00:00:00Z', "
                "quarantine_reason = 'manual' WHERE name = :name"
            ),
            {"name": "test_collector"},
        )
    budget = FailureBudget(repo_with_migrations, logger)
    await budget.load_state()
    assert budget.is_quarantined("test_collector")
    assert budget.consecutive_failures("test_collector") == 0
