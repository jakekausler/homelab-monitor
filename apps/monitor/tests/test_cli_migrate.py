"""Tests for the ``hm migrate`` CLI subcommands."""

from __future__ import annotations

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
    # Don't hardcode head revision — test that *some* head is reported.
    # The actual head revision changes as new migrations are added.
    assert "head:" in out
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
    # Don't hardcode revision — test that current matches head and status is "up to date".
    assert "current:" in out
    assert "head:" in out
    assert "up to date" in out


def test_hm_migrate_history_lists_revisions(
    db_url_env: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """``hm migrate history`` lists revision 0001."""
    rc = main(["migrate", "history"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "0001 ->" in out


def test_hm_unknown_command() -> None:
    """An unknown subcommand triggers argparse error."""
    with pytest.raises(SystemExit) as excinfo:
        main(["unknown"])
    assert excinfo.value.code == 2  # noqa: PLR2004
