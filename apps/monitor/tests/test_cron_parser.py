"""Tests for cron_parser module (STAGE-002-007)."""

from pathlib import Path

import pytest

from homelab_monitor.kernel.cron.discovery_types import CronSourceKind
from homelab_monitor.plugins.discoverers.cron_parser import parse_cron_file

FIXTURE_DIR = Path(__file__).parent / "data" / "cron_fixtures"
HOST = "test-host"


@pytest.mark.parametrize(
    ("content", "expected_entries", "expected_errors"),
    [
        ("", 0, 0),
        ("\n\n\n", 0, 0),
        ("# comment\n", 0, 0),
        ("PATH=/usr/bin\n", 0, 0),
        ("FOO_BAR=baz\n", 0, 0),
    ],
)
def test_parse_skips_non_data_lines(
    content: str, expected_entries: int, expected_errors: int
) -> None:
    """Test that parser skips blank lines, comments, and env vars."""
    entries, errors = parse_cron_file(
        content=content,
        source_kind=CronSourceKind.USER_CRONTAB,
        host=HOST,
        host_source_path="crontab:alice",
    )
    assert len(entries) == expected_entries
    assert len(errors) == expected_errors


def test_parse_reboot_user_crontab() -> None:
    """Test @reboot in user crontab."""
    entries, errors = parse_cron_file(
        content="@reboot /opt/init.sh\n",
        source_kind=CronSourceKind.USER_CRONTAB,
        host=HOST,
        host_source_path="crontab:alice",
    )
    assert len(errors) == 0
    assert len(entries) == 1
    assert entries[0].schedule == "@reboot"
    assert entries[0].command == "/opt/init.sh"


def test_parse_system_with_user_field() -> None:
    """Test 6-field system crontab line."""
    entries, errors = parse_cron_file(
        content="*/5 * * * * root /bin/true\n",
        source_kind=CronSourceKind.SYSTEM_WITH_USER_FIELD,
        host=HOST,
        host_source_path="/etc/cron.d/test",
    )
    assert errors == []
    assert len(entries) == 1
    assert entries[0].schedule == "*/5 * * * *"
    assert entries[0].command == "/bin/true"


def test_parse_invalid_schedule_records_error() -> None:
    """Test that invalid schedules are recorded as errors."""
    entries, errors = parse_cron_file(
        content="*/X * * * * /opt/foo\n",
        source_kind=CronSourceKind.USER_CRONTAB,
        host=HOST,
        host_source_path="crontab:alice",
    )
    assert len(entries) == 0
    assert len(errors) == 1
    assert "invalid schedule" in errors[0].error


def test_parse_system_cron_fixture() -> None:
    """Test parsing the system_cron.example fixture."""
    content = (FIXTURE_DIR / "system_cron.example").read_text()
    entries, errors = parse_cron_file(
        content=content,
        source_kind=CronSourceKind.SYSTEM_WITH_USER_FIELD,
        host=HOST,
        host_source_path="/etc/cron.d/example",
    )
    # 4 valid (backup, rtlamr, certbot, @hourly), 1 malformed
    assert len(entries) == 4  # noqa: PLR2004
    assert len(errors) == 1


def test_parse_user_crontab_fixture() -> None:
    """Test parsing the user_crontab.example fixture."""
    content = (FIXTURE_DIR / "user_crontab.example").read_text()
    entries, errors = parse_cron_file(
        content=content,
        source_kind=CronSourceKind.USER_CRONTAB,
        host=HOST,
        host_source_path="crontab:alice",
    )
    assert len(errors) == 0
    assert len(entries) == 3  # noqa: PLR2004


def test_parse_reboot_only_fixture() -> None:
    """Test parsing the reboot_only.example fixture."""
    content = (FIXTURE_DIR / "reboot_only.example").read_text()
    entries, errors = parse_cron_file(
        content=content,
        source_kind=CronSourceKind.USER_CRONTAB,
        host=HOST,
        host_source_path="crontab:bob",
    )
    assert len(errors) == 0
    assert len(entries) == 1
    assert entries[0].schedule == "@reboot"


# ---------------------------------------------------------------------------
# _parse_nickname_line — malformed (no command) (lines 136-137)
# ---------------------------------------------------------------------------


def test_parse_nickname_no_command_records_error() -> None:
    """@reboot with no following command raises ValueError → error captured."""
    entries, errors = parse_cron_file(
        content="@reboot\n",
        source_kind=CronSourceKind.USER_CRONTAB,
        host=HOST,
        host_source_path="crontab:alice",
    )
    assert len(entries) == 0
    assert len(errors) == 1
    assert "malformed nickname line" in errors[0].error


# ---------------------------------------------------------------------------
# _parse_nickname_line — unknown nickname (lines 140-141)
# ---------------------------------------------------------------------------


def test_parse_unknown_nickname_records_error() -> None:
    """Unknown @foobar nickname raises ValueError → error captured."""
    entries, errors = parse_cron_file(
        content="@foobar /usr/bin/script\n",
        source_kind=CronSourceKind.USER_CRONTAB,
        host=HOST,
        host_source_path="crontab:alice",
    )
    assert len(entries) == 0
    assert len(errors) == 1
    assert "unknown cron nickname" in errors[0].error


# ---------------------------------------------------------------------------
# _parse_nickname_line — SYSTEM_WITH_USER_FIELD without user field (lines 156-157)
# ---------------------------------------------------------------------------


def test_parse_system_nickname_no_user_field_falls_back_to_root() -> None:
    """@daily /usr/bin/backup in SYSTEM_WITH_USER_FIELD falls back to user=root."""
    entries, errors = parse_cron_file(
        content="@daily /usr/bin/backup\n",
        source_kind=CronSourceKind.SYSTEM_WITH_USER_FIELD,
        host=HOST,
        host_source_path="/etc/cron.d/test",
    )
    assert errors == []
    assert len(entries) == 1
    assert entries[0].command == "/usr/bin/backup"
