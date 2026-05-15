"""Tests for CronRepo.reconcile_soft_deletes (STAGE-002-007A Wave 2)."""

import json

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.cron.repository import CronRepo
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso


async def _count_audit(repo: SqliteRepository, verb: str) -> int:
    """Count audit_log rows with the given 'what' verb."""
    row = await repo.fetch_one(
        text("SELECT COUNT(*) AS c FROM audit_log WHERE what = :w"),
        {"w": verb},
    )
    return int(row.c) if row else 0


async def _soft_deleted_at(repo: SqliteRepository, fingerprint: str) -> str | None:
    """Get the soft_deleted_at value for a fingerprint."""
    row = await repo.fetch_one(
        text("SELECT soft_deleted_at FROM crons WHERE fingerprint = :fp"),
        {"fp": fingerprint},
    )
    return str(row.soft_deleted_at) if (row and row.soft_deleted_at) else None


async def _insert_cron_raw(
    repo: SqliteRepository,
    fingerprint: str,
    host: str = "test-host",
    source_path: str | None = None,
    soft_deleted_at: str | None = None,
) -> None:
    """Insert a cron row directly via raw SQL (for test setup)."""
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
            "fp": fingerprint,
            "name": f"test-{fingerprint[:8]}",
            "host": host,
            "cmd": "echo test",
            "sched": "0 * * * *",
            "sched_canon": "0 * * * *",
            "cad": 3600,
            "grace": 300,
            "enabled": 1,
            "state": "unknown",
            "created": utc_now_iso(),
            "updated": utc_now_iso(),
            "hidden": None,
            "source": source_path,
            "wrapper": None,
            "discovered": utc_now_iso(),
            "sda": soft_deleted_at,
        },
    )


@pytest.mark.asyncio
async def test_reconcile_soft_deletes_absent_fingerprint(
    repo: SqliteRepository,
) -> None:
    """Insert a cron, then call reconcile with empty found_by_path.

    Assert returns (1, 0); soft_deleted_at is now non-NULL; exactly 1
    crons.soft_delete audit row exists.
    """
    cron_repo = CronRepo(repo)
    now = utc_now_iso()

    # Insert a cron via upsert (cleaner than raw SQL)
    record, _, _ = await cron_repo.upsert_discovered(
        host="test-host",
        source_path="/etc/cron.d/backup",
        schedule="0 * * * *",
        command="echo backup",
        now=now,
    )
    fp = record.fingerprint

    # Reconcile with the path clean but fingerprint absent from scan
    soft_deleted, restored = await cron_repo.reconcile_soft_deletes(
        host="test-host",
        clean_paths=frozenset({"/etc/cron.d/backup"}),
        found_by_path={},  # empty: fingerprint not in scan
        now=now,
    )

    assert soft_deleted == 1
    assert restored == 0
    assert await _soft_deleted_at(repo, fp) is not None
    assert await _count_audit(repo, "crons.soft_delete") == 1


@pytest.mark.asyncio
async def test_reconcile_restore_found_again(
    repo: SqliteRepository,
) -> None:
    """Soft-delete a row, then reconcile with fingerprint found again.

    Assert returns (0, 1); soft_deleted_at is NULL; exactly 1 crons.restore
    audit row exists.
    """
    cron_repo = CronRepo(repo)
    now = utc_now_iso()

    # Setup: insert and soft-delete
    record, _, _ = await cron_repo.upsert_discovered(
        host="test-host",
        source_path="/etc/cron.d/backup",
        schedule="0 * * * *",
        command="echo backup",
        now=now,
    )
    fp = record.fingerprint

    # First reconcile: soft-delete it
    await cron_repo.reconcile_soft_deletes(
        host="test-host",
        clean_paths=frozenset({"/etc/cron.d/backup"}),
        found_by_path={},
        now=now,
    )
    assert await _soft_deleted_at(repo, fp) is not None

    # Second reconcile: found again
    soft_deleted, restored = await cron_repo.reconcile_soft_deletes(
        host="test-host",
        clean_paths=frozenset({"/etc/cron.d/backup"}),
        found_by_path={"/etc/cron.d/backup": frozenset({fp})},
        now=now,
    )

    assert soft_deleted == 0
    assert restored == 1
    assert await _soft_deleted_at(repo, fp) is None
    assert await _count_audit(repo, "crons.restore") == 1


@pytest.mark.asyncio
async def test_reconcile_noop_present_and_active(
    repo: SqliteRepository,
) -> None:
    """Active row with fingerprint in scan. Assert returns (0, 0); no new audit."""
    cron_repo = CronRepo(repo)
    now = utc_now_iso()

    record, _, _ = await cron_repo.upsert_discovered(
        host="test-host",
        source_path="/etc/cron.d/backup",
        schedule="0 * * * *",
        command="echo backup",
        now=now,
    )
    fp = record.fingerprint

    # Reconcile with active row found in scan
    soft_deleted, restored = await cron_repo.reconcile_soft_deletes(
        host="test-host",
        clean_paths=frozenset({"/etc/cron.d/backup"}),
        found_by_path={"/etc/cron.d/backup": frozenset({fp})},
        now=now,
    )

    assert soft_deleted == 0
    assert restored == 0
    assert await _count_audit(repo, "crons.soft_delete") == 0
    assert await _count_audit(repo, "crons.restore") == 0


@pytest.mark.asyncio
async def test_reconcile_noop_absent_and_already_soft_deleted(
    repo: SqliteRepository,
) -> None:
    """Already-soft-deleted row, fingerprint absent from scan. No-op."""
    cron_repo = CronRepo(repo)
    now = utc_now_iso()

    # Setup: insert and soft-delete
    _, _, _ = await cron_repo.upsert_discovered(
        host="test-host",
        source_path="/etc/cron.d/backup",
        schedule="0 * * * *",
        command="echo backup",
        now=now,
    )

    await cron_repo.reconcile_soft_deletes(
        host="test-host",
        clean_paths=frozenset({"/etc/cron.d/backup"}),
        found_by_path={},
        now=now,
    )

    # Clear audit log from the soft-delete
    await repo.execute(text("DELETE FROM audit_log WHERE what = 'crons.soft_delete'"))

    # Second reconcile: already soft-deleted, still absent
    soft_deleted, restored = await cron_repo.reconcile_soft_deletes(
        host="test-host",
        clean_paths=frozenset({"/etc/cron.d/backup"}),
        found_by_path={},
        now=now,
    )

    assert soft_deleted == 0
    assert restored == 0
    assert await _count_audit(repo, "crons.soft_delete") == 0


@pytest.mark.asyncio
async def test_reconcile_empty_clean_paths_returns_zero(
    repo: SqliteRepository,
) -> None:
    """Empty clean_paths; assert early return (0, 0), nothing written."""
    cron_repo = CronRepo(repo)
    now = utc_now_iso()

    soft_deleted, restored = await cron_repo.reconcile_soft_deletes(
        host="test-host",
        clean_paths=frozenset(),
        found_by_path={},
        now=now,
    )

    assert soft_deleted == 0
    assert restored == 0


@pytest.mark.asyncio
async def test_reconcile_per_host_filter(
    repo: SqliteRepository,
) -> None:
    """Same source_path on two different hosts. Only the matched host is soft-deleted."""
    cron_repo = CronRepo(repo)
    now = utc_now_iso()

    # Insert same source_path on two different hosts
    rec_a, _, _ = await cron_repo.upsert_discovered(
        host="host-a",
        source_path="/etc/cron.d/backup",
        schedule="0 * * * *",
        command="echo backup",
        now=now,
    )
    fp_a = rec_a.fingerprint

    rec_b, _, _ = await cron_repo.upsert_discovered(
        host="host-b",
        source_path="/etc/cron.d/backup",
        schedule="0 * * * *",
        command="echo backup",
        now=now,
    )
    fp_b = rec_b.fingerprint

    # Reconcile for host-a only
    soft_deleted, restored = await cron_repo.reconcile_soft_deletes(
        host="host-a",
        clean_paths=frozenset({"/etc/cron.d/backup"}),
        found_by_path={},
        now=now,
    )

    assert soft_deleted == 1
    assert restored == 0
    assert await _soft_deleted_at(repo, fp_a) is not None
    assert await _soft_deleted_at(repo, fp_b) is None  # untouched


@pytest.mark.asyncio
async def test_reconcile_remote_only_never_touched(
    repo: SqliteRepository,
) -> None:
    """remote-only row (source_path IS NULL) is never soft-deleted."""
    cron_repo = CronRepo(repo)
    now = utc_now_iso()

    # Insert a remote-only cron (source_path=None)
    await _insert_cron_raw(
        repo,
        fingerprint="remote-fp-001",
        host="test-host",
        source_path=None,
    )

    # Insert a real-path cron
    rec, _, _ = await cron_repo.upsert_discovered(
        host="test-host",
        source_path="/etc/cron.d/backup",
        schedule="0 * * * *",
        command="echo backup",
        now=now,
    )
    fp_real = rec.fingerprint

    # Reconcile real path with empty scan
    soft_deleted, restored = await cron_repo.reconcile_soft_deletes(
        host="test-host",
        clean_paths=frozenset({"/etc/cron.d/backup"}),
        found_by_path={},
        now=now,
    )

    assert soft_deleted == 1  # only the real-path one
    assert restored == 0
    assert await _soft_deleted_at(repo, "remote-fp-001") is None  # never touched
    assert await _soft_deleted_at(repo, fp_real) is not None


@pytest.mark.asyncio
async def test_reconcile_partial_scan_other_paths_still_run(
    repo: SqliteRepository,
) -> None:
    """Partial scan (one file has error) still reconciles clean paths."""
    cron_repo = CronRepo(repo)
    now = utc_now_iso()

    # Seed a stale row under /etc/cron.d/backup
    rec_stale, _, _ = await cron_repo.upsert_discovered(
        host="test-host",
        source_path="/etc/cron.d/backup",
        schedule="0 * * * *",
        command="echo stale",
        now=now,
    )
    fp_stale = rec_stale.fingerprint

    # Seed an active row under /etc/crontab
    rec_active, _, _ = await cron_repo.upsert_discovered(
        host="test-host",
        source_path="/etc/crontab",
        schedule="0 * * * *",
        command="echo active",
        now=now,
    )
    fp_active = rec_active.fingerprint

    # Simulate a partial scan where /etc/cron.d errored but /etc/crontab is clean
    soft_deleted, restored = await cron_repo.reconcile_soft_deletes(
        host="test-host",
        clean_paths=frozenset({"/etc/crontab"}),  # only crontab is clean
        found_by_path={"/etc/crontab": frozenset({fp_active})},
        now=now,
    )

    # /etc/cron.d/backup was NOT in clean_paths, so its stale row is NEVER touched
    assert soft_deleted == 0
    assert restored == 0
    assert await _soft_deleted_at(repo, fp_stale) is None  # untouched
    assert await _soft_deleted_at(repo, fp_active) is None  # active


@pytest.mark.asyncio
async def test_reconcile_unrecognized_source_path_never_touched(
    repo: SqliteRepository,
) -> None:
    """Unrecognized source_path (/opt/custom/jobs) never enters clean_source_paths."""
    cron_repo = CronRepo(repo)
    now = utc_now_iso()

    # Insert a row with a custom, unrecognized path
    await _insert_cron_raw(
        repo,
        fingerprint="custom-fp-001",
        host="test-host",
        source_path="/opt/custom/jobs",
    )

    # Reconcile for /etc/cron.d (recognized path)
    soft_deleted, restored = await cron_repo.reconcile_soft_deletes(
        host="test-host",
        clean_paths=frozenset({"/etc/cron.d/backup"}),
        found_by_path={},
        now=now,
    )

    assert soft_deleted == 0  # custom path never reconciled
    assert restored == 0
    assert await _soft_deleted_at(repo, "custom-fp-001") is None


@pytest.mark.asyncio
async def test_reconcile_deleted_cron_d_file(
    repo: SqliteRepository,
) -> None:
    """D3: operator-deleted /etc/cron.d/foo file is soft-deleted on next reconcile."""
    cron_repo = CronRepo(repo)
    now = utc_now_iso()

    # Insert a cron from /etc/cron.d/foo (as if first scan found it)
    rec, _, _ = await cron_repo.upsert_discovered(
        host="test-host",
        source_path="/etc/cron.d/foo",
        schedule="0 * * * *",
        command="echo backup",
        now=now,
    )
    fp = rec.fingerprint

    # Now /etc/cron.d/foo is deleted from disk, but DB still has it.
    # On the next clean scan, the discoverer will call list_source_paths_for_host,
    # which returns ["/etc/cron.d/foo"], and the D3 augmentation adds it to
    # clean_source_paths (because /etc/cron.d dir is clean).
    # Reconcile with that augmented set.

    soft_deleted, restored = await cron_repo.reconcile_soft_deletes(
        host="test-host",
        clean_paths=frozenset({"/etc/cron.d/foo"}),  # D3 augmentation added this
        found_by_path={},  # file is deleted, so fingerprint not in scan
        now=now,
    )

    assert soft_deleted == 1
    assert restored == 0
    assert await _soft_deleted_at(repo, fp) is not None


@pytest.mark.asyncio
async def test_reconcile_audit_minimal_diff(
    repo: SqliteRepository,
) -> None:
    """Audit rows contain only fingerprint + soft_deleted_at (minimal diff, per D4)."""
    cron_repo = CronRepo(repo)
    now = utc_now_iso()

    rec, _, _ = await cron_repo.upsert_discovered(
        host="test-host",
        source_path="/etc/cron.d/backup",
        schedule="0 * * * *",
        command="echo backup",
        now=now,
    )
    fp = rec.fingerprint

    # Soft-delete
    await cron_repo.reconcile_soft_deletes(
        host="test-host",
        clean_paths=frozenset({"/etc/cron.d/backup"}),
        found_by_path={},
        now=now,
    )

    # Fetch audit row
    audit_row = await repo.fetch_one(
        text(
            "SELECT before_json, after_json FROM audit_log WHERE what = 'crons.soft_delete' LIMIT 1"
        )
    )
    assert audit_row is not None

    before_dict = json.loads(audit_row.before_json)
    after_dict = json.loads(audit_row.after_json)

    # Minimal diff: only fingerprint + soft_deleted_at
    assert set(before_dict.keys()) == {"fingerprint", "soft_deleted_at"}
    assert set(after_dict.keys()) == {"fingerprint", "soft_deleted_at"}
    assert before_dict["fingerprint"] == fp
    assert before_dict["soft_deleted_at"] is None
    assert after_dict["fingerprint"] == fp
    assert after_dict["soft_deleted_at"] == now

    # Test restore audit as well
    await cron_repo.reconcile_soft_deletes(
        host="test-host",
        clean_paths=frozenset({"/etc/cron.d/backup"}),
        found_by_path={"/etc/cron.d/backup": frozenset({fp})},
        now=now,
    )

    audit_restore = await repo.fetch_one(
        text("SELECT before_json, after_json FROM audit_log WHERE what = 'crons.restore' LIMIT 1")
    )
    assert audit_restore is not None

    before_restore = json.loads(audit_restore.before_json)
    after_restore = json.loads(audit_restore.after_json)

    assert set(before_restore.keys()) == {"fingerprint", "soft_deleted_at"}
    assert set(after_restore.keys()) == {"fingerprint", "soft_deleted_at"}
    assert before_restore["soft_deleted_at"] == now
    assert after_restore["soft_deleted_at"] is None


@pytest.mark.asyncio
async def test_reconcile_does_not_touch_last_discovered_at(
    repo: SqliteRepository,
) -> None:
    """reconcile_soft_deletes must NOT modify last_discovered_at."""
    cron_repo = CronRepo(repo)
    now = utc_now_iso()

    rec, _, _ = await cron_repo.upsert_discovered(
        host="test-host",
        source_path="/etc/cron.d/backup",
        schedule="0 * * * *",
        command="echo backup",
        now=now,
    )
    fp = rec.fingerprint

    # Capture original last_discovered_at
    original_lda = rec.last_discovered_at

    # Soft-delete (which would update updated_at)
    await cron_repo.reconcile_soft_deletes(
        host="test-host",
        clean_paths=frozenset({"/etc/cron.d/backup"}),
        found_by_path={},
        now=now,
    )

    # Re-fetch and verify last_discovered_at unchanged
    refetched = await cron_repo.get_cron(fp, include_hidden=True)
    assert refetched is not None
    assert refetched.last_discovered_at == original_lda
    assert refetched.soft_deleted_at is not None
