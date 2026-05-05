"""Tests for the public in-transaction audit helper."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from homelab_monitor.kernel.db.audit import AUDIT_INSERT, insert_audit
from homelab_monitor.kernel.db.repository import SqliteRepository


@pytest.fixture
async def repo_with_migrations(tmp_path: Path) -> SqliteRepository:
    """Create a repo with migrations applied through 0003."""
    db_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    repo = SqliteRepository(engine)

    # Apply migrations programmatically via alembic
    # For test purposes, we manually create the audit_log and collectors tables
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


@pytest.mark.asyncio
async def test_insert_audit_writes_row_with_provided_when(
    repo_with_migrations: SqliteRepository,
) -> None:
    """insert_audit writes a row when called with explicit when timestamp."""
    when = "2026-01-01T00:00:00Z"
    before = {"x": 1}
    after = {"x": 2}

    async with repo_with_migrations.transaction() as conn:
        await insert_audit(
            conn,
            who="test",
            what="test.event",
            before=before,
            after=after,
            when=when,
        )

    # Read back the row
    async with repo_with_migrations.transaction() as conn:
        result = await conn.execute(
            text('SELECT who, what, "when", before_json, after_json FROM audit_log')
        )
        row = result.fetchone()

    assert row is not None
    assert row[0] == "test"
    assert row[1] == "test.event"
    assert row[2] == when
    assert json.loads(row[3]) == before
    assert json.loads(row[4]) == after


@pytest.mark.asyncio
async def test_insert_audit_defaults_when_to_now_iso(
    repo_with_migrations: SqliteRepository,
) -> None:
    """insert_audit defaults when to utc_now_iso when omitted."""
    test_start = time.time()

    async with repo_with_migrations.transaction() as conn:
        await insert_audit(
            conn,
            who="test",
            what="test.event",
        )

    # Read back and verify when is a valid ISO timestamp
    async with repo_with_migrations.transaction() as conn:
        result = await conn.execute(text('SELECT "when" FROM audit_log'))
        row = result.fetchone()

    assert row is not None
    when_str = row[0]
    # Parse ISO timestamp
    when_dt = datetime.fromisoformat(when_str.replace("Z", "+00:00"))
    now_ts = time.time()

    # Assert within ±5 seconds of test run
    assert abs(when_dt.timestamp() - test_start) < 5  # noqa: PLR2004
    assert abs(when_dt.timestamp() - now_ts) < 5  # noqa: PLR2004


@pytest.mark.asyncio
async def test_insert_audit_serializes_before_after_to_json(
    repo_with_migrations: SqliteRepository,
) -> None:
    """insert_audit serializes nested dicts to JSON."""
    before = {"a": {"nested": [1, 2, 3]}}
    after = {"b": {"deep": {"value": "x"}}}

    async with repo_with_migrations.transaction() as conn:
        await insert_audit(
            conn,
            who="test",
            what="test.event",
            before=before,
            after=after,
        )

    async with repo_with_migrations.transaction() as conn:
        result = await conn.execute(text("SELECT before_json, after_json FROM audit_log"))
        row = result.fetchone()

    assert row is not None
    assert json.loads(row[0]) == before
    assert json.loads(row[1]) == after


@pytest.mark.asyncio
async def test_insert_audit_handles_none_before_and_after(
    repo_with_migrations: SqliteRepository,
) -> None:
    """insert_audit stores NULL when before/after are None."""
    async with repo_with_migrations.transaction() as conn:
        await insert_audit(
            conn,
            who="test",
            what="test.event",
            before=None,
            after=None,
        )

    async with repo_with_migrations.transaction() as conn:
        result = await conn.execute(text("SELECT before_json, after_json FROM audit_log"))
        row = result.fetchone()

    assert row is not None
    assert row[0] is None
    assert row[1] is None


@pytest.mark.asyncio
async def test_insert_audit_uses_provided_connection(
    repo_with_migrations: SqliteRepository,
) -> None:
    """insert_audit runs on the supplied connection, not a new one."""
    # Start a transaction, call insert_audit, then RAISE before commit
    # to verify the row is NOT visible after rollback (proving it ran on
    # the supplied conn, not a new one).
    try:
        async with repo_with_migrations.transaction() as conn:
            await insert_audit(
                conn,
                who="test",
                what="test.event",
            )
            # Deliberately raise to trigger rollback
            raise ValueError("rollback test")
    except ValueError:
        pass

    # Verify row is not visible (transaction rolled back)
    async with repo_with_migrations.transaction() as conn:
        result = await conn.execute(text("SELECT COUNT(*) FROM audit_log"))
        count = result.scalar()

    assert count == 0


@pytest.mark.asyncio
async def test_audit_insert_constant_defined() -> None:
    """AUDIT_INSERT constant is defined and is a SQLAlchemy Text object."""
    assert AUDIT_INSERT is not None
    # Verify it's a SQLAlchemy text object
    assert hasattr(AUDIT_INSERT, "text")
