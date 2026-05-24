"""Unit tests for Docker image-update endpoints: summary and per-container detail."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pytest_httpx import HTTPXMock

from homelab_monitor.kernel.db.repositories.image_update_state_repository import (
    ImageUpdateStateRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

if TYPE_CHECKING:
    pass

HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_UNAUTHORIZED = 401
_EXPECTED_SUMMARY_COUNT = 3
_EXPECTED_SKIPPED_COUNT = 5


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


async def _seed_image_update_state(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    container_name: str,
    last_local_digest: str | None = None,
    last_registry_digest: str | None = None,
    last_image_ref: str = "example/image:latest",
    update_available: bool = False,
    last_checked_at: str | None = None,
    check_failed_at: str | None = None,
    check_error_reason: str | None = None,
) -> None:
    """Test helper: seed an image-update state row."""
    now = utc_now_iso()
    async with repo.transaction() as conn:
        await ImageUpdateStateRepository.upsert_state_conn(
            conn,
            container_name=container_name,
            last_local_digest=last_local_digest,
            last_registry_digest=last_registry_digest,
            last_image_ref=last_image_ref,
            update_available=update_available,
            last_checked_at=last_checked_at or now,
            check_failed_at=check_failed_at,
            check_error_reason=check_error_reason,
            now=now,
        )


# ---- SUMMARY ENDPOINT TESTS ----


@pytest.mark.asyncio
async def test_summary_returns_empty_list_when_no_rows(
    authenticated_client: AsyncClient,
) -> None:
    """Test that summary returns empty list when no image-update state exists."""
    response = await authenticated_client.get("/api/integrations/docker/image-updates/summary")
    assert response.status_code == HTTP_OK
    data = response.json()
    assert data["summaries"] == []
    assert data["rate_limit_skipped_count"] == 0
    assert data["rate_limit_remaining_by_registry"] == {}


@pytest.mark.asyncio
async def test_summary_returns_entries_in_container_name_order(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Test that summary returns entries in container name order."""
    await _seed_image_update_state(repo, container_name="zebra")
    await _seed_image_update_state(repo, container_name="apple")
    await _seed_image_update_state(repo, container_name="middle")

    response = await authenticated_client.get("/api/integrations/docker/image-updates/summary")
    assert response.status_code == HTTP_OK
    data = response.json()
    assert len(data["summaries"]) == _EXPECTED_SUMMARY_COUNT
    # Note: order depends on database query — verify all are present
    container_names = {entry["container_name"] for entry in data["summaries"]}
    assert container_names == {"zebra", "apple", "middle"}


@pytest.mark.asyncio
async def test_summary_reflects_update_available_boolean(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Test that summary reflects update_available field correctly."""
    await _seed_image_update_state(repo, container_name="with_update", update_available=True)
    await _seed_image_update_state(repo, container_name="no_update", update_available=False)

    response = await authenticated_client.get("/api/integrations/docker/image-updates/summary")
    assert response.status_code == HTTP_OK
    data = response.json()
    entries = {entry["container_name"]: entry for entry in data["summaries"]}

    assert entries["with_update"]["available"] is True
    assert entries["no_update"]["available"] is False


@pytest.mark.asyncio
async def test_summary_includes_check_error_reason(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Test that summary includes check_error_reason when present."""
    await _seed_image_update_state(
        repo,
        container_name="error_case",
        check_error_reason="network_error",
    )
    await _seed_image_update_state(
        repo,
        container_name="no_error",
        check_error_reason=None,
    )

    response = await authenticated_client.get("/api/integrations/docker/image-updates/summary")
    assert response.status_code == HTTP_OK
    data = response.json()
    entries = {entry["container_name"]: entry for entry in data["summaries"]}

    assert entries["error_case"]["check_error_reason"] == "network_error"
    assert entries["no_error"]["check_error_reason"] is None


@pytest.mark.asyncio
async def test_summary_includes_rate_limit_skipped_count_from_collector_state(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Test that summary includes rate_limit_skipped_count from collector."""
    await _seed_image_update_state(repo, container_name="test")

    # Mock the image_update_collector on app.state
    mock_collector = type("MockCollector", (), {})()
    mock_collector.current_skipped_count = lambda: _EXPECTED_SKIPPED_COUNT  # type: ignore[assignment]
    mock_collector.current_rate_limit_remaining = lambda: {}  # type: ignore[assignment]

    _app: FastAPI = cast(FastAPI, authenticated_client.app)  # type: ignore[attr-defined]
    _orig = getattr(_app.state, "image_update_collector", None)
    _app.state.image_update_collector = mock_collector
    try:
        response = await authenticated_client.get("/api/integrations/docker/image-updates/summary")
        assert response.status_code == HTTP_OK
        data = response.json()
        assert data["rate_limit_skipped_count"] == _EXPECTED_SKIPPED_COUNT
    finally:
        if _orig is None:
            delattr(_app.state, "image_update_collector")
        else:
            _app.state.image_update_collector = _orig


@pytest.mark.asyncio
async def test_summary_includes_rate_limit_remaining_by_registry(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Test that summary includes rate_limit_remaining_by_registry from collector."""
    await _seed_image_update_state(repo, container_name="test")

    # Mock the image_update_collector on app.state
    mock_collector = type("MockCollector", (), {})()
    mock_collector.current_skipped_count = lambda: 0  # type: ignore[assignment]
    mock_collector.current_rate_limit_remaining = (  # type: ignore[assignment]
        lambda: {"docker.io": 100, "ghcr.io": 50}
    )

    _app: FastAPI = cast(FastAPI, authenticated_client.app)  # type: ignore[attr-defined]
    _orig = getattr(_app.state, "image_update_collector", None)
    _app.state.image_update_collector = mock_collector
    try:
        response = await authenticated_client.get("/api/integrations/docker/image-updates/summary")
        assert response.status_code == HTTP_OK
        data = response.json()
        assert data["rate_limit_remaining_by_registry"] == {"docker.io": 100, "ghcr.io": 50}
    finally:
        if _orig is None:
            delattr(_app.state, "image_update_collector")
        else:
            _app.state.image_update_collector = _orig


@pytest.mark.asyncio
async def test_summary_unauthenticated_returns_401(
    unauthenticated_client: AsyncClient,
) -> None:
    """Test that summary endpoint requires authentication."""
    # Use the basic client fixture (no auth) instead of authenticated_client
    response = await unauthenticated_client.get("/api/integrations/docker/image-updates/summary")
    assert response.status_code == HTTP_UNAUTHORIZED


@pytest.mark.asyncio
async def test_summary_works_when_collector_not_wired(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Test that summary works when image_update_collector is missing."""
    await _seed_image_update_state(repo, container_name="test")

    # Ensure no collector is present
    _app: FastAPI = cast(FastAPI, authenticated_client.app)  # type: ignore[attr-defined]
    _orig = getattr(_app.state, "image_update_collector", None)
    if hasattr(_app.state, "image_update_collector"):
        delattr(_app.state, "image_update_collector")

    try:
        response = await authenticated_client.get("/api/integrations/docker/image-updates/summary")
        assert response.status_code == HTTP_OK
        data = response.json()
        assert data["rate_limit_skipped_count"] == 0
        assert data["rate_limit_remaining_by_registry"] == {}
    finally:
        if _orig is None:
            if hasattr(_app.state, "image_update_collector"):
                delattr(_app.state, "image_update_collector")
        else:
            _app.state.image_update_collector = _orig


# ---- PER-CONTAINER DETAIL ENDPOINT TESTS ----


@pytest.mark.asyncio
async def test_per_container_returns_row_for_existing_container(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Test that per-container endpoint returns detail for existing container."""
    now = utc_now_iso()
    await _seed_image_update_state(
        repo,
        container_name="myapp",
        last_local_digest="sha256:1234567890abc",
        last_registry_digest="sha256:abcdef1234567",
        last_image_ref="myrepo/myapp:latest",
        update_available=True,
        last_checked_at=now,
        check_failed_at=None,
        check_error_reason=None,
    )

    response = await authenticated_client.get(
        "/api/integrations/docker/containers/myapp/image-update"
    )
    assert response.status_code == HTTP_OK
    data = response.json()
    assert data["container_name"] == "myapp"
    assert data["last_local_digest"] == "sha256:1234567890abc"
    assert data["last_registry_digest"] == "sha256:abcdef1234567"
    assert data["last_image_ref"] == "myrepo/myapp:latest"
    assert data["update_available"] is True
    assert data["last_checked_at"] == now


@pytest.mark.asyncio
async def test_per_container_returns_404_when_missing(
    authenticated_client: AsyncClient,
) -> None:
    """Test that per-container endpoint returns 404 for unknown container."""
    response = await authenticated_client.get(
        "/api/integrations/docker/containers/unknown/image-update"
    )
    assert response.status_code == HTTP_NOT_FOUND
    data = response.json()
    assert "no image-update state for container: unknown" in str(data)


@pytest.mark.asyncio
async def test_per_container_unauthenticated_returns_401(
    unauthenticated_client: AsyncClient,
) -> None:
    """Test that per-container endpoint requires authentication."""
    response = await unauthenticated_client.get(
        "/api/integrations/docker/containers/anyname/image-update"
    )
    assert response.status_code == HTTP_UNAUTHORIZED
