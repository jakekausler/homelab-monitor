"""Tests for healthcheck-incident endpoints in docker router (STAGE-004-033).

GET /api/integrations/docker/containers/{name}/healthcheck-incidents
GET /api/integrations/docker/containers/{name}/healthcheck-incidents/{incident_id}

Project test conventions:
- asyncio_mode=auto — bare async def
- noqa: PLR2004 for magic number assertions
- Uses authenticated_client + repo fixtures (shared app, per-test DB)
"""

from __future__ import annotations

import re
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from pytest_httpx import HTTPXMock

from homelab_monitor.kernel.db.repositories.targets_repository import TargetsRepository
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.logs.healthcheck_enrichments_repo import (
    HealthcheckEnrichmentsRepository,
)
from homelab_monitor.kernel.logs.models import LogLine

HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
HTTP_NOT_FOUND = 404


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
    image: str = "ubuntu:22.04",
    exit_code: int = 0,
    finished_at: str | None = None,
    compose_project: str | None = None,
    compose_service: str | None = None,
) -> str:
    """Seed a docker container row using the production upsert path."""
    now = utc_now_iso()
    async with repo.transaction() as conn:
        return await TargetsRepository.upsert_docker_container_conn(
            conn,
            container_id=target_id,
            logical_key_kind="name",
            logical_key=name,
            name=name,
            status=status,
            image=image,
            restart_count=0,
            exit_code=exit_code,
            healthcheck=None,
            network_mode="bridge",
            labels={},
            now=now,
            cpu_pct=None,
            mem_mib=None,
            compose_project=compose_project,
            compose_service=compose_service,
            compose_file_path=None,
            finished_at=finished_at,
        )


def _make_log_line(msg: str = "test line") -> LogLine:
    return LogLine(
        timestamp="2026-06-07T00:00:00Z",
        message=msg,
        stream="s",
        severity="error",
        host=None,
        service=None,
        fields={},
    )


async def _seed_incident(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    logical_key: str,
    container_name: str,
    incident_id: str | None = None,
    previous_healthcheck: str | None = "healthy",
    new_state: str = "unhealthy",
    healthcheck_changed_at: str = "2026-06-07T00:00:00+00:00",
    lines: list[LogLine] | None = None,
) -> str:
    """Seed a healthcheck enrichment row directly."""
    if incident_id is None:
        incident_id = str(uuid.uuid4())
    if lines is None:
        lines = [_make_log_line("healthcheck log")]
    hc_repo = HealthcheckEnrichmentsRepository(repo)
    await hc_repo.insert(
        incident_id=incident_id,
        logical_key=logical_key,
        container_name=container_name,
        container_id=None,
        previous_healthcheck=previous_healthcheck,
        new_state=new_state,
        healthcheck_changed_at=healthcheck_changed_at,
        image_name="ubuntu:22.04",
        compose_project=None,
        compose_service=None,
        lines=lines,
        truncated=False,
        degraded=False,
        window_start="2026-06-06T23:59:00+00:00",
        window_end="2026-06-07T00:00:05+00:00",
    )
    return incident_id


# ---------------------------------------------------------------------------
# List incidents endpoint
# ---------------------------------------------------------------------------


async def test_list_incidents_200(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """200: container with 2 incident rows returns them; no 'lines' key in summaries."""
    await _seed_docker_container(repo, target_id="c1", name="unhealthy-ctr", status="running")
    await _seed_incident(
        repo,
        logical_key="unhealthy-ctr",
        container_name="unhealthy-ctr",
        healthcheck_changed_at="2026-06-07T00:00:00+00:00",
    )
    await _seed_incident(
        repo,
        logical_key="unhealthy-ctr",
        container_name="unhealthy-ctr",
        healthcheck_changed_at="2026-06-07T01:00:00+00:00",
    )

    resp = await authenticated_client.get(
        "/api/integrations/docker/containers/unhealthy-ctr/healthcheck-incidents"
    )
    assert resp.status_code == HTTP_OK
    body = resp.json()
    assert body["container_name"] == "unhealthy-ctr"
    assert len(body["incidents"]) == 2  # noqa: PLR2004
    for incident in body["incidents"]:
        assert "incident_id" in incident
        assert "previous_healthcheck" in incident
        assert "new_state" in incident
        assert "healthcheck_changed_at" in incident
        assert "line_count" in incident
        assert "lines" not in incident  # ContainerHealthcheckIncidentSummary has no lines field


async def test_list_incidents_404_unknown_container(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """404 for container not in inventory."""
    resp = await authenticated_client.get(
        "/api/integrations/docker/containers/nope/healthcheck-incidents"
    )
    assert resp.status_code == HTTP_NOT_FOUND


async def test_list_incidents_empty_when_none(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """200 with empty incidents list when container exists but no incident rows."""
    await _seed_docker_container(repo, target_id="c1", name="no-incidents-ctr")

    resp = await authenticated_client.get(
        "/api/integrations/docker/containers/no-incidents-ctr/healthcheck-incidents"
    )
    assert resp.status_code == HTTP_OK
    body = resp.json()
    assert body["container_name"] == "no-incidents-ctr"
    assert body["incidents"] == []


# ---------------------------------------------------------------------------
# Detail incident endpoint
# ---------------------------------------------------------------------------


async def test_incident_detail_200(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """200: incident detail includes lines, window_start, window_end, incident_id."""
    await _seed_docker_container(repo, target_id="c1", name="unhealthy-ctr", status="running")
    incident_id = await _seed_incident(
        repo,
        logical_key="unhealthy-ctr",
        container_name="unhealthy-ctr",
        lines=[_make_log_line("line 1"), _make_log_line("line 2")],
    )

    resp = await authenticated_client.get(
        f"/api/integrations/docker/containers/unhealthy-ctr/healthcheck-incidents/{incident_id}"
    )
    assert resp.status_code == HTTP_OK
    body = resp.json()
    assert body["incident_id"] == incident_id
    assert body["container_name"] == "unhealthy-ctr"
    assert body["previous_healthcheck"] == "healthy"
    assert body["new_state"] == "unhealthy"
    assert len(body["lines"]) == 2  # noqa: PLR2004
    assert body["window_start"] is not None
    assert body["window_end"] is not None


async def test_incident_detail_404_missing_incident(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """404 when incident_id does not exist (container does exist)."""
    await _seed_docker_container(repo, target_id="c1", name="unhealthy-ctr")

    resp = await authenticated_client.get(
        "/api/integrations/docker/containers/unhealthy-ctr/healthcheck-incidents/does-not-exist"
    )
    assert resp.status_code == HTTP_NOT_FOUND


async def test_incident_detail_404_unknown_container(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """404 when the container is unknown (container check runs first)."""
    resp = await authenticated_client.get(
        "/api/integrations/docker/containers/nope/healthcheck-incidents/whatever"
    )
    assert resp.status_code == HTTP_NOT_FOUND


async def test_incident_detail_404_incident_belongs_to_other_container(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """404 when incident_id exists but belongs to a different container."""
    await _seed_docker_container(repo, target_id="c1", name="unhealthy-ctr")
    await _seed_docker_container(repo, target_id="c2", name="other")

    # Incident belongs to "other" — request it via "unhealthy-ctr"
    incident_id = await _seed_incident(
        repo,
        logical_key="other",
        container_name="other",
    )

    resp = await authenticated_client.get(
        f"/api/integrations/docker/containers/unhealthy-ctr/healthcheck-incidents/{incident_id}"
    )
    assert resp.status_code == HTTP_NOT_FOUND


async def test_list_incidents_401_without_session(repo: SqliteRepository) -> None:
    """No session cookie → 401."""
    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as anon:
        resp = await anon.get(
            "/api/integrations/docker/containers/unhealthy-ctr/healthcheck-incidents"
        )
        assert resp.status_code == HTTP_UNAUTHORIZED
