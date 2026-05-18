"""Tests for kernel/cron/wrapper_constants.py (STAGE-002-009).

Covers unwrap_command(), is_wrapped(), and constant values.
100% branch coverage required.
"""

from __future__ import annotations

from homelab_monitor.kernel.cron.wrapper_constants import (
    WRAPPER_INVOCATION_PREFIX,
    WRAPPER_PATH,
    WRAPPER_SEPARATOR,
    is_wrapped,
    unwrap_command,
)


def test_constants_are_consistent() -> None:
    """WRAPPER_INVOCATION_PREFIX is built from WRAPPER_PATH + sep + space."""
    assert f"{WRAPPER_PATH} {WRAPPER_SEPARATOR} " == WRAPPER_INVOCATION_PREFIX


def test_unwrap_command_plain_returns_unchanged() -> None:
    """A plain command (not wrapped) is returned unchanged."""
    cmd = "/usr/bin/backup.sh --full"
    assert unwrap_command(cmd) == cmd


def test_unwrap_command_wrapped_strips_prefix() -> None:
    """A wrapped command has the prefix stripped, returning the inner command."""
    inner = "/usr/bin/backup.sh --full"
    wrapped = WRAPPER_INVOCATION_PREFIX + inner
    assert unwrap_command(wrapped) == inner


def test_unwrap_command_empty_string() -> None:
    """Empty string is returned unchanged (not wrapped)."""
    assert unwrap_command("") == ""


def test_is_wrapped_false_for_plain() -> None:
    assert is_wrapped("/usr/bin/backup.sh") is False


def test_is_wrapped_true_for_wrapped() -> None:
    cmd = WRAPPER_INVOCATION_PREFIX + "/usr/bin/backup.sh"
    assert is_wrapped(cmd) is True


def test_unwrap_idempotent_on_non_wrapped() -> None:
    """Calling unwrap on a non-wrapped command is idempotent."""
    cmd = "/bin/true"
    assert unwrap_command(unwrap_command(cmd)) == cmd


def test_unwrap_only_one_layer() -> None:
    """unwrap_command removes exactly one layer (prefix once)."""
    inner = "/bin/true"
    wrapped_once = WRAPPER_INVOCATION_PREFIX + inner
    wrapped_twice = WRAPPER_INVOCATION_PREFIX + wrapped_once
    # Unwrapping once leaves the still-wrapped form
    assert unwrap_command(wrapped_twice) == wrapped_once
    # Unwrapping twice gets back to inner
    assert unwrap_command(unwrap_command(wrapped_twice)) == inner
