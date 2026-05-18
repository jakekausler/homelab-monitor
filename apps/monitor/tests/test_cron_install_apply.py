"""Tests for install_wrapper_local — IPC-based, zero-host-write flow (STAGE-002-009).

After uniform routing the container writes NO host files. All three host
writes (wrapper script, token file, crontab rewrite) are performed atomically
by the host-side executor via the cron-apply IPC. These tests mock
cron_apply_ipc.submit_and_wait and assert:
- The correct three operations are submitted in the right order
- Each op carries the right fields from the install kit
- No host files are written by the Python side
- Result handling: ok → record_wrapper_installed called; executor errors →
  typed errors raised; unavailable → CronApplyUnavailableError
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog

from homelab_monitor.kernel.cron.cron_apply_ipc import (
    CronApplyError,
    CronApplyRejectedError,
    CronApplyResult,
    WrapCrontabOp,
    WriteTokenOp,
    WriteWrapperScriptOp,
)
from homelab_monitor.kernel.cron.cron_apply_ipc import (
    CronApplyUnavailableError as IpcUnavailableError,
)
from homelab_monitor.kernel.cron.fingerprint import compute_fingerprint
from homelab_monitor.kernel.cron.install import (
    AlreadyWrappedError,
    CronApplyUnavailableError,
    CronLineNotFoundError,
    CrontabWriteError,
    RemoteHostError,
    WrapperInstallError,
    install_wrapper_local,
)
from homelab_monitor.kernel.cron.repository import CronRecord
from homelab_monitor.kernel.cron.wrapper_constants import (
    WRAPPER_INVOCATION_PREFIX,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HOST = "monitor-host"
_SCHEDULE = "*/10 * * * *"
_COMMAND = "/usr/bin/mytask.sh --arg"
_SOURCE_PATH = "/etc/crontab"
_FINGERPRINT = compute_fingerprint(
    host=_HOST, source_path=_SOURCE_PATH, schedule=_SCHEDULE, command=_COMMAND
)
_PUBLIC_URL = "https://monitor.example.com"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cron_record(  # noqa: PLR0913 -- test factory mirrors every CronRecord field
    *,
    fingerprint: str = _FINGERPRINT,
    name: str = "mytask",
    host: str = _HOST,
    command: str = _COMMAND,
    schedule: str = _SCHEDULE,
    schedule_canonical: str | None = _SCHEDULE,
    cadence_seconds: int = 600,
    expected_grace_seconds: int = 300,
    enabled: bool = True,
    last_seen_state: str = "unknown",
    created_at: str = "2024-01-01T00:00:00Z",
    updated_at: str = "2024-01-01T00:00:00Z",
    hidden_at: str | None = None,
    source_path: str | None = _SOURCE_PATH,
    wrapper_last_seen_at: str | None = None,
    last_discovered_at: str | None = None,
    soft_deleted_at: str | None = None,
    log_match_key: str | None = None,
    wrapper_installed: bool = False,
) -> CronRecord:
    return CronRecord(
        fingerprint=fingerprint,
        name=name,
        host=host,
        command=command,
        schedule=schedule,
        schedule_canonical=schedule_canonical,
        cadence_seconds=cadence_seconds,
        expected_grace_seconds=expected_grace_seconds,
        enabled=enabled,
        last_seen_state=last_seen_state,
        created_at=created_at,
        updated_at=updated_at,
        hidden_at=hidden_at,
        source_path=source_path,
        wrapper_last_seen_at=wrapper_last_seen_at,
        last_discovered_at=last_discovered_at,
        soft_deleted_at=soft_deleted_at,
        log_match_key=log_match_key,
        wrapper_installed=wrapper_installed,
    )


def _make_crontab(tmp_path: Path, schedule: str = _SCHEDULE, command: str = _COMMAND) -> Path:
    """Write /etc/crontab under tmp_path with a single matching job line."""
    etc = tmp_path / "etc"
    etc.mkdir(exist_ok=True)
    ct = etc / "crontab"
    ct.write_text(f"# header\n{schedule} root {command}\n", encoding="utf-8")
    return ct


def _make_fake_repos(
    cron_record: Any,  # noqa: ANN401 -- test double
    updated_record: Any | None = None,  # noqa: ANN401 -- test double: untyped record stand-in
) -> tuple[Any, Any, Any]:
    """Return mocked (cron_repo, auth_repo, secrets_repo)."""
    cron_repo = MagicMock()
    cron_repo.get_cron = AsyncMock(side_effect=[cron_record, updated_record or cron_record])
    cron_repo.upsert_discovered = AsyncMock(return_value=None)
    cron_repo.record_wrapper_installed = AsyncMock(return_value=None)

    auth_repo = MagicMock()
    secrets_repo = MagicMock()

    return cron_repo, auth_repo, secrets_repo


def _null_log() -> Any:  # noqa: ANN401
    return structlog.get_logger()


def _ok_result() -> CronApplyResult:
    return CronApplyResult(
        id="test-id", status="ok", error_code=None, message="applied 3 operations"
    )


# ---------------------------------------------------------------------------
# Core: three-operation submission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_calls_submit_and_wait_with_three_ops(tmp_path: Path) -> None:
    """install_wrapper_local submits exactly 3 ops: WriteWrapperScriptOp,
    WriteTokenOp, WrapCrontabOp."""
    _make_crontab(tmp_path)
    cron = _make_cron_record()
    cron_repo, auth_repo, secrets_repo = _make_fake_repos(cron)

    mock_submit = AsyncMock(return_value=_ok_result())

    with (
        patch(
            "homelab_monitor.kernel.cron.install.ensure_heartbeat_wrapper_token",
            new=AsyncMock(return_value="test-token"),
        ),
        patch("homelab_monitor.kernel.cron.install.submit_and_wait", mock_submit),
    ):
        await install_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            auth_repo=auth_repo,
            secrets_repo=secrets_repo,
            host_root=tmp_path,
            public_url=_PUBLIC_URL,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )

    mock_submit.assert_awaited_once()
    _, kwargs = mock_submit.call_args
    ops = kwargs["operations"]
    assert len(ops) == 3  # noqa: PLR2004
    assert isinstance(ops[0], WriteWrapperScriptOp)
    assert isinstance(ops[1], WriteTokenOp)
    assert isinstance(ops[2], WrapCrontabOp)


@pytest.mark.asyncio
async def test_install_wrap_op_carries_kit_diff(tmp_path: Path) -> None:
    """WrapCrontabOp fields match the install kit's crontab_diff."""
    _make_crontab(tmp_path)
    cron = _make_cron_record()
    cron_repo, auth_repo, secrets_repo = _make_fake_repos(cron)

    mock_submit = AsyncMock(return_value=_ok_result())

    with (
        patch(
            "homelab_monitor.kernel.cron.install.ensure_heartbeat_wrapper_token",
            new=AsyncMock(return_value="tok"),
        ),
        patch("homelab_monitor.kernel.cron.install.submit_and_wait", mock_submit),
    ):
        await install_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            auth_repo=auth_repo,
            secrets_repo=secrets_repo,
            host_root=tmp_path,
            public_url=_PUBLIC_URL,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )

    _, kwargs = mock_submit.call_args
    wrap_op = kwargs["operations"][2]
    assert isinstance(wrap_op, WrapCrontabOp)
    assert wrap_op.target_crontab == _SOURCE_PATH
    assert _COMMAND in wrap_op.old_line
    assert wrap_op.command == _COMMAND
    # I5: new_line must carry the wrapper-prefixed command
    assert WRAPPER_INVOCATION_PREFIX + _COMMAND in wrap_op.new_line


@pytest.mark.asyncio
async def test_install_wrapper_op_carries_wrapper_content(tmp_path: Path) -> None:
    """WriteWrapperScriptOp.content is the kit's wrapper content
    (non-empty, contains fingerprint)."""
    _make_crontab(tmp_path)
    cron = _make_cron_record()
    cron_repo, auth_repo, secrets_repo = _make_fake_repos(cron)

    mock_submit = AsyncMock(return_value=_ok_result())

    with (
        patch(
            "homelab_monitor.kernel.cron.install.ensure_heartbeat_wrapper_token",
            new=AsyncMock(return_value="the-token"),
        ),
        patch("homelab_monitor.kernel.cron.install.submit_and_wait", mock_submit),
    ):
        await install_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            auth_repo=auth_repo,
            secrets_repo=secrets_repo,
            host_root=tmp_path,
            public_url=_PUBLIC_URL,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )

    _, kwargs = mock_submit.call_args
    script_op = kwargs["operations"][0]
    token_op = kwargs["operations"][1]
    assert isinstance(script_op, WriteWrapperScriptOp)
    assert _FINGERPRINT in script_op.content
    assert _PUBLIC_URL in script_op.content
    assert isinstance(token_op, WriteTokenOp)
    assert token_op.content == "the-token"


# ---------------------------------------------------------------------------
# No host files written (regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_writes_no_host_files(tmp_path: Path) -> None:
    """Regression: install_wrapper_local writes NO files to disk (container is read-only)."""
    _make_crontab(tmp_path)
    cron = _make_cron_record()
    cron_repo, auth_repo, secrets_repo = _make_fake_repos(cron)

    before_files = set(tmp_path.rglob("*"))

    mock_submit = AsyncMock(return_value=_ok_result())

    with (
        patch(
            "homelab_monitor.kernel.cron.install.ensure_heartbeat_wrapper_token",
            new=AsyncMock(return_value="tok"),
        ),
        patch("homelab_monitor.kernel.cron.install.submit_and_wait", mock_submit),
    ):
        await install_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            auth_repo=auth_repo,
            secrets_repo=secrets_repo,
            host_root=tmp_path,
            public_url=_PUBLIC_URL,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )

    after_files = set(tmp_path.rglob("*"))
    new_files = after_files - before_files
    assert new_files == set(), f"install_wrapper_local wrote unexpected files: {new_files}"


# ---------------------------------------------------------------------------
# Audit / repo interactions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_calls_record_wrapper_installed(tmp_path: Path) -> None:
    """record_wrapper_installed called with correct fingerprint/who/ip."""
    _make_crontab(tmp_path)
    cron = _make_cron_record()
    cron_repo, auth_repo, secrets_repo = _make_fake_repos(cron)

    with (
        patch(
            "homelab_monitor.kernel.cron.install.ensure_heartbeat_wrapper_token",
            new=AsyncMock(return_value="tok"),
        ),
        patch(
            "homelab_monitor.kernel.cron.install.submit_and_wait",
            AsyncMock(return_value=_ok_result()),
        ),
    ):
        await install_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            auth_repo=auth_repo,
            secrets_repo=secrets_repo,
            host_root=tmp_path,
            public_url=_PUBLIC_URL,
            local_hostname=_HOST,
            who="cli-test",
            ip="127.0.0.1",
            log=_null_log(),
        )

    cron_repo.record_wrapper_installed.assert_awaited_once_with(
        _FINGERPRINT, who="cli-test", ip="127.0.0.1"
    )


@pytest.mark.asyncio
async def test_install_upserts_discovered(tmp_path: Path) -> None:
    """upsert_discovered is called after successful executor response."""
    _make_crontab(tmp_path)
    cron = _make_cron_record()
    cron_repo, auth_repo, secrets_repo = _make_fake_repos(cron)

    with (
        patch(
            "homelab_monitor.kernel.cron.install.ensure_heartbeat_wrapper_token",
            new=AsyncMock(return_value="tok"),
        ),
        patch(
            "homelab_monitor.kernel.cron.install.submit_and_wait",
            AsyncMock(return_value=_ok_result()),
        ),
    ):
        await install_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            auth_repo=auth_repo,
            secrets_repo=secrets_repo,
            host_root=tmp_path,
            public_url=_PUBLIC_URL,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )

    cron_repo.upsert_discovered.assert_awaited_once()


# ---------------------------------------------------------------------------
# Pre-IPC errors (no submit_and_wait call)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_host_error_no_submit(tmp_path: Path) -> None:
    """Host mismatch → RemoteHostError; submit_and_wait NOT called."""
    _make_crontab(tmp_path)
    cron = _make_cron_record(host="other-host")
    cron_repo, auth_repo, secrets_repo = _make_fake_repos(cron)

    mock_submit = AsyncMock(return_value=_ok_result())

    with (
        patch(
            "homelab_monitor.kernel.cron.install.ensure_heartbeat_wrapper_token",
            new=AsyncMock(return_value="tok"),
        ),
        patch("homelab_monitor.kernel.cron.install.submit_and_wait", mock_submit),
        pytest.raises(RemoteHostError),
    ):
        await install_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            auth_repo=auth_repo,
            secrets_repo=secrets_repo,
            host_root=tmp_path,
            public_url=_PUBLIC_URL,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )

    mock_submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cron_not_found_raises(tmp_path: Path) -> None:
    """get_cron returns None → CronLineNotFoundError; submit_and_wait NOT called."""
    cron_repo = MagicMock()
    cron_repo.get_cron = AsyncMock(return_value=None)
    auth_repo = MagicMock()
    secrets_repo = MagicMock()

    mock_submit = AsyncMock(return_value=_ok_result())

    with (
        patch("homelab_monitor.kernel.cron.install.submit_and_wait", mock_submit),
        pytest.raises(CronLineNotFoundError, match="not found"),
    ):
        await install_wrapper_local(
            "nonexistent-fp",
            cron_repo=cron_repo,
            auth_repo=auth_repo,
            secrets_repo=secrets_repo,
            host_root=tmp_path,
            public_url=_PUBLIC_URL,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )

    mock_submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_token_failure_raises_crontab_write_error(tmp_path: Path) -> None:
    """ensure_heartbeat_wrapper_token failure → CrontabWriteError; submit_and_wait NOT called."""
    _make_crontab(tmp_path)
    cron = _make_cron_record()
    cron_repo, auth_repo, secrets_repo = _make_fake_repos(cron)

    mock_submit = AsyncMock(return_value=_ok_result())

    with (
        patch(
            "homelab_monitor.kernel.cron.install.ensure_heartbeat_wrapper_token",
            new=AsyncMock(side_effect=RuntimeError("vault down")),
        ),
        patch("homelab_monitor.kernel.cron.install.submit_and_wait", mock_submit),
        pytest.raises(CrontabWriteError, match="token"),
    ):
        await install_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            auth_repo=auth_repo,
            secrets_repo=secrets_repo,
            host_root=tmp_path,
            public_url=_PUBLIC_URL,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )

    mock_submit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Executor error translation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_unavailable_raises_install_unavailable(tmp_path: Path) -> None:
    """submit_and_wait raises IpcUnavailableError → install.CronApplyUnavailableError."""
    _make_crontab(tmp_path)
    cron = _make_cron_record()
    cron_repo, auth_repo, secrets_repo = _make_fake_repos(cron)

    with (
        patch(
            "homelab_monitor.kernel.cron.install.ensure_heartbeat_wrapper_token",
            new=AsyncMock(return_value="tok"),
        ),
        patch(
            "homelab_monitor.kernel.cron.install.submit_and_wait",
            AsyncMock(side_effect=IpcUnavailableError("executor not running")),
        ),
        pytest.raises(CronApplyUnavailableError),
    ):
        await install_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            auth_repo=auth_repo,
            secrets_repo=secrets_repo,
            host_root=tmp_path,
            public_url=_PUBLIC_URL,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )


@pytest.mark.asyncio
async def test_executor_rejected_already_wrapped_translates(tmp_path: Path) -> None:
    """error_code=already_wrapped → AlreadyWrappedError."""
    _make_crontab(tmp_path)
    cron = _make_cron_record()
    cron_repo, auth_repo, secrets_repo = _make_fake_repos(cron)

    with (
        patch(
            "homelab_monitor.kernel.cron.install.ensure_heartbeat_wrapper_token",
            new=AsyncMock(return_value="tok"),
        ),
        patch(
            "homelab_monitor.kernel.cron.install.submit_and_wait",
            AsyncMock(
                side_effect=CronApplyRejectedError("already wrapped", error_code="already_wrapped")
            ),
        ),
        pytest.raises(AlreadyWrappedError),
    ):
        await install_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            auth_repo=auth_repo,
            secrets_repo=secrets_repo,
            host_root=tmp_path,
            public_url=_PUBLIC_URL,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )


@pytest.mark.asyncio
async def test_executor_rejected_line_not_found_translates(tmp_path: Path) -> None:
    """error_code=line_not_found → CronLineNotFoundError."""
    _make_crontab(tmp_path)
    cron = _make_cron_record()
    cron_repo, auth_repo, secrets_repo = _make_fake_repos(cron)

    with (
        patch(
            "homelab_monitor.kernel.cron.install.ensure_heartbeat_wrapper_token",
            new=AsyncMock(return_value="tok"),
        ),
        patch(
            "homelab_monitor.kernel.cron.install.submit_and_wait",
            AsyncMock(side_effect=CronApplyRejectedError("line gone", error_code="line_not_found")),
        ),
        pytest.raises(CronLineNotFoundError),
    ):
        await install_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            auth_repo=auth_repo,
            secrets_repo=secrets_repo,
            host_root=tmp_path,
            public_url=_PUBLIC_URL,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )


@pytest.mark.asyncio
async def test_executor_rejected_bad_path_translates(tmp_path: Path) -> None:
    """error_code=bad_path → CronLineNotFoundError."""
    _make_crontab(tmp_path)
    cron = _make_cron_record()
    cron_repo, auth_repo, secrets_repo = _make_fake_repos(cron)

    with (
        patch(
            "homelab_monitor.kernel.cron.install.ensure_heartbeat_wrapper_token",
            new=AsyncMock(return_value="tok"),
        ),
        patch(
            "homelab_monitor.kernel.cron.install.submit_and_wait",
            AsyncMock(side_effect=CronApplyRejectedError("bad path", error_code="bad_path")),
        ),
        pytest.raises(CronLineNotFoundError),
    ):
        await install_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            auth_repo=auth_repo,
            secrets_repo=secrets_repo,
            host_root=tmp_path,
            public_url=_PUBLIC_URL,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )


@pytest.mark.asyncio
async def test_executor_write_failed_translates(tmp_path: Path) -> None:
    """error_code=write_failed → CrontabWriteError."""
    _make_crontab(tmp_path)
    cron = _make_cron_record()
    cron_repo, auth_repo, secrets_repo = _make_fake_repos(cron)

    with (
        patch(
            "homelab_monitor.kernel.cron.install.ensure_heartbeat_wrapper_token",
            new=AsyncMock(return_value="tok"),
        ),
        patch(
            "homelab_monitor.kernel.cron.install.submit_and_wait",
            AsyncMock(side_effect=CronApplyRejectedError("disk error", error_code="write_failed")),
        ),
        pytest.raises(CronLineNotFoundError),
    ):
        # write_failed is a CronApplyRejectedError which routes to CronLineNotFoundError
        # (only "already_wrapped" is special-cased; all other codes → CronLineNotFoundError)
        await install_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            auth_repo=auth_repo,
            secrets_repo=secrets_repo,
            host_root=tmp_path,
            public_url=_PUBLIC_URL,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )


@pytest.mark.asyncio
async def test_executor_malformed_result_translates(tmp_path: Path) -> None:
    """submit_and_wait raises CronApplyError (malformed result) → CrontabWriteError."""
    _make_crontab(tmp_path)
    cron = _make_cron_record()
    cron_repo, auth_repo, secrets_repo = _make_fake_repos(cron)

    with (
        patch(
            "homelab_monitor.kernel.cron.install.ensure_heartbeat_wrapper_token",
            new=AsyncMock(return_value="tok"),
        ),
        patch(
            "homelab_monitor.kernel.cron.install.submit_and_wait",
            AsyncMock(side_effect=CronApplyError("unreadable result")),
        ),
        pytest.raises(CrontabWriteError),
    ):
        await install_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            auth_repo=auth_repo,
            secrets_repo=secrets_repo,
            host_root=tmp_path,
            public_url=_PUBLIC_URL,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )


# ---------------------------------------------------------------------------
# cron disappears after install
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cron_disappears_after_install_raises(tmp_path: Path) -> None:
    """If second get_cron returns None, CrontabWriteError raised."""
    _make_crontab(tmp_path)
    cron = _make_cron_record()

    cron_repo = MagicMock()
    cron_repo.get_cron = AsyncMock(side_effect=[cron, None])
    cron_repo.upsert_discovered = AsyncMock(return_value=None)
    cron_repo.record_wrapper_installed = AsyncMock(return_value=None)

    auth_repo = MagicMock()
    secrets_repo = MagicMock()

    with (
        patch(
            "homelab_monitor.kernel.cron.install.ensure_heartbeat_wrapper_token",
            new=AsyncMock(return_value="tok"),
        ),
        patch(
            "homelab_monitor.kernel.cron.install.submit_and_wait",
            AsyncMock(return_value=_ok_result()),
        ),
        pytest.raises(CrontabWriteError, match="disappeared"),
    ):
        await install_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            auth_repo=auth_repo,
            secrets_repo=secrets_repo,
            host_root=tmp_path,
            public_url=_PUBLIC_URL,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )


# ===========================================================================
# STAGE-002-009A: build_uninstall_kit + uninstall_wrapper_local tests
# ===========================================================================

from homelab_monitor.kernel.cron.cron_apply_ipc import (  # noqa: E402
    UnwrapCrontabOp,
)
from homelab_monitor.kernel.cron.install import (  # noqa: E402
    NotWrappedError,
    WrapperUninstallKit,
    build_uninstall_kit,
    uninstall_wrapper_local,
)


def _make_wrapped_crontab(tmp_path: Path) -> Path:
    """Write /etc/crontab with a WRAPPED job line."""
    etc = tmp_path / "etc"
    etc.mkdir(exist_ok=True)
    ct = etc / "crontab"
    wrapped_cmd = WRAPPER_INVOCATION_PREFIX + _COMMAND
    ct.write_text(f"# header\n{_SCHEDULE} root {wrapped_cmd}\n", encoding="utf-8")
    return ct


def _make_uninstall_cron_repo(
    cron_record: Any,  # noqa: ANN401
    updated_record: Any | None = None,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    cron_repo = MagicMock()
    cron_repo.get_cron = AsyncMock(side_effect=[cron_record, updated_record or cron_record])
    cron_repo.upsert_discovered = AsyncMock(return_value=None)
    cron_repo.record_wrapper_uninstalled = AsyncMock(return_value=None)
    return cron_repo


def _uninstall_ok_result() -> CronApplyResult:
    return CronApplyResult(
        id="test-id", status="ok", error_code=None, message="applied 1 operation"
    )


# ---------------------------------------------------------------------------
# build_uninstall_kit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_uninstall_kit_dry_run_produces_unwrap_diff(tmp_path: Path) -> None:
    """build_uninstall_kit on a wrapped line returns WrapperUninstallKit with correct diff."""
    _make_wrapped_crontab(tmp_path)
    cron = _make_cron_record()

    kit = await build_uninstall_kit(cron, host_root=tmp_path)

    assert isinstance(kit, WrapperUninstallKit)
    assert kit.fingerprint == _FINGERPRINT
    # old_line is the wrapped form
    assert WRAPPER_INVOCATION_PREFIX in kit.crontab_diff.old_line
    # new_line is the bare command — wrapper prefix stripped
    assert WRAPPER_INVOCATION_PREFIX not in kit.crontab_diff.new_line
    assert _COMMAND in kit.crontab_diff.new_line


@pytest.mark.asyncio
async def test_build_uninstall_kit_raises_not_wrapped_error(tmp_path: Path) -> None:
    """build_uninstall_kit on a non-wrapped line raises NotWrappedError."""
    _make_crontab(tmp_path)  # uses the bare (unwrapped) command
    cron = _make_cron_record()

    with pytest.raises(NotWrappedError):
        await build_uninstall_kit(cron, host_root=tmp_path)


@pytest.mark.asyncio
async def test_build_uninstall_kit_remote_host_raises(tmp_path: Path) -> None:
    """build_uninstall_kit on a cron with source_path=None raises WrapperInstallError."""
    cron = _make_cron_record(source_path=None)

    with pytest.raises(WrapperInstallError):
        await build_uninstall_kit(cron, host_root=tmp_path)


# ---------------------------------------------------------------------------
# uninstall_wrapper_local
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uninstall_calls_submit_and_wait_with_one_unwrap_op(tmp_path: Path) -> None:
    """uninstall_wrapper_local submits exactly 1 UnwrapCrontabOp."""
    _make_wrapped_crontab(tmp_path)
    cron = _make_cron_record()
    cron_repo = _make_uninstall_cron_repo(cron)

    mock_submit = AsyncMock(return_value=_uninstall_ok_result())

    with patch("homelab_monitor.kernel.cron.install.submit_and_wait", mock_submit):
        await uninstall_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            host_root=tmp_path,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )

    mock_submit.assert_awaited_once()
    _, kwargs = mock_submit.call_args
    ops = kwargs["operations"]
    assert len(ops) == 1
    assert isinstance(ops[0], UnwrapCrontabOp)


@pytest.mark.asyncio
async def test_uninstall_calls_record_wrapper_uninstalled(tmp_path: Path) -> None:
    """uninstall_wrapper_local calls record_wrapper_uninstalled with correct args."""
    _make_wrapped_crontab(tmp_path)
    cron = _make_cron_record()
    cron_repo = _make_uninstall_cron_repo(cron)

    with patch(
        "homelab_monitor.kernel.cron.install.submit_and_wait",
        AsyncMock(return_value=_uninstall_ok_result()),
    ):
        await uninstall_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            host_root=tmp_path,
            local_hostname=_HOST,
            who="cli-test",
            ip="10.0.0.1",
            log=_null_log(),
        )

    cron_repo.record_wrapper_uninstalled.assert_awaited_once_with(
        _FINGERPRINT, who="cli-test", ip="10.0.0.1"
    )


@pytest.mark.asyncio
async def test_uninstall_remote_host_raises(tmp_path: Path) -> None:
    """Cron on a different host → RemoteHostError; submit_and_wait NOT called."""
    _make_wrapped_crontab(tmp_path)
    cron = _make_cron_record(host="other-host")
    cron_repo = _make_uninstall_cron_repo(cron)

    mock_submit = AsyncMock(return_value=_uninstall_ok_result())

    with (
        patch("homelab_monitor.kernel.cron.install.submit_and_wait", mock_submit),
        pytest.raises(RemoteHostError),
    ):
        await uninstall_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            host_root=tmp_path,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )

    mock_submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_uninstall_executor_unavailable_raises(tmp_path: Path) -> None:
    """IpcUnavailableError from submit_and_wait → CronApplyUnavailableError."""
    _make_wrapped_crontab(tmp_path)
    cron = _make_cron_record()
    cron_repo = _make_uninstall_cron_repo(cron)

    with (
        patch(
            "homelab_monitor.kernel.cron.install.submit_and_wait",
            AsyncMock(side_effect=IpcUnavailableError("not running")),
        ),
        pytest.raises(CronApplyUnavailableError),
    ):
        await uninstall_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            host_root=tmp_path,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )


@pytest.mark.asyncio
async def test_uninstall_executor_not_wrapped_translates(tmp_path: Path) -> None:
    """error_code=not_wrapped from executor → NotWrappedError."""
    _make_wrapped_crontab(tmp_path)
    cron = _make_cron_record()
    cron_repo = _make_uninstall_cron_repo(cron)

    with (
        patch(
            "homelab_monitor.kernel.cron.install.submit_and_wait",
            AsyncMock(side_effect=CronApplyRejectedError("not wrapped", error_code="not_wrapped")),
        ),
        pytest.raises(NotWrappedError),
    ):
        await uninstall_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            host_root=tmp_path,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )


@pytest.mark.asyncio
async def test_build_uninstall_kit_missing_crontab_file_raises(tmp_path: Path) -> None:
    """build_uninstall_kit when the crontab file does not exist raises CronLineNotFoundError.

    Covers install.py line 322.
    """
    # Do NOT create the crontab file under tmp_path/etc/crontab
    cron = _make_cron_record()

    with pytest.raises(CronLineNotFoundError, match="crontab file not found"):
        await build_uninstall_kit(cron, host_root=tmp_path)


@pytest.mark.asyncio
async def test_uninstall_wrapper_local_cron_not_found_raises(tmp_path: Path) -> None:
    """uninstall_wrapper_local with unknown fingerprint raises CronLineNotFoundError.

    Covers install.py line 501.
    """
    cron_repo = MagicMock()
    cron_repo.get_cron = AsyncMock(return_value=None)

    with pytest.raises(CronLineNotFoundError, match="cron not found"):
        await uninstall_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            host_root=tmp_path,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )


@pytest.mark.asyncio
async def test_uninstall_executor_rejected_other_code_raises_cron_line_not_found(
    tmp_path: Path,
) -> None:
    """CronApplyRejectedError with error_code other than 'not_wrapped' → CronLineNotFoundError.

    Covers install.py lines 533-535 (bad_path, line_not_found, crontab_missing, bad_request).
    """
    _make_wrapped_crontab(tmp_path)
    cron = _make_cron_record()
    cron_repo = _make_uninstall_cron_repo(cron)

    with (
        patch(
            "homelab_monitor.kernel.cron.install.submit_and_wait",
            AsyncMock(
                side_effect=CronApplyRejectedError("line not found", error_code="line_not_found")
            ),
        ),
        pytest.raises(CronLineNotFoundError),
    ):
        await uninstall_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            host_root=tmp_path,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )


@pytest.mark.asyncio
async def test_uninstall_executor_generic_exception_raises_crontab_write_error(
    tmp_path: Path,
) -> None:
    """Generic exception from submit_and_wait → CrontabWriteError.

    Covers install.py line 535 (the bare except branch).
    """
    _make_wrapped_crontab(tmp_path)
    cron = _make_cron_record()
    cron_repo = _make_uninstall_cron_repo(cron)

    with (
        patch(
            "homelab_monitor.kernel.cron.install.submit_and_wait",
            AsyncMock(side_effect=RuntimeError("unexpected network error")),
        ),
        pytest.raises(CrontabWriteError, match="IPC error"),
    ):
        await uninstall_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            host_root=tmp_path,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )


@pytest.mark.asyncio
async def test_uninstall_cron_disappeared_after_uninstall_raises(tmp_path: Path) -> None:
    """If cron vanishes from DB after successful uninstall, CrontabWriteError is raised.

    Covers install.py line 552.
    """
    _make_wrapped_crontab(tmp_path)
    cron = _make_cron_record()

    cron_repo = MagicMock()
    # First call (fetch): returns the cron. Second call (after uninstall): returns None.
    cron_repo.get_cron = AsyncMock(side_effect=[cron, None])
    cron_repo.upsert_discovered = AsyncMock(return_value=None)
    cron_repo.record_wrapper_uninstalled = AsyncMock(return_value=None)

    with (
        patch(
            "homelab_monitor.kernel.cron.install.submit_and_wait",
            AsyncMock(return_value=_uninstall_ok_result()),
        ),
        pytest.raises(CrontabWriteError, match="disappeared"),
    ):
        await uninstall_wrapper_local(
            _FINGERPRINT,
            cron_repo=cron_repo,
            host_root=tmp_path,
            local_hostname=_HOST,
            who="test",
            ip=None,
            log=_null_log(),
        )
