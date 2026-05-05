"""Tests for the 0002_secrets_columns migration."""

from __future__ import annotations

import sqlite3
from urllib.parse import urlparse

import pytest
from alembic.config import Config
from sqlalchemy import inspect

from alembic import command
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.migrations import ALEMBIC_DIR

NEW_COLUMNS = {"ciphertext", "kdf_salt", "rotated_at"}


def _cfg(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


async def test_upgrade_adds_three_columns(db_url: str) -> None:
    """Applying head adds ciphertext, kdf_salt, rotated_at to the secrets table."""
    cfg = _cfg(db_url)
    command.upgrade(cfg, "head")

    engine = get_engine(url=db_url)
    try:

        def _columns(sync_conn: object) -> set[str]:
            inspector = inspect(sync_conn)
            assert inspector is not None
            return {c["name"] for c in inspector.get_columns("secrets")}

        async with engine.connect() as conn:
            cols = await conn.run_sync(_columns)
    finally:
        await engine.dispose()

    assert NEW_COLUMNS.issubset(cols)


async def test_downgrade_removes_columns(db_url: str) -> None:
    """Downgrading 0002 → 0001 removes the three new columns."""
    cfg = _cfg(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0001")

    engine = get_engine(url=db_url)
    try:

        def _columns(sync_conn: object) -> set[str]:
            inspector = inspect(sync_conn)
            assert inspector is not None
            return {c["name"] for c in inspector.get_columns("secrets")}

        async with engine.connect() as conn:
            cols = await conn.run_sync(_columns)
    finally:
        await engine.dispose()

    assert cols.isdisjoint(NEW_COLUMNS)


async def test_round_trip_clean(db_url: str) -> None:
    """Up → down → up leaves the schema consistent."""
    cfg = _cfg(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0001")
    command.upgrade(cfg, "head")

    engine = get_engine(url=db_url)
    try:

        def _columns(sync_conn: object) -> set[str]:
            inspector = inspect(sync_conn)
            assert inspector is not None
            return {c["name"] for c in inspector.get_columns("secrets")}

        async with engine.connect() as conn:
            cols = await conn.run_sync(_columns)
    finally:
        await engine.dispose()

    assert NEW_COLUMNS.issubset(cols)


def test_0002_refuses_upgrade_when_secrets_has_rows(db_url: str) -> None:
    """If migration 0002 finds existing rows in secrets, it must abort with a clear error."""
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)

    # Apply only 0001 first.
    command.upgrade(cfg, "0001")

    # Insert a stub row directly (no ciphertext yet, since 0001 schema doesn't have it).
    parsed = urlparse(db_url.replace("sqlite+aiosqlite", "sqlite"))
    db_file = parsed.path
    conn = sqlite3.connect(db_file)
    try:
        conn.execute(
            "INSERT INTO secrets (id, name, created_at) VALUES (?, ?, ?)",
            ("stub-id", "stub-name", "2026-05-05T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    # Now attempt to upgrade to head — should refuse with our guard message.
    with pytest.raises(RuntimeError, match="refusing to upgrade 0002"):
        command.upgrade(cfg, "head")
