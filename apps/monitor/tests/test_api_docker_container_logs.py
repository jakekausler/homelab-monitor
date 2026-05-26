"""Tests for GET /api/integrations/docker/containers/{name}/logs (STAGE-003-011)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from pytest_httpx import HTTPXMock

from homelab_monitor.kernel.db.repositories.targets_repository import TargetsRepository
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
HTTP_NOT_FOUND = 404
HTTP_UNPROCESSABLE = 422
HTTP_SERVICE_UNAVAILABLE = 503

VL_URL_ENV = "HOMELAB_MONITOR_VL_URL"
VL_URL = "http://vl-test:9428"


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


def _make_ndjson(*msgs: str, base_ts: str = "2026-05-21T14:30:00+00:00") -> str:
    """Build an NDJSON stream that VictoriaLogsClient._parse_ndjson can parse."""
    lines: list[str] = []
    for _i, msg in enumerate(msgs):
        lines.append(
            json.dumps(
                {
                    "_stream_id": "svc.host",
                    "_msg": msg,
                    "_time": base_ts,
                    "service": "homeassistant",
                }
            )
        )
    return "\n".join(lines) + "\n"


@pytest.mark.asyncio
async def test_get_container_logs_available(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """200 available: container in targets, VL returns 2 lines."""
    monkeypatch.setenv(VL_URL_ENV, VL_URL)
    await _seed_docker_container(repo, target_id="abc1", name="homeassistant")
    httpx_mock.add_response(method="GET", text=_make_ndjson("line one", "line two"))

    resp = await authenticated_client.get("/api/integrations/docker/containers/homeassistant/logs")
    assert resp.status_code == HTTP_OK
    body = resp.json()
    assert body["container_name"] == "homeassistant"
    assert body["log_status"] == "available"
    assert len(body["lines"]) == 2  # noqa: PLR2004
    assert body["lines"][0]["line"] == "line one"
    assert body["lines"][1]["line"] == "line two"
    assert body["truncated"] is False
    assert body["window_start"] is not None
    assert body["window_end"] is not None


@pytest.mark.asyncio
async def test_get_container_logs_truncated_at_500(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """200 truncated: VL returns 501 lines, server caps at 500 and sets truncated=True."""
    monkeypatch.setenv(VL_URL_ENV, VL_URL)
    await _seed_docker_container(repo, target_id="abc1", name="homeassistant")
    # 501 lines: VL client reads limit+1 == 501 NDJSON lines, then sees the
    # 501st and flags truncated=True.
    httpx_mock.add_response(method="GET", text=_make_ndjson(*(f"line-{i}" for i in range(501))))

    resp = await authenticated_client.get("/api/integrations/docker/containers/homeassistant/logs")
    assert resp.status_code == HTTP_OK
    body = resp.json()
    assert body["log_status"] == "available"
    assert len(body["lines"]) == 500  # noqa: PLR2004
    assert body["truncated"] is True


@pytest.mark.asyncio
async def test_get_container_logs_no_lines(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """200 no_lines: container known but VL returns empty body."""
    monkeypatch.setenv(VL_URL_ENV, VL_URL)
    await _seed_docker_container(repo, target_id="abc1", name="homeassistant")
    httpx_mock.add_response(method="GET", text="")

    resp = await authenticated_client.get("/api/integrations/docker/containers/homeassistant/logs")
    assert resp.status_code == HTTP_OK
    body = resp.json()
    assert body["log_status"] == "no_lines"
    assert body["lines"] == []
    assert body["truncated"] is False


@pytest.mark.asyncio
async def test_get_container_logs_container_unknown(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """404 container_unknown: container not in targets table."""
    # No _seed_docker_container call → targets table is empty for this name.
    resp = await authenticated_client.get("/api/integrations/docker/containers/nonexistent/logs")
    assert resp.status_code == HTTP_NOT_FOUND
    body = resp.json()
    # FastAPI wraps HTTPException detail dict in error.details
    detail = body.get("error", {}).get("details", body.get("detail", body))
    assert detail["container_name"] == "nonexistent"
    assert detail["log_status"] == "container_unknown"
    assert detail["lines"] == []
    assert detail["window_start"] is None
    assert detail["window_end"] is None


@pytest.mark.asyncio
async def test_get_container_logs_vl_unavailable(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503 vl_unavailable: VL returns 500."""
    monkeypatch.setenv(VL_URL_ENV, VL_URL)
    await _seed_docker_container(repo, target_id="abc1", name="homeassistant")
    # Match VL URL with regex to avoid catching setup fixture mocks
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://vl-test:9428/.*"),
        status_code=500,
        text="vl down",
    )

    resp = await authenticated_client.get("/api/integrations/docker/containers/homeassistant/logs")
    assert resp.status_code == HTTP_SERVICE_UNAVAILABLE
    body = resp.json()
    # FastAPI wraps HTTPException detail dict in error.details
    detail = body.get("error", {}).get("details", body.get("detail", body))
    assert detail["log_status"] == "vl_unavailable"
    assert detail["container_name"] == "homeassistant"
    assert detail["lines"] == []


@pytest.mark.asyncio
async def test_get_container_logs_since_clamped_at_7d(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """since=8d → clamped to 7d (window spans 7d, not 8d)."""
    monkeypatch.setenv(VL_URL_ENV, VL_URL)
    await _seed_docker_container(repo, target_id="abc1", name="homeassistant")
    httpx_mock.add_response(method="GET", text=_make_ndjson("ok"))

    resp = await authenticated_client.get(
        "/api/integrations/docker/containers/homeassistant/logs?since=8d"
    )
    assert resp.status_code == HTTP_OK
    body = resp.json()
    start = datetime.fromisoformat(body["window_start"])
    end = datetime.fromisoformat(body["window_end"])
    delta = end - start
    # Expect 7d ±5s (clock skew between handler's two datetime.now() calls).
    assert (
        timedelta(days=7) - timedelta(seconds=5)
        <= delta
        <= timedelta(days=7) + timedelta(seconds=5)
    )


@pytest.mark.parametrize("bad_since", ["abc", "5x", "5", "", "0m", "-5m"])
@pytest.mark.asyncio
async def test_get_container_logs_invalid_since_422(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    bad_since: str,
) -> None:
    """422 on invalid since formats.

    Empty since="" passes through to the handler (FastAPI only falls back to
    the default when the query key is absent entirely). The handler's
    _parse_since regex requires \\d+, so empty string returns 422. Real-world
    invalid values are 'abc', '5x', '5' (missing unit), '0m', '-5m'.
    """
    await _seed_docker_container(repo, target_id="abc1", name="homeassistant")
    resp = await authenticated_client.get(
        f"/api/integrations/docker/containers/homeassistant/logs?since={bad_since}"
    )
    # FastAPI rejects negative ints / empty strings at validation layer; our
    # parser raises 422 for unknown unit / value=0. Either way, expect 422.
    assert resp.status_code == HTTP_UNPROCESSABLE


@pytest.mark.asyncio
async def test_get_container_logs_limit_clamped_at_500(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """limit=10000 → silently clamped to 500. VL HTTP request limit param == 501."""
    monkeypatch.setenv(VL_URL_ENV, VL_URL)
    await _seed_docker_container(repo, target_id="abc1", name="homeassistant")
    # Match VL URL with regex to avoid catching setup fixture mocks
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://vl-test:9428/.*"),
        text=_make_ndjson(*(f"line-{i}" for i in range(501))),
    )

    resp = await authenticated_client.get(
        "/api/integrations/docker/containers/homeassistant/logs?limit=10000"
    )
    assert resp.status_code == HTTP_OK
    body = resp.json()
    assert len(body["lines"]) == 500  # noqa: PLR2004
    assert body["truncated"] is True
    # Verify the VL client requested limit+1 == 501 (cap+1 detection)
    # Filter to only VL requests
    requests = [r for r in httpx_mock.get_requests() if "vl-test" in str(r.url)]
    assert len(requests) == 1
    assert requests[0].url.params.get("limit") == "501"


@pytest.mark.asyncio
async def test_get_container_logs_limit_zero_422(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """limit=0 → FastAPI ge=1 validator rejects → 422."""
    await _seed_docker_container(repo, target_id="abc1", name="homeassistant")
    resp = await authenticated_client.get(
        "/api/integrations/docker/containers/homeassistant/logs?limit=0"
    )
    assert resp.status_code == HTTP_UNPROCESSABLE


@pytest.mark.asyncio
async def test_get_container_logs_uses_service_label(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LogsQL `query` param must equal `service:"homeassistant"` (escaped)."""
    monkeypatch.setenv(VL_URL_ENV, VL_URL)
    await _seed_docker_container(repo, target_id="abc1", name="homeassistant")
    # Match VL URL with regex to avoid catching setup fixture mocks
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://vl-test:9428/.*"),
        text="",
    )

    await authenticated_client.get("/api/integrations/docker/containers/homeassistant/logs")
    # Filter to only VL requests
    requests = [r for r in httpx_mock.get_requests() if "vl-test" in str(r.url)]
    assert len(requests) == 1
    assert requests[0].url.params.get("query") == 'service:"homeassistant"'


@pytest.mark.asyncio
async def test_get_container_logs_service_label_escapes_quotes(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Container names with embedded quotes/backslashes get logsql_quote_phrase-escaped.

    Real Docker names cannot contain `"` or `\\`, but defense-in-depth: ensure
    the escape path is exercised, mirroring run-log endpoint's posture.
    """
    monkeypatch.setenv(VL_URL_ENV, VL_URL)
    weird_name = 'name-with-"quote'
    await _seed_docker_container(repo, target_id="abc1", name=weird_name)
    # Match VL URL with regex to avoid catching setup fixture mocks
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://vl-test:9428/.*"),
        text="",
    )

    await authenticated_client.get(f"/api/integrations/docker/containers/{weird_name}/logs")
    # Filter to only VL requests
    requests = [r for r in httpx_mock.get_requests() if "vl-test" in str(r.url)]
    assert len(requests) == 1
    # logsql_quote_phrase escapes " → \" → JSON-encoded as \\\"
    assert requests[0].url.params.get("query") == 'service:"name-with-\\"quote"'


@pytest.mark.asyncio
async def test_get_container_logs_401_without_session(repo: SqliteRepository) -> None:
    """No session → 401."""
    # Use an unauthenticated client. Pattern from test_api_cron_runs.py:529-540.
    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as anon:
        resp = await anon.get("/api/integrations/docker/containers/homeassistant/logs")
        assert resp.status_code == HTTP_UNAUTHORIZED
