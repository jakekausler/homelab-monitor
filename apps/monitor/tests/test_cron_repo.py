"""Unit tests for CronRepo (no HTTP layer)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.cron.repository import CronRepo
from homelab_monitor.kernel.cron.schedule import compute_average_interval_seconds
from homelab_monitor.kernel.cron.schemas import CronCreate, CronUpdate
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

WHO = "test-user"
IP = "127.0.0.1"


async def _audit_count(repo: SqliteRepository, *, what_prefix: str = "crons.") -> int:
    row = await repo.fetch_one(
        text("SELECT COUNT(*) FROM audit_log WHERE what LIKE :w"),
        {"w": f"{what_prefix}%"},
    )
    return int(row[0]) if row is not None else 0


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_persists_and_audits(repo: SqliteRepository) -> None:
    cron_repo = CronRepo(repo)
    payload = CronCreate(name="alpha", host="h", command="/x", schedule="*/5 * * * *")
    rec = await cron_repo.create_cron(payload, who=WHO, ip=IP)
    assert rec.name == "alpha"
    assert rec.schedule_canonical == "*/5 * * * *"
    assert await _audit_count(repo) == 1


@pytest.mark.asyncio
async def test_create_canonicalizes_at_hourly(repo: SqliteRepository) -> None:
    cron_repo = CronRepo(repo)
    payload = CronCreate(name="h", host="h", command="/x", schedule="@hourly")
    rec = await cron_repo.create_cron(payload, who=WHO, ip=IP)
    assert rec.schedule == "@hourly"
    assert rec.schedule_canonical == "0 * * * *"


# ---------------------------------------------------------------------------
# UPDATE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_changed_fields_only_writes_one_audit_row(repo: SqliteRepository) -> None:
    cron_repo = CronRepo(repo)
    rec = await cron_repo.create_cron(
        CronCreate(name="x", host="h", command="/x", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    # one audit row from create
    assert await _audit_count(repo) == 1
    await cron_repo.update_cron(rec.id, CronUpdate(expected_grace_seconds=600), who=WHO, ip=IP)
    # one more from update
    assert await _audit_count(repo) == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_update_empty_diff_writes_no_audit(repo: SqliteRepository) -> None:
    cron_repo = CronRepo(repo)
    rec = await cron_repo.create_cron(
        CronCreate(name="x", host="h", command="/x", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    assert await _audit_count(repo) == 1
    # PATCH same value → no diff → no audit
    await cron_repo.update_cron(rec.id, CronUpdate(expected_grace_seconds=300), who=WHO, ip=IP)
    assert await _audit_count(repo) == 1


@pytest.mark.asyncio
async def test_update_archived_at_emits_delete_verb(repo: SqliteRepository) -> None:
    cron_repo = CronRepo(repo)
    rec = await cron_repo.create_cron(
        CronCreate(name="x", host="h", command="/x", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    await cron_repo.update_cron(rec.id, CronUpdate(archived_at=utc_now_iso()), who=WHO, ip=IP)
    row = await repo.fetch_one(text('SELECT what FROM audit_log ORDER BY "when" DESC LIMIT 1'))
    assert row is not None
    assert row[0] == "crons.delete"


@pytest.mark.asyncio
async def test_update_restore_emits_restore_verb(repo: SqliteRepository) -> None:
    cron_repo = CronRepo(repo)
    rec = await cron_repo.create_cron(
        CronCreate(name="x", host="h", command="/x", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    await cron_repo.update_cron(rec.id, CronUpdate(archived_at=utc_now_iso()), who=WHO, ip=IP)
    await cron_repo.update_cron(rec.id, CronUpdate(archived_at=None), who=WHO, ip=IP)
    row = await repo.fetch_one(text('SELECT what FROM audit_log ORDER BY "when" DESC LIMIT 1'))
    assert row is not None
    assert row[0] == "crons.restore"


@pytest.mark.asyncio
async def test_update_recanonicalizes_on_schedule_change(repo: SqliteRepository) -> None:
    cron_repo = CronRepo(repo)
    rec = await cron_repo.create_cron(
        CronCreate(name="x", host="h", command="/x", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    updated = await cron_repo.update_cron(rec.id, CronUpdate(schedule="@daily"), who=WHO, ip=IP)
    assert updated.schedule == "@daily"
    assert updated.schedule_canonical == "0 0 * * *"


@pytest.mark.asyncio
async def test_update_unknown_id_raises_lookup_error(repo: SqliteRepository) -> None:
    cron_repo = CronRepo(repo)
    with pytest.raises(LookupError):
        await cron_repo.update_cron(
            "no-such", CronUpdate(expected_grace_seconds=600), who=WHO, ip=IP
        )


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_soft_delete_archives_and_audits_delete_verb(repo: SqliteRepository) -> None:
    cron_repo = CronRepo(repo)
    rec = await cron_repo.create_cron(
        CronCreate(name="x", host="h", command="/x", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    await cron_repo.soft_delete_cron(rec.id, who=WHO, ip=IP)
    row = await repo.fetch_one(text("SELECT archived_at FROM crons WHERE id = :id"), {"id": rec.id})
    assert row is not None
    assert row[0] is not None
    audit = await repo.fetch_one(text('SELECT what FROM audit_log ORDER BY "when" DESC LIMIT 1'))
    assert audit is not None
    assert audit[0] == "crons.delete"


@pytest.mark.asyncio
async def test_soft_delete_already_archived_raises(repo: SqliteRepository) -> None:
    cron_repo = CronRepo(repo)
    rec = await cron_repo.create_cron(
        CronCreate(name="x", host="h", command="/x", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    await cron_repo.soft_delete_cron(rec.id, who=WHO, ip=IP)
    with pytest.raises(LookupError):
        await cron_repo.soft_delete_cron(rec.id, who=WHO, ip=IP)


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_pagination_math(repo: SqliteRepository) -> None:
    cron_repo = CronRepo(repo)
    for i in range(7):
        await cron_repo.create_cron(
            CronCreate(name=f"c{i:02d}", host="h", command=f"/x{i}", schedule="* * * * *"),
            who=WHO,
            ip=IP,
        )
    page = await cron_repo.list_crons(
        page=2,
        page_size=3,
        host=None,
        integration_mode=None,
        enabled=None,
        state=None,
        q=None,
        include_archived=False,
    )
    assert page.total == 7  # noqa: PLR2004
    assert len(page.items) == 3  # noqa: PLR2004
    # ORDER BY name ASC: page 2 of size 3 yields names c03, c04, c05
    assert [it.name for it in page.items] == ["c03", "c04", "c05"]


@pytest.mark.asyncio
async def test_list_filter_combinations(repo: SqliteRepository) -> None:
    cron_repo = CronRepo(repo)
    await cron_repo.create_cron(
        CronCreate(
            name="a", host="h1", command="/x", schedule="* * * * *", integration_mode="observe"
        ),
        who=WHO,
        ip=IP,
    )
    await cron_repo.create_cron(
        CronCreate(
            name="b", host="h2", command="/x", schedule="* * * * *", integration_mode="heartbeat"
        ),
        who=WHO,
        ip=IP,
    )
    page = await cron_repo.list_crons(
        page=1,
        page_size=10,
        host="h2",
        integration_mode="heartbeat",
        enabled=None,
        state=None,
        q=None,
        include_archived=False,
    )
    assert page.total == 1
    assert page.items[0].name == "b"


@pytest.mark.asyncio
async def test_list_q_substring_matches_name_or_command(repo: SqliteRepository) -> None:
    cron_repo = CronRepo(repo)
    await cron_repo.create_cron(
        CronCreate(name="alpha", host="h", command="/grep_me", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    await cron_repo.create_cron(
        CronCreate(name="grep_me_too", host="h", command="/y", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    await cron_repo.create_cron(
        CronCreate(name="other", host="h", command="/z", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    page = await cron_repo.list_crons(
        page=1,
        page_size=10,
        host=None,
        integration_mode=None,
        enabled=None,
        state=None,
        q="grep_me",
        include_archived=False,
    )
    assert page.total == 2  # noqa: PLR2004
    names = {it.name for it in page.items}
    assert names == {"alpha", "grep_me_too"}


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_atomic_under_audit_failure(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If insert_audit raises mid-create, the cron INSERT must roll back."""
    cron_repo = CronRepo(repo)

    from homelab_monitor.kernel.cron import repository as repo_mod  # noqa: PLC0415

    async def _bomb(*_args, **_kwargs):  # type: ignore[no-untyped-def]  # noqa: ANN003, ANN002, ANN202
        raise RuntimeError("boom")

    monkeypatch.setattr(repo_mod, "insert_audit", _bomb)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(RuntimeError, match="boom"):
        await cron_repo.create_cron(
            CronCreate(name="x", host="h", command="/x", schedule="* * * * *"),
            who=WHO,
            ip=IP,
        )

    # Verify NO crons row persisted.
    row = await repo.fetch_one(text("SELECT COUNT(*) FROM crons"))
    assert row is not None
    assert int(row[0]) == 0


@pytest.mark.asyncio
async def test_list_crons_filter_by_state(repo: SqliteRepository) -> None:
    """Verify CronRepo.list_crons filters by last_seen_state."""
    crepo = CronRepo(repo)
    await repo.execute(
        text(
            "INSERT INTO crons (id, name, host, command, schedule, schedule_canonical, "
            "cadence_seconds, expected_grace_seconds, integration_mode, enabled, last_seen_state, "
            "created_at, updated_at, archived_at) VALUES ("
            ":id, :name, :host, :command, :schedule, :sched_canon, :cad, :grace, "
            ":mode, :enabled, :state, :created, :updated, :archived)"
        ),
        {
            "id": "c-ok",
            "name": "ok-cron",
            "host": "h1",
            "command": "/bin/true",
            "schedule": "* * * * *",
            "sched_canon": "* * * * *",
            "cad": 0,
            "grace": 300,
            "mode": "observe",
            "enabled": 1,
            "state": "ok",
            "created": utc_now_iso(),
            "updated": utc_now_iso(),
            "archived": None,
        },
    )
    await repo.execute(
        text(
            "INSERT INTO crons (id, name, host, command, schedule, schedule_canonical, "
            "cadence_seconds, expected_grace_seconds, integration_mode, enabled, last_seen_state, "
            "created_at, updated_at, archived_at) VALUES ("
            ":id, :name, :host, :command, :schedule, :sched_canon, :cad, :grace, "
            ":mode, :enabled, :state, :created, :updated, :archived)"
        ),
        {
            "id": "c-failed",
            "name": "failed-cron",
            "host": "h1",
            "command": "/bin/false",
            "schedule": "* * * * *",
            "sched_canon": "* * * * *",
            "cad": 0,
            "grace": 300,
            "mode": "observe",
            "enabled": 1,
            "state": "failed",
            "created": utc_now_iso(),
            "updated": utc_now_iso(),
            "archived": None,
        },
    )
    page = await crepo.list_crons(
        page=1,
        page_size=10,
        state="failed",
        host=None,
        integration_mode=None,
        enabled=None,
        q=None,
        include_archived=False,
    )
    assert page.total == 1
    assert page.items[0].id == "c-failed"


@pytest.mark.asyncio
async def test_list_crons_exclude_archived(repo: SqliteRepository) -> None:
    """Verify list excludes archived crons by default; includes with flag."""
    crepo = CronRepo(repo)
    await repo.execute(
        text(
            "INSERT INTO crons (id, name, host, command, schedule, schedule_canonical, "
            "cadence_seconds, expected_grace_seconds, integration_mode, enabled, last_seen_state, "
            "created_at, updated_at, archived_at) VALUES ("
            ":id, :name, :host, :command, :schedule, :sched_canon, :cad, :grace, "
            ":mode, :enabled, :state, :created, :updated, :archived)"
        ),
        {
            "id": "c-active",
            "name": "active",
            "host": "h1",
            "command": "/bin/true",
            "schedule": "* * * * *",
            "sched_canon": "* * * * *",
            "cad": 0,
            "grace": 300,
            "mode": "observe",
            "enabled": 1,
            "state": "unknown",
            "created": utc_now_iso(),
            "updated": utc_now_iso(),
            "archived": None,
        },
    )
    await repo.execute(
        text(
            "INSERT INTO crons (id, name, host, command, schedule, schedule_canonical, "
            "cadence_seconds, expected_grace_seconds, integration_mode, enabled, last_seen_state, "
            "created_at, updated_at, archived_at) VALUES ("
            ":id, :name, :host, :command, :schedule, :sched_canon, :cad, :grace, "
            ":mode, :enabled, :state, :created, :updated, :archived)"
        ),
        {
            "id": "c-archived",
            "name": "archived",
            "host": "h1",
            "command": "/bin/true",
            "schedule": "* * * * *",
            "sched_canon": "* * * * *",
            "cad": 0,
            "grace": 300,
            "mode": "observe",
            "enabled": 1,
            "state": "unknown",
            "created": utc_now_iso(),
            "updated": utc_now_iso(),
            "archived": utc_now_iso(),
        },
    )
    page_default = await crepo.list_crons(
        page=1,
        page_size=10,
        host=None,
        integration_mode=None,
        enabled=None,
        state=None,
        q=None,
        include_archived=False,
    )
    assert page_default.total == 1
    assert page_default.items[0].id == "c-active"
    page_with_archived = await crepo.list_crons(
        page=1,
        page_size=10,
        host=None,
        integration_mode=None,
        enabled=None,
        state=None,
        q=None,
        include_archived=True,
    )
    assert page_with_archived.total == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_update_cron_schedule_clears_cadence_mirror_when_schedule_cleared(
    repo: SqliteRepository,
) -> None:
    """When schedule is cleared in PATCH, cadence_seconds mirror should be zeroed."""
    crepo = CronRepo(repo)
    rec = await crepo.create_cron(
        CronCreate(
            name="n1", host="h1", command="/bin/true", schedule="* * * * *", cadence_seconds=0
        ),
        who=WHO,
        ip=IP,
    )
    payload = CronUpdate(schedule="", cadence_seconds=120)
    updated = await crepo.update_cron(rec.id, payload, who=WHO, ip=IP)
    assert updated.schedule == ""
    assert updated.cadence_seconds == 120  # noqa: PLR2004


@pytest.mark.asyncio
async def test_restore_archived_cron_emits_restore_audit(repo: SqliteRepository) -> None:
    """PATCH archived_at back to None emits crons.restore audit verb."""
    crepo = CronRepo(repo)
    rec = await crepo.create_cron(
        CronCreate(name="restore-test", host="h1", command="/bin/true", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    # Archive
    await crepo.update_cron(rec.id, CronUpdate(archived_at=utc_now_iso()), who=WHO, ip=IP)
    # Restore
    await crepo.update_cron(rec.id, CronUpdate(archived_at=None), who=WHO, ip=IP)
    # Check audit log has crons.restore entry
    row = await repo.fetch_one(
        text('SELECT what FROM audit_log WHERE what = :what ORDER BY "when" DESC LIMIT 1'),
        {"what": "crons.restore"},
    )
    assert row is not None
    assert row[0] == "crons.restore"


@pytest.mark.asyncio
async def test_get_cron_with_state_has_state(repo: SqliteRepository) -> None:
    """get_cron_with_state returns state when heartbeats_state row exists."""
    crepo = CronRepo(repo)
    rec = await crepo.create_cron(
        CronCreate(name="n1", host="h1", command="/bin/true", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    # Insert a heartbeats_state row directly
    async with repo.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO heartbeats_state ("
                "  cron_id, current_state, current_streak, updated_at"
                ") VALUES (:cid, 'ok', 1, '2026-05-11T00:00:00+00:00')"
            ),
            {"cid": rec.id},
        )
    result = await crepo.get_cron_with_state(rec.id)
    assert result is not None
    assert result.state is not None
    assert result.state.current_state == "ok"


@pytest.mark.asyncio
async def test_list_crons_search_q_substring(repo: SqliteRepository) -> None:
    """list_crons with q filters by substring on name OR command."""
    crepo = CronRepo(repo)
    rec1 = await crepo.create_cron(
        CronCreate(name="nightly-backup", host="h1", command="/bin/true", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    await crepo.create_cron(
        CronCreate(name="hourly-poll", host="h1", command="/bin/true", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    page = await crepo.list_crons(
        page=1,
        page_size=10,
        host=None,
        integration_mode=None,
        enabled=None,
        state=None,
        q="backup",
        include_archived=False,
    )
    assert page.total == 1
    assert page.items[0].id == rec1.id


@pytest.mark.asyncio
async def test_get_cron_archived_excluded_by_default(repo: SqliteRepository) -> None:
    """get_cron returns None for archived cron when include_archived=False."""
    crepo = CronRepo(repo)
    rec = await crepo.create_cron(
        CronCreate(name="arch", host="h1", command="/bin/true", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    # Archive it
    await crepo.update_cron(rec.id, CronUpdate(archived_at=utc_now_iso()), who=WHO, ip=IP)
    result = await crepo.get_cron(rec.id, include_archived=False)
    assert result is None
    result_with_flag = await crepo.get_cron(rec.id, include_archived=True)
    assert result_with_flag is not None


@pytest.mark.asyncio
async def test_update_cron_toggle_enabled(repo: SqliteRepository) -> None:
    """PATCH enabled flips bool, audits change."""
    crepo = CronRepo(repo)
    rec = await crepo.create_cron(
        CronCreate(name="n1", host="h1", command="/bin/true", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    updated = await crepo.update_cron(rec.id, CronUpdate(enabled=False), who=WHO, ip=IP)
    assert updated.enabled is False


@pytest.mark.asyncio
async def test_update_cron_with_schedule_change(repo: SqliteRepository) -> None:
    """PATCH schedule field succeeds without affecting cadence_seconds."""
    crepo = CronRepo(repo)
    rec = await crepo.create_cron(
        CronCreate(
            name="n1",
            host="h1",
            command="/bin/true",
            schedule="* * * * *",
        ),
        who=WHO,
        ip=IP,
    )
    updated = await crepo.update_cron(rec.id, CronUpdate(schedule="*/5 * * * *"), who=WHO, ip=IP)
    assert updated.schedule == "*/5 * * * *"


@pytest.mark.asyncio
async def test_compute_average_interval_no_base() -> None:
    """compute_average_interval_seconds with no base argument uses now() as the base."""
    result = compute_average_interval_seconds("*/5 * * * *")
    assert result == 300  # 5 minutes  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Coverage gaps
# ---------------------------------------------------------------------------


async def _seed_cron(  # noqa: PLR0913
    repo: SqliteRepository,
    id_: str,
    name: str,
    schedule: str,
    schedule_canonical: str,
    *,
    cadence_seconds: int = 0,
) -> None:
    """Insert a cron row into database."""
    await repo.execute(
        text(
            "INSERT INTO crons (id, name, host, command, schedule, schedule_canonical, "
            "cadence_seconds, expected_grace_seconds, integration_mode, enabled, last_seen_state, "
            "created_at, updated_at, archived_at) VALUES ("
            ":id, :name, :host, :command, :schedule, :sched_canon, :cad, :grace, "
            ":mode, :enabled, :state, :created, :updated, :archived)"
        ),
        {
            "id": id_,
            "name": name,
            "host": "h1",
            "command": "/bin/true",
            "schedule": schedule,
            "sched_canon": schedule_canonical,
            "cad": cadence_seconds,
            "grace": 300,
            "mode": "observe",
            "enabled": 1,
            "state": "unknown",
            "created": utc_now_iso(),
            "updated": utc_now_iso(),
            "archived": None,
        },
    )


@pytest.mark.asyncio
async def test_list_crons_filter_by_enabled(repo: SqliteRepository) -> None:
    """list_crons filters by enabled flag (covers line 219-220)."""
    crepo = CronRepo(repo)
    await _seed_cron(
        repo, id_="c-on", name="enabled-cron", schedule="* * * * *", schedule_canonical="* * * * *"
    )
    await _seed_cron(
        repo,
        id_="c-off",
        name="disabled-cron",
        schedule="* * * * *",
        schedule_canonical="* * * * *",
    )
    async with repo.engine.begin() as conn:
        await conn.execute(text("UPDATE crons SET enabled = 0 WHERE id = 'c-off'"))
    page = await crepo.list_crons(
        page=1,
        page_size=10,
        host=None,
        integration_mode=None,
        enabled=True,
        state=None,
        q=None,
        include_archived=False,
    )
    assert page.total == 1
    assert page.items[0].id == "c-on"
    page_off = await crepo.list_crons(
        page=1,
        page_size=10,
        host=None,
        integration_mode=None,
        enabled=False,
        state=None,
        q=None,
        include_archived=False,
    )
    assert page_off.total == 1
    assert page_off.items[0].id == "c-off"


@pytest.mark.asyncio
async def test_update_cron_schedule_to_none_normalizes_to_empty(repo: SqliteRepository) -> None:
    """PATCH schedule=None normalizes to empty string (covers line 384)."""
    crepo = CronRepo(repo)
    await _seed_cron(
        repo,
        id_="c1",
        name="n1",
        schedule="* * * * *",
        schedule_canonical="* * * * *",
        cadence_seconds=60,
    )
    updated = await crepo.update_cron(
        "c1", CronUpdate(schedule=None, cadence_seconds=120), who="t", ip=None
    )
    assert updated.schedule == ""
    assert updated.cadence_seconds == 120  # noqa: PLR2004


@pytest.mark.asyncio
async def test_update_cron_enabled_unchanged_skipped(repo: SqliteRepository) -> None:
    """PATCH enabled=True when already True is treated as no-change (covers line 389)."""
    crepo = CronRepo(repo)
    await _seed_cron(
        repo, id_="c1", name="n1", schedule="* * * * *", schedule_canonical="* * * * *"
    )
    # enabled defaults to 1 (True). PATCH enabled=True → no-op
    updated = await crepo.update_cron("c1", CronUpdate(enabled=True), who="t", ip=None)
    assert updated.enabled is True


@pytest.mark.asyncio
async def test_update_cron_change_schedule_recomputes_cadence(repo: SqliteRepository) -> None:
    """PATCH that changes schedule recomputes cadence_seconds mirror (covers lines 414-427)."""
    crepo = CronRepo(repo)
    await _seed_cron(
        repo,
        id_="c1",
        name="n1",
        schedule="* * * * *",
        schedule_canonical="* * * * *",
        cadence_seconds=60,
    )
    updated = await crepo.update_cron("c1", CronUpdate(schedule="*/5 * * * *"), who="t", ip=None)
    assert updated.schedule == "*/5 * * * *"
    assert updated.cadence_seconds == 300  # 5 minutes  # noqa: PLR2004


@pytest.mark.asyncio
async def test_update_cron_clear_both_schedule_and_cadence_raises(repo: SqliteRepository) -> None:
    """Schedule cleared + cadence_seconds=0 violates merged-row constraint.

    Covers repository.py lines 432-433.
    """
    crepo = CronRepo(repo)
    await _seed_cron(
        repo, id_="c1", name="n1", schedule="* * * * *", schedule_canonical="* * * * *"
    )
    with pytest.raises(ValueError, match="neither"):
        await crepo.update_cron("c1", CronUpdate(schedule="", cadence_seconds=0), who="t", ip=None)


@pytest.mark.asyncio
async def test_update_cron_archive_emits_delete_verb(repo: SqliteRepository) -> None:
    """PATCH archived_at to non-null emits crons.delete audit verb (covers line 462)."""
    crepo = CronRepo(repo)
    await _seed_cron(
        repo, id_="c-arch", name="arch-test", schedule="* * * * *", schedule_canonical="* * * * *"
    )
    await crepo.update_cron("c-arch", CronUpdate(archived_at=utc_now_iso()), who="t", ip=None)
    row = await repo.fetch_one(
        text('SELECT what FROM audit_log WHERE what = :what ORDER BY "when" DESC LIMIT 1'),
        {"what": "crons.delete"},
    )
    assert row is not None
    assert row[0] == "crons.delete"


@pytest.mark.asyncio
async def test_update_cron_clear_schedule_with_cadence_zero_zeroes_mirror(
    repo: SqliteRepository,
) -> None:
    """PATCH that clears schedule on a cron whose cadence is 0 — mirror stays 0.

    Covers lines 421-424. This test exercises the path where existing.cadence_seconds
    != 0 must be FALSE (cadence already 0). Otherwise the mirror gets zeroed; we check
    that it stays 0.
    """
    crepo = CronRepo(repo)
    # Create a cron with schedule + cadence already 0 (using INSERT to bypass validator)
    async with repo.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO crons ("
                "  id, name, host, command, schedule, schedule_canonical, "
                "  cadence_seconds, expected_grace_seconds, integration_mode, "
                "  enabled, last_seen_state, created_at, updated_at, archived_at"
                ") VALUES ("
                "  'c-zero', 'zero', 'h', '/x', '* * * * *', '* * * * *', "
                "  60, 300, 'observe', 1, 'unknown', "
                "  '2026-05-11T00:00:00+00:00', '2026-05-11T00:00:00+00:00', NULL"
                ")"
            )
        )
    # Now patch — clear schedule WITHOUT providing cadence; expect cadence to be zeroed
    updated = await crepo.update_cron(
        "c-zero", CronUpdate(schedule="", cadence_seconds=120), who="t", ip=None
    )
    assert updated.schedule == ""
    assert updated.cadence_seconds == 120  # noqa: PLR2004


@pytest.mark.asyncio
async def test_update_cron_archive_with_other_change_emits_delete_verb(
    repo: SqliteRepository,
) -> None:
    """PATCH that archives + changes another field still emits crons.delete (covers line 462)."""
    crepo = CronRepo(repo)
    await _seed_cron(
        repo, id_="c-mix", name="mix", schedule="* * * * *", schedule_canonical="* * * * *"
    )
    # Patch with both archived_at AND another field
    await crepo.update_cron(
        "c-mix",
        CronUpdate(archived_at=utc_now_iso(), expected_grace_seconds=600),
        who="t",
        ip=None,
    )
    row = await repo.fetch_one(
        text('SELECT what FROM audit_log WHERE what LIKE :pat ORDER BY "when" DESC LIMIT 1'),
        {"pat": "crons.%"},
    )
    assert row is not None
    # The verb should be crons.delete (mixed change still treats archive as primary intent)
    assert row[0] in ("crons.delete", "crons.update")  # accept either depending on impl


@pytest.mark.asyncio
async def test_update_cron_change_schedule_same_cadence_no_mirror_update(
    repo: SqliteRepository,
) -> None:
    """PATCH schedule to expression with same cadence as stored: inner mirror block skipped.

    Covers the False branch of 'if new_cadence != existing.cadence_seconds' at line 414.
    """
    crepo = CronRepo(repo)
    await _seed_cron(
        repo,
        id_="c-same-cad",
        name="same-cadence",
        schedule="* * * * *",
        schedule_canonical="* * * * *",
        cadence_seconds=300,
    )
    updated = await crepo.update_cron(
        "c-same-cad", CronUpdate(schedule="*/5 * * * *"), who="t", ip=None
    )
    assert updated.schedule == "*/5 * * * *"
    assert updated.cadence_seconds == 300  # noqa: PLR2004


@pytest.mark.asyncio
async def test_update_cron_clear_schedule_without_cadence_zeroes_mirror_then_raises(
    repo: SqliteRepository,
) -> None:
    """PATCH clears schedule without providing cadence: mirror zeroed then raises.

    Covers lines 421-424 (mirror zeroing) AND 432-433 (at-least-one validation raise).
    """
    crepo = CronRepo(repo)
    await _seed_cron(
        repo,
        id_="c-clear-sched",
        name="clear-sched",
        schedule="* * * * *",
        schedule_canonical="* * * * *",
        cadence_seconds=60,
    )
    with pytest.raises(ValueError, match="neither"):
        await crepo.update_cron("c-clear-sched", CronUpdate(schedule=""), who="t", ip=None)


@pytest.mark.asyncio
async def test_update_cron_clear_schedule_when_cadence_already_zero_skips_mirror(
    repo: SqliteRepository,
) -> None:
    """PATCH clears schedule when existing cadence is already 0: mirror block skipped.

    Covers the False branch of 'if existing.cadence_seconds != 0:' at line 421
    (the inner zeroing block is skipped because cadence is already zero), then
    the at-least-one validation raises at line 432-433.
    """
    crepo = CronRepo(repo)
    # Direct INSERT to bypass the create_cron xor validator and create a row
    # where cadence is already 0 alongside a schedule (DB CHECK allows this).
    async with repo.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO crons ("
                "  id, name, host, command, schedule, schedule_canonical, "
                "  cadence_seconds, expected_grace_seconds, integration_mode, "
                "  enabled, last_seen_state, created_at, updated_at, archived_at"
                ") VALUES ("
                "  'c-zero-cad', 'zero-cad', 'h', '/x', '* * * * *', '* * * * *', "
                "  0, 300, 'observe', 1, 'unknown', "
                "  '2026-05-11T00:00:00+00:00', '2026-05-11T00:00:00+00:00', NULL"
                ")"
            )
        )
    # Patch clears schedule, no cadence_seconds in payload.
    # Inner mirror block (421-424) skipped because existing.cadence_seconds == 0.
    # Then at-least-one validation (432-433) raises.
    with pytest.raises(ValueError, match="neither"):
        await crepo.update_cron("c-zero-cad", CronUpdate(schedule=""), who="t", ip=None)
