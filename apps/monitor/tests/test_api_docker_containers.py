"""Unit tests for GET /api/integrations/docker/containers endpoint."""

from __future__ import annotations

import re

import pytest
from httpx import AsyncClient
from pytest_httpx import HTTPXMock
from sqlalchemy import text

from homelab_monitor.kernel.db.repositories.targets_repository import TargetsRepository
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
RESTART_COUNT_2 = 2
CPU_PCT_NGINX = 15.5
MEM_MIB_256 = 256.0
CPU_PCT_SINGLE = 45.2
MEM_MIB_512 = 512.5


@pytest.fixture(autouse=True)
def _suppress_docker_socket_calls(httpx_mock: HTTPXMock) -> None:  # pyright: ignore[reportUnusedFunction]
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


async def _seed_docker_container(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    target_id: str,
    name: str,
    status: str = "running",
    image: str | None = "ubuntu:22.04",
    restart_count: int = 0,
    exit_code: int = 0,
    healthcheck: str | None = None,
    network_mode: str = "bridge",
    labels: dict[str, str] | None = None,
    cpu_pct: float | None = None,
    mem_mib: float | None = None,
    compose_project: str | None = None,
    compose_service: str | None = None,
    compose_file_path: str | None = None,
) -> str:
    """Test helper: seed a docker container row using the production upsert path.

    Returns the resolved targets.id UUID.
    """
    if labels is None:
        labels = {}
    now = utc_now_iso()

    async with repo.transaction() as conn:
        return await TargetsRepository.upsert_docker_container_conn(
            conn,
            container_id=target_id,
            logical_key_kind="name",
            logical_key=name,
            name=name,
            status=status,
            image=image or "",
            restart_count=restart_count,
            exit_code=exit_code,
            healthcheck=healthcheck,
            network_mode=network_mode,
            labels=labels,
            now=now,
            cpu_pct=cpu_pct,
            mem_mib=mem_mib,
            compose_project=compose_project,
            compose_service=compose_service,
            compose_file_path=compose_file_path,
        )


@pytest.mark.asyncio
async def test_list_containers_returns_container_rows_with_all_fields(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that the endpoint returns ContainerRow shape with all fields."""
    # Seed a container with all fields populated
    resolved_id = await _seed_docker_container(
        repo,
        target_id="abc123def456",
        name="web-server",
        status="running",
        image="nginx:1.25",
        restart_count=RESTART_COUNT_2,
        exit_code=0,
        healthcheck="healthy",
        network_mode="host",
        labels={"app": "web", "env": "prod"},
        cpu_pct=15.5,
        mem_mib=256.0,
    )

    # Call the endpoint
    response = await authenticated_client.get("/api/integrations/docker/containers")

    # Verify 200 and response shape
    assert response.status_code == HTTP_OK
    data = response.json()
    assert "containers" in data
    assert len(data["containers"]) == 1

    # Verify all fields in the row
    row = data["containers"][0]
    assert row["id"] == resolved_id
    assert row["name"] == "web-server"
    assert row["image"] == "nginx:1.25"
    assert row["status"] == "running"
    assert row["restart_count"] == RESTART_COUNT_2
    assert row["exit_code"] == 0
    assert row["healthcheck"] == "healthy"
    assert row["network_mode"] == "host"
    assert row["cpu_pct"] == CPU_PCT_NGINX
    assert row["mem_mib"] == MEM_MIB_256
    assert row["labels"] == {"app": "web", "env": "prod"}
    # STAGE-003-005 Q2 fields
    assert row["compose_project"] is None
    assert row["compose_service"] is None
    assert row["compose_file_path"] is None
    # STAGE-003-005 Q1 field
    assert row["restart_count_24h"] is None


@pytest.mark.asyncio
async def test_list_containers_requires_session_auth(
    unauthenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that 401 is returned without a session token."""
    # unauthenticated_client has no session cookie
    response = await unauthenticated_client.get("/api/integrations/docker/containers")
    assert response.status_code == HTTP_UNAUTHORIZED


@pytest.mark.asyncio
async def test_list_containers_returns_empty_array_when_no_containers(
    authenticated_client: AsyncClient,
) -> None:
    """Test that an empty list is returned when no containers exist."""
    response = await authenticated_client.get("/api/integrations/docker/containers")
    assert response.status_code == HTTP_OK
    data = response.json()
    assert data == {"containers": []}


@pytest.mark.asyncio
async def test_list_containers_includes_cadvisor_fields_when_cached(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that CPU and memory metrics are returned when cached."""
    # Seed a container WITH cached metrics
    await _seed_docker_container(
        repo,
        target_id="xyz789",
        name="app-container",
        cpu_pct=CPU_PCT_SINGLE,
        mem_mib=MEM_MIB_512,
    )

    response = await authenticated_client.get("/api/integrations/docker/containers")
    assert response.status_code == HTTP_OK
    data = response.json()
    assert len(data["containers"]) == 1

    row = data["containers"][0]
    assert row["cpu_pct"] == CPU_PCT_SINGLE
    assert row["mem_mib"] == MEM_MIB_512


@pytest.mark.asyncio
async def test_list_containers_omits_cadvisor_fields_when_not_cached(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that CPU and memory metrics are None when not cached."""
    # Seed a container WITHOUT cached metrics
    await _seed_docker_container(
        repo,
        target_id="missing-metrics",
        name="orphan-container",
        cpu_pct=None,
        mem_mib=None,
    )

    response = await authenticated_client.get("/api/integrations/docker/containers")
    assert response.status_code == HTTP_OK
    data = response.json()
    assert len(data["containers"]) == 1

    row = data["containers"][0]
    assert row["cpu_pct"] is None
    assert row["mem_mib"] is None


@pytest.mark.asyncio
async def test_list_containers_returns_compose_fields(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that compose fields are returned when populated (Q2)."""
    # Seed a compose-managed container
    await _seed_docker_container(
        repo,
        target_id="compose-container-1",
        name="karma",
        compose_project="homelab-monitor",
        compose_service="karma",
        compose_file_path="/storage/programs/homelab-monitor/deploy/compose/docker-compose.yml",
    )

    response = await authenticated_client.get("/api/integrations/docker/containers")
    assert response.status_code == HTTP_OK
    data = response.json()
    assert len(data["containers"]) == 1

    row = data["containers"][0]
    assert row["compose_project"] == "homelab-monitor"
    assert row["compose_service"] == "karma"
    assert (
        row["compose_file_path"]
        == "/storage/programs/homelab-monitor/deploy/compose/docker-compose.yml"
    )


@pytest.mark.asyncio
async def test_list_containers_restart_count_24h_from_cache(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that restart_count_24h is returned from cache (Q1)."""
    # Seed a container
    resolved_id = await _seed_docker_container(
        repo,
        target_id="restart-test-container",
        name="test-app",
    )

    # Manually update the restart_count_24h_cached column
    async with repo.transaction() as conn:
        await conn.execute(
            text("UPDATE targets_docker SET restart_count_24h_cached = 3 WHERE target_id = :tid"),
            {"tid": resolved_id},
        )

    response = await authenticated_client.get("/api/integrations/docker/containers")
    assert response.status_code == HTTP_OK
    data = response.json()
    assert len(data["containers"]) == 1

    row = data["containers"][0]
    assert row["restart_count_24h"] == 3  # noqa: PLR2004
