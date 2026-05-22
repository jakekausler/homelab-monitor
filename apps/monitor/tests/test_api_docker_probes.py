"""Unit tests for Docker probe endpoints: list, enable, disable."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from httpx import AsyncClient
from pytest_httpx import HTTPXMock
from sqlalchemy import text

from homelab_monitor.kernel.db.repositories.probe_targets_repository import (
    ProbeTargetsRepository,
)
from homelab_monitor.kernel.db.repositories.targets_repository import TargetsRepository
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

if TYPE_CHECKING:
    pass

HTTP_OK = 200
HTTP_NOT_FOUND = 404
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


async def _seed_container(
    repo: SqliteRepository,
    *,
    name: str,
) -> str:
    """Test helper: seed a container into targets table."""
    now = utc_now_iso()
    container_id = f"cid_{name}"
    async with repo.transaction() as conn:
        target_id = await TargetsRepository.upsert_docker_container_conn(
            conn,
            container_id=container_id,
            name=name,
            status="running",
            image=f"image_{name}:latest",
            restart_count=0,
            exit_code=0,
            healthcheck=None,
            network_mode="bridge",
            labels={},
            logical_key_kind="name",
            logical_key=name,
            cpu_pct=0.0,
            mem_mib=0.0,
            compose_project=None,
            compose_service=None,
            compose_file_path=None,
            restart_count_24h=0,
            now=now,
        )
    return target_id


async def _seed_probe(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    container_name: str,
    kind: str = "http",
    name: str = "healthz",
    target_value: str = "http://localhost:8080/healthz",
    config_source: str = "label",
    enabled: bool = True,
) -> str:
    """Test helper: seed a probe via the production upsert path."""
    now = utc_now_iso()
    async with repo.transaction() as conn:
        probe_id = await ProbeTargetsRepository.upsert_probe_target_conn(
            conn,
            container_name=container_name,
            kind=kind,
            name=name,
            target_value=target_value,
            config_source=config_source,
            enabled=enabled,
            now=now,
        )
    return probe_id


# ---- AUTH TESTS ----


@pytest.mark.asyncio
async def test_list_probes_unauthenticated_returns_401(
    unauthenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that GET /containers/{name}/probes requires authentication."""
    await _seed_container(repo, name="web")
    response = await unauthenticated_client.get("/api/integrations/docker/containers/web/probes")
    assert response.status_code == HTTP_UNAUTHORIZED


@pytest.mark.asyncio
async def test_disable_probe_unauthenticated_returns_401(
    unauthenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that POST /probes/{id}/disable requires authentication."""
    await _seed_container(repo, name="web")
    probe_id = await _seed_probe(repo, container_name="web")
    response = await unauthenticated_client.post(
        f"/api/integrations/docker/probes/{probe_id}/disable",
        headers={"X-CSRF-Token": "dummy"},
    )
    assert response.status_code == HTTP_UNAUTHORIZED


@pytest.mark.asyncio
async def test_enable_probe_unauthenticated_returns_401(
    unauthenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that POST /probes/{id}/enable requires authentication."""
    await _seed_container(repo, name="web")
    probe_id = await _seed_probe(repo, container_name="web")
    response = await unauthenticated_client.post(
        f"/api/integrations/docker/probes/{probe_id}/enable",
        headers={"X-CSRF-Token": "dummy"},
    )
    assert response.status_code == HTTP_UNAUTHORIZED


# ---- LIST PROBES TESTS ----


@pytest.mark.asyncio
async def test_list_probes_unknown_container_returns_404(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that listing probes for unknown container returns 404."""
    response = await authenticated_client.get("/api/integrations/docker/containers/unknown/probes")
    assert response.status_code == HTTP_NOT_FOUND
    data = response.json()
    assert "container not found" in str(data)


@pytest.mark.asyncio
async def test_list_probes_empty_returns_empty_list(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that listing probes for a container with no probes returns empty list."""
    await _seed_container(repo, name="web")
    response = await authenticated_client.get("/api/integrations/docker/containers/web/probes")
    assert response.status_code == HTTP_OK
    data = response.json()
    assert "probes" in data
    assert data["probes"] == []


@pytest.mark.asyncio
async def test_list_probes_returns_active_probes(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that listing probes returns all non-hidden probes for the container."""
    await _seed_container(repo, name="web")
    await _seed_probe(repo, container_name="web", kind="http", name="health")
    await _seed_probe(repo, container_name="web", kind="tcp", name="port_8080")
    response = await authenticated_client.get("/api/integrations/docker/containers/web/probes")
    assert response.status_code == HTTP_OK
    data = response.json()
    assert len(data["probes"]) == 2  # noqa: PLR2004
    # Verify probes are sorted by kind, name
    assert data["probes"][0]["kind"] == "http"
    assert data["probes"][1]["kind"] == "tcp"


@pytest.mark.asyncio
async def test_list_probes_excludes_hidden(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that listing probes excludes hidden probes."""
    await _seed_container(repo, name="web")
    probe_id = await _seed_probe(repo, container_name="web", kind="http", name="health")
    await _seed_probe(repo, container_name="web", kind="tcp", name="port_8080")

    # Hide one probe
    now = utc_now_iso()
    async with repo.transaction() as conn:
        await conn.execute(
            text("UPDATE probe_targets SET hidden_at = :now WHERE id = :id"),
            {"now": now, "id": probe_id},
        )

    response = await authenticated_client.get("/api/integrations/docker/containers/web/probes")
    assert response.status_code == HTTP_OK
    data = response.json()
    assert len(data["probes"]) == 1  # only the non-hidden one
    assert data["probes"][0]["kind"] == "tcp"


# ---- DISABLE PROBE TESTS ----


@pytest.mark.asyncio
async def test_disable_unknown_probe_returns_404(
    authenticated_client: AsyncClient,
) -> None:
    """Test that disabling unknown probe returns 404."""
    response = await authenticated_client.post(
        "/api/integrations/docker/probes/unknown-id/disable",
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf")},  # type: ignore[reportArgumentType]
    )
    assert response.status_code == HTTP_NOT_FOUND
    data = response.json()
    assert "probe not found" in str(data)


@pytest.mark.asyncio
async def test_disable_round_trip(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that disabling a probe sets enabled=False."""
    await _seed_container(repo, name="web")
    probe_id = await _seed_probe(repo, container_name="web", enabled=True)

    response = await authenticated_client.post(
        f"/api/integrations/docker/probes/{probe_id}/disable",
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf")},  # type: ignore[reportArgumentType]
    )
    assert response.status_code == HTTP_OK
    data = response.json()
    assert data["enabled"] is False

    # Verify in DB
    probes_repo = ProbeTargetsRepository(repo)
    probe = await probes_repo.get_by_id(probe_id)
    assert probe is not None
    assert probe.enabled is False


@pytest.mark.asyncio
async def test_disable_writes_audit_log(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that disabling a probe writes an audit log entry."""
    await _seed_container(repo, name="web")
    probe_id = await _seed_probe(repo, container_name="web", enabled=True)

    response = await authenticated_client.post(
        f"/api/integrations/docker/probes/{probe_id}/disable",
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf")},  # type: ignore[reportArgumentType]
    )
    assert response.status_code == HTTP_OK

    # Check audit log
    rows = await repo.fetch_all(
        text(
            "SELECT who, what, before_json, after_json FROM audit_log"
            " WHERE what = 'docker.probe.disable'"
        )
    )
    assert len(rows) >= 1
    row = rows[0]
    assert row.what == "docker.probe.disable"
    # Default test user is 'testuser'
    assert row.who == "testuser"


@pytest.mark.asyncio
async def test_disable_audit_log_includes_username(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that audit log includes the authenticated username."""
    await _seed_container(repo, name="web")
    probe_id = await _seed_probe(repo, container_name="web", enabled=True)

    response = await authenticated_client.post(
        f"/api/integrations/docker/probes/{probe_id}/disable",
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf")},  # type: ignore[reportArgumentType]
    )
    assert response.status_code == HTTP_OK

    # Check audit log contains username
    rows = await repo.fetch_all(
        text("SELECT who FROM audit_log WHERE what = 'docker.probe.disable'")
    )
    assert len(rows) >= 1
    assert rows[0].who == "testuser"


# ---- ENABLE PROBE TESTS ----


@pytest.mark.asyncio
async def test_enable_unknown_probe_returns_404(
    authenticated_client: AsyncClient,
) -> None:
    """Test that enabling unknown probe returns 404."""
    response = await authenticated_client.post(
        "/api/integrations/docker/probes/unknown-id/enable",
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf")},  # type: ignore[reportArgumentType]
    )
    assert response.status_code == HTTP_NOT_FOUND
    data = response.json()
    assert "probe not found" in str(data)


@pytest.mark.asyncio
async def test_enable_round_trip(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that enabling a probe sets enabled=True."""
    await _seed_container(repo, name="web")
    probe_id = await _seed_probe(repo, container_name="web", enabled=False)

    response = await authenticated_client.post(
        f"/api/integrations/docker/probes/{probe_id}/enable",
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf")},  # type: ignore[reportArgumentType]
    )
    assert response.status_code == HTTP_OK
    data = response.json()
    assert data["enabled"] is True

    # Verify in DB
    probes_repo = ProbeTargetsRepository(repo)
    probe = await probes_repo.get_by_id(probe_id)
    assert probe is not None
    assert probe.enabled is True


@pytest.mark.asyncio
async def test_enable_writes_audit_log(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that enabling a probe writes an audit log entry."""
    await _seed_container(repo, name="web")
    probe_id = await _seed_probe(repo, container_name="web", enabled=False)

    response = await authenticated_client.post(
        f"/api/integrations/docker/probes/{probe_id}/enable",
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf")},  # type: ignore[reportArgumentType]
    )
    assert response.status_code == HTTP_OK

    # Check audit log
    rows = await repo.fetch_all(
        text(
            "SELECT who, what, before_json, after_json FROM audit_log"
            " WHERE what = 'docker.probe.enable'"
        )
    )
    assert len(rows) >= 1
    row = rows[0]
    assert row.what == "docker.probe.enable"
    assert row.who == "testuser"


@pytest.mark.asyncio
async def test_enable_audit_log_includes_username(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that enable audit log includes the authenticated username."""
    await _seed_container(repo, name="web")
    probe_id = await _seed_probe(repo, container_name="web", enabled=False)

    response = await authenticated_client.post(
        f"/api/integrations/docker/probes/{probe_id}/enable",
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf")},  # type: ignore[reportArgumentType]
    )
    assert response.status_code == HTTP_OK

    # Check audit log contains username
    rows = await repo.fetch_all(
        text("SELECT who FROM audit_log WHERE what = 'docker.probe.enable'")
    )
    assert len(rows) >= 1
    assert rows[0].who == "testuser"


# ---- RESPONSE FORMAT TESTS ----


@pytest.mark.asyncio
async def test_list_probes_response_includes_all_fields(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Test that probe response includes all expected fields."""
    await _seed_container(repo, name="web")
    await _seed_probe(
        repo,
        container_name="web",
        kind="http",
        name="health",
        target_value="http://localhost:8080/health",
        config_source="label",
        enabled=True,
    )

    response = await authenticated_client.get("/api/integrations/docker/containers/web/probes")
    assert response.status_code == HTTP_OK
    data = response.json()
    probe = data["probes"][0]

    # Verify all expected fields are present
    assert "id" in probe
    assert "container_name" in probe
    assert "kind" in probe
    assert "name" in probe
    assert "target_value" in probe
    assert "config_source" in probe
    assert "enabled" in probe
    assert "interval_seconds" in probe
    assert "timeout_seconds" in probe
    assert "last_run_at" in probe
    assert "last_status" in probe
    assert "last_error" in probe
    assert "created_at" in probe
    assert "hidden_at" in probe

    # Verify values
    assert probe["container_name"] == "web"
    assert probe["kind"] == "http"
    assert probe["name"] == "health"
    assert probe["enabled"] is True


# ---- PROBES SUMMARY TESTS ----


@pytest.mark.asyncio
async def test_get_probes_summary_empty(authenticated_client: AsyncClient) -> None:
    """Empty DB => 200 with empty summaries list."""
    resp = await authenticated_client.get("/api/integrations/docker/probes/summary")
    assert resp.status_code == HTTP_OK
    assert resp.json() == {"summaries": []}


@pytest.mark.asyncio
async def test_get_probes_summary_populated(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Populated DB => 200 with one entry per container."""
    await _seed_container(repo, name="web")
    await _seed_container(repo, name="db")
    await _seed_probe(repo, container_name="web", kind="http", name="health")
    await _seed_probe(repo, container_name="web", kind="tcp", name="port_8080")
    await _seed_probe(repo, container_name="db", kind="tcp", name="default")

    resp = await authenticated_client.get("/api/integrations/docker/probes/summary")
    assert resp.status_code == HTTP_OK
    data = resp.json()
    assert len(data["summaries"]) == 2  # noqa: PLR2004
    by_name = {s["container_name"]: s for s in data["summaries"]}
    assert by_name["web"]["active"] == 2  # noqa: PLR2004
    assert by_name["web"]["failing"] == 0
    assert by_name["db"]["active"] == 1
    assert by_name["db"]["failing"] == 0


@pytest.mark.asyncio
async def test_get_probes_summary_requires_auth(unauthenticated_client: AsyncClient) -> None:
    """No auth => 401."""
    resp = await unauthenticated_client.get("/api/integrations/docker/probes/summary")
    assert resp.status_code == HTTP_UNAUTHORIZED
