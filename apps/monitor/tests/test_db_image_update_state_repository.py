"""Tests for ImageUpdateStateRepository."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.db.migrations import run_migrations
from homelab_monitor.kernel.db.repositories.image_update_state_repository import (
    ImageUpdateStateRepository,
    ImageUpdateStateRow,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from tests.conftest import make_engine

_EXPECTED_ROW_COUNT_THREE = 3
_EXPECTED_ROW_COUNT_TWO = 2


@pytest.fixture
async def repo() -> ImageUpdateStateRepository:
    """Fixture providing an ImageUpdateStateRepository with migrated schema."""
    engine = make_engine()
    await run_migrations(engine)
    sqlite_repo = SqliteRepository(engine)
    return ImageUpdateStateRepository(sqlite_repo)


async def test_upsert_inserts_new_row(repo: ImageUpdateStateRepository) -> None:
    """Verify upsert_state_conn inserts a new row."""
    async with repo._repo.transaction() as conn:  # pyright: ignore[reportPrivateUsage]
        await ImageUpdateStateRepository.upsert_state_conn(
            conn,
            container_name="test_container",
            last_image_ref="postgres:16",
            last_local_digest="sha256:local123",
            last_registry_digest="sha256:registry123",
            last_checked_at="2026-05-23T00:00:00Z",
            check_failed_at=None,
            check_error_reason=None,
            update_available=False,
            now="2026-05-23T00:00:00Z",
        )
    result = await repo.get_by_container("test_container")
    assert result is not None
    assert result.container_name == "test_container"
    assert result.last_image_ref == "postgres:16"
    assert result.last_local_digest == "sha256:local123"
    assert result.last_registry_digest == "sha256:registry123"
    assert result.update_available is False


async def test_upsert_updates_existing_row(repo: ImageUpdateStateRepository) -> None:
    """Verify upsert overwrites existing row with same container_name."""
    async with repo._repo.transaction() as conn:  # pyright: ignore[reportPrivateUsage]
        # Insert initial
        await ImageUpdateStateRepository.upsert_state_conn(
            conn,
            container_name="test_container",
            last_image_ref="postgres:16",
            last_local_digest="sha256:local123",
            last_registry_digest="sha256:registry123",
            last_checked_at="2026-05-23T00:00:00Z",
            check_failed_at=None,
            check_error_reason=None,
            update_available=False,
            now="2026-05-23T00:00:00Z",
        )
        # Update
        await ImageUpdateStateRepository.upsert_state_conn(
            conn,
            container_name="test_container",
            last_image_ref="postgres:17",
            last_local_digest="sha256:local456",
            last_registry_digest="sha256:registry456",
            last_checked_at="2026-05-23T01:00:00Z",
            check_failed_at=None,
            check_error_reason=None,
            update_available=True,
            now="2026-05-23T01:00:00Z",
        )
    result = await repo.get_by_container("test_container")
    assert result is not None
    assert result.last_image_ref == "postgres:17"
    assert result.last_local_digest == "sha256:local456"
    assert result.last_registry_digest == "sha256:registry456"
    assert result.update_available is True


async def test_upsert_rejects_invalid_error_reason(
    repo: ImageUpdateStateRepository,
) -> None:
    """Verify upsert_state_conn raises ValueError for invalid error reason."""
    async with repo._repo.transaction() as conn:  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(ValueError, match="invalid check_error_reason"):
            await ImageUpdateStateRepository.upsert_state_conn(
                conn,
                container_name="test_container",
                last_image_ref="postgres:16",
                last_local_digest=None,
                last_registry_digest=None,
                last_checked_at=None,
                check_failed_at="2026-05-23T00:00:00Z",
                check_error_reason="invalid_reason",
                update_available=False,
                now="2026-05-23T00:00:00Z",
            )


async def test_upsert_accepts_all_valid_error_reasons(
    repo: ImageUpdateStateRepository,
) -> None:
    """Verify all 5 valid error reasons are accepted."""
    valid_reasons = [
        "parse_failed",
        "network_error",
        "auth_failed",
        "rate_limited",
        "not_found",
    ]
    for i, reason in enumerate(valid_reasons):
        async with repo._repo.transaction() as conn:  # pyright: ignore[reportPrivateUsage]
            await ImageUpdateStateRepository.upsert_state_conn(
                conn,
                container_name=f"test_container_{i}",
                last_image_ref="postgres:16",
                last_local_digest=None,
                last_registry_digest=None,
                last_checked_at=None,
                check_failed_at="2026-05-23T00:00:00Z",
                check_error_reason=reason,
                update_available=False,
                now="2026-05-23T00:00:00Z",
            )
        result = await repo.get_by_container(f"test_container_{i}")
        assert result is not None
        assert result.check_error_reason == reason


async def test_upsert_accepts_null_error_reason(
    repo: ImageUpdateStateRepository,
) -> None:
    """Verify upsert accepts NULL check_error_reason."""
    async with repo._repo.transaction() as conn:  # pyright: ignore[reportPrivateUsage]
        await ImageUpdateStateRepository.upsert_state_conn(
            conn,
            container_name="test_container",
            last_image_ref="postgres:16",
            last_local_digest="sha256:local123",
            last_registry_digest="sha256:registry123",
            last_checked_at="2026-05-23T00:00:00Z",
            check_failed_at=None,
            check_error_reason=None,
            update_available=False,
            now="2026-05-23T00:00:00Z",
        )
    result = await repo.get_by_container("test_container")
    assert result is not None
    assert result.check_error_reason is None


async def test_get_by_container_returns_row(
    repo: ImageUpdateStateRepository,
) -> None:
    """Verify get_by_container returns the correct row."""
    async with repo._repo.transaction() as conn:  # pyright: ignore[reportPrivateUsage]
        await ImageUpdateStateRepository.upsert_state_conn(
            conn,
            container_name="test_container",
            last_image_ref="postgres:16",
            last_local_digest="sha256:local123",
            last_registry_digest="sha256:registry123",
            last_checked_at="2026-05-23T00:00:00Z",
            check_failed_at=None,
            check_error_reason=None,
            update_available=False,
            now="2026-05-23T00:00:00Z",
        )
    result = await repo.get_by_container("test_container")
    assert result is not None
    assert isinstance(result, ImageUpdateStateRow)
    assert result.container_name == "test_container"


async def test_get_by_container_returns_none_when_missing(
    repo: ImageUpdateStateRepository,
) -> None:
    """Verify get_by_container returns None for nonexistent container."""
    result = await repo.get_by_container("nonexistent")
    assert result is None


async def test_list_all_returns_ordered_rows(
    repo: ImageUpdateStateRepository,
) -> None:
    """Verify list_all returns rows ordered by container_name."""
    containers = ["zebra_container", "apple_container", "middle_container"]
    async with repo._repo.transaction() as conn:  # pyright: ignore[reportPrivateUsage]
        for cn in containers:
            await ImageUpdateStateRepository.upsert_state_conn(
                conn,
                container_name=cn,
                last_image_ref="postgres:16",
                last_local_digest=None,
                last_registry_digest=None,
                last_checked_at=None,
                check_failed_at=None,
                check_error_reason=None,
                update_available=False,
                now="2026-05-23T00:00:00Z",
            )
    results = await repo.list_all()
    assert len(results) == _EXPECTED_ROW_COUNT_THREE
    names = [r.container_name for r in results]
    assert names == ["apple_container", "middle_container", "zebra_container"]


async def test_list_all_returns_empty_when_no_rows(
    repo: ImageUpdateStateRepository,
) -> None:
    """Verify list_all returns empty list when no rows exist."""
    results = await repo.list_all()
    assert results == []


async def test_delete_by_container_removes_matching_rows(
    repo: ImageUpdateStateRepository,
) -> None:
    """Verify delete_by_container_conn removes specified rows."""
    containers = ["container_1", "container_2", "container_3"]
    async with repo._repo.transaction() as conn:  # pyright: ignore[reportPrivateUsage]
        for cn in containers:
            await ImageUpdateStateRepository.upsert_state_conn(
                conn,
                container_name=cn,
                last_image_ref="postgres:16",
                last_local_digest=None,
                last_registry_digest=None,
                last_checked_at=None,
                check_failed_at=None,
                check_error_reason=None,
                update_available=False,
                now="2026-05-23T00:00:00Z",
            )
        # Delete two
        await ImageUpdateStateRepository.delete_by_container_conn(
            conn,
            container_names={"container_1", "container_3"},
        )
    results = await repo.list_all()
    assert len(results) == 1
    assert results[0].container_name == "container_2"


async def test_delete_by_container_returns_count(
    repo: ImageUpdateStateRepository,
) -> None:
    """Verify delete_by_container_conn returns the count of deleted rows."""
    containers = ["container_1", "container_2"]
    async with repo._repo.transaction() as conn:  # pyright: ignore[reportPrivateUsage]
        for cn in containers:
            await ImageUpdateStateRepository.upsert_state_conn(
                conn,
                container_name=cn,
                last_image_ref="postgres:16",
                last_local_digest=None,
                last_registry_digest=None,
                last_checked_at=None,
                check_failed_at=None,
                check_error_reason=None,
                update_available=False,
                now="2026-05-23T00:00:00Z",
            )
        count = await ImageUpdateStateRepository.delete_by_container_conn(
            conn,
            container_names={"container_1", "container_2"},
        )
    assert count == _EXPECTED_ROW_COUNT_TWO


async def test_delete_by_container_no_op_on_empty_set(
    repo: ImageUpdateStateRepository,
) -> None:
    """Verify delete_by_container_conn returns 0 for empty container_names."""
    async with repo._repo.transaction() as conn:  # pyright: ignore[reportPrivateUsage]
        count = await ImageUpdateStateRepository.delete_by_container_conn(
            conn,
            container_names=set(),
        )
    assert count == 0


async def test_update_available_persists_as_boolean(
    repo: ImageUpdateStateRepository,
) -> None:
    """Verify update_available is stored as 0/1 and exposed as bool."""
    async with repo._repo.transaction() as conn:  # pyright: ignore[reportPrivateUsage]
        # Insert with True
        await ImageUpdateStateRepository.upsert_state_conn(
            conn,
            container_name="test_true",
            last_image_ref="postgres:16",
            last_local_digest=None,
            last_registry_digest=None,
            last_checked_at=None,
            check_failed_at=None,
            check_error_reason=None,
            update_available=True,
            now="2026-05-23T00:00:00Z",
        )
        # Insert with False
        await ImageUpdateStateRepository.upsert_state_conn(
            conn,
            container_name="test_false",
            last_image_ref="postgres:16",
            last_local_digest=None,
            last_registry_digest=None,
            last_checked_at=None,
            check_failed_at=None,
            check_error_reason=None,
            update_available=False,
            now="2026-05-23T00:00:00Z",
        )
    result_true = await repo.get_by_container("test_true")
    result_false = await repo.get_by_container("test_false")
    assert result_true is not None
    assert result_true.update_available is True
    assert result_false is not None
    assert result_false.update_available is False


async def test_get_by_container_conn_returns_none_for_missing(
    repo: ImageUpdateStateRepository,
) -> None:
    """get_by_container_conn returns None when container not found (covers 99-105)."""
    async with repo._repo.transaction() as conn:  # pyright: ignore[reportPrivateUsage]
        result = await ImageUpdateStateRepository.get_by_container_conn(
            conn, container_name="nonexistent"
        )
    assert result is None


async def test_get_by_container_conn_returns_row_for_existing(
    repo: ImageUpdateStateRepository,
) -> None:
    """get_by_container_conn returns the row when container exists (covers 99-111 success path)."""
    now = "2026-05-23T00:00:00Z"
    async with repo._repo.transaction() as conn:  # pyright: ignore[reportPrivateUsage]
        await ImageUpdateStateRepository.upsert_state_conn(
            conn,
            container_name="found",
            last_local_digest=None,
            last_registry_digest="sha256:abc",
            last_image_ref="nginx:latest",
            update_available=True,
            last_checked_at=now,
            check_failed_at=None,
            check_error_reason=None,
            now=now,
        )
        result = await ImageUpdateStateRepository.get_by_container_conn(
            conn, container_name="found"
        )
    assert result is not None
    assert result.container_name == "found"
    assert result.last_registry_digest == "sha256:abc"
    assert result.update_available is True
