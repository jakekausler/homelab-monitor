"""Tests for ``kernel.db.engine``: caching, env override, pragmas."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.db.engine import (
    DEFAULT_DATABASE_URL,
    dispose_engine,
    get_database_url,
    get_engine,
)


def test_get_database_url_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With env unset, falls back to the default URL."""
    monkeypatch.delenv("HOMELAB_MONITOR_DB_URL", raising=False)
    assert get_database_url() == DEFAULT_DATABASE_URL


def test_get_database_url_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env var overrides the default."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", "sqlite+aiosqlite:///./other.db")
    assert get_database_url() == "sqlite+aiosqlite:///./other.db"


async def test_get_engine_caches_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-URL ``get_engine()`` returns the same instance on repeat calls."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", "sqlite+aiosqlite:///./cache-test.db")
    await dispose_engine()
    e1 = get_engine()
    e2 = get_engine()
    try:
        assert e1 is e2
    finally:
        await dispose_engine()


async def test_get_engine_with_explicit_url_bypasses_cache(db_url: str) -> None:
    """Passing ``url=`` always returns a fresh engine."""
    e1 = get_engine(url=db_url)
    e2 = get_engine(url=db_url)
    try:
        assert e1 is not e2
    finally:
        await e1.dispose()
        await e2.dispose()


async def test_pragmas_applied_on_connect(db_url: str) -> None:
    """Verify journal_mode, foreign_keys, and busy_timeout pragmas are set."""
    engine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            jm = (await conn.execute(text("PRAGMA journal_mode"))).scalar_one()
            fk = (await conn.execute(text("PRAGMA foreign_keys"))).scalar_one()
            bt = (await conn.execute(text("PRAGMA busy_timeout"))).scalar_one()
        assert str(jm).lower() == "wal"
        assert int(fk) == 1
        assert int(bt) == 5000  # noqa: PLR2004
    finally:
        await engine.dispose()


async def test_dispose_engine_clears_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """``dispose_engine()`` resets the module-level cache."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", "sqlite+aiosqlite:///./dispose-test.db")
    await dispose_engine()
    e1 = get_engine()
    await dispose_engine()
    e2 = get_engine()
    try:
        assert e1 is not e2
    finally:
        await dispose_engine()


async def test_pragmas_applied_with_explicit_url(db_url: str) -> None:
    """Pragmas are applied even when passing an explicit URL."""
    engine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            jm = (await conn.execute(text("PRAGMA journal_mode"))).scalar_one()
        assert str(jm).lower() == "wal"
    finally:
        await engine.dispose()
