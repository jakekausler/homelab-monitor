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
    # F1: unique partial index for race-safe dedup of firing inserts
    assert "ux_alerts_fingerprint_firing" in names

    async with db_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA index_list('alert_outcomes')"))
        outcome_names = {str(row[1]) for row in result.fetchall()}
    assert "ix_alert_outcomes_alert_id" in outcome_names


async def test_listing_uses_status_index(db_engine: AsyncEngine) -> None:
    """F15: SQLite's planner uses ix_alerts_status_opened_at for the listing query.

    Inserts ~10 alerts with a mix of statuses, then runs EXPLAIN QUERY PLAN
    over the canonical listing query and asserts the planner referenced the
    new index.
    """
    # Seed a small mix so the planner has a non-trivial choice.
    async with db_engine.begin() as conn:
        for n in range(10):
            await conn.execute(
                text(
                    "INSERT INTO alerts "
                    "(id, fingerprint, source_tool, severity, status, "
                    "opened_at, last_seen_at, payload_json, created_at) "
                    "VALUES (:id, :fp, 'alertmanager', 'warning', :status, "
                    ":opened, :opened, '{}', :opened)"
                ),
                {
                    "id": f"a-{n}",
                    "fp": f"fp-{n}",
                    "status": "firing" if n % 2 == 0 else "resolved",
                    "opened": f"2026-05-07T00:{n:02d}:00+00:00",
                },
            )

    async with db_engine.connect() as conn:
        plan_rows = (
            await conn.execute(
                text(
                    "EXPLAIN QUERY PLAN "
                    "SELECT id, fingerprint, source_tool, severity, status, "
                    "opened_at, last_seen_at, resolved_at, ack_at, ack_by, "
                    "runbook_id, payload_json FROM alerts "
                    "WHERE status = 'firing' "
                    "ORDER BY opened_at DESC, id DESC LIMIT 10"
                )
            )
        ).fetchall()

    # SQLite's plan output format: (id, parent, notused, detail). The detail
    # string includes "USING INDEX <name>" when an index is consulted.
    detail = " ".join(str(r[3]) for r in plan_rows)
    assert "ix_alerts_status_opened_at" in detail, (
        f"planner did not use ix_alerts_status_opened_at; plan was: {detail!r}"
    )
