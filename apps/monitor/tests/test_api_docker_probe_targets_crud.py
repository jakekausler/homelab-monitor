"""Unit tests for STAGE-003-012 probe-target CRUD endpoints.

Covers: POST /probe-targets, PATCH /probe-targets/{id}, DELETE /probe-targets/{id}.
"""

from __future__ import annotations

import re

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

HTTP_OK = 200
HTTP_NO_CONTENT = 204
HTTP_NOT_FOUND = 404
HTTP_UNPROCESSABLE = 422
HTTP_UNAUTHORIZED = 401


@pytest.fixture(autouse=True)
def _mock_vm_lifespan_tick(httpx_mock: HTTPXMock) -> None:  # pyright: ignore[reportUnusedFunction]
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


async def _seed_container(repo: SqliteRepository, *, name: str) -> str:
    """Seed a container row."""
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
    config_source: str = "manual",
    interval_seconds: int = 60,
    timeout_seconds: int = 10,
) -> str:
    now = utc_now_iso()
    async with repo.transaction() as conn:
        probe_id = await ProbeTargetsRepository.upsert_probe_target_conn(
            conn,
            container_name=container_name,
            kind=kind,
            name=name,
            target_value=target_value,
            config_source=config_source,
            enabled=True,
            interval_seconds=interval_seconds,
            timeout_seconds=timeout_seconds,
            now=now,
        )
    return probe_id


def _csrf(client: AsyncClient) -> dict[str, str]:
    """Build CSRF header dict from the authenticated client's cookies."""
    return {"X-CSRF-Token": client.cookies.get("homelab_monitor_csrf") or ""}


@pytest.mark.asyncio
async def test_create_probe_target_returns_200_and_persists(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    await _seed_container(repo, name="web")
    body = {
        "container_name": "web",
        "kind": "http",
        "name": "healthz",
        "target_value": "http://localhost:8080/healthz",
        "interval_seconds": 60,
        "timeout_seconds": 10,
    }
    r = await authenticated_client.post(
        "/api/integrations/docker/probe-targets", json=body, headers=_csrf(authenticated_client)
    )
    assert r.status_code == HTTP_OK
    data = r.json()
    assert data["container_name"] == "web"
    assert data["kind"] == "http"
    assert data["name"] == "healthz"
    assert data["config_source"] == "manual"
    assert data["enabled"] is True


@pytest.mark.asyncio
async def test_create_probe_target_unknown_container_returns_404(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    body = {
        "container_name": "nonexistent",
        "kind": "http",
        "name": "healthz",
        "target_value": "http://localhost:8080/healthz",
    }
    r = await authenticated_client.post(
        "/api/integrations/docker/probe-targets", json=body, headers=_csrf(authenticated_client)
    )
    assert r.status_code == HTTP_NOT_FOUND


@pytest.mark.asyncio
async def test_create_probe_target_invalid_kind_returns_422(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    await _seed_container(repo, name="web")
    body = {
        "container_name": "web",
        "kind": "banana",
        "name": "healthz",
        "target_value": "http://localhost:8080/healthz",
    }
    r = await authenticated_client.post(
        "/api/integrations/docker/probe-targets", json=body, headers=_csrf(authenticated_client)
    )
    assert r.status_code == HTTP_UNPROCESSABLE


@pytest.mark.asyncio
async def test_create_probe_target_invalid_name_returns_422(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    await _seed_container(repo, name="web")
    body = {
        "container_name": "web",
        "kind": "http",
        "name": " bad name ",
        "target_value": "http://localhost:8080/healthz",
    }
    r = await authenticated_client.post(
        "/api/integrations/docker/probe-targets", json=body, headers=_csrf(authenticated_client)
    )
    assert r.status_code == HTTP_UNPROCESSABLE


@pytest.mark.asyncio
async def test_create_probe_target_interval_too_high_returns_422(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    await _seed_container(repo, name="web")
    body = {
        "container_name": "web",
        "kind": "http",
        "name": "healthz",
        "target_value": "http://localhost:8080/healthz",
        "interval_seconds": 4000,
    }
    r = await authenticated_client.post(
        "/api/integrations/docker/probe-targets", json=body, headers=_csrf(authenticated_client)
    )
    assert r.status_code == HTTP_UNPROCESSABLE


@pytest.mark.asyncio
async def test_create_probe_target_interval_too_low_returns_422(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    await _seed_container(repo, name="web")
    body = {
        "container_name": "web",
        "kind": "http",
        "name": "healthz",
        "target_value": "http://localhost:8080/healthz",
        "interval_seconds": 0,
    }
    r = await authenticated_client.post(
        "/api/integrations/docker/probe-targets", json=body, headers=_csrf(authenticated_client)
    )
    assert r.status_code == HTTP_UNPROCESSABLE


@pytest.mark.asyncio
async def test_create_probe_target_timeout_too_high_returns_422(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    await _seed_container(repo, name="web")
    body = {
        "container_name": "web",
        "kind": "http",
        "name": "healthz",
        "target_value": "http://localhost:8080/healthz",
        "timeout_seconds": 400,
    }
    r = await authenticated_client.post(
        "/api/integrations/docker/probe-targets", json=body, headers=_csrf(authenticated_client)
    )
    assert r.status_code == HTTP_UNPROCESSABLE


@pytest.mark.asyncio
async def test_create_probe_target_duplicate_upserts(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    await _seed_container(repo, name="web")
    body = {
        "container_name": "web",
        "kind": "http",
        "name": "healthz",
        "target_value": "http://localhost:8080/healthz",
        "interval_seconds": 60,
        "timeout_seconds": 10,
    }
    r1 = await authenticated_client.post(
        "/api/integrations/docker/probe-targets", json=body, headers=_csrf(authenticated_client)
    )
    assert r1.status_code == HTTP_OK
    body["target_value"] = "http://localhost:8080/updated"
    r2 = await authenticated_client.post(
        "/api/integrations/docker/probe-targets", json=body, headers=_csrf(authenticated_client)
    )
    assert r2.status_code == HTTP_OK
    assert r2.json()["target_value"] == "http://localhost:8080/updated"
    # Exactly one row exists for (container_name, kind, name).
    async with repo.transaction() as conn:
        result = await conn.execute(
            text(
                "SELECT COUNT(*) AS c FROM probe_targets "
                "WHERE container_name = :cn AND kind = :k AND name = :n"
            ),
            {"cn": "web", "k": "http", "n": "healthz"},
        )
        row = result.first()
        assert row is not None
        assert int(row.c) == 1


@pytest.mark.asyncio
async def test_create_probe_target_unauthenticated_returns_401(
    unauthenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    await _seed_container(repo, name="web")
    body = {
        "container_name": "web",
        "kind": "http",
        "name": "healthz",
        "target_value": "http://localhost:8080/healthz",
    }
    r = await unauthenticated_client.post(
        "/api/integrations/docker/probe-targets",
        json=body,
        headers={"X-CSRF-Token": "dummy"},
    )
    assert r.status_code == HTTP_UNAUTHORIZED


@pytest.mark.asyncio
async def test_update_probe_target_target_value_only_returns_200(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    await _seed_container(repo, name="web")
    probe_id = await _seed_probe(repo, container_name="web")
    body = {"target_value": "http://new"}
    r = await authenticated_client.patch(
        f"/api/integrations/docker/probe-targets/{probe_id}",
        json=body,
        headers=_csrf(authenticated_client),
    )
    assert r.status_code == HTTP_OK
    assert r.json()["target_value"] == "http://new"
    # interval_seconds should be unchanged from seed default
    original_interval = 60
    assert r.json()["interval_seconds"] == original_interval


@pytest.mark.asyncio
async def test_update_probe_target_all_fields_returns_200(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    await _seed_container(repo, name="web")
    probe_id = await _seed_probe(repo, container_name="web")
    updated_interval = 90
    updated_timeout = 15
    body = {
        "target_value": "http://new",
        "interval_seconds": updated_interval,
        "timeout_seconds": updated_timeout,
    }
    r = await authenticated_client.patch(
        f"/api/integrations/docker/probe-targets/{probe_id}",
        json=body,
        headers=_csrf(authenticated_client),
    )
    assert r.status_code == HTTP_OK
    data = r.json()
    assert data["target_value"] == "http://new"
    assert data["interval_seconds"] == updated_interval
    assert data["timeout_seconds"] == updated_timeout


@pytest.mark.asyncio
async def test_update_probe_target_interval_only_returns_200(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """PATCH with only interval_seconds (no target_value) hits the 239->242 branch."""
    await _seed_container(repo, name="web")
    probe_id = await _seed_probe(repo, container_name="web")
    updated_interval = 120
    body = {"interval_seconds": updated_interval}
    r = await authenticated_client.patch(
        f"/api/integrations/docker/probe-targets/{probe_id}",
        json=body,
        headers=_csrf(authenticated_client),
    )
    assert r.status_code == HTTP_OK
    data = r.json()
    assert data["interval_seconds"] == updated_interval
    # target_value should be unchanged from seed default
    assert data["target_value"] == "http://localhost:8080/healthz"


@pytest.mark.asyncio
async def test_update_probe_target_empty_body_returns_200_no_change(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    await _seed_container(repo, name="web")
    probe_id = await _seed_probe(repo, container_name="web")
    body: dict[str, object] = {}
    r = await authenticated_client.patch(
        f"/api/integrations/docker/probe-targets/{probe_id}",
        json=body,
        headers=_csrf(authenticated_client),
    )
    assert r.status_code == HTTP_OK


@pytest.mark.asyncio
async def test_update_probe_target_unknown_id_returns_404(
    authenticated_client: AsyncClient,
) -> None:
    body = {"target_value": "http://new"}
    r = await authenticated_client.patch(
        "/api/integrations/docker/probe-targets/does-not-exist",
        json=body,
        headers=_csrf(authenticated_client),
    )
    assert r.status_code == HTTP_NOT_FOUND


@pytest.mark.asyncio
async def test_update_probe_target_invalid_interval_returns_422(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    await _seed_container(repo, name="web")
    probe_id = await _seed_probe(repo, container_name="web")
    body = {"interval_seconds": 9999}
    r = await authenticated_client.patch(
        f"/api/integrations/docker/probe-targets/{probe_id}",
        json=body,
        headers=_csrf(authenticated_client),
    )
    assert r.status_code == HTTP_UNPROCESSABLE


@pytest.mark.asyncio
async def test_update_probe_target_invalid_kind_field_returns_422(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    await _seed_container(repo, name="web")
    probe_id = await _seed_probe(repo, container_name="web")
    body = {"kind": "http"}  # type: ignore[dict-item]
    r = await authenticated_client.patch(
        f"/api/integrations/docker/probe-targets/{probe_id}",
        json=body,
        headers=_csrf(authenticated_client),
    )
    assert r.status_code == HTTP_UNPROCESSABLE


@pytest.mark.asyncio
async def test_update_probe_target_unauthenticated_returns_401(
    unauthenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    await _seed_container(repo, name="web")
    probe_id = await _seed_probe(repo, container_name="web")
    body = {"target_value": "http://new"}
    r = await unauthenticated_client.patch(
        f"/api/integrations/docker/probe-targets/{probe_id}",
        json=body,
        headers={"X-CSRF-Token": "dummy"},
    )
    assert r.status_code == HTTP_UNAUTHORIZED


@pytest.mark.asyncio
async def test_delete_probe_target_returns_204_and_row_gone(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    await _seed_container(repo, name="web")
    probe_id = await _seed_probe(repo, container_name="web")
    r = await authenticated_client.delete(
        f"/api/integrations/docker/probe-targets/{probe_id}",
        headers=_csrf(authenticated_client),
    )
    assert r.status_code == HTTP_NO_CONTENT
    # Verify row is gone
    probes_repo = ProbeTargetsRepository(repo)
    after = await probes_repo.get_by_id(probe_id)
    assert after is None


@pytest.mark.asyncio
async def test_delete_probe_target_unknown_id_returns_404(
    authenticated_client: AsyncClient,
) -> None:
    r = await authenticated_client.delete(
        "/api/integrations/docker/probe-targets/does-not-exist",
        headers=_csrf(authenticated_client),
    )
    assert r.status_code == HTTP_NOT_FOUND


@pytest.mark.asyncio
async def test_delete_probe_target_unauthenticated_returns_401(
    unauthenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    await _seed_container(repo, name="web")
    probe_id = await _seed_probe(repo, container_name="web")
    r = await unauthenticated_client.delete(
        f"/api/integrations/docker/probe-targets/{probe_id}",
        headers={"X-CSRF-Token": "dummy"},
    )
    assert r.status_code == HTTP_UNAUTHORIZED
