"""Unit tests for HeartbeatRepo (no HTTP layer)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.heartbeat.repository import (
    HeartbeatRepo,
    _compute_expected_next_at,  # pyright: ignore[reportPrivateUsage]
)


async def _seed_cron(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    id_: str = "c1",
    name: str = "n1",
    host: str = "h1",
    integration_mode: str = "heartbeat",
    cadence_seconds: int = 60,
    grace_seconds: int = 300,
) -> None:
    now = utc_now_iso()
    async with repo.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO crons ("
                "  id, name, host, command, schedule, cadence_seconds, "
                "  expected_grace_seconds, integration_mode, enabled, "
                "  last_seen_state, created_at, updated_at"
                ") VALUES ("
                "  :id, :name, :host, '/bin/true', '* * * * *', :cadence, "
                "  :grace, :mode, 1, 'unknown', :created, :updated"
                ")"
            ),
            {
                "id": id_,
                "name": name,
                "host": host,
                "cadence": cadence_seconds,
                "grace": grace_seconds,
                "mode": integration_mode,
                "created": now,
                "updated": now,
            },
        )


@pytest.mark.asyncio
async def test_get_cron_returns_none_for_unknown_id(repo: SqliteRepository) -> None:
    hr = HeartbeatRepo(repo)
    assert await hr.get_cron("nope") is None


@pytest.mark.asyncio
async def test_get_cron_returns_record_for_registered_id(repo: SqliteRepository) -> None:
    await _seed_cron(repo, id_="cA", name="cron-a")
    hr = HeartbeatRepo(repo)
    cron = await hr.get_cron("cA")
    assert cron is not None
    assert cron.name == "cron-a"
    assert cron.integration_mode == "heartbeat"


@pytest.mark.asyncio
async def test_record_start_creates_state_row_with_streak_1(
    repo: SqliteRepository,
) -> None:
    await _seed_cron(repo)
    hr = HeartbeatRepo(repo)
    state = await hr.record_start("c1", who="t", ip=None)
    assert state.current_state == "running"
    assert state.current_streak == 1
    assert state.last_start_at is not None


@pytest.mark.asyncio
async def test_record_ok_after_start_resets_streak(repo: SqliteRepository) -> None:
    await _seed_cron(repo)
    hr = HeartbeatRepo(repo)
    await hr.record_start("c1", who="t", ip=None)
    state = await hr.record_ok("c1", duration_seconds=None, who="t", ip=None)
    assert state.current_state == "ok"
    assert state.current_streak == 1  # transition resets


@pytest.mark.asyncio
async def test_record_consecutive_oks_increments_streak(repo: SqliteRepository) -> None:
    await _seed_cron(repo)
    hr = HeartbeatRepo(repo)
    await hr.record_ok("c1", duration_seconds=None, who="t", ip=None)
    await hr.record_ok("c1", duration_seconds=None, who="t", ip=None)
    state = await hr.record_ok("c1", duration_seconds=None, who="t", ip=None)
    assert state.current_streak == 3  # noqa: PLR2004


@pytest.mark.asyncio
async def test_record_fail_after_ok_resets_streak(repo: SqliteRepository) -> None:
    await _seed_cron(repo)
    hr = HeartbeatRepo(repo)
    await hr.record_ok("c1", duration_seconds=None, who="t", ip=None)
    await hr.record_ok("c1", duration_seconds=None, who="t", ip=None)
    state = await hr.record_fail("c1", duration_seconds=None, exit_code=None, who="t", ip=None)
    assert state.current_state == "failed"
    assert state.current_streak == 1


@pytest.mark.asyncio
async def test_consecutive_fails_increment_streak(repo: SqliteRepository) -> None:
    await _seed_cron(repo)
    hr = HeartbeatRepo(repo)
    await hr.record_fail("c1", duration_seconds=None, exit_code=None, who="t", ip=None)
    state = await hr.record_fail("c1", duration_seconds=None, exit_code=None, who="t", ip=None)
    assert state.current_streak == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_record_ok_with_duration_persists_value(repo: SqliteRepository) -> None:
    await _seed_cron(repo)
    hr = HeartbeatRepo(repo)
    state = await hr.record_ok("c1", duration_seconds=4.25, who="t", ip=None)
    assert state.last_duration_seconds == 4.25  # noqa: PLR2004


@pytest.mark.asyncio
async def test_record_ok_computes_expected_next_at_with_cadence(
    repo: SqliteRepository,
) -> None:
    await _seed_cron(repo, cadence_seconds=60, grace_seconds=300)
    hr = HeartbeatRepo(repo)
    state = await hr.record_ok("c1", duration_seconds=None, who="t", ip=None)
    assert state.expected_next_at is not None


@pytest.mark.asyncio
async def test_record_ok_leaves_expected_next_at_null_when_cadence_zero(
    repo: SqliteRepository,
) -> None:
    await _seed_cron(repo, cadence_seconds=0)
    hr = HeartbeatRepo(repo)
    state = await hr.record_ok("c1", duration_seconds=None, who="t", ip=None)
    assert state.expected_next_at is None


@pytest.mark.asyncio
async def test_state_transition_updates_crons_last_seen_state_mirror(
    repo: SqliteRepository,
) -> None:
    await _seed_cron(repo)
    hr = HeartbeatRepo(repo)
    await hr.record_ok("c1", duration_seconds=None, who="t", ip=None)
    row = await repo.fetch_one(
        text("SELECT last_seen_state FROM crons WHERE id = :id"), {"id": "c1"}
    )
    assert row is not None
    assert row[0] == "ok"


@pytest.mark.asyncio
async def test_state_transition_writes_audit_log_in_same_transaction(
    repo: SqliteRepository,
) -> None:
    await _seed_cron(repo)
    hr = HeartbeatRepo(repo)
    await hr.record_ok("c1", duration_seconds=None, who="actor-x", ip="1.2.3.4")
    row = await repo.fetch_one(
        text(
            "SELECT who, what, ip FROM audit_log "
            "WHERE what = 'heartbeat.ok' ORDER BY id DESC LIMIT 1"
        ),
    )
    assert row is not None
    assert row[0] == "actor-x"
    assert row[1] == "heartbeat.ok"
    assert row[2] == "1.2.3.4"


@pytest.mark.asyncio
async def test_list_crons_orders_by_name(repo: SqliteRepository) -> None:
    await _seed_cron(repo, id_="c-x", name="zeta")
    await _seed_cron(repo, id_="c-y", name="alpha")
    hr = HeartbeatRepo(repo)
    rows = await hr.list_crons()
    names = [r.name for r in rows]
    assert names == ["alpha", "zeta"]


def test_compute_expected_next_at_with_cadence_zero_returns_none() -> None:
    assert (
        _compute_expected_next_at(
            last_ok_at_iso="2026-05-11T00:00:00+00:00",
            cadence_seconds=0,
            grace_seconds=300,
        )
        is None
    )


def test_compute_expected_next_at_adds_cadence_plus_grace() -> None:
    out = _compute_expected_next_at(
        last_ok_at_iso="2026-05-11T00:00:00+00:00",
        cadence_seconds=60,
        grace_seconds=30,
    )
    assert out == "2026-05-11T00:01:30+00:00"


@pytest.mark.asyncio
async def test_get_heartbeat_state_returns_none_for_unknown_id(
    repo: SqliteRepository,
) -> None:
    hbr = HeartbeatRepo(repo)
    result = await hbr.get_heartbeat_state("nonexistent-cron-id")
    assert result is None


@pytest.mark.asyncio
async def test_get_heartbeat_state_returns_record_after_ok(
    repo: SqliteRepository,
) -> None:
    hbr = HeartbeatRepo(repo)
    await _seed_cron(
        repo, id_="cron-x", name="x", host="h", integration_mode="heartbeat", cadence_seconds=60
    )
    await hbr.record_ok(
        "cron-x",
        duration_seconds=None,
        who="test",
        ip=None,
    )
    state = await hbr.get_heartbeat_state("cron-x")
    assert state is not None
    assert state.cron_id == "cron-x"
    assert state.current_state == "ok"
    assert state.last_ok_at is not None
    assert state.current_streak == 1
