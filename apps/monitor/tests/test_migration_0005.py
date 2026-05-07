"""Tests for migration 0005: alerts + alert_outcomes columns + indexes."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.migrations import (
    alembic_upgrade_head,
    check_pending_migrations,
)


async def test_migration_0005_in_pending_then_applied(db_url: str) -> None:
    """0005 appears in pending on a fresh DB and is empty after upgrade-head."""
    engine = get_engine(url=db_url)
    try:
        pending = await check_pending_migrations(engine)
        assert "0005" in pending
    finally:
        await engine.dispose()

    alembic_upgrade_head(db_url)

    engine = get_engine(url=db_url)
    try:
        post_pending = await check_pending_migrations(engine)
        assert post_pending == []
    finally:
        await engine.dispose()


async def test_alerts_columns_added(db_engine: AsyncEngine) -> None:
    """All spec §6.1 columns exist on alerts after migrations."""
    expected = {
        "source_tool",
        "severity",
        "status",
        "opened_at",
        "last_seen_at",
        "resolved_at",
        "ack_at",
        "ack_by",
        "runbook_id",
        "payload_json",
    }
    async with db_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA table_info(alerts)"))
        names = {str(row[1]) for row in result.fetchall()}
    assert expected.issubset(names)


async def test_alert_outcomes_columns_added(db_engine: AsyncEngine) -> None:
    """All spec §6.1 columns exist on alert_outcomes after migrations."""
    expected = {"outcome", "decided_at", "decided_by"}
    async with db_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA table_info(alert_outcomes)"))
        names = {str(row[1]) for row in result.fetchall()}
    assert expected.issubset(names)


async def test_alerts_indexes_present(db_engine: AsyncEngine) -> None:
    """ix_alerts_source_tool_opened_at and ix_alerts_status_opened_at exist."""
    async with db_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA index_list('alerts')"))
        names = {str(row[1]) for row in result.fetchall()}
    assert "ix_alerts_source_tool_opened_at" in names
    assert "ix_alerts_status_opened_at" in names

    async with db_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA index_list('alert_outcomes')"))
        outcome_names = {str(row[1]) for row in result.fetchall()}
    assert "ix_alert_outcomes_alert_id" in outcome_names
