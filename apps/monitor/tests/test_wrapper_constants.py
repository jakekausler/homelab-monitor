"""Tests for kernel/cron/wrapper_constants.py (STAGE-002-012 rewrite).

Covers build_invocation_prefix, unwrap_command, is_wrapped, wrapped_fingerprint,
and the new WRAPPER_ENV_PATH / WRAPPER_FORMAT_VERSION constants.
100% branch coverage required.
"""

from __future__ import annotations

from homelab_monitor.kernel.cron.wrapper_constants import (
    WRAPPER_ENV_PATH,
    WRAPPER_FORMAT_VERSION,
    WRAPPER_PATH,
    WRAPPER_SEPARATOR,
    build_invocation_prefix,
    is_legacy_wrapped,
    is_wrapped,
    unwrap_command,
    wrapped_fingerprint,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_wrapper_env_path_value() -> None:
    assert WRAPPER_ENV_PATH == "/etc/homelab-monitor/wrapper.env"


def test_format_version_is_semver() -> None:
    """WRAPPER_FORMAT_VERSION must be a clean 3-part semver."""
    assert WRAPPER_FORMAT_VERSION == "1.0.0"
    parts = WRAPPER_FORMAT_VERSION.split(".")
    assert len(parts) == 3  # noqa: PLR2004
    assert all(p.isdigit() for p in parts)


# ---------------------------------------------------------------------------
# build_invocation_prefix
# ---------------------------------------------------------------------------


def test_build_invocation_prefix_simple() -> None:
    prefix = build_invocation_prefix("abc")
    assert prefix == f"{WRAPPER_PATH} abc {WRAPPER_SEPARATOR} "


def test_build_invocation_prefix_includes_trailing_space() -> None:
    prefix = build_invocation_prefix("fp1")
    assert prefix.endswith(" ")


def test_build_invocation_prefix_different_fingerprints() -> None:
    p1 = build_invocation_prefix("fp1")
    p2 = build_invocation_prefix("fp2")
    assert p1 != p2
    assert "fp1" in p1
    assert "fp2" in p2


# ---------------------------------------------------------------------------
# unwrap_command
# ---------------------------------------------------------------------------


def test_unwrap_plain_unchanged() -> None:
    cmd = "/usr/bin/backup.sh --full"
    assert unwrap_command(cmd) == cmd


def test_unwrap_wrapped_strips() -> None:
    inner = "/bin/x"
    wrapped = build_invocation_prefix("fp1") + inner
    assert unwrap_command(wrapped) == inner


def test_unwrap_different_fingerprints() -> None:
    inner = "/usr/local/myscript.sh"
    for fp in ("abc123", "deadbeef00112233"):
        wrapped = build_invocation_prefix(fp) + inner
        assert unwrap_command(wrapped) == inner


def test_unwrap_empty_string() -> None:
    assert unwrap_command("") == ""


def test_unwrap_single_layer() -> None:
    """Double-wrapped → one layer stripped, leaving still-wrapped form."""
    inner = "/bin/true"
    wrapped_once = build_invocation_prefix("fp") + inner
    wrapped_twice = build_invocation_prefix("fp") + wrapped_once
    assert unwrap_command(wrapped_twice) == wrapped_once
    assert unwrap_command(unwrap_command(wrapped_twice)) == inner


def test_unwrap_command_with_double_dash_inside() -> None:
    """Inner command containing ' -- ' — the FIRST ' -- ' after fp delimits."""
    inner = "/bin/x -- y"
    wrapped = build_invocation_prefix("fp") + inner
    assert unwrap_command(wrapped) == inner


# ---------------------------------------------------------------------------
# is_wrapped
# ---------------------------------------------------------------------------


def test_is_wrapped_true() -> None:
    cmd = build_invocation_prefix("abc") + "/usr/bin/backup.sh"
    assert is_wrapped(cmd) is True


def test_is_wrapped_false_for_plain() -> None:
    assert is_wrapped("/usr/bin/backup.sh") is False


def test_is_wrapped_false_for_empty() -> None:
    assert is_wrapped("") is False


def test_is_wrapped_false_for_just_wrapper_path() -> None:
    """Just the wrapper path with no fingerprint/separator is not wrapped."""
    assert is_wrapped(WRAPPER_PATH) is False


# ---------------------------------------------------------------------------
# wrapped_fingerprint
# ---------------------------------------------------------------------------


def test_wrapped_fingerprint_returns_fp() -> None:
    fp = "deadbeef1234"
    cmd = build_invocation_prefix(fp) + "/usr/bin/do-thing"
    assert wrapped_fingerprint(cmd) == fp


def test_wrapped_fingerprint_none_for_plain() -> None:
    assert wrapped_fingerprint("/usr/bin/backup.sh") is None


def test_wrapped_fingerprint_none_for_empty() -> None:
    assert wrapped_fingerprint("") is None


# ---------------------------------------------------------------------------
# LEGACY-format support (STAGE-002-012 format-migration)
# ---------------------------------------------------------------------------


def test_is_wrapped_legacy_format() -> None:
    """Legacy-wrapped command (no fingerprint) is recognized as wrapped."""
    inner = "/usr/bin/backup.sh"
    legacy = f"{WRAPPER_PATH} {WRAPPER_SEPARATOR} {inner}"
    assert is_wrapped(legacy) is True


def test_is_wrapped_new_format_regression() -> None:
    """NEW-format wrapped command still recognized (regression)."""
    inner = "/usr/bin/backup.sh"
    new = build_invocation_prefix("fp123") + inner
    assert is_wrapped(new) is True


def test_is_wrapped_unwrapped_still_false() -> None:
    """Unwrapped command is still False (regression)."""
    assert is_wrapped("/usr/bin/backup.sh") is False


def test_unwrap_command_legacy() -> None:
    """Legacy-wrapped command unwraps correctly."""
    inner = "/bin/test.sh --arg"
    legacy = f"{WRAPPER_PATH} {WRAPPER_SEPARATOR} {inner}"
    assert unwrap_command(legacy) == inner


def test_unwrap_command_new_regression() -> None:
    """NEW-format wrapped command still unwraps (regression)."""
    inner = "/bin/test.sh --arg"
    new = build_invocation_prefix("deadbeef") + inner
    assert unwrap_command(new) == inner


def test_unwrap_command_unwrapped_unchanged() -> None:
    """Unwrapped command unchanged (regression)."""
    cmd = "/bin/test.sh --arg"
    assert unwrap_command(cmd) == cmd


def test_wrapped_fingerprint_legacy_returns_none() -> None:
    """Legacy-wrapped command has no fingerprint (returns None)."""
    inner = "/usr/bin/backup.sh"
    legacy = f"{WRAPPER_PATH} {WRAPPER_SEPARATOR} {inner}"
    assert wrapped_fingerprint(legacy) is None


def test_wrapped_fingerprint_new_returns_fp() -> None:
    """NEW-format wrapped command returns the fingerprint (regression)."""
    fp = "abc123def456"
    inner = "/usr/bin/backup.sh"
    new = build_invocation_prefix(fp) + inner
    assert wrapped_fingerprint(new) == fp


def test_wrapped_fingerprint_unwrapped_none() -> None:
    """Unwrapped command has no fingerprint (regression)."""
    assert wrapped_fingerprint("/usr/bin/backup.sh") is None


def test_is_legacy_wrapped_legacy_only() -> None:
    """is_legacy_wrapped returns True ONLY for legacy format."""
    inner = "/usr/bin/backup.sh"
    legacy = f"{WRAPPER_PATH} {WRAPPER_SEPARATOR} {inner}"
    assert is_legacy_wrapped(legacy) is True


def test_is_legacy_wrapped_new_format_false() -> None:
    """is_legacy_wrapped returns False for NEW-format wrapped."""
    fp = "fp1"
    inner = "/usr/bin/backup.sh"
    new = build_invocation_prefix(fp) + inner
    assert is_legacy_wrapped(new) is False


def test_is_legacy_wrapped_unwrapped_false() -> None:
    """is_legacy_wrapped returns False for unwrapped."""
    assert is_legacy_wrapped("/usr/bin/backup.sh") is False


def test_mutual_exclusivity_legacy_and_new() -> None:
    """A NEW-format line is never is_legacy_wrapped."""
    fp = "fp1"
    inner = "/bin/cmd"
    new = build_invocation_prefix(fp) + inner
    # new IS wrapped
    assert is_wrapped(new) is True
    # but is NOT legacy-wrapped
    assert is_legacy_wrapped(new) is False
    # and its fingerprint is not None
    assert wrapped_fingerprint(new) == fp
