"""Unit tests for kernel.cron.fingerprint.{compute_fingerprint, derive_name}."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.cron.fingerprint import compute_fingerprint, derive_name

HEX64 = 64


def test_fingerprint_is_64_char_hex() -> None:
    fp = compute_fingerprint("h", "/etc/crontab", "* * * * *", "/x")
    assert len(fp) == HEX64
    assert all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_is_deterministic() -> None:
    fp1 = compute_fingerprint("h", "/etc/crontab", "* * * * *", "/x")
    fp2 = compute_fingerprint("h", "/etc/crontab", "* * * * *", "/x")
    assert fp1 == fp2


def test_fingerprint_changes_when_host_changes() -> None:
    a = compute_fingerprint("host-a", "/etc/crontab", "* * * * *", "/x")
    b = compute_fingerprint("host-b", "/etc/crontab", "* * * * *", "/x")
    assert a != b


def test_fingerprint_changes_when_source_path_changes() -> None:
    a = compute_fingerprint("h", "/etc/crontab", "* * * * *", "/x")
    b = compute_fingerprint("h", "/etc/cron.d/foo", "* * * * *", "/x")
    assert a != b


def test_fingerprint_changes_when_schedule_changes() -> None:
    a = compute_fingerprint("h", "/etc/crontab", "* * * * *", "/x")
    b = compute_fingerprint("h", "/etc/crontab", "*/5 * * * *", "/x")
    assert a != b


def test_fingerprint_changes_when_command_changes() -> None:
    a = compute_fingerprint("h", "/etc/crontab", "* * * * *", "/x")
    b = compute_fingerprint("h", "/etc/crontab", "* * * * *", "/y")
    assert a != b


def test_null_source_path_differs_from_empty_string_source_path() -> None:
    """Per D2+D4 interaction: NULL serializes as JSON null, distinct from ''."""
    null_fp = compute_fingerprint("h", None, "* * * * *", "/x")
    empty_fp = compute_fingerprint("h", "", "* * * * *", "/x")
    assert null_fp != empty_fp


def test_fingerprint_unicode_command_does_not_crash() -> None:
    """ensure_ascii=False — Unicode in command path hashes the source bytes."""
    fp = compute_fingerprint("h", "/etc/crontab", "* * * * *", "/opt/задача.sh")
    assert len(fp) == HEX64


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # Good cases (existing behavior preserved)
        ("/opt/scripts/backup.sh", "backup.sh"),
        ("/usr/bin/true", "true"),
        ("backup.sh", "backup.sh"),
        ("/usr/bin/certbot renew --quiet", "certbot"),
        ("/storage/scripts/startup/startup.sh", "startup.sh"),
        ("/opt/scripts/backup.sh --flag --other", "backup.sh"),
        # Shell-wrapper skips (NEW)
        ("bash /home/jakekausler/backup_library.sh", "backup_library.sh"),
        ("sh /opt/script.sh", "script.sh"),
        ("sudo /usr/bin/foo", "foo"),
        ("nohup /usr/bin/foo &", "foo"),
        ("env VAR=value /usr/bin/foo", "foo"),
        # Conditional guards (NEW)
        ("test -x /usr/sbin/anacron || run-parts", "anacron"),
        ("test -x /sbin/e2scrub_all && /sbin/e2scrub_all -A", "e2scrub_all"),
        ("command -v debian-sa1 > /dev/null && debian-sa1 1 1", "debian-sa1"),
        ("[ -x /usr/lib/php/sessionclean ] && /usr/lib/php/sessionclean", "sessionclean"),
        ("cd / && run-parts --report /etc/cron.hourly", "run-parts"),
        # Pipelines (first binary)
        ("mysqldump -u user -psecret db | gzip > out.gz", "mysqldump"),
        # Edge cases — fallback to "cron"
        ("", "cron"),
        ("   ", "cron"),
        ("test", "cron"),
        ("bash", "cron"),
        ("test -x /path", "path"),  # test is wrapper, -x is flag, /path's basename is path
        ("/", "cron"),
    ],
)
def test_derive_name_cases(command: str, expected: str) -> None:
    assert derive_name(command) == expected


def test_migration_seed_fingerprints_match_helper() -> None:
    """Migration 0008's hand-coded seed fingerprints MUST agree with the
    kernel helper. If this test fails, the migration's _SEED_ROWS table is
    stale and the migration will abort at upgrade time."""
    # Mirror the four seed rows from 0008_cron_fingerprint_redesign.py.
    cases = [
        (
            "homelab-host",
            "/etc/crontab",
            "*/5 * * * *",
            "/opt/scripts/observe-job.sh",
            "9455960fd5210182e96ff98baf929d9de0be4ba52766e6f5b02ea5e612cd7d86",
        ),
        (
            "homelab-host",
            "/etc/cron.d/heartbeat-demo",
            "0 * * * *",
            "/opt/scripts/heartbeat-job.sh",
            "532041fb1598f9cfb40e08dc8aec07ddce99e6fb3001d81124a3b71e148e64d9",
        ),
        (
            "homelab-host",
            "/etc/cron.d/both-demo",
            "@daily",
            "/opt/scripts/both-job.sh",
            "23d48f4bb8f816d34bc523805ab7161d368f2a108314c7d079fa610aa93359d2",
        ),
        (
            "remote-host",
            None,
            "*/15 * * * *",
            "/opt/remote/stale-job.sh",
            "c54a8658dc597650761c7efebddde49794dc19275c8b125750606f3a6a11bc30",
        ),
    ]
    for host, source_path, schedule, command, expected in cases:
        assert compute_fingerprint(host, source_path, schedule, command) == expected
