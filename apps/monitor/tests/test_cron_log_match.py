"""Tests for canonical_log_key (STAGE-002-008)."""

from __future__ import annotations

from homelab_monitor.kernel.cron.log_match import canonical_log_key
from homelab_monitor.kernel.cron.secrets import scrub_secrets


def test_plain_command_unchanged() -> None:
    """A plain command with no special cases is unchanged."""
    assert canonical_log_key("/usr/bin/backup.sh") == "/usr/bin/backup.sh"


def test_whitespace_collapsed() -> None:
    """Multiple spaces are collapsed to single spaces."""
    assert canonical_log_key("/bin/foo   --x    --y") == "/bin/foo --x --y"


def test_leading_trailing_stripped() -> None:
    """Leading and trailing whitespace is stripped."""
    assert canonical_log_key("  /bin/foo  ") == "/bin/foo"


def test_strips_one_wrapping_paren_layer() -> None:
    """A single wrapping pair of parentheses is stripped."""
    assert canonical_log_key("(/usr/bin/backup.sh)") == "/usr/bin/backup.sh"


def test_does_not_strip_unwrapped_parens() -> None:
    """Parentheses that don't wrap the whole string are preserved."""
    assert canonical_log_key("(a) && (b)") == "(a) && (b)"


def test_strips_only_one_layer() -> None:
    """Only one layer of wrapping parentheses is stripped."""
    assert canonical_log_key("((/bin/foo))") == "(/bin/foo)"


def test_scrubs_secrets() -> None:
    """Secrets (passwords) are scrubbed."""
    assert canonical_log_key("mysqldump -u root -psecret db") == "mysqldump -u root -p<redacted> db"


def test_idempotent_on_scrubbed_input() -> None:
    """Applying canonical_log_key twice on scrubbed input is idempotent."""
    x = "(mysqldump -u root -psecret db)"
    result1 = canonical_log_key(x)
    result2 = canonical_log_key(result1)
    assert result1 == result2


def test_disk_and_log_form_converge() -> None:
    """The disk and log forms of a command converge to the same key."""
    # Disk form (stored in crons.command as scrubbed):
    raw_command = "mysqldump -u root -psecret db"
    disk_form = scrub_secrets(raw_command)
    disk_key = canonical_log_key(disk_form)

    # Log form (vanilla cron logs it wrapped):
    log_form = f"({raw_command})"
    log_key = canonical_log_key(log_form)

    assert disk_key == log_key


def test_empty_string() -> None:
    """An empty string remains empty."""
    assert canonical_log_key("") == ""


def test_only_whitespace() -> None:
    """A string of only whitespace becomes empty."""
    assert canonical_log_key("   ") == ""
