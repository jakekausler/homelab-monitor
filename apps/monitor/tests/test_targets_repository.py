"""Tests for kernel.db.repositories.targets_repository.TargetsRepository."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.db.repositories.targets_repository import (
    DockerContainerListRow,
    TargetsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository

CPU_PCT_A = 10.0
CPU_PCT_B = 20.0
CPU_PCT_C = 10.5
CPU_PCT_D = 50.0
MEM_MIB_A = 128.0
MEM_MIB_B = 256.0
RESTART_COUNT_5 = 5
LIST_COUNT_2 = 2
LIST_COUNT_4 = 4
STAT_CNT_3 = 3


def _now_iso() -> str:
    """Return current UTC time as ISO string."""
    return datetime.now(UTC).isoformat()


@pytest.mark.asyncio
async def test_upsert_docker_container_conn_inserts_new_row(
    repo: SqliteRepository,
) -> None:
    """Verify upsert inserts both targets and targets_docker rows."""
    now = _now_iso()
    target_id = "container-test-1"

    async with repo.transaction() as conn:
        await TargetsRepository.upsert_docker_container_conn(
            conn,
            target_id=target_id,
            name="test-container",
            status="running",
            image="alpine:latest",
            restart_count=0,
            exit_code=0,
            healthcheck="healthy",
            network_mode="bridge",
            labels={"app": "test"},
            now=now,
            cpu_pct=10.5,
            mem_mib=128.0,
        )

    # Verify targets row
    targets_row = await repo.fetch_one(
        text("SELECT id, name, kind, status, source FROM targets WHERE id = :i"),
        {"i": target_id},
    )
    assert targets_row is not None
    assert targets_row.name == "test-container"
    assert targets_row.kind == "docker_container"
    assert targets_row.status == "running"
    assert targets_row.source == "docker_socket"

    # Verify targets_docker row
    docker_row = await repo.fetch_one(
        text(
            "SELECT target_id, image, restart_count, exit_code, healthcheck, "
            "  network_mode, cpu_pct_cached, mem_mib_cached "
            "FROM targets_docker WHERE target_id = :i"
        ),
        {"i": target_id},
    )
    assert docker_row is not None
    assert docker_row.image == "alpine:latest"
    assert docker_row.restart_count == 0
    assert docker_row.exit_code == 0
    assert docker_row.healthcheck == "healthy"
    assert docker_row.network_mode == "bridge"
    assert docker_row.cpu_pct_cached == CPU_PCT_C
    assert docker_row.mem_mib_cached == MEM_MIB_A


@pytest.mark.asyncio
async def test_upsert_docker_container_conn_updates_existing_row(
    repo: SqliteRepository,
) -> None:
    """Verify upsert updates existing rows idempotently."""
    now = _now_iso()
    target_id = "container-test-2"

    # First insert
    async with repo.transaction() as conn:
        await TargetsRepository.upsert_docker_container_conn(
            conn,
            target_id=target_id,
            name="test-container",
            status="running",
            image="alpine:latest",
            restart_count=0,
            exit_code=0,
            healthcheck="healthy",
            network_mode="bridge",
            labels={"app": "test"},
            now=now,
            cpu_pct=10.5,
            mem_mib=128.0,
        )

    # Update same container
    now2 = _now_iso()
    async with repo.transaction() as conn:
        await TargetsRepository.upsert_docker_container_conn(
            conn,
            target_id=target_id,
            name="test-container-renamed",
            status="exited",
            image="alpine:latest",
            restart_count=5,
            exit_code=1,
            healthcheck=None,
            network_mode="bridge",
            labels={"app": "test", "version": "2"},
            now=now2,
            cpu_pct=0.0,
            mem_mib=0.0,
        )

    # Verify targets row updated
    targets_row = await repo.fetch_one(
        text("SELECT name, status FROM targets WHERE id = :i"),
        {"i": target_id},
    )
    assert targets_row is not None
    assert targets_row.name == "test-container-renamed"
    assert targets_row.status == "exited"

    # Verify targets_docker row updated
    docker_row = await repo.fetch_one(
        text(
            "SELECT restart_count, exit_code, healthcheck, cpu_pct_cached, mem_mib_cached "
            "FROM targets_docker WHERE target_id = :i"
        ),
        {"i": target_id},
    )
    assert docker_row is not None
    assert docker_row.restart_count == RESTART_COUNT_5
    assert docker_row.exit_code == 1
    assert docker_row.healthcheck is None
    assert docker_row.cpu_pct_cached == 0.0
    assert docker_row.mem_mib_cached == 0.0


@pytest.mark.asyncio
async def test_upsert_preserves_cache_when_null(repo: SqliteRepository) -> None:
    """When cpu_pct or mem_mib are None, preserve existing cached values."""
    now = _now_iso()
    target_id = "container-cache-test"

    # First insert with cache values
    async with repo.transaction() as conn:
        await TargetsRepository.upsert_docker_container_conn(
            conn,
            target_id=target_id,
            name="cached-container",
            status="running",
            image="alpine:latest",
            restart_count=0,
            exit_code=0,
            healthcheck=None,
            network_mode="bridge",
            labels={},
            now=now,
            cpu_pct=CPU_PCT_D,
            mem_mib=MEM_MIB_B,
        )

    # Update without cache values (None)
    now2 = _now_iso()
    async with repo.transaction() as conn:
        await TargetsRepository.upsert_docker_container_conn(
            conn,
            target_id=target_id,
            name="cached-container",
            status="running",
            image="alpine:latest",
            restart_count=0,
            exit_code=0,
            healthcheck=None,
            network_mode="bridge",
            labels={},
            now=now2,
            cpu_pct=None,
            mem_mib=None,
        )

    # Verify cache was preserved
    docker_row = await repo.fetch_one(
        text("SELECT cpu_pct_cached, mem_mib_cached FROM targets_docker WHERE target_id = :i"),
        {"i": target_id},
    )
    assert docker_row is not None
    assert docker_row.cpu_pct_cached == CPU_PCT_D
    assert docker_row.mem_mib_cached == MEM_MIB_B


@pytest.mark.asyncio
async def test_list_docker_containers_returns_joined_rows(repo: SqliteRepository) -> None:
    """Verify list_docker_containers returns LEFT JOINed data with correct types."""
    targets_repo = TargetsRepository(repo)
    now = _now_iso()

    # Insert two containers
    for i in range(2):
        async with repo.transaction() as conn:
            await TargetsRepository.upsert_docker_container_conn(
                conn,
                target_id=f"ctr-{i}",
                name=f"container-{i}",
                status="running",
                image=f"image-{i}:latest",
                restart_count=i,
                exit_code=0,
                healthcheck="healthy",
                network_mode="bridge",
                labels={"index": str(i)},
                now=now,
                cpu_pct=10.0 * (i + 1),
                mem_mib=100.0 * (i + 1),
            )

    rows = await targets_repo.list_docker_containers()

    assert len(rows) == LIST_COUNT_2
    assert all(isinstance(row, DockerContainerListRow) for row in rows)
    assert rows[0].name == "container-0"
    assert rows[1].name == "container-1"
    assert rows[0].restart_count == 0
    assert rows[1].restart_count == 1
    assert rows[0].cpu_pct_cached == CPU_PCT_A
    assert rows[1].cpu_pct_cached == CPU_PCT_B


@pytest.mark.asyncio
async def test_list_docker_containers_excludes_hidden_by_default(
    repo: SqliteRepository,
) -> None:
    """Verify list_docker_containers excludes rows with hidden_at IS NOT NULL."""
    targets_repo = TargetsRepository(repo)
    now = _now_iso()

    # Insert one visible container
    async with repo.transaction() as conn:
        await TargetsRepository.upsert_docker_container_conn(
            conn,
            target_id="visible",
            name="visible-container",
            status="running",
            image="alpine:latest",
            restart_count=0,
            exit_code=0,
            healthcheck=None,
            network_mode="bridge",
            labels={},
            now=now,
            cpu_pct=None,
            mem_mib=None,
        )

    # Manually insert a hidden container
    await repo.execute(
        text(
            "INSERT INTO targets (id, name, kind, status, first_seen, last_seen, "
            "  hidden_at, labels, source, created_at) "
            "VALUES (:id, :name, 'docker_container', 'running', :now, :now, :now, '{}', "
            "  'docker_socket', :now)"
        ),
        {"id": "hidden", "name": "hidden-container", "now": now},
    )

    rows = await targets_repo.list_docker_containers(include_hidden=False)
    assert len(rows) == 1
    assert rows[0].id == "visible"

    rows_with_hidden = await targets_repo.list_docker_containers(include_hidden=True)
    assert len(rows_with_hidden) == LIST_COUNT_2
    hidden_row = next(r for r in rows_with_hidden if r.id == "hidden")
    assert hidden_row.hidden_at is not None


@pytest.mark.asyncio
async def test_list_docker_containers_ordered_by_name(repo: SqliteRepository) -> None:
    """Verify list_docker_containers returns rows ordered by name ASC."""
    targets_repo = TargetsRepository(repo)
    now = _now_iso()

    names = ["zebra", "alpha", "charlie", "beta"]
    for name in names:
        async with repo.transaction() as conn:
            await TargetsRepository.upsert_docker_container_conn(
                conn,
                target_id=name,
                name=name,
                status="running",
                image="alpine:latest",
                restart_count=0,
                exit_code=0,
                healthcheck=None,
                network_mode="bridge",
                labels={},
                now=now,
                cpu_pct=None,
                mem_mib=None,
            )

    rows = await targets_repo.list_docker_containers()
    assert len(rows) == LIST_COUNT_4
    assert [r.name for r in rows] == ["alpha", "beta", "charlie", "zebra"]


@pytest.mark.asyncio
async def test_list_docker_containers_parses_labels_json(repo: SqliteRepository) -> None:
    """Verify labels are correctly deserialized from JSON."""
    targets_repo = TargetsRepository(repo)
    now = _now_iso()

    labels = {"app": "web", "version": "1.0", "env": "prod"}
    async with repo.transaction() as conn:
        await TargetsRepository.upsert_docker_container_conn(
            conn,
            target_id="labeled-ctr",
            name="labeled-container",
            status="running",
            image="alpine:latest",
            restart_count=0,
            exit_code=0,
            healthcheck=None,
            network_mode="bridge",
            labels=labels,
            now=now,
            cpu_pct=None,
            mem_mib=None,
        )

    rows = await targets_repo.list_docker_containers()
    assert len(rows) == 1
    assert rows[0].labels == labels


@pytest.mark.asyncio
async def test_mark_missing_except_conn_marks_unseen_containers(
    repo: SqliteRepository,
) -> None:
    """Verify mark_missing_except_conn updates containers not in seen_ids."""
    now = _now_iso()

    # Insert three containers
    for i in range(3):
        async with repo.transaction() as conn:
            await TargetsRepository.upsert_docker_container_conn(
                conn,
                target_id=f"ctr-{i}",
                name=f"container-{i}",
                status="running",
                image="alpine:latest",
                restart_count=0,
                exit_code=0,
                healthcheck=None,
                network_mode="bridge",
                labels={},
                now=now,
                cpu_pct=None,
                mem_mib=None,
            )

    # Mark containers 0 and 1 as seen; 2 should be missing
    now2 = _now_iso()
    async with repo.transaction() as conn:
        await TargetsRepository.mark_missing_except_conn(
            conn,
            seen_ids={"ctr-0", "ctr-1"},
            now=now2,
        )

    # Verify ctr-2 is missing
    ctr2 = await repo.fetch_one(text("SELECT status, last_seen FROM targets WHERE id = 'ctr-2'"))
    assert ctr2 is not None
    assert ctr2.status == "missing"
    assert ctr2.last_seen == now2

    # Verify ctr-0 and ctr-1 still running
    for i in range(2):
        ctr = await repo.fetch_one(
            text("SELECT status FROM targets WHERE id = :i"),
            {"i": f"ctr-{i}"},
        )
        assert ctr is not None
        assert ctr.status == "running"


@pytest.mark.asyncio
async def test_mark_missing_except_conn_idempotent(repo: SqliteRepository) -> None:
    """Verify mark_missing_except_conn does not re-mark already-missing containers."""
    now = _now_iso()

    # Insert container
    async with repo.transaction() as conn:
        await TargetsRepository.upsert_docker_container_conn(
            conn,
            target_id="ctr-test",
            name="test-container",
            status="running",
            image="alpine:latest",
            restart_count=0,
            exit_code=0,
            healthcheck=None,
            network_mode="bridge",
            labels={},
            now=now,
            cpu_pct=None,
            mem_mib=None,
        )

    # Mark as missing
    now2 = _now_iso()
    async with repo.transaction() as conn:
        await TargetsRepository.mark_missing_except_conn(
            conn,
            seen_ids=set(),  # Empty seen_ids marks all as missing
            now=now2,
        )

    # Verify status is missing
    row1 = await repo.fetch_one(text("SELECT status, last_seen FROM targets WHERE id = 'ctr-test'"))
    assert row1 is not None
    assert row1.status == "missing"
    assert row1.last_seen == now2

    # Mark as missing again with different timestamp
    now3 = _now_iso()
    async with repo.transaction() as conn:
        await TargetsRepository.mark_missing_except_conn(
            conn,
            seen_ids=set(),
            now=now3,
        )

    # Verify status is still missing and last_seen was NOT updated (idempotent)
    row2 = await repo.fetch_one(text("SELECT status, last_seen FROM targets WHERE id = 'ctr-test'"))
    assert row2 is not None
    assert row2.status == "missing"
    # The second call should not update the row because status != 'missing' is false
    assert row2.last_seen == now2


@pytest.mark.asyncio
async def test_mark_missing_except_conn_empty_seen_ids(repo: SqliteRepository) -> None:
    """Verify empty seen_ids marks ALL containers as missing."""
    now = _now_iso()

    # Insert three containers
    for i in range(3):
        async with repo.transaction() as conn:
            await TargetsRepository.upsert_docker_container_conn(
                conn,
                target_id=f"ctr-{i}",
                name=f"container-{i}",
                status="running",
                image="alpine:latest",
                restart_count=0,
                exit_code=0,
                healthcheck=None,
                network_mode="bridge",
                labels={},
                now=now,
                cpu_pct=None,
                mem_mib=None,
            )

    # Mark all as missing
    now2 = _now_iso()
    async with repo.transaction() as conn:
        await TargetsRepository.mark_missing_except_conn(
            conn,
            seen_ids=set(),
            now=now2,
        )

    # Verify all are missing
    rows = await repo.fetch_all(
        text(
            "SELECT COUNT(*) as cnt FROM targets WHERE kind='docker_container' AND status='missing'"
        )
    )
    assert rows[0].cnt == STAT_CNT_3


@pytest.mark.asyncio
async def test_cascade_delete_targets_removes_docker_sidecar(
    repo: SqliteRepository,
) -> None:
    """Verify FK CASCADE: deleting targets row removes targets_docker row."""
    now = _now_iso()
    target_id = "ctr-cascade-test"

    # Insert container
    async with repo.transaction() as conn:
        await TargetsRepository.upsert_docker_container_conn(
            conn,
            target_id=target_id,
            name="cascade-container",
            status="running",
            image="alpine:latest",
            restart_count=0,
            exit_code=0,
            healthcheck=None,
            network_mode="bridge",
            labels={},
            now=now,
            cpu_pct=None,
            mem_mib=None,
        )

    # Verify both rows exist
    targets_row = await repo.fetch_one(
        text("SELECT id FROM targets WHERE id = :i"),
        {"i": target_id},
    )
    docker_row = await repo.fetch_one(
        text("SELECT target_id FROM targets_docker WHERE target_id = :i"),
        {"i": target_id},
    )
    assert targets_row is not None
    assert docker_row is not None

    # Delete targets row
    await repo.execute(
        text("DELETE FROM targets WHERE id = :i"),
        {"i": target_id},
    )

    # Verify targets_docker row also deleted
    targets_row_after = await repo.fetch_one(
        text("SELECT id FROM targets WHERE id = :i"),
        {"i": target_id},
    )
    docker_row_after = await repo.fetch_one(
        text("SELECT target_id FROM targets_docker WHERE target_id = :i"),
        {"i": target_id},
    )
    assert targets_row_after is None
    assert docker_row_after is None


@pytest.mark.asyncio
async def test_list_docker_containers_with_null_sidecar_fields(
    repo: SqliteRepository,
) -> None:
    """Verify list_docker_containers handles LEFT JOIN with NULL sidecar fields."""
    targets_repo = TargetsRepository(repo)
    now = _now_iso()

    # Insert targets row manually without sidecar (simulating orphaned row)
    await repo.execute(
        text(
            "INSERT INTO targets (id, name, kind, status, first_seen, last_seen, "
            "  labels, source, created_at) "
            "VALUES (:id, :name, 'docker_container', 'running', :now, :now, '{}', "
            "  'docker_socket', :now)"
        ),
        {"id": "orphan-ctr", "name": "orphan-container", "now": now},
    )

    rows = await targets_repo.list_docker_containers()
    assert len(rows) == 1
    row = rows[0]
    assert row.id == "orphan-ctr"
    assert row.name == "orphan-container"
    # All sidecar fields should be None
    assert row.image is None
    assert row.restart_count is None
    assert row.exit_code is None
    assert row.healthcheck is None
    assert row.network_mode is None
    assert row.cpu_pct_cached is None
    assert row.mem_mib_cached is None
