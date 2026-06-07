"""Tests for CrashEnrichmentsRepository (STAGE-004-032).

Uses the `repo` fixture (real in-memory migrated DB). 100% coverage of
crash_enrichments_repo.py.

Project test conventions:
- asyncio_mode=auto — bare async def, no @pytest.mark.asyncio decorator
- noqa: PLR2004 for magic number assertions
- DB: repo fixture (conftest.py:135) = real in-memory migrated DB
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.crash_enrichments_repo import CrashEnrichmentsRepository
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


async def _insert_crash(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    crash_id: str | None = None,
    logical_key: str = "name:c1",
    container_name: str = "c1",
    exit_code: int = 1,
    finished_at: str = "2026-06-07T00:00:00+00:00",
    lines: list[LogLine] | None = None,
    degraded: bool = False,
    truncated: bool = False,
) -> bool:
    if crash_id is None:
        crash_id = str(uuid.uuid4())
    if lines is None:
        lines = [_line("crash log")]
    crash_repo = CrashEnrichmentsRepository(repo)
    return await crash_repo.insert(
        crash_id=crash_id,
        logical_key=logical_key,
        container_name=container_name,
        container_id="abc123",
        exit_code=exit_code,
        finished_at=finished_at,
        image_name="ubuntu:22.04",
        compose_project=None,
        compose_service=None,
        lines=lines,
        truncated=truncated,
        degraded=degraded,
        window_start="2026-06-06T23:59:00+00:00",
        window_end="2026-06-07T00:00:05+00:00",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_insert_returns_true_on_new(repo: SqliteRepository) -> None:
    """insert() returns True when a new row is inserted."""
    crash_id = str(uuid.uuid4())
    crash_repo = CrashEnrichmentsRepository(repo)
    inserted = await crash_repo.insert(
        crash_id=crash_id,
        logical_key="name:c1",
        container_name="c1",
        container_id="cid1",
        exit_code=1,
        finished_at="2026-06-07T00:00:00+00:00",
        image_name="ubuntu:22.04",
        compose_project="proj",
        compose_service="svc",
        lines=[_line("crash a"), _line("crash b")],
        truncated=False,
        degraded=False,
        window_start="2026-06-06T23:59:00+00:00",
        window_end="2026-06-07T00:00:05+00:00",
    )
    assert inserted is True

    row = await crash_repo.get(crash_id)
    assert row is not None
    assert row.crash_id == crash_id
    assert row.logical_key == "name:c1"
    assert row.container_name == "c1"
    assert row.exit_code == 1
    assert row.line_count == 2  # noqa: PLR2004
    assert row.degraded is False
    assert row.truncated is False

    lines = row.parse_lines()
    assert len(lines) == 2  # noqa: PLR2004
    assert lines[0].message == "crash a"
    assert lines[1].message == "crash b"


async def test_insert_or_ignore_duplicate_returns_false(repo: SqliteRepository) -> None:
    """Inserting with same (logical_key, finished_at) returns False; only 1 row exists."""
    crash_repo = CrashEnrichmentsRepository(repo)
    lk = "name:c1"
    fa = "2026-06-07T00:00:00+00:00"

    first_id = str(uuid.uuid4())
    r1 = await crash_repo.insert(
        crash_id=first_id,
        logical_key=lk,
        container_name="c1",
        container_id=None,
        exit_code=1,
        finished_at=fa,
        image_name=None,
        compose_project=None,
        compose_service=None,
        lines=[_line("first")],
        truncated=False,
        degraded=False,
        window_start="2026-06-06T23:59:00+00:00",
        window_end="2026-06-07T00:00:05+00:00",
    )
    assert r1 is True

    second_id = str(uuid.uuid4())
    r2 = await crash_repo.insert(
        crash_id=second_id,
        logical_key=lk,
        container_name="c1",
        container_id=None,
        exit_code=1,
        finished_at=fa,
        image_name=None,
        compose_project=None,
        compose_service=None,
        lines=[_line("second")],
        truncated=False,
        degraded=False,
        window_start="2026-06-06T23:59:00+00:00",
        window_end="2026-06-07T00:00:05+00:00",
    )
    assert r2 is False

    rows = await crash_repo.list_for_container(lk)
    assert len(rows) == 1


async def test_list_for_container_orders_newest_first(repo: SqliteRepository) -> None:
    """list_for_container returns crashes newest finished_at first."""
    crash_repo = CrashEnrichmentsRepository(repo)
    lk = "name:order-test"

    await _insert_crash(
        repo,
        logical_key=lk,
        container_name="order-test",
        finished_at="2026-06-07T01:00:00+00:00",
    )
    await _insert_crash(
        repo,
        logical_key=lk,
        container_name="order-test",
        finished_at="2026-06-07T02:00:00+00:00",
    )

    rows = await crash_repo.list_for_container(lk)
    assert len(rows) == 2  # noqa: PLR2004
    # Newest first
    assert rows[0].finished_at > rows[1].finished_at


async def test_list_for_container_scopes_by_logical_key(repo: SqliteRepository) -> None:
    """list_for_container returns only rows for the given logical_key."""
    lk_a = "name:container-a"
    lk_b = "name:container-b"
    crash_repo = CrashEnrichmentsRepository(repo)

    await _insert_crash(repo, logical_key=lk_a, container_name="container-a")
    await _insert_crash(repo, logical_key=lk_b, container_name="container-b")

    rows_a = await crash_repo.list_for_container(lk_a)
    assert len(rows_a) == 1
    assert rows_a[0].container_name == "container-a"

    rows_b = await crash_repo.list_for_container(lk_b)
    assert len(rows_b) == 1
    assert rows_b[0].container_name == "container-b"


async def test_get_missing_returns_none(repo: SqliteRepository) -> None:
    """get() returns None for a non-existent crash_id."""
    crash_repo = CrashEnrichmentsRepository(repo)
    result = await crash_repo.get("nope")
    assert result is None


async def test_parse_lines_roundtrips_logline(repo: SqliteRepository) -> None:
    """parse_lines() roundtrips a LogLine with all fields including fields dict."""
    crash_repo = CrashEnrichmentsRepository(repo)
    original = LogLine(
        timestamp="2026-06-07T00:00:00Z",
        message="roundtrip msg",
        stream="s",
        severity="error",
        host=None,
        service=None,
        fields={"k": "v"},
    )
    crash_id = str(uuid.uuid4())
    await crash_repo.insert(
        crash_id=crash_id,
        logical_key="name:roundtrip",
        container_name="roundtrip",
        container_id=None,
        exit_code=2,
        finished_at="2026-06-07T00:00:00+00:00",
        image_name=None,
        compose_project=None,
        compose_service=None,
        lines=[original],
        truncated=False,
        degraded=False,
        window_start="2026-06-06T23:59:00+00:00",
        window_end="2026-06-07T00:00:05+00:00",
    )

    row = await crash_repo.get(crash_id)
    assert row is not None
    parsed = row.parse_lines()
    assert len(parsed) == 1
    assert parsed[0] == original
    assert parsed[0].fields == {"k": "v"}


async def test_prune_by_age(repo: SqliteRepository) -> None:
    """prune() deletes rows older than retention_cutoff_iso; recent rows remain."""
    crash_repo = CrashEnrichmentsRepository(repo)
    now = datetime.now(UTC)

    old_fa = (now - timedelta(days=30)).isoformat()
    recent_fa = (now - timedelta(days=1)).isoformat()
    cutoff = (now - timedelta(days=7)).isoformat()

    await _insert_crash(
        repo,
        logical_key="name:age-test",
        container_name="age-test",
        finished_at=old_fa,
    )
    await _insert_crash(
        repo,
        logical_key="name:age-test",
        container_name="age-test",
        finished_at=recent_fa,
    )

    deleted = await crash_repo.prune(
        retention_cutoff_iso=cutoff,
        max_rows_per_container=50,
    )
    assert deleted == 1

    rows = await crash_repo.list_for_container("name:age-test")
    assert len(rows) == 1
    assert rows[0].finished_at == recent_fa


async def test_prune_by_count_cap(repo: SqliteRepository) -> None:
    """prune() removes oldest rows beyond max_rows_per_container; returns count deleted."""
    crash_repo = CrashEnrichmentsRepository(repo)
    lk = "name:cap-test"

    # Insert 3 crashes with distinct finished_at
    for i in range(3):
        fa = f"2026-06-07T00:00:0{i}+00:00"
        await _insert_crash(
            repo,
            logical_key=lk,
            container_name="cap-test",
            finished_at=fa,
        )

    # Far-past cutoff — age delete removes nothing; only cap enforced
    far_past = "2000-01-01T00:00:00+00:00"
    deleted = await crash_repo.prune(
        retention_cutoff_iso=far_past,
        max_rows_per_container=2,
    )
    assert deleted == 1

    rows = await crash_repo.list_for_container(lk)
    assert len(rows) == 2  # noqa: PLR2004
    # Newest 2 kept (T02 and T01, T00 deleted)
    fats = {r.finished_at for r in rows}
    assert "2026-06-07T00:00:02+00:00" in fats
    assert "2026-06-07T00:00:01+00:00" in fats


async def test_parse_lines_non_list_json_returns_empty(repo: SqliteRepository) -> None:
    """parse_lines() returns [] when lines_json is not a JSON array (defensive branch)."""
    crash_id = str(uuid.uuid4())
    # Insert a row with non-list lines_json directly (bypassing normal path)
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO container_crash_enrichments ("
                "  crash_id, logical_key, container_name, container_id, exit_code, "
                "  finished_at, image_name, compose_project, compose_service, "
                "  lines_json, line_count, truncated, degraded, window_start, "
                "  window_end, created_at"
                ") VALUES ("
                "  :crash_id, :lk, :cn, NULL, 1, "
                "  '2026-06-07T00:00:00+00:00', NULL, NULL, NULL, "
                "  :lines_json, 0, 0, 0, '2026-06-06T23:59:00+00:00', "
                "  '2026-06-07T00:00:05+00:00', '2026-06-07T00:00:00+00:00'"
                ")"
            ),
            {
                "crash_id": crash_id,
                "lk": "name:badrow",
                "cn": "badrow",
                "lines_json": json.dumps({"not": "a list"}),
            },
        )

    crash_repo = CrashEnrichmentsRepository(repo)
    row = await crash_repo.get(crash_id)
    assert row is not None
    # parse_lines returns [] for non-list JSON
    assert row.parse_lines() == []


async def test_parse_lines_skips_invalid_logline_dict(repo: SqliteRepository) -> None:
    """parse_lines() skips dict items that fail LogLine validation (ValidationError branch).

    A dict that is not a valid LogLine (missing required fields like timestamp,
    message, stream, severity) is silently dropped rather than raising.
    """
    crash_id = str(uuid.uuid4())
    # A dict that passes isinstance(item, dict) but fails LogLine(**fields)
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
    lines_json = json.dumps([invalid_line_dict, valid_line_dict])

    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO container_crash_enrichments ("
                "  crash_id, logical_key, container_name, container_id, exit_code, "
                "  finished_at, image_name, compose_project, compose_service, "
                "  lines_json, line_count, truncated, degraded, window_start, "
                "  window_end, created_at"
                ") VALUES ("
                "  :crash_id, :lk, :cn, NULL, 1, "
                "  '2026-06-07T00:00:00+00:00', NULL, NULL, NULL, "
                "  :lines_json, 1, 0, 0, '2026-06-06T23:59:00+00:00', "
                "  '2026-06-07T00:00:05+00:00', '2026-06-07T00:00:00+00:00'"
                ")"
            ),
            {
                "crash_id": crash_id,
                "lk": "name:invalidrow",
                "cn": "invalidrow",
                "lines_json": lines_json,
            },
        )

    crash_repo = CrashEnrichmentsRepository(repo)
    row = await crash_repo.get(crash_id)
    assert row is not None
    parsed = row.parse_lines()
    # The invalid dict is skipped; only the valid LogLine survives
    assert len(parsed) == 1
    assert parsed[0].message == "valid line"


async def test_parse_lines_skips_non_dict_items(repo: SqliteRepository) -> None:
    """parse_lines() skips non-dict items in the list (defensive branch)."""
    crash_id = str(uuid.uuid4())
    # Mix of a valid dict LogLine and a non-dict item
    valid_line_dict: dict[str, object] = {
        "timestamp": "2026-06-07T00:00:00Z",
        "message": "valid",
        "stream": "s",
        "severity": "error",
        "host": None,
        "service": None,
        "fields": {},
    }
    lines_json = json.dumps([valid_line_dict, "not-a-dict", 42])

    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO container_crash_enrichments ("
                "  crash_id, logical_key, container_name, container_id, exit_code, "
                "  finished_at, image_name, compose_project, compose_service, "
                "  lines_json, line_count, truncated, degraded, window_start, "
                "  window_end, created_at"
                ") VALUES ("
                "  :crash_id, :lk, :cn, NULL, 1, "
                "  '2026-06-07T00:00:00+00:00', NULL, NULL, NULL, "
                "  :lines_json, 1, 0, 0, '2026-06-06T23:59:00+00:00', "
                "  '2026-06-07T00:00:05+00:00', '2026-06-07T00:00:00+00:00'"
                ")"
            ),
            {
                "crash_id": crash_id,
                "lk": "name:mixedrow",
                "cn": "mixedrow",
                "lines_json": lines_json,
            },
        )

    crash_repo = CrashEnrichmentsRepository(repo)
    row = await crash_repo.get(crash_id)
    assert row is not None
    parsed = row.parse_lines()
    assert len(parsed) == 1
    assert parsed[0].message == "valid"
