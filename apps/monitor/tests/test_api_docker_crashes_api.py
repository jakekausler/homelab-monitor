"""Tests for crash endpoints in docker router (STAGE-004-032).

GET /api/integrations/docker/containers/{name}/crashes
GET /api/integrations/docker/containers/{name}/crashes/{crash_id}

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
from homelab_monitor.kernel.logs.crash_enrichments_repo import CrashEnrichmentsRepository
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


async def _seed_crash(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    logical_key: str,
    container_name: str,
    crash_id: str | None = None,
    exit_code: int = 1,
    finished_at: str = "2026-06-07T00:00:00+00:00",
    lines: list[LogLine] | None = None,
) -> str:
    """Seed a crash enrichment row directly."""
    if crash_id is None:
        crash_id = str(uuid.uuid4())
    if lines is None:
        lines = [_make_log_line("crash log")]
    crash_repo = CrashEnrichmentsRepository(repo)
    await crash_repo.insert(
        crash_id=crash_id,
        logical_key=logical_key,
        container_name=container_name,
        container_id=None,
        exit_code=exit_code,
        finished_at=finished_at,
        image_name="ubuntu:22.04",
        compose_project=None,
        compose_service=None,
        lines=lines,
        truncated=False,
        degraded=False,
        window_start="2026-06-06T23:59:00+00:00",
        window_end="2026-06-07T00:00:05+00:00",
    )
    return crash_id


# ---------------------------------------------------------------------------
# List crashes endpoint
# ---------------------------------------------------------------------------


async def test_list_crashes_200(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """200: container with 2 crash rows returns them; no 'lines' key in summaries."""
    await _seed_docker_container(repo, target_id="c1", name="crashy", status="exited", exit_code=1)
    await _seed_crash(
        repo,
        logical_key="crashy",
        container_name="crashy",
        finished_at="2026-06-07T00:00:00+00:00",
    )
    await _seed_crash(
        repo,
        logical_key="crashy",
        container_name="crashy",
        finished_at="2026-06-07T01:00:00+00:00",
    )

    resp = await authenticated_client.get("/api/integrations/docker/containers/crashy/crashes")
    assert resp.status_code == HTTP_OK
    body = resp.json()
    assert body["container_name"] == "crashy"
    assert len(body["crashes"]) == 2  # noqa: PLR2004
    for crash in body["crashes"]:
        assert "crash_id" in crash
        assert "exit_code" in crash
        assert "finished_at" in crash
        assert "line_count" in crash
        assert "lines" not in crash  # ContainerCrashSummary has no lines field


async def test_list_crashes_404_unknown_container(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """404 for container not in inventory."""
    resp = await authenticated_client.get("/api/integrations/docker/containers/nope/crashes")
    assert resp.status_code == HTTP_NOT_FOUND


async def test_list_crashes_empty_when_no_crashes(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """200 with empty crashes list when container exists but no crash rows."""
    await _seed_docker_container(repo, target_id="c1", name="no-crash-ctr")

    resp = await authenticated_client.get(
        "/api/integrations/docker/containers/no-crash-ctr/crashes"
    )
    assert resp.status_code == HTTP_OK
    body = resp.json()
    assert body["container_name"] == "no-crash-ctr"
    assert body["crashes"] == []


# ---------------------------------------------------------------------------
# Detail crash endpoint
# ---------------------------------------------------------------------------


async def test_crash_detail_200(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """200: crash detail includes lines, window_start, window_end, crash_id."""
    await _seed_docker_container(repo, target_id="c1", name="crashy", status="exited", exit_code=1)
    crash_id = await _seed_crash(
        repo,
        logical_key="crashy",
        container_name="crashy",
        lines=[_make_log_line("line 1"), _make_log_line("line 2")],
    )

    resp = await authenticated_client.get(
        f"/api/integrations/docker/containers/crashy/crashes/{crash_id}"
    )
    assert resp.status_code == HTTP_OK
    body = resp.json()
    assert body["crash_id"] == crash_id
    assert body["container_name"] == "crashy"
    assert len(body["lines"]) == 2  # noqa: PLR2004
    assert body["window_start"] is not None
    assert body["window_end"] is not None


async def test_crash_detail_404_missing_crash(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """404 when crash_id does not exist (container does exist)."""
    await _seed_docker_container(repo, target_id="c1", name="crashy")

    resp = await authenticated_client.get(
        "/api/integrations/docker/containers/crashy/crashes/does-not-exist"
    )
    assert resp.status_code == HTTP_NOT_FOUND


async def test_crash_detail_404_unknown_container(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """404 when the container is unknown (container check runs first)."""
    resp = await authenticated_client.get(
        "/api/integrations/docker/containers/nope/crashes/whatever"
    )
    assert resp.status_code == HTTP_NOT_FOUND


async def test_crash_detail_404_crash_belongs_to_other_container(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """404 when crash_id exists but belongs to a different container."""
    await _seed_docker_container(repo, target_id="c1", name="crashy")
    await _seed_docker_container(repo, target_id="c2", name="other")

    # Crash belongs to "other" — request it via "crashy"
    crash_id = await _seed_crash(
        repo,
        logical_key="other",
        container_name="other",
    )

    resp = await authenticated_client.get(
        f"/api/integrations/docker/containers/crashy/crashes/{crash_id}"
    )
    assert resp.status_code == HTTP_NOT_FOUND


async def test_list_crashes_401_without_session(repo: SqliteRepository) -> None:
    """No session cookie → 401."""
    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as anon:
        resp = await anon.get("/api/integrations/docker/containers/crashy/crashes")
        assert resp.status_code == HTTP_UNAUTHORIZED
