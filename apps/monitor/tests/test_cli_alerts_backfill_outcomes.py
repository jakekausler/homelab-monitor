"""Tests for cli/alerts.py — hm alerts backfill-outcomes (STAGE-010-004)."""

from __future__ import annotations

import argparse
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from homelab_monitor.cli.alerts import _cmd_backfill_outcomes  # pyright: ignore[reportPrivateUsage]
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.migrations import run_migrations
from homelab_monitor.kernel.db.repository import SqliteRepository

_INSERT_ALERT_SQL = text(
    "INSERT INTO alerts (id, fingerprint, created_at, payload_json, ack_at, resolved_at) "
    "VALUES (:id, :fingerprint, :created_at, :payload_json, :ack_at, :resolved_at)"
)
_INSERT_OUTCOME_SQL = text(
    "INSERT INTO alert_outcomes (id, alert_id, outcome, decided_at, decided_by, created_at) "
    "VALUES (:id, :alert_id, :outcome, :decided_at, :decided_by, :created_at)"
)
_COUNT_OUTCOMES_SQL = text("SELECT COUNT(*) FROM alert_outcomes")
_COUNT_OUTCOMES_BY_KIND_SQL = text(
    "SELECT COUNT(*) FROM alert_outcomes WHERE outcome = :outcome AND decided_by = :decided_by"
)
_COUNT_AUDIT_SQL = text("SELECT COUNT(*) FROM audit_log WHERE what = :what")
_SELECT_AUDIT_AFTER_SQL = text(
    'SELECT after_json FROM audit_log WHERE what = :what ORDER BY "when" ASC'
)
_SELECT_OUTCOME_DECIDED_AT_SQL = text(
    "SELECT decided_at FROM alert_outcomes WHERE alert_id = :alert_id AND outcome = :outcome"
)


async def _seed_alert(
    repo: SqliteRepository,
    *,
    ack_at: str | None = None,
    resolved_at: str | None = None,
) -> str:
    """Insert one alerts row with given ack_at/resolved_at. Returns row id."""
    row_id = str(uuid.uuid4())
    async with repo.transaction() as conn:
        await conn.execute(
            _INSERT_ALERT_SQL,
            {
                "id": row_id,
                "fingerprint": f"fp-{row_id}",
                "created_at": "2026-07-06T00:00:00Z",
                "payload_json": json.dumps({"labels": {"alertname": "Test"}}),
                "ack_at": ack_at,
                "resolved_at": resolved_at,
            },
        )
    return row_id


async def _seed_outcome(
    repo: SqliteRepository,
    *,
    alert_id: str,
    outcome: str,
    decided_by: str,
    decided_at: str = "2026-07-01T00:00:00Z",
) -> None:
    """Insert one pre-existing alert_outcomes row (e.g. reconciler-written)."""
    async with repo.transaction() as conn:
        await conn.execute(
            _INSERT_OUTCOME_SQL,
            {
                "id": str(uuid.uuid4()),
                "alert_id": alert_id,
                "outcome": outcome,
                "decided_at": decided_at,
                "decided_by": decided_by,
                "created_at": "2026-07-01T00:00:00Z",
            },
        )


async def _count_outcomes(repo: SqliteRepository) -> int:
    async with repo.transaction() as conn:
        result = await conn.execute(_COUNT_OUTCOMES_SQL)
        return int(result.scalar_one())


async def _count_outcomes_by_kind(repo: SqliteRepository, outcome: str, decided_by: str) -> int:
    async with repo.transaction() as conn:
        result = await conn.execute(
            _COUNT_OUTCOMES_BY_KIND_SQL, {"outcome": outcome, "decided_by": decided_by}
        )
        return int(result.scalar_one())


async def _count_audit(repo: SqliteRepository, what: str) -> int:
    async with repo.transaction() as conn:
        result = await conn.execute(_COUNT_AUDIT_SQL, {"what": what})
        return int(result.scalar_one())


async def _first_audit_after(repo: SqliteRepository, what: str) -> dict[str, Any]:
    async with repo.transaction() as conn:
        result = await conn.execute(_SELECT_AUDIT_AFTER_SQL, {"what": what})
        row = result.first()
    assert row is not None, f"no audit row with what={what}"
    parsed: dict[str, Any] = json.loads(row[0])
    assert isinstance(parsed, dict)
    return parsed


async def _outcome_decided_at(repo: SqliteRepository, alert_id: str, outcome: str) -> str:
    async with repo.transaction() as conn:
        result = await conn.execute(
            _SELECT_OUTCOME_DECIDED_AT_SQL, {"alert_id": alert_id, "outcome": outcome}
        )
        row = result.first()
    assert row is not None
    return str(row[0])


_TWO_ACKED = 2
_THREE_AUTO_RESOLVED = 3
_FIVE_TOTAL = 5
_FOUR_TOTAL_WITH_ONE_CONFLICT_SKIPPED = 4


@pytest.mark.asyncio
async def test_dry_run_reports_counts_no_writes(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--dry-run reports expected counts; writes nothing."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)

    for _ in range(_TWO_ACKED):
        await _seed_alert(repo, ack_at="2026-07-01T00:00:00Z")
    for _ in range(_THREE_AUTO_RESOLVED):
        await _seed_alert(repo, resolved_at="2026-07-01T00:00:00Z")
    await _seed_alert(repo)  # still firing — no ack_at, no resolved_at

    args = argparse.Namespace(dry_run=True, confirm_phrase=None)
    rc = await _cmd_backfill_outcomes(args)

    assert rc == 0
    captured = capsys.readouterr()
    assert "would insert 2 ACKED + 3 AUTO_RESOLVED = 5 alert_outcomes rows" in captured.out
    assert await _count_outcomes(repo) == 0
    assert await _count_audit(repo, "alert_outcomes_backfilled") == 0


@pytest.mark.asyncio
async def test_real_run_missing_confirm_phrase_aborts(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No --dry-run and no --confirm-phrase -> exit 1, no writes."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)

    args = argparse.Namespace(dry_run=False, confirm_phrase=None)
    rc = await _cmd_backfill_outcomes(args)

    assert rc == 1
    captured = capsys.readouterr()
    assert "--dry-run or --confirm-phrase required" in captured.err
    assert await _count_outcomes(repo) == 0


@pytest.mark.asyncio
async def test_real_run_wrong_confirm_phrase_aborts(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Wrong --confirm-phrase -> exit 1, no writes."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)

    args = argparse.Namespace(dry_run=False, confirm_phrase="something wrong")
    rc = await _cmd_backfill_outcomes(args)

    assert rc == 1
    captured = capsys.readouterr()
    assert "backfill outcomes from historical alert state" in captured.err
    assert await _count_outcomes(repo) == 0


@pytest.mark.asyncio
async def test_real_run_success_writes_rows_and_audit(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Real run: writes exactly N rows + 1 audit row with correct summary."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)

    acked_ids = [await _seed_alert(repo, ack_at="2026-07-01T00:00:00Z") for _ in range(_TWO_ACKED)]
    resolved_ids = [
        await _seed_alert(repo, resolved_at="2026-07-02T00:00:00Z")
        for _ in range(_THREE_AUTO_RESOLVED)
    ]
    await _seed_alert(repo)  # still firing

    args = argparse.Namespace(
        dry_run=False,
        confirm_phrase="backfill outcomes from historical alert state",
    )
    rc = await _cmd_backfill_outcomes(args)

    assert rc == 0
    assert await _count_outcomes(repo) == _FIVE_TOTAL
    assert await _count_outcomes_by_kind(repo, "acked", "backfill") == _TWO_ACKED
    assert await _count_outcomes_by_kind(repo, "auto_resolved", "backfill") == _THREE_AUTO_RESOLVED

    for alert_id in acked_ids:
        assert await _outcome_decided_at(repo, alert_id, "acked") == "2026-07-01T00:00:00Z"
    for alert_id in resolved_ids:
        assert await _outcome_decided_at(repo, alert_id, "auto_resolved") == "2026-07-02T00:00:00Z"

    assert await _count_audit(repo, "alert_outcomes_backfilled") == 1
    after = await _first_audit_after(repo, "alert_outcomes_backfilled")
    assert after["acked_count"] == _TWO_ACKED
    assert after["auto_resolved_count"] == _THREE_AUTO_RESOLVED
    assert after["total"] == _FIVE_TOTAL
    assert after["backfill_source"] == "alerts_row_state"

    captured = capsys.readouterr()
    assert "Backfilled 2 ACKED + 3 AUTO_RESOLVED = 5 alert_outcomes rows" in captured.out


@pytest.mark.asyncio
async def test_zero_matches_writes_zero_row_audit(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No acked/resolved alerts -> real run still writes one audit row with all-zero counts."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)

    await _seed_alert(repo)  # still firing only

    args = argparse.Namespace(
        dry_run=False,
        confirm_phrase="backfill outcomes from historical alert state",
    )
    rc = await _cmd_backfill_outcomes(args)

    assert rc == 0
    assert await _count_outcomes(repo) == 0
    assert await _count_audit(repo, "alert_outcomes_backfilled") == 1
    after = await _first_audit_after(repo, "alert_outcomes_backfilled")
    assert after["acked_count"] == 0
    assert after["auto_resolved_count"] == 0
    assert after["total"] == 0


@pytest.mark.asyncio
async def test_second_run_after_success_refuses(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Second real run after a successful first run refuses (backfill row exists)."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)

    await _seed_alert(repo, ack_at="2026-07-01T00:00:00Z")
    await _seed_alert(repo, resolved_at="2026-07-02T00:00:00Z")

    args = argparse.Namespace(
        dry_run=False,
        confirm_phrase="backfill outcomes from historical alert state",
    )
    rc1 = await _cmd_backfill_outcomes(args)
    assert rc1 == 0
    count_after_first = await _count_outcomes(repo)

    rc2 = await _cmd_backfill_outcomes(args)
    assert rc2 == 1
    captured = capsys.readouterr()
    assert "already been run" in captured.err
    assert await _count_outcomes(repo) == count_after_first


@pytest.mark.asyncio
async def test_dry_run_after_prior_backfill_row_still_reports_counts(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Dry-run does NOT check the guard — it always reports counts, even if backfill already ran."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)

    # Seed 2 ACKED alerts + 3 AUTO_RESOLVED alerts
    acked_ids = [await _seed_alert(repo, ack_at="2026-07-01T00:00:00Z") for _ in range(_TWO_ACKED)]
    [
        await _seed_alert(repo, resolved_at="2026-07-02T00:00:00Z")
        for _ in range(_THREE_AUTO_RESOLVED)
    ]

    # Insert a decided_by='backfill' outcome row directly (simulating prior backfill run)
    await _seed_outcome(repo, alert_id=acked_ids[0], outcome="acked", decided_by="backfill")

    # Invoke dry-run — guard should NOT trip
    args = argparse.Namespace(dry_run=True, confirm_phrase=None)
    rc = await _cmd_backfill_outcomes(args)

    assert rc == 0
    captured = capsys.readouterr()
    assert "Dry run: would insert 2 ACKED + 3 AUTO_RESOLVED = 5 alert_outcomes rows" in captured.out

    # Verify no new alert_outcomes rows were written (just the one pre-seeded)
    assert await _count_outcomes(repo) == 1
    # Verify no audit_log rows were written
    assert await _count_audit(repo, "alert_outcomes_backfilled") == 0


@pytest.mark.asyncio
async def test_second_run_with_only_reconciler_rows_does_not_refuse(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-existing decided_by='reconciler' rows do NOT trip the guard.

    ON CONFLICT DO NOTHING skips the one AUTO_RESOLVED alert already covered
    by a reconciler row; the other 4 slots (2 ACKED + 2 AUTO_RESOLVED) insert fresh.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)

    await _seed_alert(repo, ack_at="2026-07-01T00:00:00Z")
    await _seed_alert(repo, ack_at="2026-07-01T00:00:00Z")
    reconciled_id = await _seed_alert(repo, resolved_at="2026-07-02T00:00:00Z")
    await _seed_alert(repo, resolved_at="2026-07-02T00:00:00Z")
    await _seed_alert(repo, resolved_at="2026-07-02T00:00:00Z")

    # Reconciler already wrote an outcome for one of the auto-resolved alerts.
    await _seed_outcome(
        repo, alert_id=reconciled_id, outcome="auto_resolved", decided_by="reconciler"
    )

    args = argparse.Namespace(
        dry_run=False,
        confirm_phrase="backfill outcomes from historical alert state",
    )
    rc = await _cmd_backfill_outcomes(args)

    assert rc == 0
    # 1 pre-existing reconciler row + 4 new backfill rows (2 acked, 2 auto_resolved) = 5 total.
    assert await _count_outcomes(repo) == _FIVE_TOTAL
    assert await _count_outcomes_by_kind(repo, "acked", "backfill") == _TWO_ACKED
    assert await _count_outcomes_by_kind(repo, "auto_resolved", "backfill") == 2  # noqa: PLR2004
    assert await _count_outcomes_by_kind(repo, "auto_resolved", "reconciler") == 1


@pytest.mark.asyncio
async def test_mixed_states_maps_correctly(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge cases: ack_at wins when both set; still-firing gets no row."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)

    alert_a = await _seed_alert(repo, ack_at="2026-07-01T00:00:00Z")  # ACKED only
    alert_b = await _seed_alert(repo, resolved_at="2026-07-02T00:00:00Z")  # AUTO_RESOLVED only
    alert_c = await _seed_alert(
        repo, ack_at="2026-07-03T00:00:00Z", resolved_at="2026-07-04T00:00:00Z"
    )  # both set -> ACKED only
    await _seed_alert(repo)  # still firing -> no row (alert_d)

    args = argparse.Namespace(
        dry_run=False,
        confirm_phrase="backfill outcomes from historical alert state",
    )
    rc = await _cmd_backfill_outcomes(args)

    assert rc == 0
    THREE_ROWS = 3
    assert await _count_outcomes(repo) == THREE_ROWS
    assert await _outcome_decided_at(repo, alert_a, "acked") == "2026-07-01T00:00:00Z"
    assert await _outcome_decided_at(repo, alert_b, "auto_resolved") == "2026-07-02T00:00:00Z"
    assert await _outcome_decided_at(repo, alert_c, "acked") == "2026-07-03T00:00:00Z"


@pytest.mark.asyncio
async def test_progress_line_printed_when_over_threshold(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """>10,000 ACKED rows triggers at least one progress line (coverage for that branch)."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)

    ROWS = 10_001
    async with repo.transaction() as conn:
        for i in range(ROWS):
            await conn.execute(
                _INSERT_ALERT_SQL,
                {
                    "id": f"alert-{i}",
                    "fingerprint": f"fp-{i}",
                    "created_at": "2026-07-06T00:00:00Z",
                    "payload_json": json.dumps({"labels": {"alertname": "Test"}}),
                    "ack_at": "2026-07-01T00:00:00Z",
                    "resolved_at": None,
                },
            )

    args = argparse.Namespace(
        dry_run=False,
        confirm_phrase="backfill outcomes from historical alert state",
    )
    rc = await _cmd_backfill_outcomes(args)

    assert rc == 0
    captured = capsys.readouterr()
    assert "Progress: processed 10000/10001 ACKED rows..." in captured.out
    assert await _count_outcomes(repo) == ROWS


@pytest.mark.asyncio
async def test_progress_line_printed_when_over_threshold_auto_resolved(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """>10,000 AUTO_RESOLVED rows triggers at least one progress line (coverage for that branch)."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)

    ROWS = 10_001
    async with repo.transaction() as conn:
        for i in range(ROWS):
            await conn.execute(
                _INSERT_ALERT_SQL,
                {
                    "id": f"alert-{i}",
                    "fingerprint": f"fp-{i}",
                    "created_at": "2026-07-06T00:00:00Z",
                    "payload_json": json.dumps({"labels": {"alertname": "Test"}}),
                    "ack_at": None,
                    "resolved_at": "2026-07-01T00:00:00Z",
                },
            )

    args = argparse.Namespace(
        dry_run=False,
        confirm_phrase="backfill outcomes from historical alert state",
    )
    rc = await _cmd_backfill_outcomes(args)

    assert rc == 0
    captured = capsys.readouterr()
    assert "Progress: processed 10000/10001 AUTO_RESOLVED rows..." in captured.out
    assert await _count_outcomes(repo) == ROWS


class TestHandleBackfillDispatch:
    def test_dispatches_backfill_outcomes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Route alerts_cmd='backfill-outcomes' to _cmd_backfill_outcomes via asyncio.run."""
        from homelab_monitor.cli import alerts as alerts_cli  # noqa: PLC0415

        called: list[argparse.Namespace] = []

        async def fake_backfill(args: argparse.Namespace) -> int:
            called.append(args)
            return 0

        monkeypatch.setattr(
            alerts_cli,
            "_cmd_backfill_outcomes",
            fake_backfill,
        )
        args = argparse.Namespace(alerts_cmd="backfill-outcomes", dry_run=True, confirm_phrase=None)
        rc = alerts_cli._handle(args)  # pyright: ignore[reportPrivateUsage]

        assert rc == 0
        assert len(called) == 1


def test_backfill_outcomes_argparse_registration() -> None:
    """argparse: `hm alerts backfill-outcomes --dry-run` parses without error."""
    from homelab_monitor.cli import alerts as alerts_cli  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="hm")
    subparsers = parser.add_subparsers(dest="cmd")
    alerts_cli.add_subparser(subparsers)

    ns = parser.parse_args(["alerts", "backfill-outcomes", "--dry-run"])
    assert ns.alerts_cmd == "backfill-outcomes"
    assert ns.dry_run is True
