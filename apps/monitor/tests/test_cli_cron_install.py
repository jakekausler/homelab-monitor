"""Tests for 'hm cron install-wrapper' and 'hm cron get-wrapper-template' CLI commands
(STAGE-002-009).

Follows the pattern in test_cli_cron.py and test_cli_cron_discover.py.

NOTE: homelab_monitor.cli.cron imports SecretsManager from a module that does not
yet exist (homelab_monitor.kernel.secrets.manager). This is an implementation gap.
We inject a stub module before importing the CLI so that test collection succeeds.
"""

from __future__ import annotations

import argparse
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homelab_monitor.cli.cron import (
    _cmd_get_wrapper_template,  # pyright: ignore[reportPrivateUsage]
    _cmd_install_wrapper,  # pyright: ignore[reportPrivateUsage]
    _handle,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.cron.install import (
    CronLineNotFoundError,
    RemoteHostError,
)

_FINGERPRINT = "abc123fingerprint"
_PUBLIC_URL = "https://monitor.example.com"


# ---------------------------------------------------------------------------
# get-wrapper-template
# ---------------------------------------------------------------------------


class TestGetWrapperTemplate:
    def test_prints_template_to_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        """_cmd_get_wrapper_template prints the template (contains {{FINGERPRINT}})."""
        rc = _cmd_get_wrapper_template()
        captured = capsys.readouterr()
        assert rc == 0
        assert "{{FINGERPRINT}}" in captured.out
        assert "{{HEARTBEAT_URL_BASE}}" in captured.out
        assert "{{TOKEN_FILE_PATH}}" in captured.out

    def test_handle_dispatches_get_wrapper_template(self) -> None:
        """_handle with cron_cmd='get-wrapper-template' calls _cmd_get_wrapper_template."""
        called: list[int] = []

        def fake_tmpl() -> int:
            called.append(1)
            return 0

        with patch("homelab_monitor.cli.cron._cmd_get_wrapper_template", fake_tmpl):
            args = argparse.Namespace(cron_cmd="get-wrapper-template")
            rc = _handle(args)

        assert rc == 0
        assert called == [1]


# ---------------------------------------------------------------------------
# install-wrapper dry-run
# ---------------------------------------------------------------------------


def _make_fake_kit(fingerprint: str = _FINGERPRINT) -> MagicMock:
    kit = MagicMock()
    kit.fingerprint = fingerprint
    kit.wrapper_content = f"# wrapper for {fingerprint}\n"
    kit.wrapper_path = "/usr/local/bin/cron-with-heartbeat.sh"
    kit.token_file_path = "/etc/homelab-monitor/heartbeat.token"
    diff = MagicMock()
    diff.source_path = "/etc/crontab"
    diff.old_line = "* * * * * root /usr/bin/mytask.sh"
    diff.new_line = "/usr/local/bin/cron-with-heartbeat.sh -- /usr/bin/mytask.sh"
    kit.crontab_diff = diff
    return kit


class TestInstallWrapperDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_prints_preview(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dry-run (confirm=False) prints wrapper content + crontab diff, returns 0."""
        monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)
        kit = _make_fake_kit()

        with (
            patch("homelab_monitor.cli.cron.get_engine"),
            patch("homelab_monitor.cli.cron.SqliteRepository"),
            patch("homelab_monitor.cli.cron.CronRepo") as mock_cron_repo_cls,
            patch("homelab_monitor.cli.cron.AuthRepository"),
            patch("homelab_monitor.cli.cron.AsyncSecretsRepository"),
            patch("homelab_monitor.cli.cron.load_master_key", return_value=b"k" * 32),
            patch("homelab_monitor.cli.cron.resolve_hostname", return_value="local-host"),
            patch("homelab_monitor.cli.cron.build_install_kit", new=AsyncMock(return_value=kit)),
        ):
            mock_cron_repo = mock_cron_repo_cls.return_value
            mock_cron = MagicMock()
            mock_cron.host = "local-host"
            mock_cron_repo.get_cron = AsyncMock(return_value=mock_cron)

            rc = await _cmd_install_wrapper(_FINGERPRINT, confirm=False)

        assert rc == 0
        captured = capsys.readouterr()
        assert "Wrapper script" in captured.out
        assert kit.wrapper_content.strip() in captured.out
        assert "Crontab diff" in captured.out

    @pytest.mark.asyncio
    async def test_dry_run_cron_not_found_returns_1(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dry-run with unknown fingerprint returns 1 and prints error."""
        monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)

        with (
            patch("homelab_monitor.cli.cron.get_engine"),
            patch("homelab_monitor.cli.cron.SqliteRepository"),
            patch("homelab_monitor.cli.cron.CronRepo") as mock_cron_repo_cls,
            patch("homelab_monitor.cli.cron.AuthRepository"),
            patch("homelab_monitor.cli.cron.AsyncSecretsRepository"),
            patch("homelab_monitor.cli.cron.load_master_key", return_value=b"k" * 32),
            patch("homelab_monitor.cli.cron.resolve_hostname", return_value="local-host"),
        ):
            mock_cron_repo = mock_cron_repo_cls.return_value
            mock_cron_repo.get_cron = AsyncMock(return_value=None)

            rc = await _cmd_install_wrapper("bad-fp", confirm=False)

        assert rc == 1
        captured = capsys.readouterr()
        assert "ERROR" in captured.err

    @pytest.mark.asyncio
    async def test_dry_run_remote_host_returns_1(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dry-run with cron on remote host returns 1 and prints error."""
        monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)

        with (
            patch("homelab_monitor.cli.cron.get_engine"),
            patch("homelab_monitor.cli.cron.SqliteRepository"),
            patch("homelab_monitor.cli.cron.CronRepo") as mock_cron_repo_cls,
            patch("homelab_monitor.cli.cron.AuthRepository"),
            patch("homelab_monitor.cli.cron.AsyncSecretsRepository"),
            patch("homelab_monitor.cli.cron.load_master_key", return_value=b"k" * 32),
            patch("homelab_monitor.cli.cron.resolve_hostname", return_value="local-host"),
        ):
            mock_cron_repo = mock_cron_repo_cls.return_value
            mock_cron = MagicMock()
            mock_cron.host = "other-host"
            mock_cron_repo.get_cron = AsyncMock(return_value=mock_cron)

            rc = await _cmd_install_wrapper(_FINGERPRINT, confirm=False)

        assert rc == 1
        captured = capsys.readouterr()
        assert "ERROR" in captured.err

    @pytest.mark.asyncio
    async def test_dry_run_no_public_url_returns_1(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HOMELAB_MONITOR_PUBLIC_URL not set → returns 1."""
        monkeypatch.delenv("HOMELAB_MONITOR_PUBLIC_URL", raising=False)

        with (
            patch("homelab_monitor.cli.cron.get_engine"),
            patch("homelab_monitor.cli.cron.SqliteRepository"),
            patch("homelab_monitor.cli.cron.CronRepo"),
            patch("homelab_monitor.cli.cron.AuthRepository"),
            patch("homelab_monitor.cli.cron.AsyncSecretsRepository"),
            patch("homelab_monitor.cli.cron.load_master_key", return_value=b"k" * 32),
            patch("homelab_monitor.cli.cron.resolve_hostname", return_value="local-host"),
        ):
            rc = await _cmd_install_wrapper(_FINGERPRINT, confirm=False)

        assert rc == 1
        captured = capsys.readouterr()
        assert "PUBLIC_URL" in captured.err

    @pytest.mark.asyncio
    async def test_dry_run_master_key_error_returns_1(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Master key load failure → returns 1."""
        monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)

        with (
            patch("homelab_monitor.cli.cron.get_engine"),
            patch("homelab_monitor.cli.cron.SqliteRepository"),
            patch("homelab_monitor.cli.cron.CronRepo"),
            patch("homelab_monitor.cli.cron.AuthRepository"),
            patch("homelab_monitor.cli.cron.load_master_key", side_effect=RuntimeError("no key")),
        ):
            rc = await _cmd_install_wrapper(_FINGERPRINT, confirm=False)

        assert rc == 1
        captured = capsys.readouterr()
        assert "ERROR" in captured.err

    @pytest.mark.asyncio
    async def test_dry_run_wrapper_install_error_returns_1(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """build_install_kit raising WrapperInstallError returns 1."""
        monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)

        with (
            patch("homelab_monitor.cli.cron.get_engine"),
            patch("homelab_monitor.cli.cron.SqliteRepository"),
            patch("homelab_monitor.cli.cron.CronRepo") as mock_cron_repo_cls,
            patch("homelab_monitor.cli.cron.AuthRepository"),
            patch("homelab_monitor.cli.cron.AsyncSecretsRepository"),
            patch("homelab_monitor.cli.cron.load_master_key", return_value=b"k" * 32),
            patch("homelab_monitor.cli.cron.resolve_hostname", return_value="local-host"),
            patch(
                "homelab_monitor.cli.cron.build_install_kit",
                new=AsyncMock(side_effect=CronLineNotFoundError("line not found")),
            ),
        ):
            mock_cron_repo = mock_cron_repo_cls.return_value
            mock_cron = MagicMock()
            mock_cron.host = "local-host"
            mock_cron_repo.get_cron = AsyncMock(return_value=mock_cron)

            rc = await _cmd_install_wrapper(_FINGERPRINT, confirm=False)

        assert rc == 1
        captured = capsys.readouterr()
        assert "ERROR" in captured.err


# ---------------------------------------------------------------------------
# install-wrapper --confirm
# ---------------------------------------------------------------------------


class TestInstallWrapperConfirm:
    @pytest.mark.asyncio
    async def test_confirm_prints_success(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """confirm=True with successful install prints installed message and returns 0."""
        monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)
        updated_cron = MagicMock()

        with (
            patch("homelab_monitor.cli.cron.get_engine"),
            patch("homelab_monitor.cli.cron.SqliteRepository"),
            patch("homelab_monitor.cli.cron.CronRepo") as mock_cron_repo_cls,
            patch("homelab_monitor.cli.cron.AuthRepository"),
            patch("homelab_monitor.cli.cron.AsyncSecretsRepository"),
            patch("homelab_monitor.cli.cron.load_master_key", return_value=b"k" * 32),
            patch("homelab_monitor.cli.cron.resolve_hostname", return_value="local-host"),
            patch(
                "homelab_monitor.cli.cron.install_wrapper_local",
                new=AsyncMock(return_value=updated_cron),
            ),
        ):
            mock_cron_repo = mock_cron_repo_cls.return_value
            mock_cron = MagicMock()
            mock_cron.host = "local-host"
            mock_cron_repo.get_cron = AsyncMock(return_value=mock_cron)

            rc = await _cmd_install_wrapper(_FINGERPRINT, confirm=True)

        assert rc == 0
        captured = capsys.readouterr()
        assert "installed" in captured.out

    @pytest.mark.asyncio
    async def test_confirm_install_error_returns_1(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """confirm=True with install failure returns 1 and prints error."""
        monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)

        with (
            patch("homelab_monitor.cli.cron.get_engine"),
            patch("homelab_monitor.cli.cron.SqliteRepository"),
            patch("homelab_monitor.cli.cron.CronRepo") as mock_cron_repo_cls,
            patch("homelab_monitor.cli.cron.AuthRepository"),
            patch("homelab_monitor.cli.cron.AsyncSecretsRepository"),
            patch("homelab_monitor.cli.cron.load_master_key", return_value=b"k" * 32),
            patch("homelab_monitor.cli.cron.resolve_hostname", return_value="local-host"),
            patch(
                "homelab_monitor.cli.cron.install_wrapper_local",
                new=AsyncMock(side_effect=RemoteHostError("not local")),
            ),
        ):
            mock_cron_repo = mock_cron_repo_cls.return_value
            mock_cron = MagicMock()
            mock_cron.host = "local-host"
            mock_cron_repo.get_cron = AsyncMock(return_value=mock_cron)

            rc = await _cmd_install_wrapper(_FINGERPRINT, confirm=True)

        assert rc == 1
        captured = capsys.readouterr()
        assert "ERROR" in captured.err


# ---------------------------------------------------------------------------
# _handle dispatching
# ---------------------------------------------------------------------------


class TestInstallWrapperUnexpectedError:
    @pytest.mark.asyncio
    async def test_confirm_unexpected_error_returns_1(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """confirm=True with an unexpected non-WrapperInstallError returns 1 (lines 176-178)."""
        monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", "https://monitor.example.com")

        with (
            patch("homelab_monitor.cli.cron.get_engine"),
            patch("homelab_monitor.cli.cron.SqliteRepository"),
            patch("homelab_monitor.cli.cron.CronRepo") as mock_cron_repo_cls,
            patch("homelab_monitor.cli.cron.AuthRepository"),
            patch("homelab_monitor.cli.cron.AsyncSecretsRepository"),
            patch("homelab_monitor.cli.cron.load_master_key", return_value=b"k" * 32),
            patch("homelab_monitor.cli.cron.resolve_hostname", return_value="local-host"),
            patch(
                "homelab_monitor.cli.cron.install_wrapper_local",
                new=AsyncMock(side_effect=RuntimeError("database exploded")),
            ),
        ):
            mock_cron_repo = mock_cron_repo_cls.return_value
            mock_cron = MagicMock()
            mock_cron.host = "local-host"
            mock_cron_repo.get_cron = AsyncMock(return_value=mock_cron)

            rc = await _cmd_install_wrapper("abc123", confirm=True)

        assert rc == 1
        captured = capsys.readouterr()
        assert "ERROR" in captured.err
        assert "unexpected" in captured.err.lower()


class TestHandleDispatching:
    def test_dispatches_install_wrapper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_handle routes cron_cmd='install-wrapper' via asyncio.run."""
        called: list[str] = []

        async def fake_install(fp: str, *, confirm: bool) -> int:
            called.append(f"{fp}:{confirm}")
            return 0

        with patch("homelab_monitor.cli.cron._cmd_install_wrapper", fake_install):
            args = argparse.Namespace(
                cron_cmd="install-wrapper",
                fingerprint=_FINGERPRINT,
                confirm=True,
            )
            rc = _handle(args)

        assert rc == 0
        assert called == [f"{_FINGERPRINT}:True"]


# ===========================================================================
# STAGE-002-009A: hm cron uninstall-wrapper CLI tests
# ===========================================================================

from homelab_monitor.cli.cron import (  # noqa: E402
    _cmd_uninstall_wrapper,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.cron.install import (  # noqa: E402
    NotWrappedError,
    WrapperUninstallKit,
)


def _make_fake_uninstall_kit(fingerprint: str = _FINGERPRINT) -> MagicMock:
    kit = MagicMock(spec=WrapperUninstallKit)
    kit.fingerprint = fingerprint
    diff = MagicMock()
    diff.source_path = "/etc/crontab"
    diff.old_line = "* * * * * root /usr/local/bin/cron-with-heartbeat.sh -- /usr/bin/mytask.sh"
    diff.new_line = "* * * * * root /usr/bin/mytask.sh"
    kit.crontab_diff = diff
    return kit


class TestUninstallWrapperDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_prints_crontab_diff(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dry-run (confirm=False) prints the crontab diff, returns 0."""
        kit = _make_fake_uninstall_kit()

        with (
            patch("homelab_monitor.cli.cron.get_engine"),
            patch("homelab_monitor.cli.cron.SqliteRepository"),
            patch("homelab_monitor.cli.cron.CronRepo") as mock_cron_repo_cls,
            patch("homelab_monitor.cli.cron.resolve_hostname", return_value="local-host"),
            patch("homelab_monitor.cli.cron.build_uninstall_kit", new=AsyncMock(return_value=kit)),
        ):
            mock_cron_repo = mock_cron_repo_cls.return_value
            mock_cron = MagicMock()
            mock_cron.host = "local-host"
            mock_cron_repo.get_cron = AsyncMock(return_value=mock_cron)

            rc = await _cmd_uninstall_wrapper(_FINGERPRINT, confirm=False)

        assert rc == 0
        captured = capsys.readouterr()
        assert "Crontab diff" in captured.out
        assert kit.crontab_diff.old_line in captured.out
        assert kit.crontab_diff.new_line in captured.out

    @pytest.mark.asyncio
    async def test_dry_run_cron_not_found_returns_1(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Dry-run with unknown fingerprint returns 1 and prints error."""
        with (
            patch("homelab_monitor.cli.cron.get_engine"),
            patch("homelab_monitor.cli.cron.SqliteRepository"),
            patch("homelab_monitor.cli.cron.CronRepo") as mock_cron_repo_cls,
            patch("homelab_monitor.cli.cron.resolve_hostname", return_value="local-host"),
        ):
            mock_cron_repo = mock_cron_repo_cls.return_value
            mock_cron_repo.get_cron = AsyncMock(return_value=None)

            rc = await _cmd_uninstall_wrapper("bad-fp", confirm=False)

        assert rc == 1
        captured = capsys.readouterr()
        assert "ERROR" in captured.err


class TestUninstallWrapperConfirm:
    @pytest.mark.asyncio
    async def test_confirm_calls_uninstall_and_prints_success(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Confirm path calls uninstall_wrapper_local and prints success message."""
        with (
            patch("homelab_monitor.cli.cron.get_engine"),
            patch("homelab_monitor.cli.cron.SqliteRepository"),
            patch("homelab_monitor.cli.cron.CronRepo") as mock_cron_repo_cls,
            patch("homelab_monitor.cli.cron.resolve_hostname", return_value="local-host"),
            patch(
                "homelab_monitor.cli.cron.uninstall_wrapper_local",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            mock_cron_repo = mock_cron_repo_cls.return_value
            mock_cron = MagicMock()
            mock_cron.host = "local-host"
            mock_cron_repo.get_cron = AsyncMock(return_value=mock_cron)

            rc = await _cmd_uninstall_wrapper(_FINGERPRINT, confirm=True)

        assert rc == 0
        captured = capsys.readouterr()
        assert "removed" in captured.out.lower()

    @pytest.mark.asyncio
    async def test_confirm_not_wrapped_returns_1(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """NotWrappedError → returns 1 and prints error."""
        with (
            patch("homelab_monitor.cli.cron.get_engine"),
            patch("homelab_monitor.cli.cron.SqliteRepository"),
            patch("homelab_monitor.cli.cron.CronRepo") as mock_cron_repo_cls,
            patch("homelab_monitor.cli.cron.resolve_hostname", return_value="local-host"),
            patch(
                "homelab_monitor.cli.cron.uninstall_wrapper_local",
                new=AsyncMock(side_effect=NotWrappedError("not wrapped")),
            ),
        ):
            mock_cron_repo = mock_cron_repo_cls.return_value
            mock_cron = MagicMock()
            mock_cron.host = "local-host"
            mock_cron_repo.get_cron = AsyncMock(return_value=mock_cron)

            rc = await _cmd_uninstall_wrapper(_FINGERPRINT, confirm=True)

        assert rc == 1
        captured = capsys.readouterr()
        assert "ERROR" in captured.err


class TestUninstallWrapperRemoteHost:
    @pytest.mark.asyncio
    async def test_remote_host_cron_returns_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Cron on a remote host → prints error and returns 1.

        Covers cli/cron.py lines 215-219.
        """
        with (
            patch("homelab_monitor.cli.cron.get_engine"),
            patch("homelab_monitor.cli.cron.SqliteRepository"),
            patch("homelab_monitor.cli.cron.CronRepo") as mock_cron_repo_cls,
            patch("homelab_monitor.cli.cron.resolve_hostname", return_value="local-host"),
        ):
            mock_cron_repo = mock_cron_repo_cls.return_value
            mock_cron = MagicMock()
            mock_cron.host = "other-host"  # different from local-host
            mock_cron_repo.get_cron = AsyncMock(return_value=mock_cron)

            rc = await _cmd_uninstall_wrapper(_FINGERPRINT, confirm=False)

        assert rc == 1
        captured = capsys.readouterr()
        assert "ERROR" in captured.err
        assert "other-host" in captured.err or "local" in captured.err

    @pytest.mark.asyncio
    async def test_unexpected_exception_returns_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Unexpected exception in _cmd_uninstall_wrapper → returns 1.

        Covers cli/cron.py lines 247-249.
        """
        with (
            patch("homelab_monitor.cli.cron.get_engine"),
            patch("homelab_monitor.cli.cron.SqliteRepository"),
            patch("homelab_monitor.cli.cron.CronRepo") as mock_cron_repo_cls,
            patch("homelab_monitor.cli.cron.resolve_hostname", return_value="local-host"),
        ):
            mock_cron_repo = mock_cron_repo_cls.return_value
            # Trigger unexpected exception via get_cron raising something unexpected
            mock_cron_repo.get_cron = AsyncMock(side_effect=RuntimeError("db exploded"))

            rc = await _cmd_uninstall_wrapper(_FINGERPRINT, confirm=False)

        assert rc == 1
        captured = capsys.readouterr()
        assert "ERROR" in captured.err
        assert "unexpected" in captured.err.lower()


class TestHandleDispatchingUninstall:
    def test_dispatches_uninstall_wrapper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_handle routes cron_cmd='uninstall-wrapper' via asyncio.run."""
        called: list[str] = []

        async def fake_uninstall(fp: str, *, confirm: bool) -> int:
            called.append(f"{fp}:{confirm}")
            return 0

        with patch("homelab_monitor.cli.cron._cmd_uninstall_wrapper", fake_uninstall):
            args = argparse.Namespace(
                cron_cmd="uninstall-wrapper",
                fingerprint=_FINGERPRINT,
                confirm=False,
            )
            rc = _handle(args)

        assert rc == 0
        assert called == [f"{_FINGERPRINT}:False"]
