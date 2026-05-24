"""Tests for DockerBuildHashesRepository (STAGE-003-009)."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.db.repositories.docker_build_hashes_repository import (
    _VALID_ERROR_REASONS,  # pyright: ignore[reportPrivateUsage]
    DockerBuildHashesRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso


def _now() -> str:
    return utc_now_iso()


async def _upsert(  # noqa: PLR0913 -- test-only helper
    repo: SqliteRepository,
    *,
    container_name: str = "myapp",
    compose_service: str = "myapp",
    build_context_path: str = "/srv/compose/myapp",
    last_source_hash: str | None = "abc123",
    last_checked_at: str | None = None,
    check_failed_at: str | None = None,
    check_error_reason: str | None = None,
    update_available: bool = False,
    baseline_source_hash: str | None = None,
    baseline_image_id: str | None = None,
) -> None:
    async with repo.transaction() as conn:
        await DockerBuildHashesRepository.upsert_conn(
            conn,
            container_name=container_name,
            compose_service=compose_service,
            build_context_path=build_context_path,
            last_source_hash=last_source_hash,
            last_checked_at=last_checked_at or _now(),
            check_failed_at=check_failed_at,
            check_error_reason=check_error_reason,
            update_available=update_available,
            baseline_source_hash=baseline_source_hash,
            baseline_image_id=baseline_image_id,
        )


# ---------------------------------------------------------------------------
# upsert + get round trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_and_get_round_trip(repo: SqliteRepository) -> None:
    """upsert_conn + get_by_container_conn returns the inserted row."""
    now = _now()
    async with repo.transaction() as conn:
        await DockerBuildHashesRepository.upsert_conn(
            conn,
            container_name="myapp",
            compose_service="myapp",
            build_context_path="/srv/compose/myapp",
            last_source_hash="deadbeef",
            last_checked_at=now,
            check_failed_at=None,
            check_error_reason=None,
            update_available=False,
            baseline_source_hash="prior_hash_abc",
            baseline_image_id="sha256:imageabc",
        )
        row = await DockerBuildHashesRepository.get_by_container_conn(conn, container_name="myapp")

    assert row is not None
    assert row.container_name == "myapp"
    assert row.compose_service == "myapp"
    assert row.build_context_path == "/srv/compose/myapp"
    assert row.last_source_hash == "deadbeef"
    assert row.last_checked_at == now
    assert row.check_failed_at is None
    assert row.check_error_reason is None
    assert row.update_available is False
    assert row.baseline_source_hash == "prior_hash_abc"
    assert row.baseline_image_id == "sha256:imageabc"


@pytest.mark.asyncio
async def test_upsert_overwrites_existing_row(repo: SqliteRepository) -> None:
    """Second upsert with same container_name updates all fields."""
    await _upsert(repo, container_name="app", last_source_hash="hash1", update_available=False)
    await _upsert(repo, container_name="app", last_source_hash="hash2", update_available=True)

    instance_repo = DockerBuildHashesRepository(repo)
    row = await instance_repo.get_by_container("app")
    assert row is not None
    assert row.last_source_hash == "hash2"
    assert row.update_available is True


# ---------------------------------------------------------------------------
# list_all ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_returns_rows_sorted_by_container_name(repo: SqliteRepository) -> None:
    """list_all_conn returns rows in container_name alphabetical order."""
    await _upsert(repo, container_name="zebra")
    await _upsert(repo, container_name="apple")
    await _upsert(repo, container_name="middle")

    async with repo.transaction() as conn:
        rows = await DockerBuildHashesRepository.list_all_conn(conn)

    names = [r.container_name for r in rows]
    assert names == sorted(names)
    assert set(names) == {"zebra", "apple", "middle"}


# ---------------------------------------------------------------------------
# delete_by_container
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_by_container_removes_rows(repo: SqliteRepository) -> None:
    """delete_by_container_conn removes the specified rows."""
    await _upsert(repo, container_name="app1")
    await _upsert(repo, container_name="app2")
    await _upsert(repo, container_name="app3")

    async with repo.transaction() as conn:
        deleted = await DockerBuildHashesRepository.delete_by_container_conn(
            conn, container_names={"app1", "app3"}
        )
        rows = await DockerBuildHashesRepository.list_all_conn(conn)

    assert deleted == 2  # noqa: PLR2004 -- test-only literal
    assert [r.container_name for r in rows] == ["app2"]


@pytest.mark.asyncio
async def test_delete_by_container_empty_set_returns_zero(repo: SqliteRepository) -> None:
    """delete_by_container_conn with empty set returns 0 without error."""
    await _upsert(repo, container_name="app1")

    async with repo.transaction() as conn:
        deleted = await DockerBuildHashesRepository.delete_by_container_conn(
            conn, container_names=set()
        )
        rows = await DockerBuildHashesRepository.list_all_conn(conn)

    assert deleted == 0
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# check_error_reason validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_check_error_reason_raises_value_error(repo: SqliteRepository) -> None:
    """upsert_conn raises ValueError for invalid check_error_reason."""
    async with repo.transaction() as conn:
        with pytest.raises(ValueError, match="invalid check_error_reason"):
            await DockerBuildHashesRepository.upsert_conn(
                conn,
                container_name="app",
                compose_service="app",
                build_context_path="/srv/app",
                last_source_hash=None,
                last_checked_at=_now(),
                check_failed_at=None,
                check_error_reason="totally_invalid_reason",
                update_available=False,
                baseline_source_hash=None,
                baseline_image_id=None,
            )


@pytest.mark.asyncio
async def test_all_valid_error_reasons_accepted(repo: SqliteRepository) -> None:
    """Each valid check_error_reason value is accepted without error."""
    for i, reason in enumerate(_VALID_ERROR_REASONS):
        await _upsert(
            repo,
            container_name=f"app_{i}",
            check_error_reason=reason,
        )
    instance_repo = DockerBuildHashesRepository(repo)
    rows = await instance_repo.list_all()
    persisted_reasons = {r.check_error_reason for r in rows}
    assert persisted_reasons == _VALID_ERROR_REASONS


@pytest.mark.asyncio
async def test_null_check_error_reason_accepted(repo: SqliteRepository) -> None:
    """check_error_reason=None is accepted and round-trips correctly."""
    await _upsert(repo, container_name="ok", check_error_reason=None)
    instance_repo = DockerBuildHashesRepository(repo)
    row = await instance_repo.get_by_container("ok")
    assert row is not None
    assert row.check_error_reason is None


# ---------------------------------------------------------------------------
# Nullable fields round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nullable_fields_round_trip_none(repo: SqliteRepository) -> None:
    """All nullable fields survive None round-trip."""
    async with repo.transaction() as conn:
        await DockerBuildHashesRepository.upsert_conn(
            conn,
            container_name="nullapp",
            compose_service="nullsvc",
            build_context_path="/srv/null",
            last_source_hash=None,
            last_checked_at=None,
            check_failed_at=None,
            check_error_reason=None,
            update_available=False,
            baseline_source_hash=None,
            baseline_image_id=None,
        )
        row = await DockerBuildHashesRepository.get_by_container_conn(
            conn, container_name="nullapp"
        )
    assert row is not None
    assert row.last_source_hash is None
    assert row.last_checked_at is None
    assert row.check_failed_at is None
    assert row.check_error_reason is None
    assert row.baseline_source_hash is None
    assert row.baseline_image_id is None


@pytest.mark.asyncio
async def test_upsert_with_null_baselines_stores_null(repo: SqliteRepository) -> None:
    """Both baseline fields explicitly None round-trip as None."""
    async with repo.transaction() as conn:
        await DockerBuildHashesRepository.upsert_conn(
            conn,
            container_name="nullbase",
            compose_service="nullsvc",
            build_context_path="/srv/null",
            last_source_hash="somehash",
            last_checked_at=_now(),
            check_failed_at=None,
            check_error_reason=None,
            update_available=False,
            baseline_source_hash=None,
            baseline_image_id=None,
        )
        row = await DockerBuildHashesRepository.get_by_container_conn(
            conn, container_name="nullbase"
        )
    assert row is not None
    assert row.baseline_source_hash is None
    assert row.baseline_image_id is None


# ---------------------------------------------------------------------------
# update_available boolean storage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_available_true_round_trips(repo: SqliteRepository) -> None:
    """update_available=True is stored as integer 1 and read back as True."""
    await _upsert(repo, container_name="has_update", update_available=True)
    instance_repo = DockerBuildHashesRepository(repo)
    row = await instance_repo.get_by_container("has_update")
    assert row is not None
    assert row.update_available is True


@pytest.mark.asyncio
async def test_update_available_false_round_trips(repo: SqliteRepository) -> None:
    """update_available=False is stored as integer 0 and read back as False."""
    await _upsert(repo, container_name="no_update", update_available=False)
    instance_repo = DockerBuildHashesRepository(repo)
    row = await instance_repo.get_by_container("no_update")
    assert row is not None
    assert row.update_available is False


# ---------------------------------------------------------------------------
# Instance reads mirror static conn helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_instance_get_by_container_returns_none_for_missing(repo: SqliteRepository) -> None:
    """get_by_container() returns None when no row exists."""
    instance_repo = DockerBuildHashesRepository(repo)
    row = await instance_repo.get_by_container("nonexistent")
    assert row is None


@pytest.mark.asyncio
async def test_instance_list_all_empty_when_no_rows(repo: SqliteRepository) -> None:
    """list_all() returns empty list when table is empty."""
    instance_repo = DockerBuildHashesRepository(repo)
    rows = await instance_repo.list_all()
    assert rows == []


@pytest.mark.asyncio
async def test_instance_list_all_matches_static_list_all_conn(repo: SqliteRepository) -> None:
    """instance list_all() returns same data as static list_all_conn()."""
    await _upsert(repo, container_name="c1", last_source_hash="h1")
    await _upsert(repo, container_name="c2", last_source_hash="h2")

    instance_repo = DockerBuildHashesRepository(repo)
    instance_rows = await instance_repo.list_all()

    async with repo.transaction() as conn:
        static_rows = await DockerBuildHashesRepository.list_all_conn(conn)

    assert [(r.container_name, r.last_source_hash) for r in instance_rows] == [
        (r.container_name, r.last_source_hash) for r in static_rows
    ]


@pytest.mark.asyncio
async def test_get_by_container_conn_returns_none_for_missing(repo: SqliteRepository) -> None:
    """get_by_container_conn returns None when no row exists for that container name."""
    async with repo.transaction() as conn:
        row = await DockerBuildHashesRepository.get_by_container_conn(
            conn, container_name="nonexistent-container"
        )
    assert row is None
