"""Integration tests for secret scrubbing in upsert_discovered (STAGE-002-007D)."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.cron.fingerprint import compute_fingerprint
from homelab_monitor.kernel.cron.repository import CronRepo
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso


@pytest.mark.asyncio
async def test_upsert_discovered_scrubs_mysql_password(repo: SqliteRepository) -> None:
    """Test that MySQL -p password is scrubbed before storage."""
    cron_repo = CronRepo(repo)
    iso_now = utc_now_iso()
    raw_command = "mysqldump -u root -pmySecretPassword123 --all-databases > /backup.sql"
    expected_stored = "mysqldump -u root -p<redacted> --all-databases > /backup.sql"

    record, inserted, _ = await cron_repo.upsert_discovered(
        host="homelab-host",
        source_path="/etc/cron.d/backup",
        schedule="0 2 * * *",
        command=raw_command,
        now=iso_now,
    )

    # Verify the stored command is scrubbed
    assert record.command == expected_stored
    assert inserted is True

    # Verify the fingerprint was computed from the RAW command
    # (so same fingerprint regardless of password value)
    expected_fingerprint = compute_fingerprint(
        host="homelab-host",
        source_path="/etc/cron.d/backup",
        schedule="0 2 * * *",
        command=raw_command,  # raw, not scrubbed
    )
    assert record.fingerprint == expected_fingerprint


@pytest.mark.asyncio
async def test_upsert_discovered_scrubs_pgpassword_env(repo: SqliteRepository) -> None:
    """Test that PGPASSWORD environment variable is scrubbed."""
    cron_repo = CronRepo(repo)
    iso_now = utc_now_iso()
    raw_command = "PGPASSWORD=secretDBpassword psql -h db.local -U postgres -d mydb -c 'SELECT 1'"
    expected_stored = "PGPASSWORD=<redacted> psql -h db.local -U postgres -d mydb -c 'SELECT 1'"

    record, inserted, _ = await cron_repo.upsert_discovered(
        host="homelab-host",
        source_path="/etc/cron.d/db",
        schedule="*/5 * * * *",
        command=raw_command,
        now=iso_now,
    )

    assert record.command == expected_stored
    assert inserted is True


@pytest.mark.asyncio
async def test_upsert_discovered_fingerprint_based_on_raw_command(
    repo: SqliteRepository,
) -> None:
    """Test that fingerprint is computed from RAW command, not scrubbed version.

    This ensures convergence with the wrapper installer (which uses the
    raw command for fingerprinting). Two crons with different passwords
    will have DIFFERENT fingerprints (because they ARE different commands).
    But the scrubbed version is what gets stored in the database.
    """
    cron_repo = CronRepo(repo)
    iso_now = utc_now_iso()
    raw_command_1 = "mysqldump -u root -pPassword1 db > /backup.sql"
    raw_command_2 = "mysqldump -u root -pPassword2 db > /backup.sql"

    record1, inserted1, _ = await cron_repo.upsert_discovered(
        host="homelab-host",
        source_path="/etc/cron.d/backup",
        schedule="0 2 * * *",
        command=raw_command_1,
        now=iso_now,
    )
    assert inserted1 is True

    # Second insert with different password creates a DIFFERENT cron
    # (different fingerprint, because it's a different raw command)
    record2, inserted2, _ = await cron_repo.upsert_discovered(
        host="homelab-host",
        source_path="/etc/cron.d/backup",
        schedule="0 2 * * *",
        command=raw_command_2,
        now=iso_now,
    )

    # Different fingerprints (different raw commands)
    assert record1.fingerprint != record2.fingerprint
    # Second call should INSERT (new cron)
    assert inserted2 is True
    # Fingerprint for record1 matches expected fingerprint from raw_command_1
    assert record1.fingerprint == compute_fingerprint(
        host="homelab-host",
        source_path="/etc/cron.d/backup",
        schedule="0 2 * * *",
        command=raw_command_1,
    )
    # Fingerprint for record2 matches expected fingerprint from raw_command_2
    assert record2.fingerprint == compute_fingerprint(
        host="homelab-host",
        source_path="/etc/cron.d/backup",
        schedule="0 2 * * *",
        command=raw_command_2,
    )
    # Both should store scrubbed versions
    assert record1.command == "mysqldump -u root -p<redacted> db > /backup.sql"
    assert record2.command == "mysqldump -u root -p<redacted> db > /backup.sql"


@pytest.mark.asyncio
async def test_upsert_discovered_multiple_secrets_in_command(repo: SqliteRepository) -> None:
    """Test a command with multiple secret patterns."""
    cron_repo = CronRepo(repo)
    iso_now = utc_now_iso()
    raw_command = (
        "MYSQL_PWD=secret1 mysqldump -h db.local -u root -psecret2 "
        "--all-databases | curl --api-key=secret3 -d @- https://backup.example.com"
    )
    expected_stored = (
        "MYSQL_PWD=<redacted> mysqldump -h db.local -u root -p<redacted> "
        "--all-databases | curl --api-key=<redacted> -d @- https://backup.example.com"
    )

    record, inserted, _ = await cron_repo.upsert_discovered(
        host="homelab-host",
        source_path="/etc/cron.d/complex",
        schedule="0 3 * * *",
        command=raw_command,
        now=iso_now,
    )

    assert record.command == expected_stored
    assert inserted is True


@pytest.mark.asyncio
async def test_upsert_discovered_no_secrets_unchanged(repo: SqliteRepository) -> None:
    """Test that commands without secrets are stored unchanged."""
    cron_repo = CronRepo(repo)
    iso_now = utc_now_iso()
    command = "/usr/local/bin/backup.sh /data /mnt/backup"

    record, inserted, _ = await cron_repo.upsert_discovered(
        host="homelab-host",
        source_path="/etc/cron.d/backup",
        schedule="0 1 * * *",
        command=command,
        now=iso_now,
    )

    assert record.command == command
    assert inserted is True
