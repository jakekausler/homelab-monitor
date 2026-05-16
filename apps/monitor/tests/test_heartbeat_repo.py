"""Unit tests for HeartbeatRepo (no HTTP layer)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.cron.fingerprint import compute_fingerprint
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.heartbeat import repository as repo_mod
from homelab_monitor.kernel.heartbeat.repository import (
    HeartbeatRepo,
    compute_expected_next_at,
)


async def _seed_cron(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    fingerprint: str | None = None,
    name: str = "n1",
    host: str = "h1",
    cadence_seconds: int = 60,
    grace_seconds: int = 300,
    command: str = "/bin/true",
    source_path: str | None = "/etc/crontab",
) -> str:
    """Insert a cron row and return its fingerprint."""
    fp = fingerprint or compute_fingerprint(
        host=host, source_path=source_path, schedule="* * * * *", command=command
    )
    now = utc_now_iso()
    async with repo.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO crons (fingerprint, name, host, command, schedule, "
                "schedule_canonical, cadence_seconds, expected_grace_seconds, "
                "enabled, last_seen_state, created_at, updated_at, hidden_at, "
                "source_path, wrapper_last_seen_at) VALUES ("
                ":fp, :name, :host, :command, '* * * * *', '* * * * *', :cadence, "
                ":grace, 1, 'unknown', :created, :updated, NULL, :sp, NULL)"
            ),
            {
                "fp": fp,
                "name": name,
                "host": host,
                "command": command,
                "cadence": cadence_seconds,
                "grace": grace_seconds,
                "created": now,
                "updated": now,
                "sp": source_path,
            },
        )
    return fp


@pytest.mark.asyncio
async def test_get_cron_returns_none_for_unknown_id(repo: SqliteRepository) -> None:
    hr = HeartbeatRepo(repo)
    assert await hr.get_cron("nope") is None


@pytest.mark.asyncio
async def test_get_cron_returns_record_for_registered_id(repo: SqliteRepository) -> None:
    fp = await _seed_cron(repo, fingerprint="cA", name="cron-a")
    hr = HeartbeatRepo(repo)
    cron = await hr.get_cron(fp)
    assert cron is not None
    assert cron.name == "cron-a"
    assert cron.fingerprint == fp


@pytest.mark.asyncio
async def test_record_start_creates_state_row_with_streak_1(
    repo: SqliteRepository,
) -> None:
    fp = await _seed_cron(repo)
    hr = HeartbeatRepo(repo)
    state = await hr.record_start(fp, who="t", ip=None)
    assert state.current_state == "running"
    assert state.current_streak == 1
    assert state.last_start_at is not None


@pytest.mark.asyncio
async def test_record_ok_after_start_resets_streak(repo: SqliteRepository) -> None:
    fp = await _seed_cron(repo)
    hr = HeartbeatRepo(repo)
    await hr.record_start(fp, who="t", ip=None)
    state = await hr.record_ok(fp, duration_seconds=None, who="t", ip=None)
    assert state.current_state == "ok"
    assert state.current_streak == 1  # transition resets


@pytest.mark.asyncio
async def test_record_consecutive_oks_increments_streak(repo: SqliteRepository) -> None:
    fp = await _seed_cron(repo)
    hr = HeartbeatRepo(repo)
    await hr.record_ok(fp, duration_seconds=None, who="t", ip=None)
    await hr.record_ok(fp, duration_seconds=None, who="t", ip=None)
    state = await hr.record_ok(fp, duration_seconds=None, who="t", ip=None)
    assert state.current_streak == 3  # noqa: PLR2004


@pytest.mark.asyncio
async def test_record_fail_after_ok_resets_streak(repo: SqliteRepository) -> None:
    fp = await _seed_cron(repo)
    hr = HeartbeatRepo(repo)
    await hr.record_ok(fp, duration_seconds=None, who="t", ip=None)
    await hr.record_ok(fp, duration_seconds=None, who="t", ip=None)
    state = await hr.record_fail(fp, duration_seconds=None, exit_code=None, who="t", ip=None)
    assert state.current_state == "failed"
    assert state.current_streak == 1


@pytest.mark.asyncio
async def test_consecutive_fails_increment_streak(repo: SqliteRepository) -> None:
    fp = await _seed_cron(repo)
    hr = HeartbeatRepo(repo)
    await hr.record_fail(fp, duration_seconds=None, exit_code=None, who="t", ip=None)
    state = await hr.record_fail(fp, duration_seconds=None, exit_code=None, who="t", ip=None)
    assert state.current_streak == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_record_ok_with_duration_persists_value(repo: SqliteRepository) -> None:
    fp = await _seed_cron(repo)
    hr = HeartbeatRepo(repo)
    state = await hr.record_ok(fp, duration_seconds=4.25, who="t", ip=None)
    assert state.last_duration_seconds == 4.25  # noqa: PLR2004


@pytest.mark.asyncio
async def test_record_ok_computes_expected_next_at_with_cadence(
    repo: SqliteRepository,
) -> None:
    fp = await _seed_cron(repo, cadence_seconds=60, grace_seconds=300)
    hr = HeartbeatRepo(repo)
    state = await hr.record_ok(fp, duration_seconds=None, who="t", ip=None)
    assert state.expected_next_at is not None


@pytest.mark.asyncio
async def test_record_ok_leaves_expected_next_at_null_when_cadence_zero(
    repo: SqliteRepository,
) -> None:
    fp = await _seed_cron(repo, cadence_seconds=0)
    hr = HeartbeatRepo(repo)
    state = await hr.record_ok(fp, duration_seconds=None, who="t", ip=None)
    assert state.expected_next_at is None


@pytest.mark.asyncio
async def test_fail_after_ok_clears_expected_next_at(repo: SqliteRepository) -> None:
    """A /fail transition must NULL expected_next_at to prevent phantom alerts."""
    fp = await _seed_cron(repo, cadence_seconds=60)
    hbr = HeartbeatRepo(repo)
    # First /ok sets expected_next_at.
    ok_state = await hbr.record_ok(fp, duration_seconds=None, who="t", ip=None)
    assert ok_state.expected_next_at is not None
    # Then /fail should clear it.
    fail_state = await hbr.record_fail(fp, duration_seconds=None, exit_code=None, who="t", ip=None)
    assert fail_state.expected_next_at is None


@pytest.mark.asyncio
async def test_state_transition_updates_crons_last_seen_state_mirror(
    repo: SqliteRepository,
) -> None:
    fp = await _seed_cron(repo)
    hr = HeartbeatRepo(repo)
    await hr.record_ok(fp, duration_seconds=None, who="t", ip=None)
    row = await repo.fetch_one(
        text("SELECT last_seen_state FROM crons WHERE fingerprint = :fp"), {"fp": fp}
    )
    assert row is not None
    assert row[0] == "ok"


@pytest.mark.asyncio
async def test_state_transition_writes_audit_log_in_same_transaction(
    repo: SqliteRepository,
) -> None:
    fp = await _seed_cron(repo)
    hr = HeartbeatRepo(repo)
    await hr.record_ok(fp, duration_seconds=None, who="actor-x", ip="1.2.3.4")
    row = await repo.fetch_one(
        text(
            "SELECT who, what, ip, "
            "json_extract(after_json, '$.cron_fingerprint') AS cron_fingerprint "
            "FROM audit_log "
            "WHERE what = 'heartbeat.ok' ORDER BY id DESC LIMIT 1"
        ),
    )
    assert row is not None
    assert row[0] == "actor-x"
    assert row[1] == "heartbeat.ok"
    assert row[2] == "1.2.3.4"
    assert row[3] == fp


@pytest.mark.asyncio
async def test_writes_are_atomic_under_simulated_failure(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If insert_audit raises, both heartbeats_state and crons mirror roll back."""
    fp = await _seed_cron(repo)
    hbr = HeartbeatRepo(repo)

    async def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(repo_mod, "insert_audit", boom)

    with pytest.raises(RuntimeError, match="simulated"):
        await hbr.record_ok(fp, duration_seconds=None, who="t", ip=None)

    # heartbeats_state row must NOT exist (transaction rolled back)
    hb_row = await repo.fetch_one(
        text("SELECT 1 FROM heartbeats_state WHERE cron_fingerprint = :fp"),
        {"fp": fp},
    )
    assert hb_row is None

    # crons.last_seen_state must still be 'unknown' (mirror not updated)
    crons_row = await repo.fetch_one(
        text("SELECT last_seen_state FROM crons WHERE fingerprint = :fp"),
        {"fp": fp},
    )
    assert crons_row is not None
    assert crons_row[0] == "unknown"


@pytest.mark.asyncio
async def test_three_consecutive_oks_write_three_audit_rows(
    repo: SqliteRepository,
) -> None:
    """Verify no accidental de-duplication of audit rows on repeated same-state pings."""
    fp = await _seed_cron(repo)
    hbr = HeartbeatRepo(repo)

    for _ in range(3):
        await hbr.record_ok(fp, duration_seconds=None, who="t", ip=None)

    row = await repo.fetch_one(text("SELECT COUNT(*) FROM audit_log WHERE what LIKE '%heartbeat%'"))
    assert row is not None
    assert int(row[0]) == 3  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_crons_orders_by_name(repo: SqliteRepository) -> None:
    await _seed_cron(repo, fingerprint="c-x", name="zeta")
    await _seed_cron(repo, fingerprint="c-y", name="alpha")
    hr = HeartbeatRepo(repo)
    rows = await hr.list_crons()
    names = [r.name for r in rows]
    assert names == ["alpha", "zeta"]


def test_compute_expected_next_at_with_cadence_zero_returns_none() -> None:
    assert (
        compute_expected_next_at(
            last_ok_at_iso="2026-05-11T00:00:00+00:00",
            cadence_seconds=0,
            grace_seconds=300,
        )
        is None
    )


def test_compute_expected_next_at_adds_cadence_plus_grace() -> None:
    out = compute_expected_next_at(
        last_ok_at_iso="2026-05-11T00:00:00+00:00",
        cadence_seconds=60,
        grace_seconds=30,
    )
    assert out == "2026-05-11T00:01:30+00:00"


def test_compute_expected_next_at_rejects_naive_iso() -> None:
    """Guard against silently producing tz-naive timestamps from naive ISO input."""
    with pytest.raises(ValueError, match="must be tz-aware"):
        compute_expected_next_at(
            last_ok_at_iso="2026-05-11T00:00:00",  # no +00:00 suffix → naive
            cadence_seconds=60,
            grace_seconds=30,
        )


@pytest.mark.asyncio
async def test_get_heartbeat_state_returns_none_for_unknown_id(
    repo: SqliteRepository,
) -> None:
    hbr = HeartbeatRepo(repo)
    result = await hbr.get_heartbeat_state("nonexistent-cron-fingerprint")
    assert result is None


@pytest.mark.asyncio
async def test_get_heartbeat_state_returns_record_after_ok(
    repo: SqliteRepository,
) -> None:
    hbr = HeartbeatRepo(repo)
    fp = await _seed_cron(repo, fingerprint="cron-x", name="x", host="h", cadence_seconds=60)
    await hbr.record_ok(
        fp,
        duration_seconds=None,
        who="test",
        ip=None,
    )
    state = await hbr.get_heartbeat_state(fp)
    assert state is not None
    assert state.cron_fingerprint == fp
    assert state.current_state == "ok"
    assert state.last_ok_at is not None
    assert state.current_streak == 1


@pytest.mark.asyncio
async def test_record_observed_run_first_time_creates_neutral_row(
    repo: SqliteRepository,
) -> None:
    """Observed run on a cron with no prior state creates a neutral state row."""
    fp = await _seed_cron(repo, fingerprint="cron-obs-1", name="obs1", host="h1")
    hbr = HeartbeatRepo(repo)
    now = utc_now_iso()
    state = await hbr.record_observed_run(fp, observed_at=now, who="test", ip=None)
    assert state.cron_fingerprint == fp
    assert state.observed_runs_total == 1
    assert state.last_observed_run_at == now
    assert state.current_state == "unknown"
    assert state.last_ok_at is None
    assert state.last_fail_at is None
    assert state.current_streak == 0
    assert state.last_exit_code is None


@pytest.mark.asyncio
async def test_record_observed_run_increments_existing(repo: SqliteRepository) -> None:
    """Observed run preserves state fields (D1 invariant)."""
    fp = await _seed_cron(repo, fingerprint="cron-obs-2", name="obs2", host="h1")
    hbr = HeartbeatRepo(repo)
    now = utc_now_iso()
    # First record an OK to set up state
    await hbr.record_ok(fp, duration_seconds=10.5, who="test", ip=None)
    ok_state = await hbr.get_heartbeat_state(fp)
    assert ok_state is not None
    assert ok_state.current_state == "ok"
    assert ok_state.current_streak == 1
    ok_at = ok_state.last_ok_at
    # Now record an observed run
    state = await hbr.record_observed_run(fp, observed_at=now, who="test", ip=None)
    assert state.observed_runs_total == 1  # first observed run
    assert state.last_observed_run_at == now
    # State fields UNCHANGED
    assert state.current_state == "ok"  # not changed to unknown
    assert state.current_streak == 1  # not reset
    assert state.last_ok_at == ok_at  # not changed


@pytest.mark.asyncio
async def test_record_observed_run_twice_increments_to_two(
    repo: SqliteRepository,
) -> None:
    """Two consecutive observed runs increment the counter."""
    fp = await _seed_cron(repo, fingerprint="cron-obs-3", name="obs3", host="h1")
    hbr = HeartbeatRepo(repo)
    now1 = utc_now_iso()
    state1 = await hbr.record_observed_run(fp, observed_at=now1, who="test", ip=None)
    assert state1.observed_runs_total == 1
    now2 = utc_now_iso()
    state2 = await hbr.record_observed_run(fp, observed_at=now2, who="test", ip=None)
    assert state2.observed_runs_total == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_record_observed_run_writes_audit_row(repo: SqliteRepository) -> None:
    """Observed run writes an audit row with what=cron.observed_run."""
    fp = await _seed_cron(repo, fingerprint="cron-obs-4", name="obs4", host="h1")
    hbr = HeartbeatRepo(repo)
    now = utc_now_iso()
    await hbr.record_observed_run(fp, observed_at=now, who="audit-test", ip="1.2.3.4")
    # Query audit log
    async with repo.engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT what, who, after_json FROM audit_log "
                    "WHERE what = :what AND "
                    "json_extract(after_json, '$.cron_fingerprint') = :fp "
                    'ORDER BY "when" DESC'
                ),
                {"what": "cron.observed_run", "fp": fp},
            )
        ).fetchall()
    assert len(rows) >= 1
    audit_row = rows[0]
    assert audit_row.who == "audit-test"
    assert "observed_runs_total" in audit_row.after_json


@pytest.mark.asyncio
async def test_record_observed_run_does_not_mirror_crons_last_seen_state(
    repo: SqliteRepository,
) -> None:
    """Observed run does not update crons.last_seen_state (D1: no mirror)."""
    fp = await _seed_cron(repo, fingerprint="cron-obs-5", name="obs5", host="h1")
    hbr = HeartbeatRepo(repo)
    # Verify initial cron state
    cron = await hbr.get_cron(fp)
    assert cron is not None
    assert cron.last_seen_state == "unknown"
    # Record observed run
    now = utc_now_iso()
    await hbr.record_observed_run(fp, observed_at=now, who="test", ip=None)
    # Re-fetch cron; last_seen_state should NOT change
    cron_after = await hbr.get_cron(fp)
    assert cron_after is not None
    assert cron_after.last_seen_state == "unknown"


@pytest.mark.asyncio
async def test_record_ok_preserves_observed_runs_total(repo: SqliteRepository) -> None:
    """record_ok carries observed_runs_total through (_record_state_transition)."""
    fp = await _seed_cron(repo, fingerprint="cron-obs-6", name="obs6", host="h1")
    hbr = HeartbeatRepo(repo)
    now = utc_now_iso()
    # Record an observed run first
    await hbr.record_observed_run(fp, observed_at=now, who="test", ip=None)
    state_after_obs = await hbr.get_heartbeat_state(fp)
    assert state_after_obs is not None
    assert state_after_obs.observed_runs_total == 1
    # Now record an OK
    state_after_ok = await hbr.record_ok(fp, duration_seconds=5.0, who="test", ip=None)
    # observed_runs_total should be preserved
    assert state_after_ok.observed_runs_total == 1
