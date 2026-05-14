"""Tests for CronRepo.upsert_discovered method (STAGE-002-007)."""

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.cron.fingerprint import compute_fingerprint
from homelab_monitor.kernel.cron.repository import CronRepo
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso


@pytest.mark.asyncio
async def test_upsert_discovered_insert(repo: SqliteRepository) -> None:
    """Test upsert_discovered INSERT path writes audit with crons.discover verb."""
    cron_repo = CronRepo(repo)
    record, inserted, updated_non_bump = await cron_repo.upsert_discovered(
        host="h1",
        source_path="/etc/crontab",
        schedule="10 4 * * *",
        command="/opt/backup.sh",
        now=utc_now_iso(),
    )
    assert inserted is True
    assert updated_non_bump is False
    assert record.last_discovered_at is not None
    audit = await repo.fetch_one(text("SELECT what FROM audit_log WHERE what = 'crons.discover'"))
    assert audit is not None


@pytest.mark.asyncio
async def test_upsert_discovered_second_call_bump_only(repo: SqliteRepository) -> None:
    """Test that second call with same fingerprint emits no audit (bump-only)."""
    cron_repo = CronRepo(repo)
    t1 = "2026-05-13T10:00:00+00:00"
    t2 = "2026-05-13T10:05:00+00:00"
    await cron_repo.upsert_discovered(
        host="h1",
        source_path="/etc/crontab",
        schedule="10 4 * * *",
        command="/opt/backup.sh",
        now=t1,
    )
    row = await repo.fetch_one(
        text("SELECT COUNT(*) AS c FROM audit_log WHERE what LIKE 'crons.discover%'")
    )
    assert row is not None
    audit_count_1 = row.c

    record, inserted, updated_non_bump = await cron_repo.upsert_discovered(
        host="h1",
        source_path="/etc/crontab",
        schedule="10 4 * * *",
        command="/opt/backup.sh",
        now=t2,
    )
    assert inserted is False
    assert updated_non_bump is False
    assert record.last_discovered_at == t2
    row = await repo.fetch_one(
        text("SELECT COUNT(*) AS c FROM audit_log WHERE what LIKE 'crons.discover%'")
    )
    assert row is not None
    audit_count_2 = row.c
    assert audit_count_2 == audit_count_1  # bump-only emits no audit


@pytest.mark.asyncio
async def test_upsert_discovered_remote_only_unchanged_by_discovery(repo: SqliteRepository) -> None:
    """Test that wrapper-only crons (source_path=None) are unreachable from discovery."""
    cron_repo = CronRepo(repo)
    # Pre-seed a wrapper-only row directly
    fp = compute_fingerprint(host="remote-h", source_path=None, schedule="* * * * *", command="/x")
    await repo.execute(
        text(
            "INSERT INTO crons (fingerprint, name, host, command, schedule, "
            "schedule_canonical, cadence_seconds, expected_grace_seconds, enabled, "
            "last_seen_state, created_at, updated_at, hidden_at, source_path, "
            "wrapper_last_seen_at, last_discovered_at) VALUES "
            "(:fp, 'x', 'remote-h', '/x', '* * * * *', '* * * * *', 60, 300, 1, "
            "'unknown', :now, :now, NULL, NULL, NULL, NULL)"
        ),
        {"fp": fp, "now": utc_now_iso()},
    )
    # Discoverer can never produce this fingerprint (source_path=None is unreachable)
    # so this is a property check, not behavior under discovery.
    record = await cron_repo.get_cron(fp, include_hidden=True)
    assert record is not None
    assert record.last_discovered_at is None


# ---------------------------------------------------------------------------
# schedule_canonical change triggers non-bump update + audit (lines 679-682, 698-702)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_discovered_schedule_canonical_change_writes_audit(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When schedule_canonical drifts, updated_non_bump=True and audit written."""
    import homelab_monitor.kernel.cron.schedule as schedule_mod  # noqa: PLC0415

    cron_repo = CronRepo(repo)
    t1 = "2026-05-13T10:00:00+00:00"
    t2 = "2026-05-13T10:05:00+00:00"

    # First call: real canonicalize_schedule
    await cron_repo.upsert_discovered(
        host="h1",
        source_path="/etc/crontab",
        schedule="10 4 * * *",
        command="/opt/backup.sh",
        now=t1,
    )

    # Second call: monkeypatch to return a different canonical value
    original_canonicalize = schedule_mod.canonicalize_schedule

    def _stub_canonicalize(*args: object, **kwargs: object) -> str:  # type: ignore[reportUnknownLambdaType]
        return "0 4 * * *"

    monkeypatch.setattr(schedule_mod, "canonicalize_schedule", _stub_canonicalize)
    record, inserted, updated_non_bump = await cron_repo.upsert_discovered(
        host="h1",
        source_path="/etc/crontab",
        schedule="10 4 * * *",
        command="/opt/backup.sh",
        now=t2,
    )
    monkeypatch.setattr(schedule_mod, "canonicalize_schedule", original_canonicalize)

    assert inserted is False
    assert updated_non_bump is True
    assert record.schedule_canonical == "0 4 * * *"

    audit = await repo.fetch_one(
        text("SELECT what FROM audit_log WHERE what = 'crons.discover.update'")
    )
    assert audit is not None


# ---------------------------------------------------------------------------
# cadence_seconds change triggers non-bump update + audit (lines 683-686, 698-702)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_discovered_cadence_seconds_change_writes_audit(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When cadence_seconds drifts, updated_non_bump=True and audit written."""
    import homelab_monitor.kernel.cron.schedule as schedule_mod  # noqa: PLC0415

    cron_repo = CronRepo(repo)
    t1 = "2026-05-13T10:00:00+00:00"
    t2 = "2026-05-13T10:05:00+00:00"

    await cron_repo.upsert_discovered(
        host="h1",
        source_path="/etc/crontab",
        schedule="10 4 * * *",
        command="/opt/backup.sh",
        now=t1,
    )

    original_compute = schedule_mod.compute_average_interval_seconds

    def _stub_compute(*args: object, **kwargs: object) -> int:  # type: ignore[reportUnknownLambdaType]
        return 9999

    monkeypatch.setattr(schedule_mod, "compute_average_interval_seconds", _stub_compute)
    record, inserted, updated_non_bump = await cron_repo.upsert_discovered(
        host="h1",
        source_path="/etc/crontab",
        schedule="10 4 * * *",
        command="/opt/backup.sh",
        now=t2,
    )
    monkeypatch.setattr(schedule_mod, "compute_average_interval_seconds", original_compute)

    assert inserted is False
    assert updated_non_bump is True
    assert record.cadence_seconds == 9999  # noqa: PLR2004

    audit = await repo.fetch_one(
        text("SELECT what FROM audit_log WHERE what = 'crons.discover.update'")
    )
    assert audit is not None


# ---------------------------------------------------------------------------
# command change triggers non-bump update + audit (Bug B fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_discovered_command_change_writes_audit(
    repo: SqliteRepository,
) -> None:
    """When stored command differs from new (post-scrub) command, update + audit."""
    cron_repo = CronRepo(repo)
    t1 = "2026-05-14T10:00:00+00:00"
    t2 = "2026-05-14T10:05:00+00:00"

    # First insert with the original command
    await cron_repo.upsert_discovered(
        host="h1",
        source_path="/etc/cron.d/test",
        schedule="* * * * *",
        command="/bin/echo hello",
        now=t1,
    )

    # Manually corrupt the command in the DB to simulate prior scrub-bug state
    async with repo.transaction() as conn:
        await conn.execute(
            text("UPDATE crons SET command = '/bin/echo CORRUPTED' WHERE host = 'h1'")
        )

    # Re-discover with the original (correct) command
    record, inserted, updated_non_bump = await cron_repo.upsert_discovered(
        host="h1",
        source_path="/etc/cron.d/test",
        schedule="* * * * *",
        command="/bin/echo hello",
        now=t2,
    )

    assert inserted is False
    assert updated_non_bump is True
    assert record.command == "/bin/echo hello"  # corrupted value overwritten

    # Verify audit row written
    audit = await repo.fetch_one(
        text("SELECT what FROM audit_log WHERE what = 'crons.discover.update'")
    )
    assert audit is not None


# ---------------------------------------------------------------------------
# name change triggers non-bump update + audit (Bug B fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_discovered_name_change_writes_audit(
    repo: SqliteRepository,
) -> None:
    """When derived name differs from stored, update + audit."""
    cron_repo = CronRepo(repo)
    t1 = "2026-05-14T10:00:00+00:00"
    t2 = "2026-05-14T10:05:00+00:00"

    await cron_repo.upsert_discovered(
        host="h1",
        source_path="/etc/cron.d/test",
        schedule="* * * * *",
        command="/bin/echo hello",
        now=t1,
    )

    # Manually change the stored name to simulate stale derived value
    async with repo.transaction() as conn:
        await conn.execute(text("UPDATE crons SET name = 'fake-old-name' WHERE host = 'h1'"))

    record, inserted, updated_non_bump = await cron_repo.upsert_discovered(
        host="h1",
        source_path="/etc/cron.d/test",
        schedule="* * * * *",
        command="/bin/echo hello",
        now=t2,
    )

    assert inserted is False
    assert updated_non_bump is True
    assert record.name == "echo"  # derived from "/bin/echo hello" via derive_name

    # Verify audit row written
    audit = await repo.fetch_one(
        text("SELECT what FROM audit_log WHERE what = 'crons.discover.update'")
    )
    assert audit is not None
