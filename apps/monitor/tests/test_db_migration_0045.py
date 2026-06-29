"""Tests for alembic migration 0045: runbooks + runbook_runs auto-fix columns.

Round-trip: upgrade to head adds all new columns (+ the runbook_runs->alerts FK);
downgrade to 0044 removes them cleanly.
"""

from __future__ import annotations

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.migrations import ALEMBIC_DIR

_RUNBOOKS_NEW_COLUMNS = {
    "alert_match_patterns",
    "risk_tag",
    "dry_run_required",
    "rate_limit_per_hour",
    "cooldown_seconds",
    "enabled",
    "auto_trigger",
    "content_hash",
}
_RUNBOOK_RUNS_NEW_COLUMNS = {
    "alert_id",
    "mode",
    "prompt",
    "transcript_path",
    "exit_code",
    "started_at",
    "ended_at",
    "fixer_user",
    "host",
    "runbook_hash",
}


def _make_cfg(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.mark.asyncio
async def test_migration_0045_adds_runbooks_columns(db_url: str) -> None:
    """After upgrade to head, runbooks has all auto-fix columns."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "head")
    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:

            def _get_runbooks_col_names(sync_conn: object) -> set[str]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return set()
                return {col["name"] for col in inspector.get_columns("runbooks")}

            cols = await conn.run_sync(_get_runbooks_col_names)

        assert _RUNBOOKS_NEW_COLUMNS.issubset(cols)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0045_adds_runbook_runs_columns_and_fk(db_url: str) -> None:
    """After upgrade, runbook_runs has all columns + an FK to alerts."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "head")
    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:

            def _get_runs_col_names(sync_conn: object) -> set[str]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return set()
                return {col["name"] for col in inspector.get_columns("runbook_runs")}

            def _get_fk_targets(sync_conn: object) -> set[str]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return set()
                return {fk["referred_table"] for fk in inspector.get_foreign_keys("runbook_runs")}

            cols = await conn.run_sync(_get_runs_col_names)
            fk_targets = await conn.run_sync(_get_fk_targets)

        assert _RUNBOOK_RUNS_NEW_COLUMNS.issubset(cols)
        assert "alerts" in fk_targets, "runbook_runs.alert_id FK to alerts missing"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0045_conservative_server_defaults(db_url: str) -> None:
    """A row inserted with only id/path/created_at picks up the safety defaults."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "head")
    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO runbooks (id, path, created_at) "
                    "VALUES ('rb1', '/runbooks/rb1', '2026-06-29T00:00:00+00:00')"
                )
            )
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT risk_tag, dry_run_required, enabled, auto_trigger "
                        "FROM runbooks WHERE id = 'rb1'"
                    )
                )
            ).one()
        assert row.risk_tag == "risky"
        assert row.dry_run_required == 1
        assert row.enabled == 0
        assert row.auto_trigger == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0045_downgrade_removes_columns(db_url: str) -> None:
    """Downgrade to 0044 drops every added column from both tables."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0044")
    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:

            def _get_runbooks_col_names(sync_conn: object) -> set[str]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return set()
                return {col["name"] for col in inspector.get_columns("runbooks")}

            def _get_runs_col_names(sync_conn: object) -> set[str]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return set()
                return {col["name"] for col in inspector.get_columns("runbook_runs")}

            runbooks_cols = await conn.run_sync(_get_runbooks_col_names)
            runs_cols = await conn.run_sync(_get_runs_col_names)

        assert _RUNBOOKS_NEW_COLUMNS.isdisjoint(runbooks_cols)
        assert _RUNBOOK_RUNS_NEW_COLUMNS.isdisjoint(runs_cols)
        # Original stub columns survive.
        assert {"id", "path", "created_at"}.issubset(runbooks_cols)
        assert {"id", "runbook_id", "created_at"}.issubset(runs_cols)
    finally:
        await engine.dispose()
