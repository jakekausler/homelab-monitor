"""Tests for ``kernel.db.migrations``: pending check, run, round-trip, env gate."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.migrations import (
    ALEMBIC_DIR,
    MigrationsPendingError,
    alembic_current_revision,
    alembic_head_revision,
    alembic_history,
    alembic_upgrade_head,
    check_pending_migrations,
    run_migrations,
)

EXPECTED_TABLES = {
    "users",
    "sessions",
    "audit_log",
    "api_tokens",
    "targets",
    "collectors",
    "crons",
    "heartbeats_state",
    "alerts",
    "alert_outcomes",
    "runbooks",
    "runbook_runs",
    "secrets",
    "channels",
    "routing_rules",
    "digest_configs",
    "maintenance_windows",
    "suggestions",
    "tool_scorecards",
}


async def test_check_pending_returns_revisions_on_empty_db(db_url: str) -> None:
    """A fresh DB lists all known revisions as pending."""
    engine = get_engine(url=db_url)
    try:
        pending = await check_pending_migrations(engine)
        # Don't hardcode the migration list — test that all migrations are pending
        # and that 0001 is the oldest (last in newest-to-oldest order).
        assert len(pending) >= 1
        assert pending[-1] == "0001"
    finally:
        await engine.dispose()


async def test_run_migrations_applies_head(db_url: str) -> None:
    """After ``run_migrations`` the DB is at head and contains all 19 tables."""
    engine = get_engine(url=db_url)
    try:
        await run_migrations(engine)
        pending = await check_pending_migrations(engine)
        assert pending == []

        def _list_tables(sync_conn: object) -> set[str]:
            inspector = inspect(sync_conn)
            return set(inspector.get_table_names()) if inspector is not None else set()

        async with engine.connect() as conn:
            tables = await conn.run_sync(_list_tables)
        # alembic_version is added by Alembic itself; remove for the assertion.
        tables.discard("alembic_version")
        assert tables == EXPECTED_TABLES
    finally:
        await engine.dispose()


async def test_run_migrations_no_op_at_head(db_url: str) -> None:
    """Calling ``run_migrations`` twice is safe."""
    engine = get_engine(url=db_url)
    try:
        await run_migrations(engine)
        await run_migrations(engine)  # no-op
        assert await check_pending_migrations(engine) == []
    finally:
        await engine.dispose()


async def test_run_migrations_raises_when_disabled(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With auto-migrate disabled and pending migrations, refuse to start."""
    monkeypatch.setenv("HOMELAB_MONITOR_AUTO_MIGRATE", "false")
    engine = get_engine(url=db_url)
    try:
        with pytest.raises(MigrationsPendingError, match="HOMELAB_MONITOR_AUTO_MIGRATE"):
            await run_migrations(engine)
    finally:
        await engine.dispose()


async def test_round_trip_downgrade_then_upgrade(db_url: str) -> None:
    """Upgrade head -> downgrade base -> upgrade head leaves the DB at head."""
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' AND name='alerts'")
                )
            ).first()
        assert row is not None
    finally:
        await engine.dispose()


def test_alembic_helpers_at_head(db_url: str) -> None:
    """``current``/``head``/``history`` helpers return sensible values after upgrade."""
    alembic_upgrade_head(db_url)
    # Current and head should be equal (and non-None) after upgrade.
    current = alembic_current_revision(db_url)
    head = alembic_head_revision(db_url)
    assert current is not None
    assert head is not None
    assert current == head
    history = alembic_history(db_url)
    # 0001 is always the base revision regardless of how many migrations exist.
    assert any(line.startswith("0001 ->") for line in history)


def test_alembic_current_revision_empty_db(db_url: str) -> None:
    """Without running upgrade, current revision is ``None``."""
    assert alembic_current_revision(db_url) is None


async def test_check_pending_returns_empty_at_head(db_url: str) -> None:
    """Once at head, pending migrations list is empty."""
    engine = get_engine(url=db_url)
    try:
        await run_migrations(engine)
        pending = await check_pending_migrations(engine)
        assert pending == []
    finally:
        await engine.dispose()


async def test_check_pending_migrations_with_intermediate_current(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When current revision is mid-history, only revisions newer than current are pending.

    Mocks both the script directory (to simulate a multi-revision history) and the
    MigrationContext (to control what "current" the function reads). CRIT-1's fix
    must work for future N-revision states regardless of how many real migrations exist.
    """
    # Mock walk_revisions to pretend there are three revisions: 0003 (head), 0002, 0001.
    # walk_revisions yields newest-to-oldest. Mock current = 0001 (intermediate).
    mock_revs = [
        MagicMock(revision="0003"),
        MagicMock(revision="0002"),
        MagicMock(revision="0001"),
    ]
    mock_script = MagicMock()
    mock_script.get_current_head.return_value = "0003"
    mock_script.walk_revisions.return_value = mock_revs

    with (
        patch(
            "homelab_monitor.kernel.db.migrations.ScriptDirectory.from_config",
            return_value=mock_script,
        ),
        patch("homelab_monitor.kernel.db.migrations.MigrationContext.configure") as mock_ctx,
    ):
        # DB's actual current revision (0002) is overridden by the mock to be 0001
        # so the loop walks from 0003 down and breaks when it hits 0001.
        mock_ctx.return_value.get_current_revision.return_value = "0001"
        pending = await check_pending_migrations(db_engine)

    # Pending should be [0003, 0002] (everything newer than mocked-current 0001).
    assert pending == ["0003", "0002"]


async def test_check_pending_migrations_with_unknown_current(
    db_engine: AsyncEngine,
) -> None:
    """If DB's current revision is not in the script directory, the loop exhausts.

    Defensive: a stale ``alembic_version`` row pointing at a revision file that no
    longer exists in the script tree. Exercises the ``for`` loop's no-break exit
    branch in :func:`check_pending_migrations`. The function returns all walked
    revisions because none of them matches ``current``.
    """
    mock_revs = [
        MagicMock(revision="0002"),
        MagicMock(revision="0001"),
    ]
    mock_script = MagicMock()
    mock_script.get_current_head.return_value = "0002"
    mock_script.walk_revisions.return_value = mock_revs

    with (
        patch(
            "homelab_monitor.kernel.db.migrations.ScriptDirectory.from_config",
            return_value=mock_script,
        ),
        patch("homelab_monitor.kernel.db.migrations.MigrationContext.configure") as mock_ctx,
    ):
        mock_ctx.return_value.get_current_revision.return_value = "9999-stale"
        pending = await check_pending_migrations(db_engine)

    # No mock revision matches 9999-stale, so the loop exhausts and returns all.
    assert pending == ["0002", "0001"]
