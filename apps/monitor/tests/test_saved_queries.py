"""Tests for SavedQueriesRepository and the /api/logs/saved-queries endpoints.

Project test conventions:
- Framework: pytest-asyncio with async fixtures
- DB: tempfile-backed SQLite + alembic_upgrade_head (via `repo` fixture from conftest)
- API: authenticated_client / unauthenticated_client fixtures from conftest
- CSRF: extracted from homelab_monitor_csrf cookie → X-CSRF-Token header
- Assertions: direct field checks, not just structural
"""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.saved_queries_repo import (
    DuplicateNameError,
    SavedQueriesRepository,
    SavedQueryRow,
)

# ---------------------------------------------------------------------------
# CSRF helper (mirrors test_api_crons.py pattern)
# ---------------------------------------------------------------------------


def _csrf(client: AsyncClient) -> dict[str, str]:
    """Return X-CSRF-Token header extracted from the session cookie."""
    csrf: str = client.cookies.get("homelab_monitor_csrf") or ""
    return {"X-CSRF-Token": csrf}


# ---------------------------------------------------------------------------
# Repo: helper
# ---------------------------------------------------------------------------


def _make_repo(repo: SqliteRepository) -> SavedQueriesRepository:
    return SavedQueriesRepository(repo)


async def _create_preset(  # noqa: PLR0913 -- keyword-only fields mirror the row
    repo: SavedQueriesRepository,
    *,
    name: str = "My Query",
    logs_ql: str = "error",
    selected_services: list[dict[str, str]] | None = None,
    since_preset: str = "1h",
    advanced_mode: bool = False,
) -> SavedQueryRow:
    return await repo.create(
        name=name,
        logs_ql=logs_ql,
        selected_services=selected_services or [],
        since_preset=since_preset,
        range_start_iso=None,
        range_end_iso=None,
        advanced_mode=advanced_mode,
    )


async def _create_custom_range(  # noqa: PLR0913 -- keyword-only fields mirror the row
    repo: SavedQueriesRepository,
    *,
    name: str = "Custom Range Query",
    logs_ql: str = "warn",
    selected_services: list[dict[str, str]] | None = None,
    range_start_iso: str = "2026-01-01T00:00:00Z",
    range_end_iso: str = "2026-01-02T00:00:00Z",
    advanced_mode: bool = False,
) -> SavedQueryRow:
    return await repo.create(
        name=name,
        logs_ql=logs_ql,
        selected_services=selected_services or [],
        since_preset=None,
        range_start_iso=range_start_iso,
        range_end_iso=range_end_iso,
        advanced_mode=advanced_mode,
    )


# ===========================================================================
# Repository unit tests
# ===========================================================================


@pytest.mark.asyncio
async def test_create_returns_row_with_correct_fields(repo: SqliteRepository) -> None:
    """create() returns a SavedQueryRow with all the provided values."""
    sq_repo = _make_repo(repo)
    row = await _create_preset(
        sq_repo,
        name="Alpha Query",
        logs_ql="level:error",
        selected_services=[{"service": "nginx", "source_type": "docker"}],
        since_preset="30m",
        advanced_mode=True,
    )

    assert isinstance(row, SavedQueryRow)
    assert row.id > 0
    assert row.name == "Alpha Query"
    assert row.logs_ql == "level:error"
    assert row.selected_services == [{"service": "nginx", "source_type": "docker"}]
    assert row.since_preset == "30m"
    assert row.range_start_iso is None
    assert row.range_end_iso is None
    assert row.advanced_mode is True


@pytest.mark.asyncio
async def test_create_sets_created_at_and_updated_at(repo: SqliteRepository) -> None:
    """create() sets non-empty created_at and updated_at ISO strings."""
    sq_repo = _make_repo(repo)
    row = await _create_preset(sq_repo)

    assert row.created_at != ""
    assert row.updated_at != ""
    # Both should be equal right after creation
    assert row.created_at == row.updated_at


@pytest.mark.asyncio
async def test_create_custom_range_row(repo: SqliteRepository) -> None:
    """create() with custom range stores range fields and null preset."""
    sq_repo = _make_repo(repo)
    row = await _create_custom_range(
        sq_repo,
        range_start_iso="2026-01-01T00:00:00Z",
        range_end_iso="2026-01-02T00:00:00Z",
    )

    assert row.since_preset is None
    assert row.range_start_iso == "2026-01-01T00:00:00Z"
    assert row.range_end_iso == "2026-01-02T00:00:00Z"


@pytest.mark.asyncio
async def test_create_duplicate_name_raises(repo: SqliteRepository) -> None:
    """create() with a duplicate name raises DuplicateNameError."""
    sq_repo = _make_repo(repo)
    await _create_preset(sq_repo, name="Duplicate")
    with pytest.raises(DuplicateNameError) as exc_info:
        await _create_preset(sq_repo, name="Duplicate")
    assert exc_info.value.name == "Duplicate"


@pytest.mark.asyncio
async def test_list_sorted_returns_rows_sorted_by_name(repo: SqliteRepository) -> None:
    """list_sorted() returns rows sorted by name COLLATE NOCASE ascending."""
    sq_repo = _make_repo(repo)
    await _create_preset(sq_repo, name="Zebra Query")
    await _create_preset(sq_repo, name="alpha query")
    await _create_preset(sq_repo, name="MIDDLE Query")

    rows = await sq_repo.list_sorted()
    names = [r.name for r in rows]
    assert names == ["alpha query", "MIDDLE Query", "Zebra Query"]


@pytest.mark.asyncio
async def test_list_sorted_empty(repo: SqliteRepository) -> None:
    """list_sorted() returns an empty list when no rows exist."""
    sq_repo = _make_repo(repo)
    rows = await sq_repo.list_sorted()
    assert rows == []


@pytest.mark.asyncio
async def test_get_returns_row(repo: SqliteRepository) -> None:
    """get(id) returns the matching SavedQueryRow."""
    sq_repo = _make_repo(repo)
    created = await _create_preset(sq_repo, name="GetMe")
    fetched = await sq_repo.get(created.id)

    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.name == "GetMe"


@pytest.mark.asyncio
async def test_get_missing_id_returns_none(repo: SqliteRepository) -> None:
    """get() returns None for a non-existent id."""
    sq_repo = _make_repo(repo)
    result = await sq_repo.get(99999)
    assert result is None


@pytest.mark.asyncio
async def test_rename_changes_name_and_updates_updated_at(repo: SqliteRepository) -> None:
    """rename() changes the name and advances updated_at."""
    sq_repo = _make_repo(repo)
    created = await _create_preset(sq_repo, name="OldName")
    original_updated_at = created.updated_at

    import asyncio  # noqa: PLC0415 -- need short sleep to advance clock

    await asyncio.sleep(0.01)

    renamed = await sq_repo.rename(query_id=created.id, new_name="NewName")
    assert renamed is not None
    assert renamed.name == "NewName"
    assert renamed.id == created.id
    # updated_at must be same or later (SQLite time resolution; at minimum not earlier)
    assert renamed.updated_at >= original_updated_at


@pytest.mark.asyncio
async def test_rename_duplicate_name_raises(repo: SqliteRepository) -> None:
    """rename() to an existing name raises DuplicateNameError."""
    sq_repo = _make_repo(repo)
    await _create_preset(sq_repo, name="Exists")
    created = await _create_preset(sq_repo, name="ToRename")

    with pytest.raises(DuplicateNameError) as exc_info:
        await sq_repo.rename(query_id=created.id, new_name="Exists")
    assert exc_info.value.name == "Exists"


@pytest.mark.asyncio
async def test_rename_missing_id_returns_none(repo: SqliteRepository) -> None:
    """rename() on a non-existent id returns None."""
    sq_repo = _make_repo(repo)
    result = await sq_repo.rename(query_id=99999, new_name="Whatever")
    assert result is None


@pytest.mark.asyncio
async def test_update_overwrites_payload_fields(repo: SqliteRepository) -> None:
    """update() overwrites logs_ql, services, since_preset, and advanced_mode."""
    sq_repo = _make_repo(repo)
    created = await _create_preset(
        sq_repo,
        name="UpdateMe",
        logs_ql="info",
        since_preset="1h",
        advanced_mode=False,
    )

    updated = await sq_repo.update(
        query_id=created.id,
        logs_ql="level:critical",
        selected_services=[{"service": "redis", "source_type": "systemd"}],
        since_preset="6h",
        range_start_iso=None,
        range_end_iso=None,
        advanced_mode=True,
    )

    assert updated is not None
    assert updated.logs_ql == "level:critical"
    assert updated.selected_services == [{"service": "redis", "source_type": "systemd"}]
    assert updated.since_preset == "6h"
    assert updated.range_start_iso is None
    assert updated.range_end_iso is None
    assert updated.advanced_mode is True


@pytest.mark.asyncio
async def test_update_does_not_change_name(repo: SqliteRepository) -> None:
    """update() never changes the name column."""
    sq_repo = _make_repo(repo)
    created = await _create_preset(sq_repo, name="KeepMyName")

    updated = await sq_repo.update(
        query_id=created.id,
        logs_ql="new expr",
        selected_services=[],
        since_preset="24h",
        range_start_iso=None,
        range_end_iso=None,
        advanced_mode=False,
    )

    assert updated is not None
    assert updated.name == "KeepMyName"


@pytest.mark.asyncio
async def test_update_custom_range_row(repo: SqliteRepository) -> None:
    """update() replaces a preset-based row with a custom-range payload."""
    sq_repo = _make_repo(repo)
    created = await _create_preset(sq_repo, since_preset="1h")

    updated = await sq_repo.update(
        query_id=created.id,
        logs_ql="warn",
        selected_services=[],
        since_preset=None,
        range_start_iso="2026-05-01T00:00:00Z",
        range_end_iso="2026-05-02T00:00:00Z",
        advanced_mode=False,
    )

    assert updated is not None
    assert updated.since_preset is None
    assert updated.range_start_iso == "2026-05-01T00:00:00Z"
    assert updated.range_end_iso == "2026-05-02T00:00:00Z"


@pytest.mark.asyncio
async def test_update_missing_id_returns_none(repo: SqliteRepository) -> None:
    """update() on a non-existent id returns None."""
    sq_repo = _make_repo(repo)
    result = await sq_repo.update(
        query_id=99999,
        logs_ql="x",
        selected_services=[],
        since_preset="1h",
        range_start_iso=None,
        range_end_iso=None,
        advanced_mode=False,
    )
    assert result is None


@pytest.mark.asyncio
async def test_delete_removes_row(repo: SqliteRepository) -> None:
    """delete() returns True and the row is gone afterwards."""
    sq_repo = _make_repo(repo)
    created = await _create_preset(sq_repo)

    deleted = await sq_repo.delete(created.id)
    assert deleted is True
    assert await sq_repo.get(created.id) is None


@pytest.mark.asyncio
async def test_delete_missing_id_returns_false(repo: SqliteRepository) -> None:
    """delete() returns False when the id does not exist."""
    sq_repo = _make_repo(repo)
    result = await sq_repo.delete(99999)
    assert result is False


@pytest.mark.asyncio
async def test_selected_services_json_round_trips(repo: SqliteRepository) -> None:
    """selected_services list of {service, source_type} dicts round-trips through JSON."""
    sq_repo = _make_repo(repo)
    services = [
        {"service": "nginx", "source_type": "docker"},
        {"service": "sshd", "source_type": "systemd"},
    ]
    created = await _create_preset(sq_repo, selected_services=services)
    fetched = await sq_repo.get(created.id)

    assert fetched is not None
    assert fetched.selected_services == services


@pytest.mark.asyncio
async def test_selected_services_empty_list_round_trips(repo: SqliteRepository) -> None:
    """An empty selected_services list round-trips correctly."""
    sq_repo = _make_repo(repo)
    created = await _create_preset(sq_repo, selected_services=[])
    fetched = await sq_repo.get(created.id)

    assert fetched is not None
    assert fetched.selected_services == []


# ===========================================================================
# API endpoint tests
# ===========================================================================

_LIST_URL = "/api/logs/saved-queries"


@pytest.mark.asyncio
async def test_list_requires_session(authenticated_client: AsyncClient) -> None:
    """GET /api/logs/saved-queries without session returns 401."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get(_LIST_URL)
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_returns_empty(authenticated_client: AsyncClient) -> None:
    """GET /api/logs/saved-queries returns 200 with empty list initially."""
    resp = await authenticated_client.get(_LIST_URL)
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["saved_queries"] == []


@pytest.mark.asyncio
async def test_list_returns_sorted_queries(authenticated_client: AsyncClient) -> None:
    """GET returns saved_queries sorted by name (case-insensitive)."""
    csrf = _csrf(authenticated_client)
    for name, preset in [("Zebra", "1h"), ("alpha", "6h"), ("MIDDLE", "24h")]:
        resp = await authenticated_client.post(
            _LIST_URL,
            json={
                "name": name,
                "logs_ql": "error",
                "selected_services": [],
                "since_preset": preset,
                "advanced_mode": False,
            },
            headers=csrf,
        )
        assert resp.status_code == 201  # noqa: PLR2004

    resp = await authenticated_client.get(_LIST_URL)
    assert resp.status_code == 200  # noqa: PLR2004
    names = [q["name"] for q in resp.json()["saved_queries"]]
    assert names == ["alpha", "MIDDLE", "Zebra"]


@pytest.mark.asyncio
async def test_create_201_with_preset(authenticated_client: AsyncClient) -> None:
    """POST /api/logs/saved-queries returns 201 and the created row."""
    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.post(
        _LIST_URL,
        json={
            "name": "My Preset Query",
            "logs_ql": "level:error",
            "selected_services": [{"service": "nginx", "source_type": "docker"}],
            "since_preset": "1h",
            "advanced_mode": True,
        },
        headers=csrf,
    )
    assert resp.status_code == 201  # noqa: PLR2004
    body = resp.json()
    assert body["name"] == "My Preset Query"
    assert body["logs_ql"] == "level:error"
    assert body["since_preset"] == "1h"
    assert body["range_start_iso"] is None
    assert body["range_end_iso"] is None
    assert body["advanced_mode"] is True
    assert body["id"] > 0
    assert body["created_at"] != ""
    assert body["updated_at"] != ""
    assert body["selected_services"] == [{"service": "nginx", "source_type": "docker"}]


@pytest.mark.asyncio
async def test_create_201_with_custom_range(authenticated_client: AsyncClient) -> None:
    """POST with custom range (no preset) returns 201."""
    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.post(
        _LIST_URL,
        json={
            "name": "Custom Range Query",
            "logs_ql": "warn",
            "selected_services": [],
            "range_start_iso": "2026-01-01T00:00:00Z",
            "range_end_iso": "2026-01-02T00:00:00Z",
            "advanced_mode": False,
        },
        headers=csrf,
    )
    assert resp.status_code == 201  # noqa: PLR2004
    body = resp.json()
    assert body["since_preset"] is None
    assert body["range_start_iso"] == "2026-01-01T00:00:00Z"
    assert body["range_end_iso"] == "2026-01-02T00:00:00Z"


@pytest.mark.asyncio
async def test_create_duplicate_name_returns_409(authenticated_client: AsyncClient) -> None:
    """POST with duplicate name returns 409."""
    csrf = _csrf(authenticated_client)
    payload: dict[str, object] = {
        "name": "Duplicate Query",
        "logs_ql": "error",
        "selected_services": [],
        "since_preset": "1h",
        "advanced_mode": False,
    }
    resp1 = await authenticated_client.post(_LIST_URL, json=payload, headers=csrf)
    assert resp1.status_code == 201  # noqa: PLR2004

    resp2 = await authenticated_client.post(_LIST_URL, json=payload, headers=csrf)
    assert resp2.status_code == 409  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_requires_session(authenticated_client: AsyncClient) -> None:
    """POST without session cookie returns 401."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.post(
            _LIST_URL,
            json={
                "name": "Anon Query",
                "logs_ql": "error",
                "selected_services": [],
                "since_preset": "1h",
                "advanced_mode": False,
            },
        )
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_422_both_preset_and_range(authenticated_client: AsyncClient) -> None:
    """POST with both since_preset and custom range → 422."""
    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.post(
        _LIST_URL,
        json={
            "name": "Bad Query",
            "logs_ql": "error",
            "selected_services": [],
            "since_preset": "1h",
            "range_start_iso": "2026-01-01T00:00:00Z",
            "range_end_iso": "2026-01-02T00:00:00Z",
            "advanced_mode": False,
        },
        headers=csrf,
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_422_neither_preset_nor_range(authenticated_client: AsyncClient) -> None:
    """POST with neither since_preset nor custom range → 422."""
    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.post(
        _LIST_URL,
        json={
            "name": "Neither Query",
            "logs_ql": "error",
            "selected_services": [],
            "advanced_mode": False,
        },
        headers=csrf,
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_422_partial_range_start_only(authenticated_client: AsyncClient) -> None:
    """POST with range_start_iso but no range_end_iso → 422."""
    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.post(
        _LIST_URL,
        json={
            "name": "Partial Range Query",
            "logs_ql": "error",
            "selected_services": [],
            "range_start_iso": "2026-01-01T00:00:00Z",
            "advanced_mode": False,
        },
        headers=csrf,
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_422_partial_range_end_only(authenticated_client: AsyncClient) -> None:
    """POST with range_end_iso but no range_start_iso → 422."""
    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.post(
        _LIST_URL,
        json={
            "name": "Partial End Only",
            "logs_ql": "error",
            "selected_services": [],
            "range_end_iso": "2026-01-02T00:00:00Z",
            "advanced_mode": False,
        },
        headers=csrf,
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_rename_200(authenticated_client: AsyncClient) -> None:
    """PATCH /api/logs/saved-queries/{id} returns 200 with updated name."""
    csrf = _csrf(authenticated_client)
    create_resp = await authenticated_client.post(
        _LIST_URL,
        json={
            "name": "Before Rename",
            "logs_ql": "error",
            "selected_services": [],
            "since_preset": "1h",
            "advanced_mode": False,
        },
        headers=csrf,
    )
    assert create_resp.status_code == 201  # noqa: PLR2004
    query_id = create_resp.json()["id"]

    rename_resp = await authenticated_client.patch(
        f"{_LIST_URL}/{query_id}",
        json={"name": "After Rename"},
        headers=csrf,
    )
    assert rename_resp.status_code == 200  # noqa: PLR2004
    assert rename_resp.json()["name"] == "After Rename"
    assert rename_resp.json()["id"] == query_id


@pytest.mark.asyncio
async def test_rename_duplicate_returns_409(authenticated_client: AsyncClient) -> None:
    """PATCH rename to existing name returns 409."""
    csrf = _csrf(authenticated_client)
    await authenticated_client.post(
        _LIST_URL,
        json={
            "name": "Existing Name",
            "logs_ql": "error",
            "selected_services": [],
            "since_preset": "1h",
            "advanced_mode": False,
        },
        headers=csrf,
    )
    create_resp = await authenticated_client.post(
        _LIST_URL,
        json={
            "name": "Another Name",
            "logs_ql": "warn",
            "selected_services": [],
            "since_preset": "6h",
            "advanced_mode": False,
        },
        headers=csrf,
    )
    query_id = create_resp.json()["id"]

    resp = await authenticated_client.patch(
        f"{_LIST_URL}/{query_id}",
        json={"name": "Existing Name"},
        headers=csrf,
    )
    assert resp.status_code == 409  # noqa: PLR2004


@pytest.mark.asyncio
async def test_rename_missing_id_returns_404(authenticated_client: AsyncClient) -> None:
    """PATCH rename of non-existent id returns 404."""
    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.patch(
        f"{_LIST_URL}/99999",
        json={"name": "Anything"},
        headers=csrf,
    )
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_update_200_and_ignores_body_name(authenticated_client: AsyncClient) -> None:
    """PUT update returns 200 and body.name is ignored (name unchanged)."""
    csrf = _csrf(authenticated_client)
    create_resp = await authenticated_client.post(
        _LIST_URL,
        json={
            "name": "Stable Name",
            "logs_ql": "info",
            "selected_services": [],
            "since_preset": "1h",
            "advanced_mode": False,
        },
        headers=csrf,
    )
    assert create_resp.status_code == 201  # noqa: PLR2004
    query_id = create_resp.json()["id"]

    # Send a different name in the body — it should be ignored
    update_resp = await authenticated_client.put(
        f"{_LIST_URL}/{query_id}",
        json={
            "name": "SHOULD BE IGNORED",
            "logs_ql": "level:critical",
            "selected_services": [{"service": "redis", "source_type": "systemd"}],
            "since_preset": "24h",
            "advanced_mode": True,
        },
        headers=csrf,
    )
    assert update_resp.status_code == 200  # noqa: PLR2004
    body = update_resp.json()
    assert body["name"] == "Stable Name"  # name unchanged
    assert body["logs_ql"] == "level:critical"
    assert body["since_preset"] == "24h"
    assert body["advanced_mode"] is True
    assert body["selected_services"] == [{"service": "redis", "source_type": "systemd"}]


@pytest.mark.asyncio
async def test_update_missing_id_returns_404(authenticated_client: AsyncClient) -> None:
    """PUT update of non-existent id returns 404."""
    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.put(
        f"{_LIST_URL}/99999",
        json={
            "name": "X",
            "logs_ql": "error",
            "selected_services": [],
            "since_preset": "1h",
            "advanced_mode": False,
        },
        headers=csrf,
    )
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_update_422_both_preset_and_range(authenticated_client: AsyncClient) -> None:
    """PUT update with both since_preset and custom range → 422."""
    csrf = _csrf(authenticated_client)
    create_resp = await authenticated_client.post(
        _LIST_URL,
        json={
            "name": "Valid Query",
            "logs_ql": "error",
            "selected_services": [],
            "since_preset": "1h",
            "advanced_mode": False,
        },
        headers=csrf,
    )
    query_id = create_resp.json()["id"]

    resp = await authenticated_client.put(
        f"{_LIST_URL}/{query_id}",
        json={
            "name": "Valid Query",
            "logs_ql": "error",
            "selected_services": [],
            "since_preset": "1h",
            "range_start_iso": "2026-01-01T00:00:00Z",
            "range_end_iso": "2026-01-02T00:00:00Z",
            "advanced_mode": False,
        },
        headers=csrf,
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_update_422_partial_range(authenticated_client: AsyncClient) -> None:
    """PUT update with partial custom range (start only) → 422."""
    csrf = _csrf(authenticated_client)
    create_resp = await authenticated_client.post(
        _LIST_URL,
        json={
            "name": "Valid Query 2",
            "logs_ql": "error",
            "selected_services": [],
            "since_preset": "1h",
            "advanced_mode": False,
        },
        headers=csrf,
    )
    query_id = create_resp.json()["id"]

    resp = await authenticated_client.put(
        f"{_LIST_URL}/{query_id}",
        json={
            "name": "Valid Query 2",
            "logs_ql": "error",
            "selected_services": [],
            "range_start_iso": "2026-01-01T00:00:00Z",
            "advanced_mode": False,
        },
        headers=csrf,
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_delete_204(authenticated_client: AsyncClient) -> None:
    """DELETE /api/logs/saved-queries/{id} returns 204."""
    csrf = _csrf(authenticated_client)
    create_resp = await authenticated_client.post(
        _LIST_URL,
        json={
            "name": "To Delete",
            "logs_ql": "error",
            "selected_services": [],
            "since_preset": "1h",
            "advanced_mode": False,
        },
        headers=csrf,
    )
    query_id = create_resp.json()["id"]

    delete_resp = await authenticated_client.delete(f"{_LIST_URL}/{query_id}", headers=csrf)
    assert delete_resp.status_code == 204  # noqa: PLR2004

    # Row is gone — GET list should be empty
    list_resp = await authenticated_client.get(_LIST_URL)
    assert list_resp.json()["saved_queries"] == []


@pytest.mark.asyncio
async def test_delete_missing_id_returns_404(authenticated_client: AsyncClient) -> None:
    """DELETE of non-existent id returns 404."""
    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.delete(f"{_LIST_URL}/99999", headers=csrf)
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_delete_requires_session(authenticated_client: AsyncClient) -> None:
    """DELETE without session returns 401."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.delete(f"{_LIST_URL}/1")
    assert resp.status_code == 401  # noqa: PLR2004
