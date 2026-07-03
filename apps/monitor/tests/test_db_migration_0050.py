"""Tests for alembic migration 0050: add initiated_by column to runbook_runs.

Upgrade adds a NOT NULL TEXT column with server_default='alert' (so existing
rows backfill to 'alert'), then drops the server default so future INSERTs
must supply the value explicitly. Downgrade drops the column.
"""

from __future__ import annotations

import json

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.migrations import ALEMBIC_DIR


def _make_cfg(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.mark.asyncio
async def test_migration_0050_adds_initiated_by_column(db_url: str) -> None:
    """After upgrade to head, runbook_runs.initiated_by exists, NOT NULL, TEXT."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "head")
    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:

            def _get_col(sync_conn: object) -> dict[str, object] | None:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return None
                for col in inspector.get_columns("runbook_runs"):
                    if col["name"] == "initiated_by":
                        return col
                return None

            col = await conn.run_sync(_get_col)
        assert col is not None
        assert col["nullable"] is False
        assert "TEXT" in str(col["type"]).upper()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0050_backfills_existing_rows_to_alert(db_url: str) -> None:
    """A row inserted at revision 0049 (pre-migration schema) backfills to 'alert'
    after upgrading through 0050.
    """
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "0049")
    engine: AsyncEngine = get_engine(url=db_url)
    try:
        # Seed minimal required columns for a runbook_runs row at 0049 schema.
        # Need a parent runbooks row to satisfy the FK.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO runbooks "
                    "(id, path, created_at, alert_match_patterns, risk_tag, "
                    " dry_run_required, rate_limit_per_hour, cooldown_seconds, "
                    " enabled, auto_trigger, content_hash) "
                    "VALUES (:id, :path, :created_at, :patterns, :risk_tag, "
                    " :dry_run, :rate_limit, :cooldown, :enabled, :auto_trigger, :hash)"
                ),
                {
                    "id": "rb-backfill",
                    "path": "/runbooks/backfill",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "patterns": json.dumps([{"alertname": "X", "labels": {}}]),
                    "risk_tag": "safe",
                    "dry_run": 0,
                    "rate_limit": None,
                    "cooldown": None,
                    "enabled": 1,
                    "auto_trigger": 0,
                    "hash": "hash1",
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO runbook_runs "
                    "(id, runbook_id, alert_id, mode, prompt, transcript_path, "
                    " exit_code, started_at, ended_at, fixer_user, host, runbook_hash, created_at) "
                    "VALUES (:id, :rbid, NULL, :mode, :prompt, NULL, NULL, "
                    " :started, NULL, :fixer_user, :host, :hash, :created_at)"
                ),
                {
                    "id": "run-backfill",
                    "rbid": "rb-backfill",
                    "mode": "real",
                    "prompt": "/runbooks/backfill",
                    "started": "2026-01-01T00:00:00+00:00",
                    "fixer_user": "homelab-fixer",
                    "host": "test-host",
                    "hash": "hash1",
                    "created_at": "2026-01-01T00:00:00+00:00",
                },
            )

        command.upgrade(cfg, "0050")

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT initiated_by FROM runbook_runs WHERE id = 'run-backfill'")
                )
            ).first()
        assert row is not None
        assert row[0] == "alert"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0050_downgrade_round_trip(db_url: str) -> None:
    """0049 -> 0050 -> 0049 completes without error; column is gone after downgrade."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "0049")
    command.upgrade(cfg, "0050")
    command.downgrade(cfg, "0049")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:

            def _get_col_names(sync_conn: object) -> set[str]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return set()
                return {col["name"] for col in inspector.get_columns("runbook_runs")}

            cols = await conn.run_sync(_get_col_names)
        assert "initiated_by" not in cols
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0050_drops_server_default_insert_without_value_fails(
    db_url: str,
) -> None:
    """After upgrade, the server_default is dropped: an INSERT omitting
    initiated_by must fail with IntegrityError (NOT NULL constraint, no default).
    """
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "head")
    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO runbooks "
                    "(id, path, created_at, alert_match_patterns, risk_tag, "
                    " dry_run_required, rate_limit_per_hour, cooldown_seconds, "
                    " enabled, auto_trigger, content_hash) "
                    "VALUES (:id, :path, :created_at, :patterns, :risk_tag, "
                    " :dry_run, :rate_limit, :cooldown, :enabled, :auto_trigger, :hash)"
                ),
                {
                    "id": "rb-nodefault",
                    "path": "/runbooks/nodefault",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "patterns": json.dumps([{"alertname": "X", "labels": {}}]),
                    "risk_tag": "safe",
                    "dry_run": 0,
                    "rate_limit": None,
                    "cooldown": None,
                    "enabled": 1,
                    "auto_trigger": 0,
                    "hash": "hash2",
                },
            )

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO runbook_runs "
                        "(id, runbook_id, alert_id, mode, prompt, transcript_path, "
                        " exit_code, started_at, ended_at, fixer_user, host, runbook_hash) "
                        "VALUES (:id, :rbid, NULL, :mode, :prompt, NULL, NULL, "
                        " :started, NULL, :fixer_user, :host, :hash)"
                    ),
                    {
                        "id": "run-nodefault",
                        "rbid": "rb-nodefault",
                        "mode": "real",
                        "prompt": "/runbooks/nodefault",
                        "started": "2026-01-01T00:00:00+00:00",
                        "fixer_user": "homelab-fixer",
                        "host": "test-host",
                        "hash": "hash2",
                    },
                )
    finally:
        await engine.dispose()
