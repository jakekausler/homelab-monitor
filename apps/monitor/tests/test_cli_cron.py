"""Tests for ``hm cron`` CLI dispatcher and _StderrLog (STAGE-002-007)."""

from __future__ import annotations

import argparse

import pytest

from homelab_monitor.cli import cron as cron_cli
from homelab_monitor.cli.cron import _StderrLog  # pyright: ignore[reportPrivateUsage]


class TestStderrLog:
    def test_warning_prints_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        """_StderrLog.warning should write to stderr with WARN prefix."""
        log = _StderrLog()
        log.warning("something_happened", count=5, source="test")
        captured = capsys.readouterr()
        assert "WARN something_happened" in captured.err
        assert "count" in captured.err

    def test_info_prints_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        """_StderrLog.info should write to stderr with INFO prefix."""
        log = _StderrLog()
        log.info("scan_started", host="myhost", items=3)
        captured = capsys.readouterr()
        assert "INFO scan_started" in captured.err
        assert "host" in captured.err


class TestHandle:
    def test_dispatches_discover(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_handle should route cron_cmd='discover' to _cmd_discover via asyncio.run."""
        called: list[str] = []

        async def fake_discover() -> int:
            called.append("called")
            return 0

        monkeypatch.setattr(cron_cli, "_cmd_discover", fake_discover)
        args = argparse.Namespace(cron_cmd="discover")
        exit_code = cron_cli._handle(args)  # pyright: ignore[reportPrivateUsage]

        assert exit_code == 0
        assert called == ["called"]

    def test_missing_subcommand_returns_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        """_handle without cron_cmd attr should print usage and return 2."""
        args = argparse.Namespace()  # no cron_cmd attribute
        exit_code = cron_cli._handle(args)  # pyright: ignore[reportPrivateUsage]

        assert exit_code == 2  # noqa: PLR2004
        captured = capsys.readouterr()
        assert "usage: hm cron" in captured.err

    def test_unknown_subcommand_returns_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        """_handle with unknown cron_cmd should print usage and return 2."""
        args = argparse.Namespace(cron_cmd="nonexistent")
        exit_code = cron_cli._handle(args)  # pyright: ignore[reportPrivateUsage]

        assert exit_code == 2  # noqa: PLR2004
        captured = capsys.readouterr()
        assert "usage: hm cron" in captured.err
