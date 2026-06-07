"""Tests for CronRunFailureEnrichmentsRepository (STAGE-004-034).

Uses the `repo` fixture (real in-memory migrated DB). 100% coverage of
cron_run_failure_enrichments_repo.py.

Project test conventions:
- asyncio_mode=auto — bare async def, no @pytest.mark.asyncio decorator
- noqa: PLR2004 for magic number assertions
- DB: repo fixture (conftest.py) = real in-memory migrated DB
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.cron_run_failure_enrichments_repo import (
    CronRunFailureEnrichmentsRepository,
)
from homelab_monitor.kernel.logs.models import LogLine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _line(msg: str) -> LogLine:
    return LogLine(
        timestamp="2026-06-07T00:00:00Z",
        message=msg,
        stream="s",
        severity="error",
        host=None,
        service=None,
        fields={},
    )


# ---------------------------------------------------------------------------
# Tests: insert
# ---------------------------------------------------------------------------


async def test_insert_returns_true_on_new(repo: SqliteRepository) -> None:
    """insert() returns True when a new row is inserted."""
    failure_id: str = uuid.uuid4().hex
    run_id: str = str(uuid.uuid4())
    fp: str = "fp-insert-true"
    failure_repo = CronRunFailureEnrichmentsRepository(repo)
    inserted: bool = await failure_repo.insert(
        failure_id=failure_id,
        cron_fingerprint=fp,
        run_id=run_id,
        exit_code=2,
        started_at="2026-06-07T00:00:00+00:00",
        ended_at="2026-06-07T00:00:10+00:00",
        lines=[_line("line a"), _line("line b")],
        truncated=False,
        degraded=False,
        window_start="2026-06-06T23:59:00+00:00",
        window_end="2026-06-07T00:00:15+00:00",
    )
    assert inserted is True

    row = await failure_repo.get_by_run(fp, run_id)
    assert row is not None
    assert row.failure_id == failure_id
    assert row.cron_fingerprint == fp
    assert row.run_id == run_id
    assert row.exit_code == 2  # noqa: PLR2004
    assert row.line_count == 2  # noqa: PLR2004
    assert row.degraded is False
    assert row.truncated is False

    parsed = row.parse_lines()
    assert len(parsed) == 2  # noqa: PLR2004
    assert parsed[0].message == "line a"
    assert parsed[1].message == "line b"


async def test_insert_or_ignore_duplicate_returns_false(repo: SqliteRepository) -> None:
    """Inserting with same (cron_fingerprint, run_id) returns False; only 1 row exists."""
    fp: str = "fp-dup-test"
    run_id: str = str(uuid.uuid4())
    failure_repo = CronRunFailureEnrichmentsRepository(repo)

    r1: bool = await failure_repo.insert(
        failure_id=uuid.uuid4().hex,
        cron_fingerprint=fp,
        run_id=run_id,
        exit_code=1,
        started_at="2026-06-07T00:00:00+00:00",
        ended_at="2026-06-07T00:00:10+00:00",
        lines=[_line("first")],
        truncated=False,
        degraded=False,
        window_start="2026-06-06T23:59:00+00:00",
        window_end="2026-06-07T00:00:15+00:00",
    )
    assert r1 is True

    # Same (fp, run_id) — different failure_id
    r2: bool = await failure_repo.insert(
        failure_id=uuid.uuid4().hex,
        cron_fingerprint=fp,
        run_id=run_id,
        exit_code=1,
        started_at="2026-06-07T00:00:00+00:00",
        ended_at="2026-06-07T00:00:10+00:00",
        lines=[_line("second")],
        truncated=False,
        degraded=False,
        window_start="2026-06-06T23:59:00+00:00",
        window_end="2026-06-07T00:00:15+00:00",
    )
    assert r2 is False

    # Still only one row exists
    row = await failure_repo.get_by_run(fp, run_id)
    assert row is not None
    # Original row has the "first" line (not overwritten by second attempt)
    lines = row.parse_lines()
    assert len(lines) == 1
    assert lines[0].message == "first"


async def test_get_by_run_returns_row(repo: SqliteRepository) -> None:
    """get_by_run returns the enrichment for (fp, run_id)."""
    fp: str = "fp-get-test"
    run_id: str = str(uuid.uuid4())
    failure_repo = CronRunFailureEnrichmentsRepository(repo)
    await failure_repo.insert(
        failure_id=uuid.uuid4().hex,
        cron_fingerprint=fp,
        run_id=run_id,
        exit_code=3,
        started_at="2026-06-07T00:00:00+00:00",
        ended_at="2026-06-07T00:00:10+00:00",
        lines=[_line("the line")],
        truncated=False,
        degraded=True,
        window_start="2026-06-06T23:59:00+00:00",
        window_end="2026-06-07T00:00:15+00:00",
    )

    row = await failure_repo.get_by_run(fp, run_id)
    assert row is not None
    assert row.cron_fingerprint == fp
    assert row.run_id == run_id
    assert row.exit_code == 3  # noqa: PLR2004
    assert row.degraded is True


async def test_get_by_run_missing_returns_none(repo: SqliteRepository) -> None:
    """get_by_run returns None for a non-existent (fp, run_id)."""
    failure_repo = CronRunFailureEnrichmentsRepository(repo)
    result = await failure_repo.get_by_run("fp-none", "run-none")
    assert result is None


# ---------------------------------------------------------------------------
# Tests: prune
# ---------------------------------------------------------------------------


async def test_prune_by_age(repo: SqliteRepository) -> None:
    """prune() deletes rows with created_at before cutoff; recent rows remain."""
    fp: str = "fp-age-prune"
    failure_repo = CronRunFailureEnrichmentsRepository(repo)
    now: datetime = datetime.now(UTC)

    # Old row — insert directly so we can set created_at to past
    old_run_id: str = str(uuid.uuid4())
    old_created_at: str = (now - timedelta(days=40)).isoformat()
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO cron_run_failure_enrichments ("
                "  failure_id, cron_fingerprint, run_id, exit_code, started_at, "
                "  ended_at, lines_json, line_count, truncated, degraded, "
                "  window_start, window_end, created_at"
                ") VALUES ("
                "  :fid, :fp, :run_id, 1, '2026-06-07T00:00:00+00:00', "
                "  '2026-06-07T00:00:10+00:00', '[]', 0, 0, 0, "
                "  '2026-06-06T23:59:00+00:00', '2026-06-07T00:00:15+00:00', :created_at"
                ")"
            ),
            {
                "fid": uuid.uuid4().hex,
                "fp": fp,
                "run_id": old_run_id,
                "created_at": old_created_at,
            },
        )

    # Recent row via insert()
    recent_run_id: str = str(uuid.uuid4())
    await failure_repo.insert(
        failure_id=uuid.uuid4().hex,
        cron_fingerprint=fp,
        run_id=recent_run_id,
        exit_code=1,
        started_at="2026-06-07T00:00:00+00:00",
        ended_at="2026-06-07T00:00:10+00:00",
        lines=[_line("recent")],
        truncated=False,
        degraded=False,
        window_start="2026-06-06T23:59:00+00:00",
        window_end="2026-06-07T00:00:15+00:00",
    )

    cutoff: str = (now - timedelta(days=7)).isoformat()
    deleted: int = await failure_repo.prune(
        retention_cutoff_iso=cutoff,
        max_rows_per_cron=50,
    )
    assert deleted == 1

    # Old row gone
    assert await failure_repo.get_by_run(fp, old_run_id) is None
    # Recent row remains
    assert await failure_repo.get_by_run(fp, recent_run_id) is not None


async def test_prune_by_count_cap(repo: SqliteRepository) -> None:
    """prune() removes oldest rows beyond max_rows_per_cron; returns deleted count."""
    fp: str = "fp-cap-prune"
    failure_repo = CronRunFailureEnrichmentsRepository(repo)
    now: datetime = datetime.now(UTC)

    # Insert 3 rows with distinct created_at (forced via direct insert)
    run_ids: list[str] = [str(uuid.uuid4()) for _ in range(3)]
    for i, run_id in enumerate(run_ids):
        created_at: str = (now - timedelta(seconds=10 - i)).isoformat()  # oldest first
        async with repo.transaction() as conn:
            await conn.execute(
                text(
                    "INSERT INTO cron_run_failure_enrichments ("
                    "  failure_id, cron_fingerprint, run_id, exit_code, started_at, "
                    "  ended_at, lines_json, line_count, truncated, degraded, "
                    "  window_start, window_end, created_at"
                    ") VALUES ("
                    "  :fid, :fp, :run_id, 1, '2026-06-07T00:00:00+00:00', "
                    "  '2026-06-07T00:00:10+00:00', '[]', 0, 0, 0, "
                    "  '2026-06-06T23:59:00+00:00', '2026-06-07T00:00:15+00:00', :created_at"
                    ")"
                ),
                {
                    "fid": uuid.uuid4().hex,
                    "fp": fp,
                    "run_id": run_id,
                    "created_at": created_at,
                },
            )

    # Far-past cutoff — age delete removes nothing; only cap applies
    far_past: str = "2000-01-01T00:00:00+00:00"
    deleted: int = await failure_repo.prune(
        retention_cutoff_iso=far_past,
        max_rows_per_cron=2,
    )
    assert deleted == 1

    # The oldest run_id should be gone; newest 2 remain
    assert await failure_repo.get_by_run(fp, run_ids[0]) is None
    assert await failure_repo.get_by_run(fp, run_ids[1]) is not None
    assert await failure_repo.get_by_run(fp, run_ids[2]) is not None


# ---------------------------------------------------------------------------
# Tests: parse_lines
# ---------------------------------------------------------------------------


async def test_parse_lines_roundtrip(repo: SqliteRepository) -> None:
    """parse_lines() roundtrips a LogLine with all fields including fields dict."""
    failure_repo = CronRunFailureEnrichmentsRepository(repo)
    fp: str = "fp-roundtrip"
    run_id: str = str(uuid.uuid4())
    original = LogLine(
        timestamp="2026-06-07T00:00:00Z",
        message="roundtrip msg",
        stream="s",
        severity="error",
        host=None,
        service=None,
        fields={"k": "v"},
    )
    await failure_repo.insert(
        failure_id=uuid.uuid4().hex,
        cron_fingerprint=fp,
        run_id=run_id,
        exit_code=1,
        started_at="2026-06-07T00:00:00+00:00",
        ended_at="2026-06-07T00:00:10+00:00",
        lines=[original],
        truncated=False,
        degraded=False,
        window_start="2026-06-06T23:59:00+00:00",
        window_end="2026-06-07T00:00:15+00:00",
    )

    row = await failure_repo.get_by_run(fp, run_id)
    assert row is not None
    parsed = row.parse_lines()
    assert len(parsed) == 1
    assert parsed[0] == original
    assert parsed[0].fields == {"k": "v"}


async def test_parse_lines_non_list_json_returns_empty(repo: SqliteRepository) -> None:
    """parse_lines() returns [] when lines_json is not a JSON array (defensive branch)."""
    fp: str = "fp-badrow"
    run_id: str = str(uuid.uuid4())
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO cron_run_failure_enrichments ("
                "  failure_id, cron_fingerprint, run_id, exit_code, started_at, "
                "  ended_at, lines_json, line_count, truncated, degraded, "
                "  window_start, window_end, created_at"
                ") VALUES ("
                "  :fid, :fp, :run_id, 1, '2026-06-07T00:00:00+00:00', "
                "  '2026-06-07T00:00:10+00:00', :lines_json, 0, 0, 0, "
                "  '2026-06-06T23:59:00+00:00', '2026-06-07T00:00:15+00:00', "
                "  '2026-06-07T00:00:00+00:00'"
                ")"
            ),
            {
                "fid": uuid.uuid4().hex,
                "fp": fp,
                "run_id": run_id,
                "lines_json": json.dumps({"not": "a list"}),
            },
        )

    failure_repo = CronRunFailureEnrichmentsRepository(repo)
    row = await failure_repo.get_by_run(fp, run_id)
    assert row is not None
    assert row.parse_lines() == []


async def test_parse_lines_skips_non_dict_items(repo: SqliteRepository) -> None:
    """parse_lines() skips non-dict items in the list (isinstance(item, dict) branch)."""
    fp: str = "fp-mixedrow"
    run_id: str = str(uuid.uuid4())
    valid_line_dict: dict[str, object] = {
        "timestamp": "2026-06-07T00:00:00Z",
        "message": "valid",
        "stream": "s",
        "severity": "error",
        "host": None,
        "service": None,
        "fields": {},
    }
    lines_json: str = json.dumps([valid_line_dict, "not-a-dict", 42])
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO cron_run_failure_enrichments ("
                "  failure_id, cron_fingerprint, run_id, exit_code, started_at, "
                "  ended_at, lines_json, line_count, truncated, degraded, "
                "  window_start, window_end, created_at"
                ") VALUES ("
                "  :fid, :fp, :run_id, 1, '2026-06-07T00:00:00+00:00', "
                "  '2026-06-07T00:00:10+00:00', :lines_json, 1, 0, 0, "
                "  '2026-06-06T23:59:00+00:00', '2026-06-07T00:00:15+00:00', "
                "  '2026-06-07T00:00:00+00:00'"
                ")"
            ),
            {
                "fid": uuid.uuid4().hex,
                "fp": fp,
                "run_id": run_id,
                "lines_json": lines_json,
            },
        )

    failure_repo = CronRunFailureEnrichmentsRepository(repo)
    row = await failure_repo.get_by_run(fp, run_id)
    assert row is not None
    parsed = row.parse_lines()
    assert len(parsed) == 1
    assert parsed[0].message == "valid"


async def test_parse_lines_skips_invalid_logline_dict(repo: SqliteRepository) -> None:
    """parse_lines() skips dict items that fail LogLine validation (ValidationError branch)."""
    fp: str = "fp-invalidrow"
    run_id: str = str(uuid.uuid4())
    invalid_line_dict: dict[str, object] = {"not": "a logline", "missing": "required fields"}
    valid_line_dict: dict[str, object] = {
        "timestamp": "2026-06-07T00:00:00Z",
        "message": "valid line",
        "stream": "s",
        "severity": "error",
        "host": None,
        "service": None,
        "fields": {},
    }
    lines_json: str = json.dumps([invalid_line_dict, valid_line_dict])
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO cron_run_failure_enrichments ("
                "  failure_id, cron_fingerprint, run_id, exit_code, started_at, "
                "  ended_at, lines_json, line_count, truncated, degraded, "
                "  window_start, window_end, created_at"
                ") VALUES ("
                "  :fid, :fp, :run_id, 1, '2026-06-07T00:00:00+00:00', "
                "  '2026-06-07T00:00:10+00:00', :lines_json, 1, 0, 0, "
                "  '2026-06-06T23:59:00+00:00', '2026-06-07T00:00:15+00:00', "
                "  '2026-06-07T00:00:00+00:00'"
                ")"
            ),
            {
                "fid": uuid.uuid4().hex,
                "fp": fp,
                "run_id": run_id,
                "lines_json": lines_json,
            },
        )

    failure_repo = CronRunFailureEnrichmentsRepository(repo)
    row = await failure_repo.get_by_run(fp, run_id)
    assert row is not None
    parsed = row.parse_lines()
    # The invalid dict is skipped; only the valid LogLine survives
    assert len(parsed) == 1
    assert parsed[0].message == "valid line"
