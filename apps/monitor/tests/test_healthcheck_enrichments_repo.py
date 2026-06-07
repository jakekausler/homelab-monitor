"""Tests for HealthcheckEnrichmentsRepository (STAGE-004-033).

Uses the `repo` fixture (real in-memory migrated DB). 100% coverage of
healthcheck_enrichments_repo.py.

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
from homelab_monitor.kernel.logs.healthcheck_enrichments_repo import (
    HealthcheckEnrichmentsRepository,
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


async def _insert_incident(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    incident_id: str | None = None,
    logical_key: str = "name:c1",
    container_name: str = "c1",
    previous_healthcheck: str | None = "healthy",
    new_state: str = "unhealthy",
    healthcheck_changed_at: str = "2026-06-07T00:00:00+00:00",
    lines: list[LogLine] | None = None,
    degraded: bool = False,
    truncated: bool = False,
) -> bool:
    if incident_id is None:
        incident_id = str(uuid.uuid4())
    if lines is None:
        lines = [_line("healthcheck log")]
    hc_repo = HealthcheckEnrichmentsRepository(repo)
    return await hc_repo.insert(
        incident_id=incident_id,
        logical_key=logical_key,
        container_name=container_name,
        container_id="abc123",
        previous_healthcheck=previous_healthcheck,
        new_state=new_state,
        healthcheck_changed_at=healthcheck_changed_at,
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
    incident_id = str(uuid.uuid4())
    hc_repo = HealthcheckEnrichmentsRepository(repo)
    inserted = await hc_repo.insert(
        incident_id=incident_id,
        logical_key="name:c1",
        container_name="c1",
        container_id="cid1",
        previous_healthcheck="healthy",
        new_state="unhealthy",
        healthcheck_changed_at="2026-06-07T00:00:00+00:00",
        image_name="ubuntu:22.04",
        compose_project="proj",
        compose_service="svc",
        lines=[_line("hc a"), _line("hc b")],
        truncated=False,
        degraded=False,
        window_start="2026-06-06T23:59:00+00:00",
        window_end="2026-06-07T00:00:05+00:00",
    )
    assert inserted is True

    row = await hc_repo.get(incident_id)
    assert row is not None
    assert row.incident_id == incident_id
    assert row.logical_key == "name:c1"
    assert row.container_name == "c1"
    assert row.previous_healthcheck == "healthy"
    assert row.new_state == "unhealthy"
    assert row.line_count == 2  # noqa: PLR2004
    assert row.degraded is False
    assert row.truncated is False

    lines = row.parse_lines()
    assert len(lines) == 2  # noqa: PLR2004
    assert lines[0].message == "hc a"
    assert lines[1].message == "hc b"


async def test_insert_or_ignore_duplicate_returns_false(repo: SqliteRepository) -> None:
    """Inserting with same (logical_key, healthcheck_changed_at) returns False; only 1 row."""
    hc_repo = HealthcheckEnrichmentsRepository(repo)
    lk = "name:c1"
    changed_at = "2026-06-07T00:00:00+00:00"

    first_id = str(uuid.uuid4())
    r1 = await hc_repo.insert(
        incident_id=first_id,
        logical_key=lk,
        container_name="c1",
        container_id=None,
        previous_healthcheck="healthy",
        new_state="unhealthy",
        healthcheck_changed_at=changed_at,
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
    r2 = await hc_repo.insert(
        incident_id=second_id,
        logical_key=lk,
        container_name="c1",
        container_id=None,
        previous_healthcheck="healthy",
        new_state="unhealthy",
        healthcheck_changed_at=changed_at,
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

    rows = await hc_repo.list_for_container(lk)
    assert len(rows) == 1


async def test_list_for_container_orders_newest_first(repo: SqliteRepository) -> None:
    """list_for_container returns incidents newest healthcheck_changed_at first."""
    hc_repo = HealthcheckEnrichmentsRepository(repo)
    lk = "name:order-test"

    await _insert_incident(
        repo,
        logical_key=lk,
        container_name="order-test",
        healthcheck_changed_at="2026-06-07T01:00:00+00:00",
    )
    await _insert_incident(
        repo,
        logical_key=lk,
        container_name="order-test",
        healthcheck_changed_at="2026-06-07T02:00:00+00:00",
    )

    rows = await hc_repo.list_for_container(lk)
    assert len(rows) == 2  # noqa: PLR2004
    # Newest first
    assert rows[0].healthcheck_changed_at > rows[1].healthcheck_changed_at


async def test_list_for_container_scopes_by_logical_key(repo: SqliteRepository) -> None:
    """list_for_container returns only rows for the given logical_key."""
    lk_a = "name:container-a"
    lk_b = "name:container-b"
    hc_repo = HealthcheckEnrichmentsRepository(repo)

    await _insert_incident(repo, logical_key=lk_a, container_name="container-a")
    await _insert_incident(repo, logical_key=lk_b, container_name="container-b")

    rows_a = await hc_repo.list_for_container(lk_a)
    assert len(rows_a) == 1
    assert rows_a[0].container_name == "container-a"

    rows_b = await hc_repo.list_for_container(lk_b)
    assert len(rows_b) == 1
    assert rows_b[0].container_name == "container-b"


async def test_get_missing_returns_none(repo: SqliteRepository) -> None:
    """get() returns None for a non-existent incident_id."""
    hc_repo = HealthcheckEnrichmentsRepository(repo)
    result = await hc_repo.get("nope")
    assert result is None


async def test_parse_lines_roundtrips_logline(repo: SqliteRepository) -> None:
    """parse_lines() roundtrips a LogLine with all fields including fields dict."""
    hc_repo = HealthcheckEnrichmentsRepository(repo)
    original = LogLine(
        timestamp="2026-06-07T00:00:00Z",
        message="roundtrip msg",
        stream="s",
        severity="error",
        host=None,
        service=None,
        fields={"k": "v"},
    )
    incident_id = str(uuid.uuid4())
    await hc_repo.insert(
        incident_id=incident_id,
        logical_key="name:roundtrip",
        container_name="roundtrip",
        container_id=None,
        previous_healthcheck=None,
        new_state="unhealthy",
        healthcheck_changed_at="2026-06-07T00:00:00+00:00",
        image_name=None,
        compose_project=None,
        compose_service=None,
        lines=[original],
        truncated=False,
        degraded=False,
        window_start="2026-06-06T23:59:00+00:00",
        window_end="2026-06-07T00:00:05+00:00",
    )

    row = await hc_repo.get(incident_id)
    assert row is not None
    parsed = row.parse_lines()
    assert len(parsed) == 1
    assert parsed[0] == original
    assert parsed[0].fields == {"k": "v"}


async def test_prune_by_age(repo: SqliteRepository) -> None:
    """prune() deletes rows older than retention_cutoff_iso; recent rows remain."""
    hc_repo = HealthcheckEnrichmentsRepository(repo)
    now = datetime.now(UTC)

    old_changed_at = (now - timedelta(days=30)).isoformat()
    recent_changed_at = (now - timedelta(days=1)).isoformat()
    cutoff = (now - timedelta(days=7)).isoformat()

    await _insert_incident(
        repo,
        logical_key="name:age-test",
        container_name="age-test",
        healthcheck_changed_at=old_changed_at,
    )
    await _insert_incident(
        repo,
        logical_key="name:age-test",
        container_name="age-test",
        healthcheck_changed_at=recent_changed_at,
    )

    deleted = await hc_repo.prune(
        retention_cutoff_iso=cutoff,
        max_rows_per_container=50,
    )
    assert deleted == 1

    rows = await hc_repo.list_for_container("name:age-test")
    assert len(rows) == 1
    assert rows[0].healthcheck_changed_at == recent_changed_at


async def test_prune_by_count_cap(repo: SqliteRepository) -> None:
    """prune() removes oldest rows beyond max_rows_per_container; returns count deleted."""
    hc_repo = HealthcheckEnrichmentsRepository(repo)
    lk = "name:cap-test"

    # Insert 3 incidents with distinct healthcheck_changed_at
    for i in range(3):
        changed_at = f"2026-06-07T00:00:0{i}+00:00"
        await _insert_incident(
            repo,
            logical_key=lk,
            container_name="cap-test",
            healthcheck_changed_at=changed_at,
        )

    # Far-past cutoff — age delete removes nothing; only cap enforced
    far_past = "2000-01-01T00:00:00+00:00"
    deleted = await hc_repo.prune(
        retention_cutoff_iso=far_past,
        max_rows_per_container=2,
    )
    assert deleted == 1

    rows = await hc_repo.list_for_container(lk)
    assert len(rows) == 2  # noqa: PLR2004
    # Newest 2 kept (T02 and T01, T00 deleted)
    changed_ats = {r.healthcheck_changed_at for r in rows}
    assert "2026-06-07T00:00:02+00:00" in changed_ats
    assert "2026-06-07T00:00:01+00:00" in changed_ats


async def test_parse_lines_non_list_json_returns_empty(repo: SqliteRepository) -> None:
    """parse_lines() returns [] when lines_json is not a JSON array (defensive branch)."""
    incident_id = str(uuid.uuid4())
    # Insert a row with non-list lines_json directly (bypassing normal path)
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO container_healthcheck_enrichments ("
                "  incident_id, logical_key, container_name, container_id, "
                "  previous_healthcheck, new_state, healthcheck_changed_at, image_name, "
                "  compose_project, compose_service, lines_json, line_count, "
                "  truncated, degraded, window_start, window_end, created_at"
                ") VALUES ("
                "  :incident_id, :lk, :cn, NULL, "
                "  NULL, 'unhealthy', '2026-06-07T00:00:00+00:00', NULL, "
                "  NULL, NULL, :lines_json, 0, "
                "  0, 0, '2026-06-06T23:59:00+00:00', '2026-06-07T00:00:05+00:00', "
                "  '2026-06-07T00:00:00+00:00'"
                ")"
            ),
            {
                "incident_id": incident_id,
                "lk": "name:badrow",
                "cn": "badrow",
                "lines_json": json.dumps({"not": "a list"}),
            },
        )

    hc_repo = HealthcheckEnrichmentsRepository(repo)
    row = await hc_repo.get(incident_id)
    assert row is not None
    # parse_lines returns [] for non-list JSON
    assert row.parse_lines() == []


async def test_parse_lines_skips_invalid_logline_dict(repo: SqliteRepository) -> None:
    """parse_lines() skips dict items that fail LogLine validation (ValidationError branch).

    A dict that is not a valid LogLine (missing required fields like timestamp,
    message, stream, severity) is silently dropped rather than raising.
    """
    incident_id = str(uuid.uuid4())
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
                "INSERT INTO container_healthcheck_enrichments ("
                "  incident_id, logical_key, container_name, container_id, "
                "  previous_healthcheck, new_state, healthcheck_changed_at, image_name, "
                "  compose_project, compose_service, lines_json, line_count, "
                "  truncated, degraded, window_start, window_end, created_at"
                ") VALUES ("
                "  :incident_id, :lk, :cn, NULL, "
                "  NULL, 'unhealthy', '2026-06-07T00:00:00+00:00', NULL, "
                "  NULL, NULL, :lines_json, 1, "
                "  0, 0, '2026-06-06T23:59:00+00:00', '2026-06-07T00:00:05+00:00', "
                "  '2026-06-07T00:00:00+00:00'"
                ")"
            ),
            {
                "incident_id": incident_id,
                "lk": "name:invalidrow",
                "cn": "invalidrow",
                "lines_json": lines_json,
            },
        )

    hc_repo = HealthcheckEnrichmentsRepository(repo)
    row = await hc_repo.get(incident_id)
    assert row is not None
    parsed = row.parse_lines()
    # The invalid dict is skipped; only the valid LogLine survives
    assert len(parsed) == 1
    assert parsed[0].message == "valid line"


async def test_parse_lines_skips_non_dict_items(repo: SqliteRepository) -> None:
    """parse_lines() skips non-dict items in the list (defensive branch)."""
    incident_id = str(uuid.uuid4())
    # Mix of a valid dict LogLine and non-dict items
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
                "INSERT INTO container_healthcheck_enrichments ("
                "  incident_id, logical_key, container_name, container_id, "
                "  previous_healthcheck, new_state, healthcheck_changed_at, image_name, "
                "  compose_project, compose_service, lines_json, line_count, "
                "  truncated, degraded, window_start, window_end, created_at"
                ") VALUES ("
                "  :incident_id, :lk, :cn, NULL, "
                "  NULL, 'unhealthy', '2026-06-07T00:00:00+00:00', NULL, "
                "  NULL, NULL, :lines_json, 1, "
                "  0, 0, '2026-06-06T23:59:00+00:00', '2026-06-07T00:00:05+00:00', "
                "  '2026-06-07T00:00:00+00:00'"
                ")"
            ),
            {
                "incident_id": incident_id,
                "lk": "name:mixedrow",
                "cn": "mixedrow",
                "lines_json": lines_json,
            },
        )

    hc_repo = HealthcheckEnrichmentsRepository(repo)
    row = await hc_repo.get(incident_id)
    assert row is not None
    parsed = row.parse_lines()
    assert len(parsed) == 1
    assert parsed[0].message == "valid"
