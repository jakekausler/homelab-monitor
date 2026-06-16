"""Tests for alembic migration 0043: remap log_user_rules severity 'error' → 'critical'.

Exercises the migration's upgrade() at the correct schema: insert error row at 0042,
then let 0043's upgrade() perform the transformation.
"""

from __future__ import annotations

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.migrations import ALEMBIC_DIR

_NOW = "2026-06-16T00:00:00+00:00"


def _make_cfg(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.mark.asyncio
async def test_migration_0043_remaps_error_to_critical(db_url: str) -> None:
    """An existing severity='error' row becomes 'critical'; others unchanged.

    Seed rows at 0042 schema (before 0043 runs), then upgrade to head so
    0043's upgrade() performs the transformation on pre-existing data.
    """
    cfg = _make_cfg(db_url)
    # Upgrade only to 0042 so we can seed rows before 0043 runs.
    command.upgrade(cfg, "0042")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            # Insert three rows at the 0042 schema.
            await conn.execute(
                text(
                    "INSERT INTO log_user_rules "
                    "(rule_name, expr, expr_kind, severity, summary, description, "
                    " for_duration, source_kind, enabled, created_at, updated_at) "
                    "VALUES (:name, :expr, :kind, :severity, :summary, :desc, "
                    " :duration, :source, :enabled, :created, :updated)"
                ),
                {
                    "name": "ErrRow",
                    "expr": "up == 0",
                    "kind": "metricsql",
                    "severity": "error",
                    "summary": "s",
                    "desc": "",
                    "duration": "0s",
                    "source": "manual",
                    "enabled": 1,
                    "created": _NOW,
                    "updated": _NOW,
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO log_user_rules "
                    "(rule_name, expr, expr_kind, severity, summary, description, "
                    " for_duration, source_kind, enabled, created_at, updated_at) "
                    "VALUES (:name, :expr, :kind, :severity, :summary, :desc, "
                    " :duration, :source, :enabled, :created, :updated)"
                ),
                {
                    "name": "CritRow",
                    "expr": "up == 0",
                    "kind": "metricsql",
                    "severity": "critical",
                    "summary": "s",
                    "desc": "",
                    "duration": "0s",
                    "source": "manual",
                    "enabled": 1,
                    "created": _NOW,
                    "updated": _NOW,
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO log_user_rules "
                    "(rule_name, expr, expr_kind, severity, summary, description, "
                    " for_duration, source_kind, enabled, created_at, updated_at) "
                    "VALUES (:name, :expr, :kind, :severity, :summary, :desc, "
                    " :duration, :source, :enabled, :created, :updated)"
                ),
                {
                    "name": "WarnRow",
                    "expr": "up == 0",
                    "kind": "metricsql",
                    "severity": "warning",
                    "summary": "s",
                    "desc": "",
                    "duration": "0s",
                    "source": "manual",
                    "enabled": 1,
                    "created": _NOW,
                    "updated": _NOW,
                },
            )
            await conn.commit()
    finally:
        await engine.dispose()

    # Now upgrade through 0043 — its upgrade() will transform error → critical.
    command.upgrade(cfg, "head")

    engine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(text("SELECT rule_name, severity FROM log_user_rules"))
            ).fetchall()

        by_name = {r.rule_name: r.severity for r in rows}
        assert by_name["ErrRow"] == "critical", (
            "error row should be transformed to critical by migration"
        )
        assert by_name["CritRow"] == "critical", "critical row should remain critical"
        assert by_name["WarnRow"] == "warning", "warning row should remain warning"
    finally:
        await engine.dispose()
