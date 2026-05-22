"""Tests for ProbeTargetsRepository: probe_targets CRUD.

Tests cover insert, upsert, updates, hidden rows, unique constraints, and queries.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.db.repositories.probe_targets_repository import (
    ProbeTargetsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

_DEFAULT_INTERVAL = 30
_DEFAULT_TIMEOUT = 5
_ALTERNATE_INTERVAL = 60
_ALTERNATE_TIMEOUT = 10
_PROBE_COUNT_THREE = 3
_PROBE_COUNT_TWO = 2


@pytest.mark.asyncio
async def test_upsert_new_creates_row(repo: SqliteRepository) -> None:
    """Upsert a new probe → returns id; row is visible."""
    probe_repo = ProbeTargetsRepository(repo)
    now = utc_now_iso()

    async with repo.transaction() as conn:
        probe_id = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="my-container",
            kind="http",
            name="api",
            target_value="http://localhost:8080/healthz",
            config_source="label",
            enabled=True,
            interval_seconds=30,
            timeout_seconds=5,
            now=now,
        )

    assert probe_id  # Should be a non-empty UUID7

    # Verify the row exists and has the right fields
    row = await repo.fetch_one(
        text(
            "SELECT id, container_name, kind, name, target_value, config_source, "
            "  enabled, interval_seconds, timeout_seconds, created_at, hidden_at "
            "FROM probe_targets WHERE id = :id"
        ),
        {"id": probe_id},
    )
    assert row is not None
    assert row.container_name == "my-container"
    assert row.kind == "http"
    assert row.name == "api"
    assert row.target_value == "http://localhost:8080/healthz"
    assert row.config_source == "label"
    assert row.enabled == 1
    assert row.interval_seconds == _DEFAULT_INTERVAL
    assert row.timeout_seconds == _DEFAULT_TIMEOUT
    assert row.created_at == now
    assert row.hidden_at is None


@pytest.mark.asyncio
async def test_upsert_existing_updates_row_preserves_enabled_and_outcome(
    repo: SqliteRepository,
) -> None:
    """Second upsert with same key → UPDATE; preserves enabled and outcome fields."""
    probe_repo = ProbeTargetsRepository(repo)
    now = utc_now_iso()

    # First upsert with enabled=False and an outcome
    async with repo.transaction() as conn:
        probe_id = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="my-container",
            kind="http",
            name="api",
            target_value="http://localhost:8080/healthz",
            config_source="label",
            enabled=False,
            interval_seconds=30,
            timeout_seconds=5,
            now=now,
        )

    # Manually set outcome to simulate a previous run
    await repo.execute(
        text(
            "UPDATE probe_targets SET last_run_at = :run, last_status = :st, last_error = :err "
            "WHERE id = :id"
        ),
        {"run": now, "st": "fail", "err": "timeout", "id": probe_id},
    )

    # Second upsert with different target_value and interval
    async with repo.transaction() as conn:
        probe_id_2 = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="my-container",
            kind="http",
            name="api",
            target_value="http://localhost:9090/health",
            config_source="label",
            enabled=True,  # Try to override, but should be ignored
            interval_seconds=60,
            timeout_seconds=10,
            now=now,
        )

    # Should return the same ID
    assert probe_id_2 == probe_id

    # Verify the row was updated: target_value and interval changed
    row = await repo.fetch_one(
        text(
            "SELECT target_value, interval_seconds, timeout_seconds, enabled, "
            "  last_run_at, last_status, last_error "
            "FROM probe_targets WHERE id = :id"
        ),
        {"id": probe_id},
    )
    assert row is not None
    assert row.target_value == "http://localhost:9090/health"
    assert row.interval_seconds == _ALTERNATE_INTERVAL
    assert row.timeout_seconds == _ALTERNATE_TIMEOUT
    # enabled should NOT change — it stays False (manually disabled)
    assert row.enabled == 0
    # Outcome fields should be preserved
    assert row.last_status == "fail"
    assert row.last_error == "timeout"


@pytest.mark.asyncio
async def test_upsert_unhides_previously_hidden_row(repo: SqliteRepository) -> None:
    """Upsert a row that exists but hidden_at IS NOT NULL → un-hides it."""
    probe_repo = ProbeTargetsRepository(repo)
    now = utc_now_iso()

    # Insert and hide
    async with repo.transaction() as conn:
        probe_id = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="my-container",
            kind="http",
            name="api",
            target_value="http://localhost:8080/healthz",
            config_source="label",
            now=now,
        )

    await repo.execute(
        text("UPDATE probe_targets SET hidden_at = :now WHERE id = :id"),
        {"id": probe_id, "now": now},
    )

    # Verify it's hidden
    row = await repo.fetch_one(
        text("SELECT hidden_at FROM probe_targets WHERE id = :id"),
        {"id": probe_id},
    )
    assert row is not None
    assert row.hidden_at == now

    # Upsert with same key
    async with repo.transaction() as conn:
        probe_id_2 = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="my-container",
            kind="http",
            name="api",
            target_value="http://localhost:8080/healthz",
            config_source="label",
            now=now,
        )

    assert probe_id_2 == probe_id

    # Verify hidden_at is now NULL
    row = await repo.fetch_one(
        text("SELECT hidden_at FROM probe_targets WHERE id = :id"),
        {"id": probe_id},
    )
    assert row is not None
    assert row.hidden_at is None


@pytest.mark.asyncio
async def test_upsert_distinct_kinds_same_name_creates_separate_rows(
    repo: SqliteRepository,
) -> None:
    """(container, http, api) and (container, tcp, api) → different probe IDs."""
    probe_repo = ProbeTargetsRepository(repo)
    now = utc_now_iso()

    async with repo.transaction() as conn:
        http_id = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="my-container",
            kind="http",
            name="api",
            target_value="http://localhost:8080/",
            config_source="label",
            now=now,
        )
        tcp_id = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="my-container",
            kind="tcp",
            name="api",
            target_value="tcp://localhost:5432",
            config_source="label",
            now=now,
        )

    assert http_id != tcp_id

    # Verify both rows exist
    http_row = await repo.fetch_one(
        text("SELECT kind FROM probe_targets WHERE id = :id"), {"id": http_id}
    )
    tcp_row = await repo.fetch_one(
        text("SELECT kind FROM probe_targets WHERE id = :id"), {"id": tcp_id}
    )
    assert http_row is not None
    assert tcp_row is not None
    assert http_row.kind == "http"
    assert tcp_row.kind == "tcp"


@pytest.mark.asyncio
async def test_set_enabled_toggles_flag(repo: SqliteRepository) -> None:
    """set_enabled_conn toggles the enabled flag."""
    probe_repo = ProbeTargetsRepository(repo)
    now = utc_now_iso()

    async with repo.transaction() as conn:
        probe_id = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="my-container",
            kind="http",
            name="api",
            target_value="http://localhost:8080/",
            config_source="label",
            enabled=True,
            now=now,
        )

    # Disable
    async with repo.transaction() as conn:
        count = await probe_repo.set_enabled_conn(conn, probe_id=probe_id, enabled=False)
    assert count == 1

    row = await repo.fetch_one(
        text("SELECT enabled FROM probe_targets WHERE id = :id"), {"id": probe_id}
    )
    assert row is not None
    assert row.enabled == 0

    # Re-enable
    async with repo.transaction() as conn:
        count = await probe_repo.set_enabled_conn(conn, probe_id=probe_id, enabled=True)
    assert count == 1

    row = await repo.fetch_one(
        text("SELECT enabled FROM probe_targets WHERE id = :id"), {"id": probe_id}
    )
    assert row is not None
    assert row.enabled == 1


@pytest.mark.asyncio
async def test_set_enabled_returns_zero_for_unknown_id(repo: SqliteRepository) -> None:
    """set_enabled_conn on a non-existent id → returns 0."""
    probe_repo = ProbeTargetsRepository(repo)

    async with repo.transaction() as conn:
        count = await probe_repo.set_enabled_conn(conn, probe_id="nonexistent-id", enabled=True)

    assert count == 0


@pytest.mark.asyncio
async def test_update_run_outcome_sets_fields(repo: SqliteRepository) -> None:
    """update_run_outcome_conn sets last_run_at, last_status, last_error."""
    probe_repo = ProbeTargetsRepository(repo)
    now = utc_now_iso()

    async with repo.transaction() as conn:
        probe_id = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="my-container",
            kind="http",
            name="api",
            target_value="http://localhost:8080/",
            config_source="label",
            now=now,
        )

    run_time = utc_now_iso()

    # Update with ok status
    async with repo.transaction() as conn:
        await probe_repo.update_run_outcome_conn(
            conn, probe_id=probe_id, status="ok", error=None, now=run_time
        )

    row = await repo.fetch_one(
        text("SELECT last_run_at, last_status, last_error FROM probe_targets WHERE id = :id"),
        {"id": probe_id},
    )
    assert row is not None
    assert row.last_run_at == run_time
    assert row.last_status == "ok"
    assert row.last_error is None

    # Update with fail status
    run_time_2 = utc_now_iso()
    async with repo.transaction() as conn:
        await probe_repo.update_run_outcome_conn(
            conn, probe_id=probe_id, status="fail", error="timeout", now=run_time_2
        )

    row = await repo.fetch_one(
        text("SELECT last_run_at, last_status, last_error FROM probe_targets WHERE id = :id"),
        {"id": probe_id},
    )
    assert row is not None
    assert row.last_run_at == run_time_2
    assert row.last_status == "fail"
    assert row.last_error == "timeout"


@pytest.mark.asyncio
async def test_mark_missing_except_with_empty_kept_keys_hides_all(
    repo: SqliteRepository,
) -> None:
    """mark_missing_except with empty kept_keys → hides all visible probes."""
    probe_repo = ProbeTargetsRepository(repo)
    now = utc_now_iso()

    # Insert 3 probes for container "c"
    async with repo.transaction() as conn:
        probe_id_1 = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="c",
            kind="http",
            name="a",
            target_value="http://x:8080/",
            config_source="label",
            now=now,
        )
        probe_id_2 = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="c",
            kind="http",
            name="b",
            target_value="http://x:8081/",
            config_source="label",
            now=now,
        )
        probe_id_3 = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="c",
            kind="tcp",
            name="a",
            target_value="tcp://x:5432",
            config_source="label",
            now=now,
        )

    # Hide all
    async with repo.transaction() as conn:
        count = await probe_repo.mark_missing_except_conn(
            conn, container_name="c", kept_keys=set(), now=now
        )

    # Should hide all 3 probes
    assert count == _PROBE_COUNT_THREE

    # Verify all are hidden
    for probe_id in [probe_id_1, probe_id_2, probe_id_3]:
        row = await repo.fetch_one(
            text("SELECT hidden_at FROM probe_targets WHERE id = :id"),
            {"id": probe_id},
        )
        assert row is not None
        assert row.hidden_at == now


@pytest.mark.asyncio
async def test_mark_missing_except_preserves_kept_keys(repo: SqliteRepository) -> None:
    """mark_missing_except hides rows NOT in kept_keys."""
    probe_repo = ProbeTargetsRepository(repo)
    now = utc_now_iso()

    # Insert 3 probes
    async with repo.transaction() as conn:
        probe_id_1 = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="c",
            kind="http",
            name="a",
            target_value="http://x:8080/",
            config_source="label",
            now=now,
        )
        probe_id_2 = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="c",
            kind="http",
            name="b",
            target_value="http://x:8081/",
            config_source="label",
            now=now,
        )
        probe_id_3 = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="c",
            kind="tcp",
            name="a",
            target_value="tcp://x:5432",
            config_source="label",
            now=now,
        )

    # Hide all except (http, a)
    async with repo.transaction() as conn:
        count = await probe_repo.mark_missing_except_conn(
            conn,
            container_name="c",
            kept_keys={("http", "a")},
            now=now,
        )

    # Should hide 2 probes (http, b) and (tcp, a)
    assert count == _PROBE_COUNT_TWO

    # Verify (http, a) is NOT hidden
    row = await repo.fetch_one(
        text("SELECT hidden_at FROM probe_targets WHERE id = :id"),
        {"id": probe_id_1},
    )
    assert row is not None
    assert row.hidden_at is None

    # Verify the other two ARE hidden
    for probe_id in [probe_id_2, probe_id_3]:
        row = await repo.fetch_one(
            text("SELECT hidden_at FROM probe_targets WHERE id = :id"),
            {"id": probe_id},
        )
        assert row is not None
        assert row.hidden_at == now


@pytest.mark.asyncio
async def test_mark_missing_except_idempotent(repo: SqliteRepository) -> None:
    """mark_missing_except called twice with empty kept_keys → second call returns 0."""
    probe_repo = ProbeTargetsRepository(repo)
    now = utc_now_iso()

    # Insert one probe
    async with repo.transaction() as conn:
        await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="c",
            kind="http",
            name="a",
            target_value="http://x:8080/",
            config_source="label",
            now=now,
        )

    # First call hides it
    async with repo.transaction() as conn:
        count_1 = await probe_repo.mark_missing_except_conn(
            conn, container_name="c", kept_keys=set(), now=now
        )
    assert count_1 == 1

    # Second call should return 0 (already hidden)
    async with repo.transaction() as conn:
        count_2 = await probe_repo.mark_missing_except_conn(
            conn, container_name="c", kept_keys=set(), now=now
        )
    assert count_2 == 0


@pytest.mark.asyncio
async def test_list_for_container_excludes_hidden_by_default(
    repo: SqliteRepository,
) -> None:
    """list_for_container excludes hidden rows by default."""
    probe_repo = ProbeTargetsRepository(repo)
    now = utc_now_iso()

    # Insert one visible and one hidden
    async with repo.transaction() as conn:
        probe_id_1 = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="c",
            kind="http",
            name="a",
            target_value="http://x:8080/",
            config_source="label",
            now=now,
        )
        probe_id_2 = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="c",
            kind="tcp",
            name="a",
            target_value="tcp://x:5432",
            config_source="label",
            now=now,
        )

    # Hide the second one
    await repo.execute(
        text("UPDATE probe_targets SET hidden_at = :now WHERE id = :id"),
        {"id": probe_id_2, "now": now},
    )

    # List should return only the visible one
    rows = await probe_repo.list_for_container(container_name="c")
    assert len(rows) == 1
    assert rows[0].id == probe_id_1


@pytest.mark.asyncio
async def test_list_for_container_includes_hidden_when_requested(
    repo: SqliteRepository,
) -> None:
    """list_for_container with include_hidden=True returns hidden rows too."""
    probe_repo = ProbeTargetsRepository(repo)
    now = utc_now_iso()

    # Insert one visible and one hidden
    async with repo.transaction() as conn:
        probe_id_1 = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="c",
            kind="http",
            name="a",
            target_value="http://x:8080/",
            config_source="label",
            now=now,
        )
        probe_id_2 = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="c",
            kind="tcp",
            name="a",
            target_value="tcp://x:5432",
            config_source="label",
            now=now,
        )

    # Hide the second one
    await repo.execute(
        text("UPDATE probe_targets SET hidden_at = :now WHERE id = :id"),
        {"id": probe_id_2, "now": now},
    )

    # List with include_hidden=True should return both visible and hidden
    rows = await probe_repo.list_for_container(container_name="c", include_hidden=True)
    assert len(rows) == _PROBE_COUNT_TWO  # 1 visible + 1 hidden
    ids = {r.id for r in rows}
    assert probe_id_1 in ids
    assert probe_id_2 in ids


@pytest.mark.asyncio
async def test_list_for_container_ordered_by_kind_then_name(
    repo: SqliteRepository,
) -> None:
    """list_for_container returns rows sorted by (kind, name)."""
    probe_repo = ProbeTargetsRepository(repo)
    now = utc_now_iso()

    # Insert in non-deterministic order
    async with repo.transaction() as conn:
        await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="c",
            kind="tcp",
            name="z",
            target_value="tcp://x:5432",
            config_source="label",
            now=now,
        )
        await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="c",
            kind="http",
            name="b",
            target_value="http://x:8080/",
            config_source="label",
            now=now,
        )
        await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="c",
            kind="http",
            name="a",
            target_value="http://x:8081/",
            config_source="label",
            now=now,
        )

    rows = await probe_repo.list_for_container(container_name="c")
    assert len(rows) == _PROBE_COUNT_THREE  # All 3 probes should be returned
    # Should be ordered: (http, a), (http, b), (tcp, z)
    assert (rows[0].kind, rows[0].name) == ("http", "a")
    assert (rows[1].kind, rows[1].name) == ("http", "b")
    assert (rows[2].kind, rows[2].name) == ("tcp", "z")


@pytest.mark.asyncio
async def test_get_by_id_returns_none_for_unknown(repo: SqliteRepository) -> None:
    """get_by_id returns None for non-existent id."""
    probe_repo = ProbeTargetsRepository(repo)
    result = await probe_repo.get_by_id("nonexistent-id")
    assert result is None


@pytest.mark.asyncio
async def test_get_by_id_returns_row_for_existing(repo: SqliteRepository) -> None:
    """get_by_id returns the row for an existing id."""
    probe_repo = ProbeTargetsRepository(repo)
    now = utc_now_iso()

    async with repo.transaction() as conn:
        probe_id = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="c",
            kind="http",
            name="a",
            target_value="http://x:8080/",
            config_source="label",
            now=now,
        )

    row = await probe_repo.get_by_id(probe_id)
    assert row is not None
    assert row.id == probe_id
    assert row.container_name == "c"
    assert row.kind == "http"
    assert row.name == "a"


@pytest.mark.asyncio
async def test_list_distinct_container_names_with_enabled_probes_excludes_disabled(
    repo: SqliteRepository,
) -> None:
    """list_distinct_container_names returns only containers with enabled+visible probes."""
    probe_repo = ProbeTargetsRepository(repo)
    now = utc_now_iso()

    # Insert across multiple containers with various states
    async with repo.transaction() as conn:
        # Container "c1": enabled
        await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="c1",
            kind="http",
            name="a",
            target_value="http://x:8080/",
            config_source="label",
            enabled=True,
            now=now,
        )
        # Container "c2": disabled
        await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="c2",
            kind="http",
            name="a",
            target_value="http://x:8080/",
            config_source="label",
            enabled=False,
            now=now,
        )
        # Container "c3": enabled but hidden
        probe_id_c3 = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="c3",
            kind="http",
            name="a",
            target_value="http://x:8080/",
            config_source="label",
            enabled=True,
            now=now,
        )

    # Hide c3
    await repo.execute(
        text("UPDATE probe_targets SET hidden_at = :now WHERE id = :id"),
        {"id": probe_id_c3, "now": now},
    )

    containers = await probe_repo.list_distinct_container_names_with_enabled_probes()
    assert containers == ["c1"]  # Only c1 has enabled+visible probes


@pytest.mark.asyncio
async def test_summarize_by_container_empty(repo: SqliteRepository) -> None:
    """No probes at all => empty list."""
    probe_repo = ProbeTargetsRepository(repo)
    summaries = await probe_repo.summarize_by_container()
    assert summaries == []


@pytest.mark.asyncio
async def test_summarize_by_container_groups_by_container(repo: SqliteRepository) -> None:
    """Probes group by container_name; active + failing counts correct."""
    probe_repo = ProbeTargetsRepository(repo)
    now = utc_now_iso()
    # Container A: 2 probes (1 ok, 1 fail)
    async with repo.transaction() as conn:
        a1 = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="a",
            kind="http",
            name="p1",
            target_value="http://a/",
            config_source="label",
            now=now,
        )
        await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="a",
            kind="http",
            name="p2",
            target_value="http://b/",
            config_source="label",
            now=now,
        )
        # Container B: 1 probe (ok)
        await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="b",
            kind="tcp",
            name="default",
            target_value="tcp://host:80",
            config_source="label",
            now=now,
        )
    # Mark a1 as fail
    await repo.execute(
        text("UPDATE probe_targets SET last_status = 'fail' WHERE id = :id"),
        {"id": a1},
    )

    summaries = await probe_repo.summarize_by_container()
    by_name = {s.container_name: s for s in summaries}
    assert by_name["a"].active == _PROBE_COUNT_TWO
    assert by_name["a"].failing == 1
    assert by_name["b"].active == 1
    assert by_name["b"].failing == 0


@pytest.mark.asyncio
async def test_summarize_by_container_excludes_disabled_and_hidden(
    repo: SqliteRepository,
) -> None:
    """Disabled probes (enabled=0) and hidden probes (hidden_at IS NOT NULL) are excluded."""
    probe_repo = ProbeTargetsRepository(repo)
    now = utc_now_iso()
    async with repo.transaction() as conn:
        await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="x",
            kind="http",
            name="active",
            target_value="http://a/",
            config_source="label",
            now=now,
        )
        disabled_id = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="x",
            kind="http",
            name="disabled",
            target_value="http://b/",
            config_source="label",
            enabled=False,
            now=now,
        )
        hidden_id = await probe_repo.upsert_probe_target_conn(
            conn,
            container_name="x",
            kind="http",
            name="hidden",
            target_value="http://c/",
            config_source="label",
            now=now,
        )
    # Hide the third probe
    await repo.execute(
        text("UPDATE probe_targets SET hidden_at = :now WHERE id = :id"),
        {"id": hidden_id, "now": now},
    )
    # Silence unused-var: ids only needed for setup
    _ = (disabled_id,)

    summaries = await probe_repo.summarize_by_container()
    assert len(summaries) == 1
    assert summaries[0].container_name == "x"
    assert summaries[0].active == 1
    assert summaries[0].failing == 0
