"""Tests for the runbook registry repository."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.runbooks.config import RunbookConfig
from homelab_monitor.kernel.runbooks.loader import LoadedRunbook, LoadError, ScanResult
from homelab_monitor.kernel.runbooks.repository import RunbookRepo


def _config(**overrides: object) -> RunbookConfig:
    """Build a minimal RunbookConfig with optional overrides."""
    base: dict[str, object] = {
        "runbook": 1,
        "name": "rb-one",
        "match_patterns": [{"alertname": "HighCPU"}],
        "risk_tag": "safe",
        "dry_run_required": True,
        "rate_limit_per_hour": 5,
        "cooldown_seconds": 300,
        "scoped_capabilities": {"docker": {"container": "c1", "allowed_actions": ["restart"]}},
    }
    base.update(overrides)
    return RunbookConfig.model_validate(base)


async def _audit_rows(repo: SqliteRepository, what: str) -> list[dict[str, object]]:
    """Fetch audit rows matching the given 'what' value."""
    rows = await repo.fetch_all(
        text(
            "SELECT who, what, before_json, after_json, ip FROM audit_log "
            'WHERE what = :w ORDER BY "when" ASC'
        ),
        {"w": what},
    )
    return [
        {
            "who": row[0],
            "what": row[1],
            "before_json": row[2],
            "after_json": row[3],
            "ip": row[4],
        }
        for row in rows
    ]


def _test_user() -> User:
    """Create a test user."""
    return User(
        id=1,
        username="test_user",
        created_at=utc_now_iso(),
    )


@pytest.mark.asyncio
async def test_reconcile_inserts_new(repo: SqliteRepository, tmp_path: Path) -> None:
    """Reconcile a ScanResult with one new LoadedRunbook."""
    runbook_repo = RunbookRepo(repo)
    folder = tmp_path / "test-rb"
    folder.mkdir()
    loaded = LoadedRunbook(folder=folder, config=_config())
    scan = ScanResult(loaded=[loaded], errors=[])

    user = _test_user()
    outcome = await runbook_repo.reconcile(scan, who_principal=user, ip="127.0.0.1")

    assert outcome.registered == [str(folder)]
    assert outcome.refreshed == []
    assert outcome.skipped == []
    assert outcome.errors == []

    records = await runbook_repo.list_runbooks()
    assert len(records) == 1
    assert records[0].path == str(folder)
    assert records[0].enabled is False
    assert records[0].auto_trigger is False
    assert records[0].content_hash is not None
    assert records[0].risk_tag == "safe"


@pytest.mark.asyncio
async def test_reconcile_insert_dry_run_false_binds_zero(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """Config with dry_run_required=False -> stored dry_run_required is False."""
    runbook_repo = RunbookRepo(repo)
    folder = tmp_path / "test-rb"
    folder.mkdir()
    loaded = LoadedRunbook(folder=folder, config=_config(dry_run_required=False))
    scan = ScanResult(loaded=[loaded], errors=[])

    user = _test_user()
    await runbook_repo.reconcile(scan, who_principal=user, ip=None)

    records = await runbook_repo.list_runbooks()
    assert len(records) == 1
    assert records[0].dry_run_required is False


@pytest.mark.asyncio
async def test_reconcile_unchanged_is_noop(repo: SqliteRepository, tmp_path: Path) -> None:
    """Reconcile same folder twice (same config) -> 2nd run: skipped, no new row."""
    runbook_repo = RunbookRepo(repo)
    folder = tmp_path / "test-rb"
    folder.mkdir()
    config = _config()
    loaded = LoadedRunbook(folder=folder, config=config)
    scan = ScanResult(loaded=[loaded], errors=[])

    user = _test_user()
    outcome1 = await runbook_repo.reconcile(scan, who_principal=user, ip=None)
    assert outcome1.registered == [str(folder)]

    outcome2 = await runbook_repo.reconcile(scan, who_principal=user, ip=None)
    assert outcome2.skipped == [str(folder)]
    assert outcome2.registered == []

    records = await runbook_repo.list_runbooks()
    assert len(records) == 1  # Still only one row


@pytest.mark.asyncio
async def test_reconcile_changed_updates_cached(repo: SqliteRepository, tmp_path: Path) -> None:
    """Reconcile, then reconcile with mutated config -> cached fields updated."""
    runbook_repo = RunbookRepo(repo)
    folder = tmp_path / "test-rb"
    folder.mkdir()

    user = _test_user()
    config1 = _config(cooldown_seconds=100)
    loaded1 = LoadedRunbook(folder=folder, config=config1)
    scan1 = ScanResult(loaded=[loaded1], errors=[])
    await runbook_repo.reconcile(scan1, who_principal=user, ip=None)

    record1 = await runbook_repo.get_runbook((await runbook_repo.list_runbooks())[0].id)
    assert record1 is not None
    hash1 = record1.content_hash

    config2 = _config(cooldown_seconds=999)
    loaded2 = LoadedRunbook(folder=folder, config=config2)
    scan2 = ScanResult(loaded=[loaded2], errors=[])
    outcome = await runbook_repo.reconcile(scan2, who_principal=user, ip=None)

    assert outcome.refreshed == [str(folder)]
    record2 = await runbook_repo.get_runbook(record1.id)
    assert record2 is not None
    assert record2.cooldown_seconds == 999  # noqa: PLR2004
    assert record2.content_hash != hash1


@pytest.mark.asyncio
async def test_reconcile_update_preserves_gates(repo: SqliteRepository, tmp_path: Path) -> None:
    """INSERT, then operator sets enabled=True, then reconcile CHANGED config."""
    runbook_repo = RunbookRepo(repo)
    folder = tmp_path / "test-rb"
    folder.mkdir()

    user = _test_user()
    config1 = _config()
    loaded1 = LoadedRunbook(folder=folder, config=config1)
    scan1 = ScanResult(loaded=[loaded1], errors=[])
    await runbook_repo.reconcile(scan1, who_principal=user, ip=None)

    record_id = (await runbook_repo.list_runbooks())[0].id
    await runbook_repo.patch_gates(
        record_id, enabled=True, auto_trigger=None, who_principal=user, ip=None
    )

    config2 = _config(cooldown_seconds=999)
    loaded2 = LoadedRunbook(folder=folder, config=config2)
    scan2 = ScanResult(loaded=[loaded2], errors=[])
    await runbook_repo.reconcile(scan2, who_principal=user, ip=None)

    record = await runbook_repo.get_runbook(record_id)
    assert record is not None
    assert record.enabled is True  # Preserved!
    assert record.auto_trigger is False  # Unchanged


@pytest.mark.asyncio
async def test_reconcile_passes_through_errors(repo: SqliteRepository) -> None:
    """ScanResult with errors=[LoadError(...)] -> outcome.errors equals that list."""
    runbook_repo = RunbookRepo(repo)
    errors = [LoadError(path="/some/path", message="test error")]
    scan = ScanResult(loaded=[], errors=errors)

    user = _test_user()
    outcome = await runbook_repo.reconcile(scan, who_principal=user, ip=None)

    assert outcome.errors == errors


@pytest.mark.asyncio
async def test_reconcile_mixed_batch(repo: SqliteRepository, tmp_path: Path) -> None:
    """One new + one unchanged + one changed in a single reconcile."""
    runbook_repo = RunbookRepo(repo)

    user = _test_user()
    new_folder = tmp_path / "new"
    new_folder.mkdir()
    unchanged_folder = tmp_path / "unchanged"
    unchanged_folder.mkdir()
    changed_folder = tmp_path / "changed"
    changed_folder.mkdir()

    config = _config()
    scan1 = ScanResult(
        loaded=[
            LoadedRunbook(folder=unchanged_folder, config=config),
            LoadedRunbook(folder=changed_folder, config=_config(cooldown_seconds=100)),
        ],
        errors=[],
    )
    await runbook_repo.reconcile(scan1, who_principal=user, ip=None)

    scan2 = ScanResult(
        loaded=[
            LoadedRunbook(folder=new_folder, config=config),
            LoadedRunbook(folder=unchanged_folder, config=config),
            LoadedRunbook(folder=changed_folder, config=_config(cooldown_seconds=999)),
        ],
        errors=[],
    )
    outcome = await runbook_repo.reconcile(scan2, who_principal=user, ip=None)

    assert str(new_folder) in outcome.registered
    assert str(unchanged_folder) in outcome.skipped
    assert str(changed_folder) in outcome.refreshed
    assert len(outcome.errors) == 0


@pytest.mark.asyncio
async def test_register_writes_audit(repo: SqliteRepository, tmp_path: Path) -> None:
    """After INSERT, exactly one runbook_registered audit row."""
    runbook_repo = RunbookRepo(repo)
    folder = tmp_path / "test-rb"
    folder.mkdir()
    loaded = LoadedRunbook(folder=folder, config=_config())
    scan = ScanResult(loaded=[loaded], errors=[])

    user = _test_user()
    await runbook_repo.reconcile(scan, who_principal=user, ip="127.0.0.1")

    audit_rows = await _audit_rows(repo, "runbook_registered")
    assert len(audit_rows) == 1
    row = audit_rows[0]
    assert row["who"] == f"user:{user.username}"
    assert row["before_json"] is None
    assert row["after_json"] is not None
    after = json.loads(str(row["after_json"]))
    assert isinstance(after, dict)
    assert "path" in after
    assert row["ip"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_refresh_writes_audit(repo: SqliteRepository, tmp_path: Path) -> None:
    """After a cached UPDATE, one runbook_refreshed audit row with before/after."""
    runbook_repo = RunbookRepo(repo)
    folder = tmp_path / "test-rb"
    folder.mkdir()

    user = _test_user()
    config1 = _config(cooldown_seconds=100)
    loaded1 = LoadedRunbook(folder=folder, config=config1)
    await runbook_repo.reconcile(
        ScanResult(loaded=[loaded1], errors=[]), who_principal=user, ip=None
    )

    config2 = _config(cooldown_seconds=999)
    loaded2 = LoadedRunbook(folder=folder, config=config2)
    await runbook_repo.reconcile(
        ScanResult(loaded=[loaded2], errors=[]), who_principal=user, ip=None
    )

    audit_rows = await _audit_rows(repo, "runbook_refreshed")
    assert len(audit_rows) == 1
    row = audit_rows[0]
    assert row["before_json"] is not None
    assert row["after_json"] is not None
    before = json.loads(str(row["before_json"]))
    after = json.loads(str(row["after_json"]))
    assert isinstance(before, dict)
    assert isinstance(after, dict)
    assert "content_hash" in before
    assert "content_hash" in after
    assert before["content_hash"] is not None
    assert before["content_hash"] != after["content_hash"]
    assert "cooldown_seconds" in before
    assert before["cooldown_seconds"] == 100  # noqa: PLR2004
    assert after["cooldown_seconds"] == 999  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_empty(repo: SqliteRepository) -> None:
    """list_runbooks() on empty DB -> []."""
    runbook_repo = RunbookRepo(repo)
    records = await runbook_repo.list_runbooks()
    assert records == []


@pytest.mark.asyncio
async def test_list_nonempty(repo: SqliteRepository, tmp_path: Path) -> None:
    """Two inserts -> list returns both ordered by path."""
    runbook_repo = RunbookRepo(repo)
    user = _test_user()

    folder_a = tmp_path / "a-runbook"
    folder_a.mkdir()
    folder_b = tmp_path / "b-runbook"
    folder_b.mkdir()

    scan = ScanResult(
        loaded=[
            LoadedRunbook(folder=folder_b, config=_config(name="rb-b")),
            LoadedRunbook(folder=folder_a, config=_config(name="rb-a")),
        ],
        errors=[],
    )
    await runbook_repo.reconcile(scan, who_principal=user, ip=None)

    records = await runbook_repo.list_runbooks()
    assert len(records) == 2  # noqa: PLR2004
    assert records[0].path == str(folder_a)
    assert records[1].path == str(folder_b)


@pytest.mark.asyncio
async def test_list_null_patterns_deserializes_to_empty(repo: SqliteRepository) -> None:
    """Raw-INSERT row with alert_match_patterns = NULL -> deserializes to []."""
    runbook_repo = RunbookRepo(repo)

    now = utc_now_iso()
    await repo.execute(
        text(
            "INSERT INTO runbooks (id, path, created_at, alert_match_patterns, risk_tag, "
            "dry_run_required, enabled, auto_trigger) VALUES "
            "(:id, :path, :created_at, NULL, :risk_tag, :dry_run_required, :enabled, :auto_trigger)"
        ),
        {
            "id": "test-id",
            "path": "/test/path",
            "created_at": now,
            "risk_tag": "safe",
            "dry_run_required": 1,
            "enabled": 0,
            "auto_trigger": 0,
        },
    )

    records = await runbook_repo.list_runbooks()
    assert len(records) == 1
    assert records[0].alert_match_patterns == []


@pytest.mark.asyncio
async def test_row_nullable_fields_none(repo: SqliteRepository) -> None:
    """Row with nullable fields NULL -> record has all three None."""
    runbook_repo = RunbookRepo(repo)

    now = utc_now_iso()
    await repo.execute(
        text(
            "INSERT INTO runbooks (id, path, created_at, risk_tag, "
            "dry_run_required, rate_limit_per_hour, cooldown_seconds, enabled, "
            "auto_trigger, content_hash) VALUES (:id, :path, :created_at, :risk_tag, "
            ":dry_run_required, NULL, NULL, :enabled, :auto_trigger, NULL)"
        ),
        {
            "id": "test-id",
            "path": "/test/path",
            "created_at": now,
            "risk_tag": "safe",
            "dry_run_required": 1,
            "enabled": 0,
            "auto_trigger": 0,
        },
    )

    record = await runbook_repo.get_runbook("test-id")
    assert record is not None
    assert record.rate_limit_per_hour is None
    assert record.cooldown_seconds is None
    assert record.content_hash is None


@pytest.mark.asyncio
async def test_row_nullable_fields_present(repo: SqliteRepository) -> None:
    """Row with nullable fields set -> record has values."""
    runbook_repo = RunbookRepo(repo)

    now = utc_now_iso()
    await repo.execute(
        text(
            "INSERT INTO runbooks (id, path, created_at, risk_tag, "
            "dry_run_required, rate_limit_per_hour, cooldown_seconds, enabled, auto_trigger, "
            "content_hash) VALUES (:id, :path, :created_at, :risk_tag, :dry_run_required, "
            ":rate_limit, :cooldown, :enabled, :auto_trigger, :hash)"
        ),
        {
            "id": "test-id",
            "path": "/test/path",
            "created_at": now,
            "risk_tag": "safe",
            "dry_run_required": 1,
            "rate_limit": 10,
            "cooldown": 60,
            "enabled": 0,
            "auto_trigger": 0,
            "hash": "abc123",
        },
    )

    record = await runbook_repo.get_runbook("test-id")
    assert record is not None
    assert record.rate_limit_per_hour == 10  # noqa: PLR2004
    assert record.cooldown_seconds == 60  # noqa: PLR2004
    assert record.content_hash == "abc123"


@pytest.mark.asyncio
async def test_get_runbook_found(repo: SqliteRepository, tmp_path: Path) -> None:
    """Get by id returns record."""
    runbook_repo = RunbookRepo(repo)
    user = _test_user()

    folder = tmp_path / "test-rb"
    folder.mkdir()
    loaded = LoadedRunbook(folder=folder, config=_config())
    scan = ScanResult(loaded=[loaded], errors=[])
    await runbook_repo.reconcile(scan, who_principal=user, ip=None)

    record_id = (await runbook_repo.list_runbooks())[0].id
    record = await runbook_repo.get_runbook(record_id)
    assert record is not None
    assert record.id == record_id


@pytest.mark.asyncio
async def test_get_runbook_not_found(repo: SqliteRepository) -> None:
    """Get by unknown id -> None."""
    runbook_repo = RunbookRepo(repo)
    record = await runbook_repo.get_runbook("unknown-id")
    assert record is None


@pytest.mark.asyncio
async def test_patch_gates_not_found_raises(repo: SqliteRepository) -> None:
    """patch_gates on unknown id -> LookupError."""
    runbook_repo = RunbookRepo(repo)
    user = _test_user()

    with pytest.raises(LookupError, match="not found"):
        await runbook_repo.patch_gates(
            "unknown-id",
            enabled=True,
            auto_trigger=None,
            who_principal=user,
            ip=None,
        )


@pytest.mark.asyncio
async def test_patch_enabled_only(repo: SqliteRepository, tmp_path: Path) -> None:
    """Set enabled=True, auto_trigger=None -> enabled True, auto_trigger unchanged."""
    runbook_repo = RunbookRepo(repo)
    user = _test_user()

    folder = tmp_path / "test-rb"
    folder.mkdir()
    loaded = LoadedRunbook(folder=folder, config=_config())
    scan = ScanResult(loaded=[loaded], errors=[])
    await runbook_repo.reconcile(scan, who_principal=user, ip=None)

    record_id = (await runbook_repo.list_runbooks())[0].id
    rec = await runbook_repo.patch_gates(
        record_id, enabled=True, auto_trigger=None, who_principal=user, ip=None
    )

    assert rec.enabled is True
    assert rec.auto_trigger is False

    audit_rows = await _audit_rows(repo, "runbook_gates_changed")
    assert len(audit_rows) == 1


@pytest.mark.asyncio
async def test_patch_auto_trigger_only(repo: SqliteRepository, tmp_path: Path) -> None:
    """Set auto_trigger=True only -> auto_trigger True, enabled unchanged."""
    runbook_repo = RunbookRepo(repo)
    user = _test_user()

    folder = tmp_path / "test-rb"
    folder.mkdir()
    loaded = LoadedRunbook(folder=folder, config=_config())
    scan = ScanResult(loaded=[loaded], errors=[])
    await runbook_repo.reconcile(scan, who_principal=user, ip=None)

    record_id = (await runbook_repo.list_runbooks())[0].id
    rec = await runbook_repo.patch_gates(
        record_id, enabled=None, auto_trigger=True, who_principal=user, ip=None
    )

    assert rec.auto_trigger is True
    assert rec.enabled is False


@pytest.mark.asyncio
async def test_patch_both_gates(repo: SqliteRepository, tmp_path: Path) -> None:
    """Set both gates -> both updated, audit before/after include both keys."""
    runbook_repo = RunbookRepo(repo)
    user = _test_user()

    folder = tmp_path / "test-rb"
    folder.mkdir()
    loaded = LoadedRunbook(folder=folder, config=_config())
    scan = ScanResult(loaded=[loaded], errors=[])
    await runbook_repo.reconcile(scan, who_principal=user, ip=None)

    record_id = (await runbook_repo.list_runbooks())[0].id
    rec = await runbook_repo.patch_gates(
        record_id, enabled=True, auto_trigger=True, who_principal=user, ip=None
    )

    assert rec.enabled is True
    assert rec.auto_trigger is True


@pytest.mark.asyncio
async def test_patch_noop_same_value(repo: SqliteRepository, tmp_path: Path) -> None:
    """enabled=False when already False -> no write, no audit."""
    runbook_repo = RunbookRepo(repo)
    user = _test_user()

    folder = tmp_path / "test-rb"
    folder.mkdir()
    loaded = LoadedRunbook(folder=folder, config=_config())
    scan = ScanResult(loaded=[loaded], errors=[])
    await runbook_repo.reconcile(scan, who_principal=user, ip=None)

    record_id = (await runbook_repo.list_runbooks())[0].id
    rec = await runbook_repo.patch_gates(
        record_id, enabled=False, auto_trigger=None, who_principal=user, ip=None
    )

    assert rec.enabled is False
    audit_rows = await _audit_rows(repo, "runbook_gates_changed")
    assert len(audit_rows) == 0


@pytest.mark.asyncio
async def test_patch_both_none_noop(repo: SqliteRepository, tmp_path: Path) -> None:
    """Both None -> no write, no audit."""
    runbook_repo = RunbookRepo(repo)
    user = _test_user()

    folder = tmp_path / "test-rb"
    folder.mkdir()
    loaded = LoadedRunbook(folder=folder, config=_config())
    scan = ScanResult(loaded=[loaded], errors=[])
    await runbook_repo.reconcile(scan, who_principal=user, ip=None)

    record_id = (await runbook_repo.list_runbooks())[0].id
    rec = await runbook_repo.patch_gates(
        record_id, enabled=None, auto_trigger=None, who_principal=user, ip=None
    )

    assert rec.enabled is False
    assert rec.auto_trigger is False
    audit_rows = await _audit_rows(repo, "runbook_gates_changed")
    assert len(audit_rows) == 0


@pytest.mark.asyncio
async def test_patch_enabled_change_preserves_auto_trigger(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """Pre-set auto_trigger True; patch enabled only -> auto_trigger stays True."""
    runbook_repo = RunbookRepo(repo)
    user = _test_user()

    folder = tmp_path / "test-rb"
    folder.mkdir()
    loaded = LoadedRunbook(folder=folder, config=_config())
    scan = ScanResult(loaded=[loaded], errors=[])
    await runbook_repo.reconcile(scan, who_principal=user, ip=None)

    record_id = (await runbook_repo.list_runbooks())[0].id
    await runbook_repo.patch_gates(
        record_id, enabled=None, auto_trigger=True, who_principal=user, ip=None
    )

    rec = await runbook_repo.patch_gates(
        record_id, enabled=True, auto_trigger=None, who_principal=user, ip=None
    )

    assert rec.enabled is True
    assert rec.auto_trigger is True
