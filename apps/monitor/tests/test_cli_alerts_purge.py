"""Tests for cli/alerts.py — hm alerts purge (STAGE-010-001)."""

from __future__ import annotations

import argparse
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from homelab_monitor.cli.alerts import _cmd_purge  # pyright: ignore[reportPrivateUsage]
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.migrations import run_migrations
from homelab_monitor.kernel.db.repository import SqliteRepository

_INSERT_ALERT_SQL = text(
    "INSERT INTO alerts (id, fingerprint, created_at, payload_json) "
    "VALUES (:id, :fingerprint, :created_at, :payload_json)"
)
_COUNT_BY_ALERTNAME_SQL = text(
    "SELECT COUNT(*) FROM alerts "
    "WHERE json_extract(payload_json, '$.labels.alertname') = :alertname"
)
_COUNT_AUDIT_SQL = text("SELECT COUNT(*) FROM audit_log WHERE what = :what")
_SELECT_AUDIT_BEFORE_SQL = text(
    'SELECT before_json FROM audit_log WHERE what = :what ORDER BY "when" ASC'
)

_TWO_ALERTS_SEEDED = 2
_TWO_ROWS_PURGED_IN_ONE_RUN = 2
_TWO_AUDIT_ROWS_AFTER_TWO_RUNS = 2


async def _seed_alert(repo: SqliteRepository, alertname: str) -> str:
    """Insert one alerts row carrying labels.alertname=<alertname>. Returns row id."""
    row_id = str(uuid.uuid4())
    payload = json.dumps({"labels": {"alertname": alertname}})
    async with repo.transaction() as conn:
        await conn.execute(
            _INSERT_ALERT_SQL,
            {
                "id": row_id,
                "fingerprint": f"fp-{row_id}",
                "created_at": "2026-07-06T00:00:00Z",
                "payload_json": payload,
            },
        )
    return row_id


async def _count_by_alertname(repo: SqliteRepository, alertname: str) -> int:
    async with repo.transaction() as conn:
        result = await conn.execute(_COUNT_BY_ALERTNAME_SQL, {"alertname": alertname})
        return int(result.scalar_one())


async def _count_audit(repo: SqliteRepository, what: str) -> int:
    async with repo.transaction() as conn:
        result = await conn.execute(_COUNT_AUDIT_SQL, {"what": what})
        return int(result.scalar_one())


async def _first_audit_before(repo: SqliteRepository, what: str) -> dict[str, Any]:
    async with repo.transaction() as conn:
        result = await conn.execute(_SELECT_AUDIT_BEFORE_SQL, {"what": what})
        row = result.first()
    assert row is not None, f"no audit row with what={what}"
    parsed: dict[str, Any] = json.loads(row[0])
    assert isinstance(parsed, dict)
    return parsed


def _stub_reloader_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force VmalertReloader.reload() to succeed without hitting the network."""
    from homelab_monitor.cli import alerts as alerts_mod  # noqa: PLC0415

    async def fake_reload(self: object) -> bool:
        return True

    monkeypatch.setattr(alerts_mod.VmalertReloader, "reload", fake_reload)


def _stub_reloader_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force VmalertReloader.reload() to return False (simulate vmalert down)."""
    from homelab_monitor.cli import alerts as alerts_mod  # noqa: PLC0415

    async def fake_reload(self: object) -> bool:
        return False

    monkeypatch.setattr(alerts_mod.VmalertReloader, "reload", fake_reload)


@pytest.mark.asyncio
async def test_purge_refuses_without_mode(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Neither --dry-run nor --confirm-phrase → exit 1 with clear error."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    engine = get_engine(url=db_url)
    await run_migrations(engine)

    args = argparse.Namespace(alertname="X", dry_run=False, confirm_phrase=None)
    rc = await _cmd_purge(args)

    assert rc == 1
    captured = capsys.readouterr()
    assert "required" in captured.err.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_alertname", ["", "   ", "\t\n"])
async def test_purge_empty_alertname_rejected(
    bad_alertname: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty or whitespace-only --alertname exits 1 with clear error message."""
    args = argparse.Namespace(
        alertname=bad_alertname,
        dry_run=True,
        confirm_phrase=None,
    )
    rc = await _cmd_purge(args)

    assert rc == 1
    captured = capsys.readouterr()
    assert "must be a non-empty string" in captured.err


@pytest.mark.asyncio
async def test_purge_confirm_phrase_mismatch_aborts(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Wrong confirm-phrase → exit 1, no delete, no audit row."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)

    await _seed_alert(repo, "SignatureWentSilent")

    args = argparse.Namespace(
        alertname="SignatureWentSilent",
        dry_run=False,
        confirm_phrase="wrong phrase",
    )
    rc = await _cmd_purge(args)

    assert rc == 1
    captured = capsys.readouterr()
    assert "delete signature-silent noise" in captured.err
    assert await _count_by_alertname(repo, "SignatureWentSilent") == 1
    assert await _count_audit(repo, "alerts_bulk_purged") == 0


@pytest.mark.asyncio
async def test_purge_dry_run_reports_count_without_deleting(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--dry-run: prints count, deletes nothing, writes no audit row."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)

    await _seed_alert(repo, "SignatureWentSilent")
    await _seed_alert(repo, "SignatureWentSilent")
    await _seed_alert(repo, "OtherAlert")

    args = argparse.Namespace(
        alertname="SignatureWentSilent",
        dry_run=True,
        confirm_phrase=None,
    )
    rc = await _cmd_purge(args)

    assert rc == 0
    captured = capsys.readouterr()
    assert "2" in captured.out
    assert "SignatureWentSilent" in captured.out
    # Nothing deleted.
    assert await _count_by_alertname(repo, "SignatureWentSilent") == _TWO_ALERTS_SEEDED
    assert await _count_by_alertname(repo, "OtherAlert") == 1
    # No audit row.
    assert await _count_audit(repo, "alerts_bulk_purged") == 0


@pytest.mark.asyncio
async def test_purge_success_deletes_and_writes_audit(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Real run: deletes N matching, writes 1 audit row, leaves others untouched."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)
    _stub_reloader_true(monkeypatch)

    await _seed_alert(repo, "SignatureWentSilent")
    await _seed_alert(repo, "SignatureWentSilent")
    await _seed_alert(repo, "OtherAlert")

    args = argparse.Namespace(
        alertname="SignatureWentSilent",
        dry_run=False,
        confirm_phrase="delete signature-silent noise",
    )
    rc = await _cmd_purge(args)

    assert rc == 0
    captured = capsys.readouterr()
    assert "Purged 2 alerts" in captured.out
    # Deletes exactly the 2 matching rows.
    assert await _count_by_alertname(repo, "SignatureWentSilent") == 0
    assert await _count_by_alertname(repo, "OtherAlert") == 1
    # Exactly one audit row.
    assert await _count_audit(repo, "alerts_bulk_purged") == 1
    before = await _first_audit_before(repo, "alerts_bulk_purged")
    assert before["alertname"] == "SignatureWentSilent"
    assert before["row_count"] == _TWO_ROWS_PURGED_IN_ONE_RUN
    assert before["source_tool"] == "vmalert-metrics"
    assert before["alertgroup"] == "signature_silent"


@pytest.mark.asyncio
async def test_purge_case_fold_confirm_phrase(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confirm-phrase match is case-fold + strip; mixed-case + padded still works."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)
    _stub_reloader_true(monkeypatch)

    await _seed_alert(repo, "SignatureWentSilent")

    args = argparse.Namespace(
        alertname="SignatureWentSilent",
        dry_run=False,
        confirm_phrase="  DELETE Signature-Silent NOISE  ",
    )
    rc = await _cmd_purge(args)

    assert rc == 0
    assert await _count_by_alertname(repo, "SignatureWentSilent") == 0
    assert await _count_audit(repo, "alerts_bulk_purged") == 1


@pytest.mark.asyncio
async def test_purge_idempotent_second_run(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second run with same args deletes 0 rows and writes a 2nd audit row (row_count=0)."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)
    _stub_reloader_true(monkeypatch)

    await _seed_alert(repo, "SignatureWentSilent")
    await _seed_alert(repo, "SignatureWentSilent")

    args = argparse.Namespace(
        alertname="SignatureWentSilent",
        dry_run=False,
        confirm_phrase="delete signature-silent noise",
    )

    # First run.
    rc1 = await _cmd_purge(args)
    assert rc1 == 0
    assert await _count_by_alertname(repo, "SignatureWentSilent") == 0
    assert await _count_audit(repo, "alerts_bulk_purged") == 1

    # Second run.
    rc2 = await _cmd_purge(args)
    assert rc2 == 0
    assert await _count_by_alertname(repo, "SignatureWentSilent") == 0
    assert await _count_audit(repo, "alerts_bulk_purged") == _TWO_AUDIT_ROWS_AFTER_TWO_RUNS


@pytest.mark.asyncio
async def test_purge_does_not_touch_other_alertnames(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Purging A leaves B and C untouched."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)
    _stub_reloader_true(monkeypatch)

    await _seed_alert(repo, "AlertA")
    await _seed_alert(repo, "AlertA")
    await _seed_alert(repo, "AlertB")
    await _seed_alert(repo, "AlertC")

    args = argparse.Namespace(
        alertname="AlertA",
        dry_run=False,
        confirm_phrase="delete signature-silent noise",
    )
    rc = await _cmd_purge(args)

    assert rc == 0
    assert await _count_by_alertname(repo, "AlertA") == 0
    assert await _count_by_alertname(repo, "AlertB") == 1
    assert await _count_by_alertname(repo, "AlertC") == 1


@pytest.mark.asyncio
async def test_purge_warns_on_reload_failure_but_returns_0(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed vmalert reload logs a warning to stderr but still exits 0."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)
    _stub_reloader_false(monkeypatch)

    await _seed_alert(repo, "SignatureWentSilent")

    args = argparse.Namespace(
        alertname="SignatureWentSilent",
        dry_run=False,
        confirm_phrase="delete signature-silent noise",
    )
    rc = await _cmd_purge(args)

    assert rc == 0
    captured = capsys.readouterr()
    assert "reload failed" in captured.err
    # Delete + audit still happened despite the reload failure.
    assert await _count_by_alertname(repo, "SignatureWentSilent") == 0
    assert await _count_audit(repo, "alerts_bulk_purged") == 1


class TestHandle:
    def test_dispatches_purge(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_handle routes alerts_cmd='purge' to _cmd_purge via asyncio.run."""
        from homelab_monitor.cli import alerts as alerts_cli  # noqa: PLC0415

        called: list[argparse.Namespace] = []

        async def fake_purge(args: argparse.Namespace) -> int:
            called.append(args)
            return 0

        monkeypatch.setattr(
            alerts_cli,
            "_cmd_purge",
            fake_purge,
        )
        args = argparse.Namespace(
            alerts_cmd="purge",
            alertname="SignatureWentSilent",
            dry_run=True,
            confirm_phrase=None,
        )
        rc = alerts_cli._handle(args)  # pyright: ignore[reportPrivateUsage]

        assert rc == 0
        assert len(called) == 1
        assert called[0].alertname == "SignatureWentSilent"

    def test_missing_subcommand_returns_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        """_handle without alerts_cmd prints usage and returns 2."""
        from homelab_monitor.cli import alerts as alerts_cli  # noqa: PLC0415

        args = argparse.Namespace()
        rc = alerts_cli._handle(args)  # pyright: ignore[reportPrivateUsage]

        assert rc == 2  # noqa: PLR2004
        captured = capsys.readouterr()
        assert "usage: hm alerts" in captured.err

    def test_unknown_subcommand_returns_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        """_handle with unknown alerts_cmd prints usage and returns 2."""
        from homelab_monitor.cli import alerts as alerts_cli  # noqa: PLC0415

        args = argparse.Namespace(alerts_cmd="nonexistent")
        rc = alerts_cli._handle(args)  # pyright: ignore[reportPrivateUsage]

        assert rc == 2  # noqa: PLR2004
        captured = capsys.readouterr()
        assert "usage: hm alerts" in captured.err


def test_purge_missing_alertname_argparse_rejects() -> None:
    """argparse: `hm alerts purge --dry-run` (no --alertname) → SystemExit(2)."""
    from homelab_monitor.cli import alerts as alerts_cli  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="hm")
    subparsers = parser.add_subparsers(dest="cmd")
    alerts_cli.add_subparser(subparsers)

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["alerts", "purge", "--dry-run"])
    assert excinfo.value.code == 2  # noqa: PLR2004
