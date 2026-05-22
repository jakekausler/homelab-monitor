"""Unit tests for GET /api/integrations/docker/suggestions endpoint."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from httpx import AsyncClient
from pytest_httpx import HTTPXMock
from sqlalchemy import text

from homelab_monitor.kernel.db.repositories.suggestions_repository import (
    SuggestionsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

if TYPE_CHECKING:
    pass

HTTP_OK = 200
HTTP_CREATED = 201
HTTP_BAD_REQUEST = 400
HTTP_UNPROCESSABLE_ENTITY = 422
HTTP_UNAUTHORIZED = 401


@pytest.fixture(autouse=True)
def _mock_vm_lifespan_tick(httpx_mock: HTTPXMock) -> None:  # pyright: ignore[reportUnusedFunction]
    """Mock VictoriaMetrics calls from lifespan startup to prevent contamination."""
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://victoriametrics:8428/.*"),
        json={"data": {"resultType": "vector", "result": []}},
        is_optional=True,
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://localhost/events.*"),
        content=b"",
        is_optional=True,
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://localhost/containers/json.*"),
        json=[],
        is_optional=True,
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://victorialogs:9428/.*"),
        json={},
        is_optional=True,
        is_reusable=True,
    )


async def _seed_suggestion(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    container_id: str,
    container_name: str,
    image_ref: str,
    kind: str = "docker_container_discovered",
    detection_reason: str = "no_homelab_monitor_label",
    state: str = "pending",
    labels: dict[str, str] | None = None,
    compose_project: str | None = None,
    compose_service: str | None = None,
    compose_file_path: str | None = None,
) -> str:
    """Test helper: seed a suggestion via the production upsert path."""
    if labels is None:
        labels = {}
    now = utc_now_iso()

    async with repo.transaction() as conn:
        suggestion_id = await SuggestionsRepository.insert_or_update_docker_suggestion_conn(
            conn,
            kind=kind,
            deduplication_key=container_id,
            container_id=container_id,
            container_name=container_name,
            image_ref=image_ref,
            labels=labels,
            compose_project=compose_project,
            compose_service=compose_service,
            compose_file_path=compose_file_path,
            detection_reason=detection_reason,
            now=now,
        )
        # If state is not 'pending', transition it
        if state != "pending":
            await conn.execute(
                text("UPDATE suggestions SET state = :state WHERE id = :id"),
                {"state": state, "id": suggestion_id},
            )
    return suggestion_id


@pytest.mark.asyncio
async def test_lists_pending_suggestions(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that GET returns pending suggestions by default."""
    # Seed 3 pending suggestions
    await _seed_suggestion(
        repo,
        container_id="cid001",
        container_name="web",
        image_ref="nginx:1.25",
    )
    await _seed_suggestion(
        repo,
        container_id="cid002",
        container_name="db",
        image_ref="postgres:15",
    )
    await _seed_suggestion(
        repo,
        container_id="cid003",
        container_name="redis",
        image_ref="redis:7",
    )

    response = await authenticated_client.get("/api/integrations/docker/suggestions")

    assert response.status_code == HTTP_OK
    data = response.json()
    assert "suggestions" in data
    assert len(data["suggestions"]) == 3  # noqa: PLR2004
    # Default status is pending
    for sugg in data["suggestions"]:
        assert sugg["state"] == "pending"


@pytest.mark.asyncio
async def test_filter_by_status_container_gone(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test filtering by status=container_gone."""
    # Seed one pending, one container_gone
    await _seed_suggestion(
        repo,
        container_id="cid_pending",
        container_name="pending",
        image_ref="ubuntu:22.04",
        state="pending",
    )
    await _seed_suggestion(
        repo,
        container_id="cid_gone",
        container_name="gone",
        image_ref="ubuntu:22.04",
        state="container_gone",
    )

    response = await authenticated_client.get(
        "/api/integrations/docker/suggestions?status=container_gone"
    )

    assert response.status_code == HTTP_OK
    data = response.json()
    assert len(data["suggestions"]) == 1
    assert data["suggestions"][0]["state"] == "container_gone"
    assert data["suggestions"][0]["container_name"] == "gone"


@pytest.mark.asyncio
async def test_filter_by_status_all_returns_everything(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test filtering by status=all returns all states."""
    # Seed one of each state
    await _seed_suggestion(
        repo,
        container_id="cid_pending",
        container_name="pending",
        image_ref="ubuntu:22.04",
        state="pending",
    )
    await _seed_suggestion(
        repo,
        container_id="cid_ignored",
        container_name="ignored",
        image_ref="ubuntu:22.04",
        state="ignored",
    )
    await _seed_suggestion(
        repo,
        container_id="cid_gone",
        container_name="gone",
        image_ref="ubuntu:22.04",
        state="container_gone",
    )

    response = await authenticated_client.get("/api/integrations/docker/suggestions?status=all")

    assert response.status_code == HTTP_OK
    data = response.json()
    assert len(data["suggestions"]) == 3  # noqa: PLR2004
    states = {sugg["state"] for sugg in data["suggestions"]}
    assert states == {"pending", "ignored", "container_gone"}


@pytest.mark.asyncio
async def test_response_includes_detection_reason_and_compose_metadata(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that detection_reason and compose fields are present."""
    await _seed_suggestion(
        repo,
        container_id="app_cid",
        container_name="app_container",
        image_ref="myapp:1.0",
        detection_reason="disabled_profile",
        compose_project="myapp",
        compose_service="web",
    )

    response = await authenticated_client.get("/api/integrations/docker/suggestions")

    assert response.status_code == HTTP_OK
    data = response.json()
    assert len(data["suggestions"]) == 1
    sugg = data["suggestions"][0]
    assert sugg["detection_reason"] == "disabled_profile"
    assert sugg["compose_project"] == "myapp"
    assert sugg["compose_service"] == "web"


@pytest.mark.asyncio
async def test_cursor_pagination_first_page(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test pagination: first page returns 50 rows + non-null cursor."""
    # Seed 75 rows
    for i in range(75):
        await _seed_suggestion(
            repo,
            container_id=f"cid_{i:03d}",
            container_name=f"container_{i}",
            image_ref=f"image:{i}",
        )

    response = await authenticated_client.get("/api/integrations/docker/suggestions?limit=50")

    assert response.status_code == HTTP_OK
    data = response.json()
    assert len(data["suggestions"]) == 50  # noqa: PLR2004
    assert data["next_cursor"] is not None


@pytest.mark.asyncio
async def test_cursor_pagination_subsequent_page(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test pagination: using cursor from first page returns different rows."""
    # Seed 75 rows
    for i in range(75):
        await _seed_suggestion(
            repo,
            container_id=f"cid_{i:03d}",
            container_name=f"container_{i}",
            image_ref=f"image:{i}",
        )

    # Get first page
    response1 = await authenticated_client.get("/api/integrations/docker/suggestions?limit=50")
    assert response1.status_code == HTTP_OK
    data1 = response1.json()
    first_page_ids = {sugg["id"] for sugg in data1["suggestions"]}
    cursor = data1["next_cursor"]
    assert cursor is not None

    # Get second page using cursor
    response2 = await authenticated_client.get(
        f"/api/integrations/docker/suggestions?limit=50&cursor={cursor}"
    )
    assert response2.status_code == HTTP_OK
    data2 = response2.json()
    second_page_ids = {sugg["id"] for sugg in data2["suggestions"]}

    # Pages should have different rows
    assert first_page_ids.isdisjoint(second_page_ids)
    assert len(data2["suggestions"]) == 25  # noqa: PLR2004


@pytest.mark.asyncio
async def test_cursor_pagination_last_page_null_cursor(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test pagination: last page returns next_cursor=null."""
    # Seed 75 rows
    for i in range(75):
        await _seed_suggestion(
            repo,
            container_id=f"cid_{i:03d}",
            container_name=f"container_{i}",
            image_ref=f"image:{i}",
        )

    # Get all pages until we see null cursor
    cursor: str | None = None
    page_count = 0
    while True:
        params = "?limit=50"
        if cursor:
            params += f"&cursor={cursor}"
        response = await authenticated_client.get(f"/api/integrations/docker/suggestions{params}")
        assert response.status_code == HTTP_OK
        data = response.json()
        page_count += 1
        cursor = data["next_cursor"]
        if cursor is None:
            break

    # Should reach end with 2 pages (50 + 25)
    assert page_count == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_invalid_status_returns_422(
    authenticated_client: AsyncClient,
) -> None:
    """Test that ?status=bogus returns 422 (FastAPI Literal validation)."""
    response = await authenticated_client.get("/api/integrations/docker/suggestions?status=bogus")

    # FastAPI's Literal validation returns 422 for invalid values
    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY


@pytest.mark.asyncio
async def test_invalid_cursor_returns_400(
    authenticated_client: AsyncClient,
) -> None:
    """Test that invalid cursor format returns 400."""
    response = await authenticated_client.get("/api/integrations/docker/suggestions?cursor=no-pipe")

    assert response.status_code == HTTP_BAD_REQUEST


@pytest.mark.asyncio
async def test_requires_authentication(
    unauthenticated_client: AsyncClient,
) -> None:
    """Test that 401 is returned without a session token."""
    response = await unauthenticated_client.get("/api/integrations/docker/suggestions")
    assert response.status_code == HTTP_UNAUTHORIZED


@pytest.mark.asyncio
async def test_limit_validated_between_1_and_200(
    authenticated_client: AsyncClient,
) -> None:
    """Test that limit is validated: ?limit=0 or >200 returns 422."""
    # Test limit=0
    response0 = await authenticated_client.get("/api/integrations/docker/suggestions?limit=0")
    assert response0.status_code == HTTP_UNPROCESSABLE_ENTITY

    # Test limit=201
    response201 = await authenticated_client.get("/api/integrations/docker/suggestions?limit=201")
    assert response201.status_code == HTTP_UNPROCESSABLE_ENTITY


@pytest.mark.asyncio
async def test_kind_label_collision_appears(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that docker_label_collision kind is preserved in response."""
    # Seed one discovered + one collision
    await _seed_suggestion(
        repo,
        container_id="cid_discovered",
        container_name="discovered",
        image_ref="ubuntu:22.04",
        kind="docker_container_discovered",
    )
    await _seed_suggestion(
        repo,
        container_id="cid_collision",
        container_name="collision",
        image_ref="ubuntu:22.04",
        kind="docker_label_collision",
    )

    response = await authenticated_client.get("/api/integrations/docker/suggestions")

    assert response.status_code == HTTP_OK
    data = response.json()
    assert len(data["suggestions"]) == 2  # noqa: PLR2004

    kinds = {sugg["kind"] for sugg in data["suggestions"]}
    assert "docker_container_discovered" in kinds
    assert "docker_label_collision" in kinds


@pytest.mark.asyncio
async def test_list_suggestions_invalid_status_handler_direct(
    repo: SqliteRepository,
) -> None:
    """Directly invoke the list_suggestions handler with a status not in ALLOWED_STATES
    and not 'all', to cover the defensive HTTPException branch (line 148 in docker.py).

    FastAPI's Literal validation prevents this value from reaching the handler via HTTP,
    so we call the function directly.
    """
    from unittest.mock import MagicMock  # noqa: PLC0415

    from fastapi import HTTPException  # noqa: PLC0415

    from homelab_monitor.kernel.api.routers.docker import list_suggestions  # noqa: PLC0415
    from homelab_monitor.kernel.db.repositories.suggestions_repository import (  # noqa: PLC0415
        SuggestionsRepository,
    )

    sugg_repo = SuggestionsRepository(repo)
    mock_user = MagicMock()

    with pytest.raises(HTTPException) as exc_info:
        await list_suggestions(
            _user=mock_user,  # pyright: ignore[reportArgumentType]
            suggestions_repo=sugg_repo,
            status_filter="unknown_value",  # type: ignore[arg-type]  # bypasses Literal
            cursor=None,
            limit=50,
        )

    assert exc_info.value.status_code == 400  # noqa: PLR2004


@pytest.mark.asyncio
async def test_response_includes_compose_file_path(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that compose_file_path is present in API response when seeded."""
    await _seed_suggestion(
        repo,
        container_id="cid_compose",
        container_name="compose_container",
        image_ref="myapp:1.0",
        compose_project="myapp",
        compose_service="web",
        compose_file_path="/storage/docker/compose/docker-compose.yml",
    )

    response = await authenticated_client.get("/api/integrations/docker/suggestions")

    assert response.status_code == HTTP_OK
    data = response.json()
    assert len(data["suggestions"]) == 1
    sugg = data["suggestions"][0]
    assert sugg["compose_file_path"] == "/storage/docker/compose/docker-compose.yml"


@pytest.mark.asyncio
async def test_compose_file_path_null_when_absent(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that compose_file_path is null when not seeded."""
    await _seed_suggestion(
        repo,
        container_id="cid_no_path",
        container_name="no_path_container",
        image_ref="myapp:1.0",
    )

    response = await authenticated_client.get("/api/integrations/docker/suggestions")

    assert response.status_code == HTTP_OK
    data = response.json()
    assert len(data["suggestions"]) == 1
    sugg = data["suggestions"][0]
    assert sugg["compose_file_path"] is None
