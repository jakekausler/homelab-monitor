"""Unit tests for CronRepo (no HTTP layer)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.cron.fingerprint import compute_fingerprint
from homelab_monitor.kernel.cron.repository import CronRepo
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


async def _seed_cron(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    name: str,
    host: str = "h1",
    command: str | None = None,
    schedule: str = "* * * * *",
    schedule_canonical: str | None = "* * * * *",
    cadence_seconds: int = 0,
    source_path: str | None = "/etc/crontab",
    last_seen_state: str = "unknown",
    hidden_at: str | None = None,
    fingerprint: str | None = None,
) -> str:
    """Insert a cron with a computed fingerprint (or the caller-supplied one).

    Returns the fingerprint so tests can use it for follow-up assertions.
    """
    command = command if command is not None else f"/bin/true-{name}"
    fp = fingerprint or compute_fingerprint(
        host=host, source_path=source_path, schedule=schedule, command=command
    )
    await repo.execute(
        text(
            "INSERT INTO crons (fingerprint, name, host, command, schedule, "
            "schedule_canonical, cadence_seconds, expected_grace_seconds, "
            "enabled, last_seen_state, created_at, updated_at, hidden_at, "
            "source_path, wrapper_installed_at) VALUES ("
            ":fp, :name, :host, :command, :schedule, :sched_canon, :cad, "
            ":grace, :enabled, :state, :created, :updated, :hidden, :sp, :wia)"
        ),
        {
            "fp": fp,
            "name": name,
            "host": host,
            "command": command,
            "schedule": schedule if schedule else None,
            "sched_canon": schedule_canonical,
            "cad": cadence_seconds,
            "grace": 300,
            "enabled": 1,
            "state": last_seen_state,
            "created": utc_now_iso(),
            "updated": utc_now_iso(),
            "hidden": hidden_at,
            "sp": source_path,
            "wia": None,
        },
    )
    return fp


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


@pytest.mark.asyncio
async def test_create_cron_assigns_fingerprint_pk(repo: SqliteRepository) -> None:
    """The PK is the SHA256 fingerprint of (host, source_path, schedule, command)."""
    cron_repo = CronRepo(repo)
    rec = await cron_repo.create_cron(
        CronCreate(
            name="x",
            host="h",
            command="/x",
            schedule="* * * * *",
            source_path="/etc/crontab",
        ),
        who=WHO,
        ip=IP,
    )
    expected = compute_fingerprint("h", "/etc/crontab", "* * * * *", "/x")
    assert rec.fingerprint == expected


@pytest.mark.asyncio
async def test_create_cron_null_source_path_round_trip(
    repo: SqliteRepository,
) -> None:
    cron_repo = CronRepo(repo)
    rec = await cron_repo.create_cron(
        CronCreate(
            name="remote",
            host="rh",
            command="/x",
            schedule="* * * * *",
            source_path=None,
        ),
        who=WHO,
        ip=IP,
    )
    assert rec.source_path is None


@pytest.mark.asyncio
async def test_create_cron_duplicate_fingerprint_raises(
    repo: SqliteRepository,
) -> None:
    cron_repo = CronRepo(repo)
    payload = CronCreate(
        name="x",
        host="h",
        command="/x",
        schedule="* * * * *",
        source_path="/etc/crontab",
    )
    await cron_repo.create_cron(payload, who=WHO, ip=IP)
    with pytest.raises(ValueError, match="already exists"):
        await cron_repo.create_cron(payload, who=WHO, ip=IP)


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
    await cron_repo.update_cron(
        rec.fingerprint, CronUpdate(expected_grace_seconds=600), who=WHO, ip=IP
    )
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
    await cron_repo.update_cron(
        rec.fingerprint, CronUpdate(expected_grace_seconds=300), who=WHO, ip=IP
    )
    assert await _audit_count(repo) == 1


@pytest.mark.asyncio
async def test_update_cron_hide_emits_crons_hide_verb(
    repo: SqliteRepository,
) -> None:
    cron_repo = CronRepo(repo)
    rec = await cron_repo.create_cron(
        CronCreate(name="x", host="h", command="/x", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    await cron_repo.update_cron(
        rec.fingerprint,
        CronUpdate(hidden_at=utc_now_iso()),
        who=WHO,
        ip=IP,
    )
    row = await repo.fetch_one(text('SELECT what FROM audit_log ORDER BY "when" DESC LIMIT 1'))
    assert row is not None
    assert row[0] == "crons.hide"


@pytest.mark.asyncio
async def test_update_cron_unhide_emits_crons_unhide_verb(
    repo: SqliteRepository,
) -> None:
    cron_repo = CronRepo(repo)
    rec = await cron_repo.create_cron(
        CronCreate(name="x", host="h", command="/x", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    await cron_repo.update_cron(
        rec.fingerprint,
        CronUpdate(hidden_at=utc_now_iso()),
        who=WHO,
        ip=IP,
    )
    await cron_repo.update_cron(
        rec.fingerprint,
        CronUpdate(hidden_at=None),
        who=WHO,
        ip=IP,
    )
    row = await repo.fetch_one(text('SELECT what FROM audit_log ORDER BY "when" DESC LIMIT 1'))
    assert row is not None
    assert row[0] == "crons.unhide"


@pytest.mark.asyncio
async def test_update_unknown_id_raises_lookup_error(repo: SqliteRepository) -> None:
    cron_repo = CronRepo(repo)
    with pytest.raises(LookupError):
        await cron_repo.update_cron(
            "no-such" + "a" * 57, CronUpdate(expected_grace_seconds=600), who=WHO, ip=IP
        )


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_soft_delete_hides_and_audits_hide_verb(repo: SqliteRepository) -> None:
    cron_repo = CronRepo(repo)
    rec = await cron_repo.create_cron(
        CronCreate(name="x", host="h", command="/x", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    await cron_repo.soft_delete_cron(rec.fingerprint, who=WHO, ip=IP)
    row = await repo.fetch_one(
        text("SELECT hidden_at FROM crons WHERE fingerprint = :fp"),
        {"fp": rec.fingerprint},
    )
    assert row is not None
    assert row[0] is not None
    audit = await repo.fetch_one(text('SELECT what FROM audit_log ORDER BY "when" DESC LIMIT 1'))
    assert audit is not None
    assert audit[0] == "crons.hide"


@pytest.mark.asyncio
async def test_soft_delete_already_hidden_raises(repo: SqliteRepository) -> None:
    cron_repo = CronRepo(repo)
    rec = await cron_repo.create_cron(
        CronCreate(name="x", host="h", command="/x", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    await cron_repo.soft_delete_cron(rec.fingerprint, who=WHO, ip=IP)
    with pytest.raises(LookupError):
        await cron_repo.soft_delete_cron(rec.fingerprint, who=WHO, ip=IP)


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
        enabled=None,
        state=None,
        q=None,
        include_hidden=False,
    )
    assert page.total == 7  # noqa: PLR2004
    assert len(page.items) == 3  # noqa: PLR2004
    # ORDER BY name ASC: page 2 of size 3 yields names c03, c04, c05
    assert [it.name for it in page.items] == ["c03", "c04", "c05"]


@pytest.mark.asyncio
async def test_list_filter_combinations(repo: SqliteRepository) -> None:
    cron_repo = CronRepo(repo)
    await cron_repo.create_cron(
        CronCreate(name="a", host="h1", command="/x", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    await cron_repo.create_cron(
        CronCreate(name="b", host="h2", command="/x", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    page = await cron_repo.list_crons(
        page=1,
        page_size=10,
        host="h2",
        enabled=None,
        state=None,
        q=None,
        include_hidden=False,
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
        enabled=None,
        state=None,
        q="grep_me",
        include_hidden=False,
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
    await _seed_cron(repo, name="ok-cron", host="h1", command="/bin/true", last_seen_state="ok")
    fp_failed = await _seed_cron(
        repo,
        name="failed-cron",
        host="h1",
        command="/bin/false",
        last_seen_state="failed",
    )
    page = await crepo.list_crons(
        page=1,
        page_size=10,
        state="failed",
        host=None,
        enabled=None,
        q=None,
        include_hidden=False,
    )
    assert page.total == 1
    assert page.items[0].fingerprint == fp_failed


@pytest.mark.asyncio
async def test_list_crons_exclude_hidden(repo: SqliteRepository) -> None:
    """Verify list excludes hidden crons by default; includes with flag."""
    crepo = CronRepo(repo)
    # Vary source_path between the two crons so their fingerprints differ
    # (host + source_path + schedule + command form the fingerprint tuple).
    fp_active = await _seed_cron(
        repo, name="active", host="h1", command="/bin/true", source_path="/etc/crontab"
    )
    await _seed_cron(
        repo,
        name="hidden",
        host="h1",
        command="/bin/true",
        source_path="/etc/cron.d/hidden",
        hidden_at=utc_now_iso(),
    )
    page_default = await crepo.list_crons(
        page=1,
        page_size=10,
        host=None,
        enabled=None,
        state=None,
        q=None,
        include_hidden=False,
    )
    assert page_default.total == 1
    assert page_default.items[0].fingerprint == fp_active
    page_with_hidden = await crepo.list_crons(
        page=1,
        page_size=10,
        host=None,
        enabled=None,
        state=None,
        q=None,
        include_hidden=True,
    )
    assert page_with_hidden.total == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_cron_hidden_excluded_by_default(repo: SqliteRepository) -> None:
    """get_cron returns None for hidden cron when include_hidden=False."""
    crepo = CronRepo(repo)
    rec = await crepo.create_cron(
        CronCreate(name="arch", host="h1", command="/bin/true", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    # Hide it
    await crepo.update_cron(rec.fingerprint, CronUpdate(hidden_at=utc_now_iso()), who=WHO, ip=IP)
    result = await crepo.get_cron(rec.fingerprint, include_hidden=False)
    assert result is None
    result_with_flag = await crepo.get_cron(rec.fingerprint, include_hidden=True)
    assert result_with_flag is not None


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
                "  cron_fingerprint, current_state, current_streak, updated_at"
                ") VALUES (:cfp, 'ok', 1, '2026-05-11T00:00:00+00:00')"
            ),
            {"cfp": rec.fingerprint},
        )
    result = await crepo.get_cron_with_state(rec.fingerprint)
    assert result is not None
    assert result.state is not None
    assert result.state.current_state == "ok"


@pytest.mark.asyncio
async def test_list_crons_search_q_substring(repo: SqliteRepository) -> None:
    """list_crons with q filters by substring on name OR command."""
    crepo = CronRepo(repo)
    rec1 = await crepo.create_cron(
        CronCreate(
            name="nightly-backup", host="h1", command="/bin/true-nightly", schedule="* * * * *"
        ),
        who=WHO,
        ip=IP,
    )
    await crepo.create_cron(
        CronCreate(name="hourly-poll", host="h1", command="/bin/true-hourly", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    page = await crepo.list_crons(
        page=1,
        page_size=10,
        host=None,
        enabled=None,
        state=None,
        q="backup",
        include_hidden=False,
    )
    assert page.total == 1
    assert page.items[0].fingerprint == rec1.fingerprint


@pytest.mark.asyncio
async def test_update_cron_toggle_enabled(repo: SqliteRepository) -> None:
    """PATCH enabled flips bool, audits change."""
    crepo = CronRepo(repo)
    rec = await crepo.create_cron(
        CronCreate(name="n1", host="h1", command="/bin/true", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    updated = await crepo.update_cron(rec.fingerprint, CronUpdate(enabled=False), who=WHO, ip=IP)
    assert updated.enabled is False


@pytest.mark.asyncio
async def test_list_crons_filter_by_enabled(repo: SqliteRepository) -> None:
    """list_crons with enabled=True/False filters correctly (lines 217-218)."""
    crepo = CronRepo(repo)
    # Create two crons: one enabled, one disabled
    rec_enabled = await crepo.create_cron(
        CronCreate(
            name="enabled-cron",
            host="h1",
            command="/bin/true-enabled",
            schedule="* * * * *",
        ),
        who=WHO,
        ip=IP,
    )
    rec_disabled = await crepo.create_cron(
        CronCreate(
            name="disabled-cron",
            host="h1",
            command="/bin/true-disabled",
            schedule="* * * * *",
        ),
        who=WHO,
        ip=IP,
    )
    # Disable the second cron
    await crepo.update_cron(rec_disabled.fingerprint, CronUpdate(enabled=False), who=WHO, ip=IP)

    # Query with enabled=True should return only the enabled cron
    page_enabled = await crepo.list_crons(
        page=1,
        page_size=10,
        host=None,
        enabled=True,
        state=None,
        q=None,
        include_hidden=False,
    )
    assert page_enabled.total == 1
    assert page_enabled.items[0].fingerprint == rec_enabled.fingerprint

    # Query with enabled=False should return only the disabled cron
    page_disabled = await crepo.list_crons(
        page=1,
        page_size=10,
        host=None,
        enabled=False,
        state=None,
        q=None,
        include_hidden=False,
    )
    assert page_disabled.total == 1
    assert page_disabled.items[0].fingerprint == rec_disabled.fingerprint


@pytest.mark.asyncio
async def test_update_cron_enabled_same_value_is_no_op(repo: SqliteRepository) -> None:
    """PATCH enabled with same value is no-op, no audit row created (line 385)."""
    crepo = CronRepo(repo)
    rec = await crepo.create_cron(
        CronCreate(name="n2", host="h1", command="/bin/true-n2", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    # Get audit count before PATCH
    audit_before = await _audit_count(repo)

    # PATCH enabled=True when it's already True (no-op)
    updated = await crepo.update_cron(rec.fingerprint, CronUpdate(enabled=True), who=WHO, ip=IP)
    assert updated.enabled is True
    assert updated.fingerprint == rec.fingerprint

    # Audit count should not increase (no audit row for no-op)
    audit_after = await _audit_count(repo)
    assert audit_after == audit_before


@pytest.mark.asyncio
async def test_update_cron_hide_and_rename_uses_hide_verb(repo: SqliteRepository) -> None:
    """PATCH hidden_at AND another field together uses hide/unhide verb (line 413)."""
    crepo = CronRepo(repo)
    rec = await crepo.create_cron(
        CronCreate(name="n3", host="h1", command="/bin/true-n3", schedule="* * * * *"),
        who=WHO,
        ip=IP,
    )
    # Get audit count before PATCH
    audit_before = await _audit_count(repo)

    # PATCH both hidden_at and name simultaneously
    now = utc_now_iso()
    updated = await crepo.update_cron(
        rec.fingerprint, CronUpdate(hidden_at=now, name="n3-renamed"), who=WHO, ip=IP
    )
    assert updated.hidden_at == now
    assert updated.name == "n3-renamed"

    # Verify that an audit row was created with the hide verb
    # (line 413 selects "crons.hide" when went_to_hidden=True and other_fields_changed=True)
    audit_after = await _audit_count(repo)
    assert audit_after == audit_before + 1

    # Verify the audit row has verb "crons.hide" (not "crons.update")
    audit_row = await repo.fetch_one(
        text("SELECT what FROM audit_log WHERE what = 'crons.hide' ORDER BY \"when\" DESC LIMIT 1")
    )
    assert audit_row is not None
    assert audit_row[0] == "crons.hide"
