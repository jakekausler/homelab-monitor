"""Unit tests for CronRepo (no HTTP layer)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.cron.fingerprint import compute_fingerprint
from homelab_monitor.kernel.cron.repository import CronRepo
from homelab_monitor.kernel.cron.schemas import CronUpdate
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
# UPDATE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_changed_fields_only_writes_one_audit_row(repo: SqliteRepository) -> None:
    cron_repo = CronRepo(repo)
    fp = await _seed_cron(repo, name="x")
    # _seed_cron bypasses audit, so count starts at 0
    assert await _audit_count(repo) == 0
    await cron_repo.update_cron(fp, CronUpdate(expected_grace_seconds=600), who=WHO, ip=IP)
    # one from update
    assert await _audit_count(repo) == 1


@pytest.mark.asyncio
async def test_update_empty_diff_writes_no_audit(repo: SqliteRepository) -> None:
    cron_repo = CronRepo(repo)
    fp = await _seed_cron(repo, name="x")
    assert await _audit_count(repo) == 0
    # PATCH same value → no diff → no audit
    await cron_repo.update_cron(fp, CronUpdate(expected_grace_seconds=300), who=WHO, ip=IP)
    assert await _audit_count(repo) == 0


@pytest.mark.asyncio
async def test_update_cron_hide_emits_crons_hide_verb(
    repo: SqliteRepository,
) -> None:
    cron_repo = CronRepo(repo)
    fp = await _seed_cron(repo, name="x")
    await cron_repo.update_cron(
        fp,
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
    fp = await _seed_cron(repo, name="x")
    await cron_repo.update_cron(
        fp,
        CronUpdate(hidden_at=utc_now_iso()),
        who=WHO,
        ip=IP,
    )
    await cron_repo.update_cron(
        fp,
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
    fp = await _seed_cron(repo, name="x")
    await cron_repo.soft_delete_cron(fp, who=WHO, ip=IP)
    row = await repo.fetch_one(
        text("SELECT hidden_at FROM crons WHERE fingerprint = :fp"),
        {"fp": fp},
    )
    assert row is not None
    assert row[0] is not None
    audit = await repo.fetch_one(text('SELECT what FROM audit_log ORDER BY "when" DESC LIMIT 1'))
    assert audit is not None
    assert audit[0] == "crons.hide"


@pytest.mark.asyncio
async def test_soft_delete_already_hidden_raises(repo: SqliteRepository) -> None:
    cron_repo = CronRepo(repo)
    fp = await _seed_cron(repo, name="x")
    await cron_repo.soft_delete_cron(fp, who=WHO, ip=IP)
    with pytest.raises(LookupError):
        await cron_repo.soft_delete_cron(fp, who=WHO, ip=IP)


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_pagination_math(repo: SqliteRepository) -> None:
    cron_repo = CronRepo(repo)
    for i in range(7):
        await _seed_cron(repo, name=f"c{i:02d}", command=f"/x{i}")
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
    await _seed_cron(repo, name="a", host="h1")
    await _seed_cron(repo, name="b", host="h2")
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
    await _seed_cron(repo, name="alpha", command="/grep_me")
    await _seed_cron(repo, name="grep_me_too", command="/y")
    await _seed_cron(repo, name="other", command="/z")
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
    fp = await _seed_cron(repo, name="arch")
    # Hide it
    await crepo.update_cron(fp, CronUpdate(hidden_at=utc_now_iso()), who=WHO, ip=IP)
    result = await crepo.get_cron(fp, include_hidden=False)
    assert result is None
    result_with_flag = await crepo.get_cron(fp, include_hidden=True)
    assert result_with_flag is not None


@pytest.mark.asyncio
async def test_get_cron_with_state_has_state(repo: SqliteRepository) -> None:
    """get_cron_with_state returns state when heartbeats_state row exists."""
    crepo = CronRepo(repo)
    fp = await _seed_cron(repo, name="n1")
    # Insert a heartbeats_state row directly
    async with repo.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO heartbeats_state ("
                "  cron_fingerprint, current_state, current_streak, updated_at"
                ") VALUES (:cfp, 'ok', 1, '2026-05-11T00:00:00+00:00')"
            ),
            {"cfp": fp},
        )
    result = await crepo.get_cron_with_state(fp)
    assert result is not None
    assert result.state is not None
    assert result.state.current_state == "ok"


@pytest.mark.asyncio
async def test_list_crons_search_q_substring(repo: SqliteRepository) -> None:
    """list_crons with q filters by substring on name OR command."""
    crepo = CronRepo(repo)
    fp1 = await _seed_cron(repo, name="nightly-backup", command="/bin/true-nightly")
    await _seed_cron(repo, name="hourly-poll", command="/bin/true-hourly")
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
    assert page.items[0].fingerprint == fp1


@pytest.mark.asyncio
async def test_update_cron_toggle_enabled(repo: SqliteRepository) -> None:
    """PATCH enabled flips bool, audits change."""
    crepo = CronRepo(repo)
    fp = await _seed_cron(repo, name="n1")
    updated = await crepo.update_cron(fp, CronUpdate(enabled=False), who=WHO, ip=IP)
    assert updated.enabled is False


@pytest.mark.asyncio
async def test_list_crons_filter_by_enabled(repo: SqliteRepository) -> None:
    """list_crons with enabled=True/False filters correctly (lines 217-218)."""
    crepo = CronRepo(repo)
    # Create two crons: one enabled, one disabled
    fp_enabled = await _seed_cron(repo, name="enabled-cron", command="/bin/true-enabled")
    fp_disabled = await _seed_cron(repo, name="disabled-cron", command="/bin/true-disabled")
    # Disable the second cron
    await crepo.update_cron(fp_disabled, CronUpdate(enabled=False), who=WHO, ip=IP)

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
    assert page_enabled.items[0].fingerprint == fp_enabled

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
    assert page_disabled.items[0].fingerprint == fp_disabled


@pytest.mark.asyncio
async def test_update_cron_enabled_same_value_is_no_op(repo: SqliteRepository) -> None:
    """PATCH enabled with same value is no-op, no audit row created (line 385)."""
    crepo = CronRepo(repo)
    fp = await _seed_cron(repo, name="n2")
    # Get audit count before PATCH
    audit_before = await _audit_count(repo)

    # PATCH enabled=True when it's already True (no-op)
    updated = await crepo.update_cron(fp, CronUpdate(enabled=True), who=WHO, ip=IP)
    assert updated.enabled is True
    assert updated.fingerprint == fp

    # Audit count should not increase (no audit row for no-op)
    audit_after = await _audit_count(repo)
    assert audit_after == audit_before


@pytest.mark.asyncio
async def test_update_cron_hide_and_rename_uses_hide_verb(repo: SqliteRepository) -> None:
    """PATCH hidden_at AND another field together uses hide/unhide verb (line 413)."""
    crepo = CronRepo(repo)
    fp = await _seed_cron(repo, name="n3")
    # Get audit count before PATCH
    audit_before = await _audit_count(repo)

    # PATCH both hidden_at and name simultaneously
    now = utc_now_iso()
    updated = await crepo.update_cron(
        fp, CronUpdate(hidden_at=now, name="n3-renamed"), who=WHO, ip=IP
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
