"""Tests for ``SqliteRepository`` against a migrated DB."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso


async def test_engine_property_returns_underlying_engine(
    db_engine: AsyncEngine, repo: SqliteRepository
) -> None:
    """``repo.engine`` returns the same engine passed to the constructor."""
    assert repo.engine is db_engine


async def test_execute_and_fetch_one(repo: SqliteRepository) -> None:
    """Insert a row via execute() and fetch it back via fetch_one()."""
    target_id = uuid7()
    await repo.execute(
        text("INSERT INTO targets (id, name, created_at) VALUES (:id, :name, :ts)"),
        {"id": target_id, "name": "router", "ts": utc_now_iso()},
    )
    row = await repo.fetch_one(
        text("SELECT id, name FROM targets WHERE id = :id"), {"id": target_id}
    )
    assert row is not None
    assert row.id == target_id
    assert row.name == "router"


async def test_fetch_one_returns_none_when_empty(repo: SqliteRepository) -> None:
    """Empty result set yields ``None``."""
    row = await repo.fetch_one(
        text("SELECT id FROM targets WHERE id = :id"), {"id": "does-not-exist"}
    )
    assert row is None


async def test_fetch_all_returns_list(repo: SqliteRepository) -> None:
    """``fetch_all`` returns a list of rows in insertion order."""
    ts = utc_now_iso()
    for name in ("a", "b", "c"):
        await repo.execute(
            text("INSERT INTO targets (id, name, created_at) VALUES (:id, :name, :ts)"),
            {"id": uuid7(), "name": name, "ts": ts},
        )
    rows = await repo.fetch_all(text("SELECT name FROM targets ORDER BY name"))
    assert [r.name for r in rows] == ["a", "b", "c"]


async def test_transaction_commits_on_success(repo: SqliteRepository) -> None:
    """Statements inside a successful transaction are persisted."""
    target_id = uuid7()
    async with repo.transaction() as conn:
        await conn.execute(
            text("INSERT INTO targets (id, name, created_at) VALUES (:id, :name, :ts)"),
            {"id": target_id, "name": "tx-ok", "ts": utc_now_iso()},
        )
    row = await repo.fetch_one(text("SELECT name FROM targets WHERE id = :id"), {"id": target_id})
    assert row is not None
    assert row.name == "tx-ok"


async def test_transaction_rolls_back_on_exception(repo: SqliteRepository) -> None:
    """An exception inside the transaction discards its writes."""
    target_id = uuid7()

    class Boom(RuntimeError):
        pass

    try:
        async with repo.transaction() as conn:
            await conn.execute(
                text("INSERT INTO targets (id, name, created_at) VALUES (:id, :name, :ts)"),
                {"id": target_id, "name": "tx-rollback", "ts": utc_now_iso()},
            )
            raise Boom
    except Boom:
        pass

    row = await repo.fetch_one(text("SELECT id FROM targets WHERE id = :id"), {"id": target_id})
    assert row is None
