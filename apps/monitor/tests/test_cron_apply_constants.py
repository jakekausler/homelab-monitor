"""Tests for cron_apply_constants (STAGE-002-012 additions).

Covers:
- is_valid_target_crontab accepts valid targets, rejects invalid ones
- get_ipc_dir returns env-var value when set, /host-ipc default otherwise
- Fixed-path constants have expected values (regression guard)
- STAGE-002-012 additions: OP_WRITE_WRAPPER_ENV, WRAPPER_ENV_HOST_PATH
"""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.cron.cron_apply_constants import (
    OP_WRITE_WRAPPER_ENV,
    TOKEN_HOST_PATH,
    WRAPPER_ENV_HOST_PATH,
    WRAPPER_SCRIPT_HOST_PATH,
    get_ipc_dir,
    is_valid_target_crontab,
)

# ---------------------------------------------------------------------------
# is_valid_target_crontab — accept cases
# ---------------------------------------------------------------------------


def test_valid_etc_crontab() -> None:
    assert is_valid_target_crontab("/etc/crontab") is True


def test_valid_cron_d_simple() -> None:
    assert is_valid_target_crontab("/etc/cron.d/foo") is True


def test_valid_cron_d_with_dashes() -> None:
    assert is_valid_target_crontab("/etc/cron.d/my-job-01") is True


def test_valid_user_crontab() -> None:
    assert is_valid_target_crontab("crontab:alice") is True


def test_valid_user_crontab_with_dashes() -> None:
    assert is_valid_target_crontab("crontab:some-user") is True


# ---------------------------------------------------------------------------
# is_valid_target_crontab — reject cases
# ---------------------------------------------------------------------------


def test_reject_etc_passwd() -> None:
    assert is_valid_target_crontab("/etc/passwd") is False


def test_reject_path_traversal_cron_d() -> None:
    assert is_valid_target_crontab("/etc/cron.d/../shadow") is False


def test_reject_nested_cron_d() -> None:
    assert is_valid_target_crontab("/etc/cron.d/foo/bar") is False


def test_reject_empty_cron_d_name() -> None:
    assert is_valid_target_crontab("/etc/cron.d/") is False


def test_reject_empty_user() -> None:
    assert is_valid_target_crontab("crontab:") is False


def test_reject_user_with_slash() -> None:
    assert is_valid_target_crontab("crontab:a/b") is False


def test_reject_user_traversal() -> None:
    assert is_valid_target_crontab("crontab:../../etc/passwd") is False


def test_reject_arbitrary_string() -> None:
    assert is_valid_target_crontab("totally-invalid") is False


def test_reject_empty_string() -> None:
    assert is_valid_target_crontab("") is False


def test_reject_slash_only() -> None:
    assert is_valid_target_crontab("/") is False


# ---------------------------------------------------------------------------
# get_ipc_dir
# ---------------------------------------------------------------------------


def test_get_ipc_dir_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HM_CRON_APPLY_IPC_DIR", raising=False)
    result = get_ipc_dir()
    assert str(result) == "/host-ipc"


def test_get_ipc_dir_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HM_CRON_APPLY_IPC_DIR", "/tmp/my-ipc-dir")
    result = get_ipc_dir()
    assert str(result) == "/tmp/my-ipc-dir"


# ---------------------------------------------------------------------------
# Fixed-path constant regression guards (pre-existing)
# ---------------------------------------------------------------------------


def test_wrapper_script_host_path() -> None:
    """WRAPPER_SCRIPT_HOST_PATH must match the bash apply-script constant."""
    assert WRAPPER_SCRIPT_HOST_PATH == "/usr/local/bin/cron-with-heartbeat.sh"


def test_token_host_path() -> None:
    """TOKEN_HOST_PATH must match the bash apply-script constant."""
    assert TOKEN_HOST_PATH == "/etc/homelab-monitor/heartbeat.token"


# ---------------------------------------------------------------------------
# STAGE-002-012 new constants
# ---------------------------------------------------------------------------


def test_op_write_wrapper_env_value() -> None:
    """OP_WRITE_WRAPPER_ENV must match the bash apply-script op kind string."""
    assert OP_WRITE_WRAPPER_ENV == "write-wrapper-env"


def test_wrapper_env_host_path_value() -> None:
    """WRAPPER_ENV_HOST_PATH must match the bash apply-script fixed path."""
    assert WRAPPER_ENV_HOST_PATH == "/etc/homelab-monitor/wrapper.env"
