"""Tests for secret-pattern scrubbing in cron commands (STAGE-002-007D)."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.cron.secrets import scrub_secrets


class TestScrubSecrets:
    """Parametrized tests for common secret patterns."""

    @pytest.mark.parametrize(
        ("input_cmd", "expected"),
        [
            # MySQL/MariaDB: -pPASSWORD (no space after -p)
            (
                "mysqldump -u user -psecret123 db",
                "mysqldump -u user -p<redacted> db",
            ),
            # Quoted password with no spaces is scrubbed
            (
                "mysqldump -u user -p'mysecret' db",
                "mysqldump -u user -p<redacted> db",
            ),
            # Long-form with equals: --password=VALUE
            (
                "psql --password=hunter2 mydb",
                "psql --password=<redacted> mydb",
            ),
            # Long-form with space: --password VALUE
            (
                "psql --password hunter2 mydb",
                "psql --password <redacted> mydb",
            ),
            # Environment variable: MYSQL_PWD
            (
                "MYSQL_PWD=foo mysqldump db",
                "MYSQL_PWD=<redacted> mysqldump db",
            ),
            # Environment variable: PGPASSWORD
            (
                "PGPASSWORD=foo psql db",
                "PGPASSWORD=<redacted> psql db",
            ),
            # API key with equals
            (
                "curl -H 'Authorization: Bearer xyz' --api-key=secret https://x",
                "curl -H 'Authorization: Bearer xyz' --api-key=<redacted> https://x",
            ),
            # API key variant: --apikey (no hyphen), output preserves the form
            (
                "curl --apikey=mysecret https://example.com",
                "curl --apikey=<redacted> https://example.com",
            ),
            # Token pattern
            (
                "curl --token=abc123xyz https://api.example.com/data",
                "curl --token=<redacted> https://api.example.com/data",
            ),
            # No secrets — unchanged
            (
                "/usr/bin/backup.sh",
                "/usr/bin/backup.sh",
            ),
            # SSH port flag (-p with space): NOT mysql password, should NOT be scrubbed
            (
                "ssh -p 22 user@host",
                "ssh -p 22 user@host",
            ),
            # Multiple secrets in one command
            (
                "MYSQL_PWD=secret1 mysqldump --user=admin -psecret2 db | gzip",
                "MYSQL_PWD=<redacted> mysqldump --user=admin -p<redacted> db | gzip",
            ),
            # run-parts FALSE POSITIVE FIX: embedded -p should NOT be scrubbed
            (
                "run-parts --report /etc/cron.hourly",
                "run-parts --report /etc/cron.hourly",
            ),
            # run-parts in pipeline: still should NOT be scrubbed
            (
                "cd / && run-parts /etc/cron.daily",
                "cd / && run-parts /etc/cron.daily",
            ),
            # run-parts with complex condition: embedded -p should NOT be scrubbed
            (
                "test -x /usr/sbin/anacron || run-parts /etc/cron.weekly",
                "test -x /usr/sbin/anacron || run-parts /etc/cron.weekly",
            ),
            # --apropos: -p at start of flag, not a password flag, should NOT be scrubbed
            (
                "--apropos search",
                "--apropos search",
            ),
            # --prefix: similar pattern, should NOT be scrubbed
            (
                "--prefix=/usr/local",
                "--prefix=/usr/local",
            ),
            # mysqldump with -p at start of string (SHOULD be scrubbed)
            (
                "-psecret_at_start",
                "-p<redacted>",
            ),
            # mysqldump -p variant with both long and short form
            (
                "mysqldump --password=foo -psecret db",
                "mysqldump --password=<redacted> -p<redacted> db",
            ),
            # Empty command
            (
                "",
                "",
            ),
            # Whitespace-only command
            (
                "   ",
                "   ",
            ),
        ],
    )
    def test_scrub_secrets(self, input_cmd: str, expected: str) -> None:
        """Test scrubbing of various secret patterns."""
        assert scrub_secrets(input_cmd) == expected

    def test_real_world_mysqldump_with_password(self) -> None:
        """Test a realistic mysqldump command with password in command line."""
        input_cmd = "mysqldump -u root -pmyS3curePass123! --all-databases > /backup/full.sql"
        expected = "mysqldump -u root -p<redacted> --all-databases > /backup/full.sql"
        assert scrub_secrets(input_cmd) == expected

    def test_mixed_env_vars_and_flags(self) -> None:
        """Test command with both env vars and password flags."""
        input_cmd = (
            "PGPASSWORD=db_secret psql -h localhost --user=postgres --password=cli_secret mydb"
        )
        expected = (
            "PGPASSWORD=<redacted> psql -h localhost --user=postgres --password=<redacted> mydb"
        )
        assert scrub_secrets(input_cmd) == expected
