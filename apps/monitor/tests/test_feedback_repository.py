"""Unit tests for RunbookRunFeedbackRepository (STAGE-009-009).

Uses a real migrated SQLite DB (via conftest `repo` fixture) so INSERT/SELECT
SQL and FK enforcement are exercised for real.

100% branch coverage target on feedback_repository.py.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from homelab_monitor.kernel.alerts.types import Alert, AlertStatus, Severity
from homelab_monitor.kernel.autofix.feedback_parser import ParsedFeedbackItem
from homelab_monitor.kernel.autofix.feedback_repository import RunbookRunFeedbackRepository
from homelab_monitor.kernel.autofix.runs_repository import RunbookRunsRepository
from homelab_monitor.kernel.autofix.types import FeedbackKind, RunMode
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.runbooks.repository import RunbookRecord


def _make_alert(alertname: str = "TestAlert") -> Alert:
    labels = {"alertname": alertname, "severity": "warning"}
    return Alert(
        id=uuid7(),
        fingerprint=f"fp-{uuid7()}",
        source_tool="vmalert",
        severity=Severity.WARNING,
        status=AlertStatus.FIRING,
        opened_at=utc_now_iso(),
        last_seen_at=utc_now_iso(),
        payload={"labels": labels, "annotations": {}},
        labels=labels,
        annotations={},
    )


def _make_runbook_record(runbook_id: str | None = None) -> RunbookRecord:
    return RunbookRecord(
        id=runbook_id or uuid7(),
        path="/runbooks/test-runbook",
        created_at=utc_now_iso(),
        alert_match_patterns=[{"alertname": "TestAlert", "labels": {}}],
        risk_tag="safe",
        dry_run_required=False,
        rate_limit_per_hour=None,
        cooldown_seconds=None,
        enabled=True,
        auto_trigger=True,
        content_hash="abc123",
    )


async def _insert_runbook(repo: SqliteRepository, record: RunbookRecord) -> None:
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO runbooks "
                "(id, path, created_at, alert_match_patterns, risk_tag, "
                " dry_run_required, rate_limit_per_hour, cooldown_seconds, "
                " enabled, auto_trigger, content_hash) "
                "VALUES (:id, :path, :created_at, :patterns, :risk_tag, "
                " :dry_run, :rate_limit, :cooldown, :enabled, :auto_trigger, :hash)"
            ),
            {
                "id": record.id,
                "path": record.path,
                "created_at": record.created_at,
                "patterns": json.dumps(record.alert_match_patterns),
                "risk_tag": record.risk_tag,
                "dry_run": int(record.dry_run_required),
                "rate_limit": record.rate_limit_per_hour,
                "cooldown": record.cooldown_seconds,
                "enabled": int(record.enabled),
                "auto_trigger": int(record.auto_trigger),
                "hash": record.content_hash,
            },
        )


async def _insert_alert(repo: SqliteRepository, alert: Alert) -> None:
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO alerts "
                "(id, fingerprint, source_tool, severity, status, "
                " opened_at, last_seen_at, payload_json, created_at) "
                "VALUES (:id, :fp, :st, :sev, :status, :opened, :last_seen, :pj, :created)"
            ),
            {
                "id": alert.id,
                "fp": alert.fingerprint,
                "st": alert.source_tool,
                "sev": alert.severity.value,
                "status": alert.status.value,
                "opened": alert.opened_at,
                "last_seen": alert.last_seen_at,
                "pj": json.dumps(alert.payload, sort_keys=True),
                "created": utc_now_iso(),
            },
        )


async def _make_run(repo: SqliteRepository) -> str:
    """Insert a runbook + alert + runbook_runs row; return the run id."""
    rb = _make_runbook_record()
    await _insert_runbook(repo, rb)
    alert = _make_alert()
    await _insert_alert(repo, alert)
    runs_repo = RunbookRunsRepository(repo)
    async with repo.transaction() as conn:
        run_id = await runs_repo.insert_started(
            conn,
            runbook_id=rb.id,
            alert_id=alert.id,
            prompt=rb.path,
            fixer_user="homelab-fixer",
            host="testhost",
            runbook_hash=rb.content_hash,
            mode=RunMode.REAL,
            initiated_by="alert",
        )
    return run_id


@pytest.mark.asyncio
async def test_insert_and_list_round_trips_all_fields(repo: SqliteRepository) -> None:
    run_id = await _make_run(repo)
    feedback_repo = RunbookRunFeedbackRepository(repo)
    item = ParsedFeedbackItem(
        kind=FeedbackKind.MISSING_CAPABILITY,
        suggestion_text="need ssh grant",
        structured_hint={"target": "udm"},
    )
    async with repo.transaction() as conn:
        inserted = await feedback_repo.insert_conn(conn, runbook_run_id=run_id, item=item)

    results = await feedback_repo.list_by_run(run_id)
    assert len(results) == 1
    got = results[0]
    assert got.id == inserted.id
    assert got.runbook_run_id == run_id
    assert got.kind is FeedbackKind.MISSING_CAPABILITY
    assert got.suggestion_text == "need ssh grant"
    assert got.structured_hint == {"target": "udm"}
    assert got.created_at == inserted.created_at


@pytest.mark.asyncio
async def test_insert_three_rows_same_run_returns_all_in_order(repo: SqliteRepository) -> None:
    run_id = await _make_run(repo)
    feedback_repo = RunbookRunFeedbackRepository(repo)
    items = [
        ParsedFeedbackItem(
            kind=FeedbackKind.CONFIG_CHANGE, suggestion_text="a", structured_hint=None
        ),
        ParsedFeedbackItem(kind=FeedbackKind.BLOCKED, suggestion_text="b", structured_hint=None),
        ParsedFeedbackItem(
            kind=FeedbackKind.WORKED_AROUND,
            suggestion_text="c",
            structured_hint=None,
        ),
    ]
    for idx, item in enumerate(items):
        async with repo.transaction() as conn:
            await feedback_repo.insert_conn(
                conn,
                runbook_run_id=run_id,
                item=item,
                created_at=f"2026-01-01T00:00:0{idx}Z",
            )

    results = await feedback_repo.list_by_run(run_id)
    assert len(results) == 3  # noqa: PLR2004
    assert [r.suggestion_text for r in results] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_structured_hint_none_round_trips_as_none(repo: SqliteRepository) -> None:
    run_id = await _make_run(repo)
    feedback_repo = RunbookRunFeedbackRepository(repo)
    item = ParsedFeedbackItem(kind=FeedbackKind.OTHER, suggestion_text="x", structured_hint=None)
    async with repo.transaction() as conn:
        await feedback_repo.insert_conn(conn, runbook_run_id=run_id, item=item)

    results = await feedback_repo.list_by_run(run_id)
    assert len(results) == 1
    assert results[0].structured_hint is None


@pytest.mark.asyncio
async def test_nested_dict_structured_hint_round_trips_shape(repo: SqliteRepository) -> None:
    run_id = await _make_run(repo)
    feedback_repo = RunbookRunFeedbackRepository(repo)
    nested_hint: dict[str, object] = {
        "target": "udm",
        "details": {"missing": ["ssh"], "count": 2},
    }
    item = ParsedFeedbackItem(
        kind=FeedbackKind.RUNBOOK_GAP, suggestion_text="x", structured_hint=nested_hint
    )
    async with repo.transaction() as conn:
        await feedback_repo.insert_conn(conn, runbook_run_id=run_id, item=item)

    results = await feedback_repo.list_by_run(run_id)
    assert len(results) == 1
    assert results[0].structured_hint == nested_hint


@pytest.mark.asyncio
async def test_kind_parse_error_persists_and_returns_correctly(repo: SqliteRepository) -> None:
    run_id = await _make_run(repo)
    feedback_repo = RunbookRunFeedbackRepository(repo)
    item = ParsedFeedbackItem(
        kind=FeedbackKind.PARSE_ERROR, suggestion_text="parse_error: bad json", structured_hint=None
    )
    async with repo.transaction() as conn:
        await feedback_repo.insert_conn(conn, runbook_run_id=run_id, item=item)

    results = await feedback_repo.list_by_run(run_id)
    assert len(results) == 1
    assert results[0].kind is FeedbackKind.PARSE_ERROR


@pytest.mark.asyncio
async def test_insert_with_nonexistent_runbook_run_id_raises_fk_violation(
    repo: SqliteRepository,
) -> None:
    """FK enforcement: runbook_run_id must reference an existing runbook_runs row."""
    # Confirm FK pragma is actually enabled on this connection (mirrors
    # test_db_engine.test pattern) so a pass here isn't a false negative.
    async with repo.transaction() as conn:
        fk_enabled = (await conn.execute(text("PRAGMA foreign_keys"))).scalar_one()
    assert fk_enabled == 1

    feedback_repo = RunbookRunFeedbackRepository(repo)
    item = ParsedFeedbackItem(kind=FeedbackKind.OTHER, suggestion_text="x", structured_hint=None)
    with pytest.raises(IntegrityError):
        async with repo.transaction() as conn:
            await feedback_repo.insert_conn(conn, runbook_run_id="does-not-exist", item=item)


@pytest.mark.asyncio
async def test_list_by_run_filters_by_run(repo: SqliteRepository) -> None:
    run_id_1 = await _make_run(repo)
    run_id_2 = await _make_run(repo)
    feedback_repo = RunbookRunFeedbackRepository(repo)

    item_1 = ParsedFeedbackItem(
        kind=FeedbackKind.OTHER, suggestion_text="run1", structured_hint=None
    )
    item_2 = ParsedFeedbackItem(
        kind=FeedbackKind.OTHER, suggestion_text="run2", structured_hint=None
    )
    async with repo.transaction() as conn:
        await feedback_repo.insert_conn(conn, runbook_run_id=run_id_1, item=item_1)
    async with repo.transaction() as conn:
        await feedback_repo.insert_conn(conn, runbook_run_id=run_id_2, item=item_2)

    results_1 = await feedback_repo.list_by_run(run_id_1)
    results_2 = await feedback_repo.list_by_run(run_id_2)
    assert len(results_1) == 1
    assert results_1[0].suggestion_text == "run1"
    assert len(results_2) == 1
    assert results_2[0].suggestion_text == "run2"


@pytest.mark.asyncio
async def test_list_by_run_no_rows_returns_empty(repo: SqliteRepository) -> None:
    run_id = await _make_run(repo)
    feedback_repo = RunbookRunFeedbackRepository(repo)
    assert await feedback_repo.list_by_run(run_id) == []


@pytest.mark.asyncio
async def test_structured_hint_json_decodes_to_non_dict_yields_none(
    repo: SqliteRepository,
) -> None:
    """_row_to_feedback only accepts a dict-shaped decode; a JSON array (or any
    other non-dict) stored in the column round-trips as structured_hint=None.

    This can't happen via insert_conn (it only ever writes dict-or-null), so we
    write the row directly via raw SQL to exercise the row-hydration branch.
    """
    run_id = await _make_run(repo)
    feedback_repo = RunbookRunFeedbackRepository(repo)
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO runbook_run_feedback "
                "(id, runbook_run_id, kind, suggestion_text, structured_hint, created_at) "
                "VALUES (:id, :rid, :kind, :text, :hint, :created)"
            ),
            {
                "id": uuid7(),
                "rid": run_id,
                "kind": FeedbackKind.OTHER.value,
                "text": "x",
                "hint": json.dumps(["not", "a", "dict"]),
                "created": utc_now_iso(),
            },
        )

    results = await feedback_repo.list_by_run(run_id)
    assert len(results) == 1
    assert results[0].structured_hint is None
