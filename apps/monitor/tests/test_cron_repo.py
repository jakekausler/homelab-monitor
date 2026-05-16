"""Unit tests for CronRepo (no HTTP layer)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.cron.fingerprint import compute_fingerprint
from homelab_monitor.kernel.cron.repository import CronRepo
from homelab_monitor.kernel.cron.schemas import CronUpdate, RegisterCronBody
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

WHO = "test-user"
IP = "127.0.0.1"
DEFAULT_EXPECTED_GRACE_SECONDS = 300  # matches kernel/cron/repository.py register_cron default


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
            "source_path, wrapper_last_seen_at, last_discovered_at) VALUES ("
            ":fp, :name, :host, :command, :schedule, :sched_canon, :cad, "
            ":grace, :enabled, :state, :created, :updated, :hidden, :sp, :wlsa, :ldis)"
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
            "wlsa": None,
            "ldis": None,
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


# ---------------------------------------------------------------------------
# REGISTER_CRON
# ---------------------------------------------------------------------------

_REG_BODY = RegisterCronBody(
    host="reg-host",
    source_path="/etc/crontab",
    schedule="*/5 * * * *",
    command="/usr/bin/backup.sh",
    wrapper=False,
)
_REG_FP = compute_fingerprint(
    host="reg-host",
    source_path="/etc/crontab",
    schedule="*/5 * * * *",
    command="/usr/bin/backup.sh",
)


@pytest.mark.asyncio
async def test_register_cron_creates_returns_tuple_true(repo: SqliteRepository) -> None:
    """First call on a new fingerprint returns (CronRecord, True) and writes one audit row."""
    crepo = CronRepo(repo)
    record, created = await crepo.register_cron(
        _REG_BODY, url_fingerprint=_REG_FP, who="testactor", ip="1.2.3.4"
    )

    assert created is True
    assert record.fingerprint == _REG_FP
    assert record.host == "reg-host"
    assert record.command == "/usr/bin/backup.sh"
    assert record.expected_grace_seconds == DEFAULT_EXPECTED_GRACE_SECONDS
    assert record.enabled is True
    assert record.hidden_at is None
    assert record.wrapper_last_seen_at is None  # wrapper=False

    # DB row present
    db_row = await repo.fetch_one(
        text("SELECT fingerprint FROM crons WHERE fingerprint = :fp"),
        {"fp": _REG_FP},
    )
    assert db_row is not None

    # Exactly one audit row for crons.register with correct attribution and shape
    audit_row = await repo.fetch_one(
        text(
            "SELECT who, ip, before_json, after_json FROM audit_log "
            "WHERE what = 'crons.register' ORDER BY \"when\" DESC LIMIT 1"
        )
    )
    assert audit_row is not None
    assert audit_row[0] == "testactor"
    assert audit_row[1] == "1.2.3.4"
    assert audit_row[2] is None  # before_json IS NULL on create
    after = json.loads(audit_row[3])
    assert after["fingerprint"] == _REG_FP


@pytest.mark.asyncio
async def test_register_cron_idempotent_returns_tuple_false_no_audit(
    repo: SqliteRepository,
) -> None:
    """Second call with wrapper=False returns (CronRecord, False) and writes NO new audit row."""
    crepo = CronRepo(repo)

    body = RegisterCronBody(
        host="reg-host",
        source_path="/etc/crontab",
        schedule="*/5 * * * *",
        command="/usr/bin/backup.sh",
        wrapper=False,
    )
    fp = compute_fingerprint(
        host="reg-host",
        source_path="/etc/crontab",
        schedule="*/5 * * * *",
        command="/usr/bin/backup.sh",
    )

    _, created_first = await crepo.register_cron(body, url_fingerprint=fp, who="testactor", ip=None)
    assert created_first is True

    # Audit count after first call: 1
    audit_row = await repo.fetch_one(
        text("SELECT COUNT(*) FROM audit_log WHERE what = 'crons.register'")
    )
    assert audit_row is not None
    assert int(audit_row[0]) == 1

    # Second call: same body, wrapper=False
    _, created_second = await crepo.register_cron(
        body, url_fingerprint=fp, who="testactor", ip=None
    )
    assert created_second is False

    # Audit count must still be exactly 1 (no new row written)
    audit_row2 = await repo.fetch_one(
        text("SELECT COUNT(*) FROM audit_log WHERE what = 'crons.register'")
    )
    assert audit_row2 is not None
    assert int(audit_row2[0]) == 1


@pytest.mark.asyncio
async def test_register_cron_refresh_emits_audit(repo: SqliteRepository) -> None:
    """Two wrapper=True calls: first creates, second refreshes wrapper_last_seen_at with audit."""
    crepo = CronRepo(repo)

    body = RegisterCronBody(
        host="wrap-host",
        source_path="/etc/crontab",
        schedule="0 * * * *",
        command="/usr/bin/wrap.sh",
        wrapper=True,
    )
    fp = compute_fingerprint(
        host="wrap-host",
        source_path="/etc/crontab",
        schedule="0 * * * *",
        command="/usr/bin/wrap.sh",
    )

    rec1, created1 = await crepo.register_cron(body, url_fingerprint=fp, who="testactor", ip=None)
    assert created1 is True
    assert rec1.wrapper_last_seen_at is not None
    ts1 = rec1.wrapper_last_seen_at

    rec2, created2 = await crepo.register_cron(body, url_fingerprint=fp, who="testactor", ip=None)
    assert created2 is False
    assert rec2.wrapper_last_seen_at is not None
    ts2 = rec2.wrapper_last_seen_at
    assert ts2 >= ts1

    # Two audit rows: creation + refresh
    audit_count_row = await repo.fetch_one(
        text("SELECT COUNT(*) FROM audit_log WHERE what = 'crons.register'")
    )
    assert audit_count_row is not None
    assert int(audit_count_row[0]) == 2  # noqa: PLR2004

    # Second audit row: before_json has old ts, after_json has new ts
    second_audit = await repo.fetch_one(
        text(
            "SELECT before_json, after_json FROM audit_log "
            "WHERE what = 'crons.register' ORDER BY \"when\" DESC LIMIT 1"
        )
    )
    assert second_audit is not None
    before = json.loads(second_audit[0])
    after = json.loads(second_audit[1])
    assert before["wrapper_last_seen_at"] == ts1
    assert after["wrapper_last_seen_at"] == ts2


@pytest.mark.asyncio
async def test_register_cron_first_wrapper_install_on_existing_emits_audit(
    repo: SqliteRepository,
) -> None:
    """Existing row (wrapper_last_seen_at=NULL) + wrapper=True call writes audit with null→ts."""
    crepo = CronRepo(repo)

    # Manually seed a row with wrapper_last_seen_at=NULL (simulates discovery path)
    fp = await _seed_cron(
        repo,
        name="discovered",
        host="disc-host",
        command="/usr/bin/discovered.sh",
        schedule="30 6 * * *",
        source_path="/etc/cron.d/discovered",
    )
    # Confirm wrapper_last_seen_at is NULL
    pre_row = await repo.fetch_one(
        text("SELECT wrapper_last_seen_at FROM crons WHERE fingerprint = :fp"),
        {"fp": fp},
    )
    assert pre_row is not None
    assert pre_row[0] is None

    # Construct matching body and call register_cron with wrapper=True
    body = RegisterCronBody(
        host="disc-host",
        source_path="/etc/cron.d/discovered",
        schedule="30 6 * * *",
        command="/usr/bin/discovered.sh",
        wrapper=True,
    )
    record, created = await crepo.register_cron(body, url_fingerprint=fp, who="testactor", ip=None)

    assert created is False  # row already existed
    assert record.wrapper_last_seen_at is not None

    # Exactly one audit row for this call
    audit_row = await repo.fetch_one(
        text(
            "SELECT before_json, after_json FROM audit_log "
            "WHERE what = 'crons.register' ORDER BY \"when\" DESC LIMIT 1"
        )
    )
    assert audit_row is not None
    before = json.loads(audit_row[0])
    after = json.loads(audit_row[1])
    assert before["wrapper_last_seen_at"] is None
    assert after["wrapper_last_seen_at"] == record.wrapper_last_seen_at


# ---------------------------------------------------------------------------
# MATCH_BY_LOG_KEY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_match_by_log_key_returns_matching_crons(repo: SqliteRepository) -> None:
    """match_by_log_key returns crons with the given (host, log_match_key)."""
    cron_repo = CronRepo(repo)
    key = "/usr/bin/backup.sh"
    await repo.execute(
        text(
            "INSERT INTO crons (fingerprint, name, host, command, schedule, "
            "schedule_canonical, cadence_seconds, expected_grace_seconds, "
            "enabled, last_seen_state, created_at, updated_at, hidden_at, "
            "source_path, wrapper_last_seen_at, last_discovered_at, log_match_key) VALUES ("
            ":fp, :name, :host, :cmd, :sched, :sched_canon, :cad, :grace, "
            ":enabled, :state, :created, :updated, :hidden, :sp, :wlsa, :ldis, :lmk)"
        ),
        {
            "fp": "fp-lmk-match",
            "name": "backup",
            "host": "h1",
            "cmd": "/usr/bin/backup.sh",
            "sched": "0 4 * * *",
            "sched_canon": "0 4 * * *",
            "cad": 86400,
            "grace": 300,
            "enabled": 1,
            "state": "unknown",
            "created": utc_now_iso(),
            "updated": utc_now_iso(),
            "hidden": None,
            "sp": "/etc/crontab",
            "wlsa": None,
            "ldis": None,
            "lmk": key,
        },
    )
    results = await cron_repo.match_by_log_key("h1", key)
    assert len(results) == 1
    assert results[0].fingerprint == "fp-lmk-match"
    assert results[0].log_match_key == key


@pytest.mark.asyncio
async def test_match_by_log_key_excludes_soft_deleted(repo: SqliteRepository) -> None:
    """match_by_log_key does NOT return soft-deleted crons."""
    cron_repo = CronRepo(repo)
    key = "/usr/bin/soft-deleted.sh"
    now = utc_now_iso()
    await repo.execute(
        text(
            "INSERT INTO crons (fingerprint, name, host, command, schedule, "
            "schedule_canonical, cadence_seconds, expected_grace_seconds, "
            "enabled, last_seen_state, created_at, updated_at, hidden_at, "
            "source_path, wrapper_last_seen_at, last_discovered_at, "
            "soft_deleted_at, log_match_key) VALUES ("
            ":fp, :name, :host, :cmd, :sched, :sched_canon, :cad, :grace, "
            ":enabled, :state, :created, :updated, :hidden, :sp, :wlsa, :ldis, "
            ":sda, :lmk)"
        ),
        {
            "fp": "fp-lmk-softdel",
            "name": "soft-deleted",
            "host": "h1",
            "cmd": "/usr/bin/soft-deleted.sh",
            "sched": "0 5 * * *",
            "sched_canon": "0 5 * * *",
            "cad": 86400,
            "grace": 300,
            "enabled": 1,
            "state": "unknown",
            "created": now,
            "updated": now,
            "hidden": None,
            "sp": "/etc/crontab",
            "wlsa": None,
            "ldis": None,
            "sda": now,
            "lmk": key,
        },
    )
    results = await cron_repo.match_by_log_key("h1", key)
    assert results == []


@pytest.mark.asyncio
async def test_match_by_log_key_includes_hidden(repo: SqliteRepository) -> None:
    """match_by_log_key includes hidden crons (hidden = display suppression only)."""
    cron_repo = CronRepo(repo)
    key = "/usr/bin/hidden.sh"
    now = utc_now_iso()
    await repo.execute(
        text(
            "INSERT INTO crons (fingerprint, name, host, command, schedule, "
            "schedule_canonical, cadence_seconds, expected_grace_seconds, "
            "enabled, last_seen_state, created_at, updated_at, hidden_at, "
            "source_path, wrapper_last_seen_at, last_discovered_at, log_match_key) VALUES ("
            ":fp, :name, :host, :cmd, :sched, :sched_canon, :cad, :grace, "
            ":enabled, :state, :created, :updated, :hidden, :sp, :wlsa, :ldis, :lmk)"
        ),
        {
            "fp": "fp-lmk-hidden",
            "name": "hidden",
            "host": "h1",
            "cmd": "/usr/bin/hidden.sh",
            "sched": "0 6 * * *",
            "sched_canon": "0 6 * * *",
            "cad": 86400,
            "grace": 300,
            "enabled": 1,
            "state": "unknown",
            "created": now,
            "updated": now,
            "hidden": now,
            "sp": "/etc/crontab",
            "wlsa": None,
            "ldis": None,
            "lmk": key,
        },
    )
    results = await cron_repo.match_by_log_key("h1", key)
    assert len(results) == 1
    assert results[0].fingerprint == "fp-lmk-hidden"
    assert results[0].hidden_at is not None


@pytest.mark.asyncio
async def test_match_by_log_key_no_match_returns_empty(repo: SqliteRepository) -> None:
    """match_by_log_key returns an empty list when no cron matches."""
    cron_repo = CronRepo(repo)
    results = await cron_repo.match_by_log_key("h1", "/nonexistent/command.sh")
    assert results == []


@pytest.mark.asyncio
async def test_match_by_log_key_empty_key_returns_empty(repo: SqliteRepository) -> None:
    """match_by_log_key returns [] immediately for empty / whitespace-only keys."""
    cron_repo = CronRepo(repo)
    key = "/usr/bin/guard-check.sh"
    # Seed a real cron so the guard is proven to short-circuit rather than
    # simply finding no rows.
    await repo.execute(
        text(
            "INSERT INTO crons (fingerprint, name, host, command, schedule, "
            "schedule_canonical, cadence_seconds, expected_grace_seconds, "
            "enabled, last_seen_state, created_at, updated_at, hidden_at, "
            "source_path, wrapper_last_seen_at, last_discovered_at, log_match_key) VALUES ("
            ":fp, :name, :host, :cmd, :sched, :sched_canon, :cad, :grace, "
            ":enabled, :state, :created, :updated, :hidden, :sp, :wlsa, :ldis, :lmk)"
        ),
        {
            "fp": "fp-lmk-guard",
            "name": "guard-check",
            "host": "h1",
            "cmd": key,
            "sched": "0 7 * * *",
            "sched_canon": "0 7 * * *",
            "cad": 86400,
            "grace": 300,
            "enabled": 1,
            "state": "unknown",
            "created": utc_now_iso(),
            "updated": utc_now_iso(),
            "hidden": None,
            "sp": "/etc/crontab",
            "wlsa": None,
            "ldis": None,
            "lmk": key,
        },
    )
    # Empty string — early-return guard fires.
    assert await cron_repo.match_by_log_key("h1", "") == []
    # Whitespace-only — .strip() branch is exercised.
    assert await cron_repo.match_by_log_key("h1", "   ") == []


# ---------------------------------------------------------------------------
# TRY_CLAIM_CURSOR
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_try_claim_cursor_first_claim_returns_true(repo: SqliteRepository) -> None:
    """try_claim_cursor returns True when the cursor is newly claimed."""
    cron_repo = CronRepo(repo)
    result = await cron_repo.try_claim_cursor("cursor-abc-001", utc_now_iso())
    assert result is True


@pytest.mark.asyncio
async def test_try_claim_cursor_replay_returns_false(repo: SqliteRepository) -> None:
    """try_claim_cursor returns False when the cursor was already claimed (replay)."""
    cron_repo = CronRepo(repo)
    now = utc_now_iso()
    first = await cron_repo.try_claim_cursor("cursor-abc-002", now)
    assert first is True
    second = await cron_repo.try_claim_cursor("cursor-abc-002", now)
    assert second is False


@pytest.mark.asyncio
async def test_register_cron_restores_soft_deleted_no_wrapper(repo: SqliteRepository) -> None:
    """D5: register_cron on a soft-deleted fingerprint with wrapper=False restores it.

    Verifies that:
    - soft_deleted_at is cleared (NULL) in the DB.
    - A crons.restore audit row is written.
    - Returns (CronRecord, False) — not a new creation.
    - soft_deleted_at on the returned record is None.
    """
    crepo = CronRepo(repo)
    now = utc_now_iso()

    body = RegisterCronBody(
        host="restore-host",
        source_path="/etc/crontab",
        schedule="0 2 * * *",
        command="/usr/bin/restorable.sh",
        wrapper=False,
    )
    fp = compute_fingerprint(
        host="restore-host",
        source_path="/etc/crontab",
        schedule="0 2 * * *",
        command="/usr/bin/restorable.sh",
    )

    # Pre-insert the cron in soft-deleted state via raw SQL
    await repo.execute(
        text(
            "INSERT INTO crons ("
            "  fingerprint, name, host, command, schedule, schedule_canonical, "
            "  cadence_seconds, expected_grace_seconds, enabled, last_seen_state, "
            "  created_at, updated_at, hidden_at, source_path, wrapper_last_seen_at, "
            "  last_discovered_at, soft_deleted_at"
            ") VALUES ("
            "  :fp, :name, :host, :cmd, :sched, :sched_canon, :cad, :grace, :enabled, "
            "  :state, :created, :updated, :hidden, :source, :wrapper, :discovered, :sda"
            ")"
        ),
        {
            "fp": fp,
            "name": "restorable-cron",
            "host": "restore-host",
            "cmd": "/usr/bin/restorable.sh",
            "sched": "0 2 * * *",
            "sched_canon": "0 2 * * *",
            "cad": 86400,
            "grace": 300,
            "enabled": 1,
            "state": "unknown",
            "created": now,
            "updated": now,
            "hidden": None,
            "source": "/etc/crontab",
            "wrapper": None,
            "discovered": now,
            "sda": now,  # soft-deleted
        },
    )

    # register_cron should restore it
    record, created = await crepo.register_cron(
        body, url_fingerprint=fp, who="testactor", ip="1.2.3.4"
    )

    assert created is False
    assert record.soft_deleted_at is None

    # DB row should have soft_deleted_at cleared
    db_row = await repo.fetch_one(
        text("SELECT soft_deleted_at FROM crons WHERE fingerprint = :fp"),
        {"fp": fp},
    )
    assert db_row is not None
    assert db_row[0] is None

    # A crons.restore audit row must exist
    restore_row = await repo.fetch_one(
        text("SELECT who FROM audit_log WHERE what = 'crons.restore'")
    )
    assert restore_row is not None
    assert restore_row[0] == "testactor"
