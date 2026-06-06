"""Tests for the signature annotations API endpoints (STAGE-004-029).

Covers:
  GET  /api/logs/signatures/{h}/{s}/annotations      (list)
  POST /api/logs/signatures/{h}/{s}/annotations      (create)
  DELETE /api/logs/signatures/{h}/{s}/annotations/{id} (delete)
  Cascade delete when parent signature is deleted

Project test conventions:
- Framework: pytest-asyncio (asyncio_mode=auto)
- CSRF: _csrf() reads homelab_monitor_csrf cookie → X-CSRF-Token header
- Repo seeding: direct INSERT via SqliteRepository
"""

from __future__ import annotations

from typing import cast

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from homelab_monitor.kernel.db.repository import SqliteRepository

# ---------------------------------------------------------------------------
# CSRF helper
# ---------------------------------------------------------------------------


def _csrf(client: AsyncClient) -> dict[str, str]:
    """Return X-CSRF-Token header extracted from the session cookie."""
    csrf: str = client.cookies.get("homelab_monitor_csrf") or ""
    return {"X-CSRF-Token": csrf}


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _insert_sig(
    repo: SqliteRepository,
    *,
    template_hash: str,
    service_key: str,
    template_str: str = "foo <*> bar",
) -> None:
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO log_signatures "
                "  (template_hash, service_key, template_str, label, status, "
                "   first_seen_at, last_seen_at, total_count) "
                "VALUES "
                "  (:h, :s, :tstr, NULL, 'active', 1000, 2000, 5)"
            ),
            {
                "h": template_hash,
                "s": service_key,
                "tstr": template_str,
            },
        )


def _get_app_repo(client: AsyncClient) -> SqliteRepository:
    """Extract the per-test SqliteRepository from the shared app state."""
    app = cast(FastAPI, client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    return cast(SqliteRepository, app.state.repo)  # pyright: ignore[reportAttributeAccessIssue]


# ===========================================================================
# GET /api/logs/signatures/{h}/{s}/annotations — list
# ===========================================================================


async def test_get_annotations_empty_returns_empty_list(
    authenticated_client: AsyncClient,
) -> None:
    """GET .../annotations on a seeded signature returns 200 with empty list."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svc1")

    resp = await authenticated_client.get("/api/logs/signatures/h1/svc1/annotations")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["annotations"] == []


# ===========================================================================
# POST /api/logs/signatures/{h}/{s}/annotations — create
# ===========================================================================


async def test_post_annotation_201_persists_and_author_from_session(
    authenticated_client: AsyncClient,
) -> None:
    """POST with {"note": "..."} returns 201, persists note + author, and is queryable."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svc1")

    resp = await authenticated_client.post(
        "/api/logs/signatures/h1/svc1/annotations",
        json={"note": "hello world"},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 201  # noqa: PLR2004
    body = resp.json()
    assert body["note"] == "hello world"
    assert body["author"] == "testuser"
    assert body["id"] > 0
    assert "T" in body["created_at"]

    # Verify it appears in the list
    list_resp = await authenticated_client.get("/api/logs/signatures/h1/svc1/annotations")
    assert list_resp.status_code == 200  # noqa: PLR2004
    list_body = list_resp.json()
    assert len(list_body["annotations"]) == 1
    assert list_body["annotations"][0]["note"] == "hello world"


async def test_post_annotation_strips_whitespace(
    authenticated_client: AsyncClient,
) -> None:
    """POST with whitespace-padded note is stripped before storage."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svc1")

    resp = await authenticated_client.post(
        "/api/logs/signatures/h1/svc1/annotations",
        json={"note": "  padded  "},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 201  # noqa: PLR2004
    body = resp.json()
    assert body["note"] == "padded"


async def test_post_annotation_404_when_signature_absent(
    authenticated_client: AsyncClient,
) -> None:
    """POST to nonexistent signature returns 404."""
    resp = await authenticated_client.post(
        "/api/logs/signatures/nosuch/svc/annotations",
        json={"note": "hello"},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 404  # noqa: PLR2004
    body = resp.json()
    assert body["error"]["code"] == "not_found"


async def test_post_annotation_422_empty(
    authenticated_client: AsyncClient,
) -> None:
    """POST with empty note returns 422."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svc1")

    resp = await authenticated_client.post(
        "/api/logs/signatures/h1/svc1/annotations",
        json={"note": ""},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 422  # noqa: PLR2004


async def test_post_annotation_422_whitespace_only(
    authenticated_client: AsyncClient,
) -> None:
    """POST with whitespace-only note returns 422."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svc1")

    resp = await authenticated_client.post(
        "/api/logs/signatures/h1/svc1/annotations",
        json={"note": "   "},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 422  # noqa: PLR2004


async def test_post_annotation_422_too_long(
    authenticated_client: AsyncClient,
) -> None:
    """POST with note > 2000 chars returns 422."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svc1")

    resp = await authenticated_client.post(
        "/api/logs/signatures/h1/svc1/annotations",
        json={"note": "x" * 2001},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 422  # noqa: PLR2004


async def test_post_annotation_requires_csrf(
    authenticated_client: AsyncClient,
) -> None:
    """POST without X-CSRF-Token header returns 403."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svc1")

    resp = await authenticated_client.post(
        "/api/logs/signatures/h1/svc1/annotations",
        json={"note": "hello"},
    )
    assert resp.status_code == 403  # noqa: PLR2004


async def test_post_annotation_401_anon(authenticated_client: AsyncClient) -> None:
    """POST without session returns 401."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.post(
            "/api/logs/signatures/h1/svc1/annotations",
            json={"note": "hello"},
        )
    assert resp.status_code == 401  # noqa: PLR2004


# ===========================================================================
# DELETE /api/logs/signatures/{h}/{s}/annotations/{id} — delete
# ===========================================================================


async def test_delete_annotation_204_and_gone(
    authenticated_client: AsyncClient,
) -> None:
    """DELETE .../annotations/{id} returns 204 and removes the annotation."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svc1")

    # Create
    create_resp = await authenticated_client.post(
        "/api/logs/signatures/h1/svc1/annotations",
        json={"note": "to delete"},
        headers=_csrf(authenticated_client),
    )
    assert create_resp.status_code == 201  # noqa: PLR2004
    annotation_id = create_resp.json()["id"]

    # Delete
    del_resp = await authenticated_client.delete(
        f"/api/logs/signatures/h1/svc1/annotations/{annotation_id}",
        headers=_csrf(authenticated_client),
    )
    assert del_resp.status_code == 204  # noqa: PLR2004

    # Verify it's gone
    list_resp = await authenticated_client.get("/api/logs/signatures/h1/svc1/annotations")
    assert list_resp.status_code == 200  # noqa: PLR2004
    assert list_resp.json()["annotations"] == []


async def test_delete_annotation_404_miss(
    authenticated_client: AsyncClient,
) -> None:
    """DELETE .../annotations/9999 returns 404."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svc1")

    resp = await authenticated_client.delete(
        "/api/logs/signatures/h1/svc1/annotations/9999",
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 404  # noqa: PLR2004


async def test_delete_annotation_requires_csrf(
    authenticated_client: AsyncClient,
) -> None:
    """DELETE without X-CSRF-Token returns 403."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svc1")

    # Create first
    create_resp = await authenticated_client.post(
        "/api/logs/signatures/h1/svc1/annotations",
        json={"note": "test"},
        headers=_csrf(authenticated_client),
    )
    annotation_id = create_resp.json()["id"]

    # Delete without CSRF
    del_resp = await authenticated_client.delete(
        f"/api/logs/signatures/h1/svc1/annotations/{annotation_id}"
    )
    assert del_resp.status_code == 403  # noqa: PLR2004


async def test_delete_annotation_401_anon(
    authenticated_client: AsyncClient,
) -> None:
    """DELETE without session returns 401."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.delete("/api/logs/signatures/h1/svc1/annotations/999")
    assert resp.status_code == 401  # noqa: PLR2004


# ===========================================================================
# Cascade delete
# ===========================================================================


async def test_cascade_deletes_annotations(
    authenticated_client: AsyncClient,
) -> None:
    """Deleting the parent signature cascades delete to annotations."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svc1")

    # Create two annotations
    await authenticated_client.post(
        "/api/logs/signatures/h1/svc1/annotations",
        json={"note": "first"},
        headers=_csrf(authenticated_client),
    )
    await authenticated_client.post(
        "/api/logs/signatures/h1/svc1/annotations",
        json={"note": "second"},
        headers=_csrf(authenticated_client),
    )

    # Verify they exist
    list_resp = await authenticated_client.get("/api/logs/signatures/h1/svc1/annotations")
    assert len(list_resp.json()["annotations"]) == 2  # noqa: PLR2004

    # Delete the parent signature via direct SQL
    async with repo.transaction() as conn:
        await conn.execute(
            text("DELETE FROM log_signatures WHERE template_hash = :h AND service_key = :s"),
            {"h": "h1", "s": "svc1"},
        )

    # Verify annotations are gone
    rows = await repo.fetch_all(
        text(
            "SELECT id FROM log_signature_annotations WHERE template_hash = :h AND service_key = :s"
        ),
        {"h": "h1", "s": "svc1"},
    )
    assert rows == []
