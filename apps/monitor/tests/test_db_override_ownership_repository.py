"""Unit tests for OverrideOwnershipRepository."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.db.repositories.override_ownership_repository import (
    OverrideOwnershipRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso


@pytest.mark.asyncio
async def test_list_empty_returns_empty_set(repo: SqliteRepository) -> None:
    """Fresh DB, list_owned() returns empty set."""
    ownership_repo = OverrideOwnershipRepository(repo)
    owned = await ownership_repo.list_owned()
    assert owned == set()


@pytest.mark.asyncio
async def test_set_owned_inserts_new_set(repo: SqliteRepository) -> None:
    """Call set_owned with {"a", "b"}, then list_owned returns {"a", "b"}."""
    ownership_repo = OverrideOwnershipRepository(repo)
    now = utc_now_iso()
    async with repo.transaction() as conn:
        await OverrideOwnershipRepository.set_owned_conn(conn, container_names={"a", "b"}, now=now)
    owned = await ownership_repo.list_owned()
    assert owned == {"a", "b"}


@pytest.mark.asyncio
async def test_set_owned_replaces_existing_set(repo: SqliteRepository) -> None:
    """Seed {"a", "b"}, call with {"b", "c"}, result is {"b", "c"}."""
    ownership_repo = OverrideOwnershipRepository(repo)
    now = utc_now_iso()
    async with repo.transaction() as conn:
        await OverrideOwnershipRepository.set_owned_conn(conn, container_names={"a", "b"}, now=now)
    # Second call
    async with repo.transaction() as conn:
        await OverrideOwnershipRepository.set_owned_conn(conn, container_names={"b", "c"}, now=now)
    owned = await ownership_repo.list_owned()
    assert owned == {"b", "c"}


@pytest.mark.asyncio
async def test_set_owned_empty_clears_table(repo: SqliteRepository) -> None:
    """Seed {"a"}, call with empty set(), result is empty set."""
    ownership_repo = OverrideOwnershipRepository(repo)
    now = utc_now_iso()
    async with repo.transaction() as conn:
        await OverrideOwnershipRepository.set_owned_conn(conn, container_names={"a"}, now=now)
    # Clear
    async with repo.transaction() as conn:
        await OverrideOwnershipRepository.set_owned_conn(conn, container_names=set(), now=now)
    owned = await ownership_repo.list_owned()
    assert owned == set()


@pytest.mark.asyncio
async def test_set_owned_preserves_claimed_at_on_conflict(
    repo: SqliteRepository,
) -> None:
    """Call twice with {"a"} using different now values; claimed_at doesn't change."""
    now1 = "2026-05-22T10:00:00Z"
    now2 = "2026-05-22T10:05:00Z"

    async with repo.transaction() as conn:
        await OverrideOwnershipRepository.set_owned_conn(conn, container_names={"a"}, now=now1)

    # Second call with different timestamp
    async with repo.transaction() as conn:
        await OverrideOwnershipRepository.set_owned_conn(conn, container_names={"a"}, now=now2)

    # Verify claimed_at was NOT updated
    rows = await repo.fetch_all(
        text("SELECT container_name, claimed_at FROM docker_override_ownership")
    )
    assert len(rows) == 1
    assert rows[0].container_name == "a"
    assert rows[0].claimed_at == now1  # should NOT be now2


@pytest.mark.asyncio
async def test_list_owned_conn_inside_transaction(repo: SqliteRepository) -> None:
    """Open transaction, call set_owned_conn then list_owned_conn; both see same state."""
    now = utc_now_iso()
    async with repo.transaction() as conn:
        await OverrideOwnershipRepository.set_owned_conn(conn, container_names={"x", "y"}, now=now)
        owned = await OverrideOwnershipRepository.list_owned_conn(conn)
        assert owned == {"x", "y"}


@pytest.mark.asyncio
async def test_set_owned_conn_with_empty_set_only_deletes(repo: SqliteRepository) -> None:
    """Empty container_names triggers the early DELETE-only path (covers ownership_repo:59)."""
    now = utc_now_iso()
    async with repo.transaction() as conn:
        await OverrideOwnershipRepository.set_owned_conn(conn, container_names={"foo"}, now=now)
    async with repo.transaction() as conn:
        await OverrideOwnershipRepository.set_owned_conn(conn, container_names=set(), now=now)
        owned = await OverrideOwnershipRepository.list_owned_conn(conn)
    assert owned == set()
