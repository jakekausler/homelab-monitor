"""Smoke tests: verify package is importable and CLI prints version."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from pytest import CaptureFixture

from homelab_monitor import __version__
from homelab_monitor.cli.main import main


def test_version() -> None:
    """Package version string must be 0.0.0."""
    assert __version__ == "0.0.0"


def test_main_prints_version(capsys: CaptureFixture[str]) -> None:
    """CLI main() with no args must print a line ending with the version."""
    main([])
    captured = capsys.readouterr()
    assert captured.out.strip().endswith("0.0.0")


def test_main_version_flag(capsys: CaptureFixture[str]) -> None:
    """``hm --version`` exits 0 and prints the version."""
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert __version__ in captured.out


def test_main_migrate_no_subcommand_prints_help(
    capsys: CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``hm migrate`` without a subcommand still works (runs upgrade)."""
    # Steer DB URL away from the project default.
    fd_path = Path(tempfile.mkstemp(prefix="hm-help-", suffix=".db")[1])
    fd_path.unlink(missing_ok=True)
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", f"sqlite+aiosqlite:///{fd_path}")
    try:
        rc = main(["migrate"])
        assert rc == 0
    finally:
        for suffix in ("", "-wal", "-shm"):
            (fd_path.parent / (fd_path.name + suffix)).unlink(missing_ok=True)
