"""Tests for the ``hm migrate`` CLI subcommands."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from homelab_monitor.cli.main import main


def test_hm_no_args_prints_version(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``hm`` with no subcommand prints the version line (preserves smoke behaviour)."""
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "homelab-monitor" in out


def test_hm_migrate_status_empty(db_url_env: str, capsys: pytest.CaptureFixture[str]) -> None:
    """``hm migrate status`` against an empty DB reports no current revision."""
    rc = main(["migrate", "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "current: <empty>" in out
    assert "head:    0001" in out
    assert "pending migrations" in out


def test_hm_migrate_applies_head(db_url_env: str, capsys: pytest.CaptureFixture[str]) -> None:
    """``hm migrate`` applies ``upgrade head`` and reports success."""
    rc = main(["migrate"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Migrations applied" in out


def test_hm_migrate_status_after_upgrade(
    db_url_env: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """After ``hm migrate``, status reports up-to-date."""
    main(["migrate"])
    capsys.readouterr()  # discard upgrade output
    rc = main(["migrate", "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "current: 0001" in out
    assert "head:    0001" in out
    assert "up to date" in out


def test_hm_migrate_history_lists_revisions(
    db_url_env: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """``hm migrate history`` lists revision 0001."""
    rc = main(["migrate", "history"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "0001 ->" in out


def test_hm_unknown_command(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown subcommand triggers argparse error."""
    fd_path = Path(tempfile.mkstemp(prefix="hm-unknown-", suffix=".db")[1])
    fd_path.unlink(missing_ok=True)
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", f"sqlite+aiosqlite:///{fd_path}")
    try:
        with pytest.raises(SystemExit) as excinfo:
            main(["unknown"])
        assert excinfo.value.code == 2  # noqa: PLR2004
    finally:
        for suffix in ("", "-wal", "-shm"):
            (fd_path.parent / (fd_path.name + suffix)).unlink(missing_ok=True)
