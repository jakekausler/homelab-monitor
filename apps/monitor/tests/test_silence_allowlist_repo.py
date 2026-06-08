"""Tests for SilenceAllowlistRepository (STAGE-004-038)."""

from __future__ import annotations

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.silence_allowlist_repo import (
    SilenceAllowlistEntry,
    SilenceAllowlistRepository,
)


async def test_create_returns_entry_with_id_and_timestamp(repo: SqliteRepository) -> None:
    r = SilenceAllowlistRepository(repo)
    e = await r.create(
        template_hash="h1",
        service_key="svc1",
        schedule_kind="always",
        schedule_value="",
        reason="known quiet",
        expires_at=None,
    )
    assert e.id > 0
    assert e.template_hash == "h1"
    assert e.service_key == "svc1"
    assert e.schedule_kind == "always"
    assert e.reason == "known quiet"
    assert e.expires_at is None
    assert "T" in e.created_at
    assert "+00:00" in e.created_at


async def test_create_per_service_null_hash(repo: SqliteRepository) -> None:
    r = SilenceAllowlistRepository(repo)
    e = await r.create(
        template_hash=None,
        service_key="svc1",
        schedule_kind="cron",
        schedule_value="0 * * * *",
        reason="hourly job",
        expires_at="2026-12-31T00:00:00+00:00",
    )
    assert e.template_hash is None
    assert e.expires_at == "2026-12-31T00:00:00+00:00"
    fetched = await r.get(e.id)
    assert fetched is not None
    assert isinstance(fetched, SilenceAllowlistEntry)
    assert fetched.template_hash is None


async def test_get_missing_returns_none(repo: SqliteRepository) -> None:
    r = SilenceAllowlistRepository(repo)
    assert await r.get(9999) is None


async def test_list_all_orders_newest_first(repo: SqliteRepository) -> None:
    r = SilenceAllowlistRepository(repo)
    e1 = await r.create(
        template_hash="h1",
        service_key="s",
        schedule_kind="always",
        schedule_value="",
        reason="a",
        expires_at=None,
    )
    e2 = await r.create(
        template_hash="h2",
        service_key="s",
        schedule_kind="always",
        schedule_value="",
        reason="b",
        expires_at=None,
    )
    rows = await r.list_all()
    assert len(rows) == 2  # noqa: PLR2004
    assert rows[0].id == e2.id
    assert rows[1].id == e1.id


async def test_list_all_empty(repo: SqliteRepository) -> None:
    r = SilenceAllowlistRepository(repo)
    assert await r.list_all() == []


async def test_delete_hit_then_gone(repo: SqliteRepository) -> None:
    r = SilenceAllowlistRepository(repo)
    e = await r.create(
        template_hash="h1",
        service_key="s",
        schedule_kind="always",
        schedule_value="",
        reason="x",
        expires_at=None,
    )
    assert await r.delete(e.id) is True
    assert await r.get(e.id) is None


async def test_delete_miss_returns_false(repo: SqliteRepository) -> None:
    r = SilenceAllowlistRepository(repo)
    assert await r.delete(9999) is False


__all__: list[str] = []
