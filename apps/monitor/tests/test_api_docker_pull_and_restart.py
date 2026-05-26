"""API tests for STAGE-003-010 Pull & Restart endpoints."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pytest_httpx import HTTPXMock

from homelab_monitor.kernel.auth.scopes import Scope
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.docker.build_sources_schema import (
    BuildSourcesConfig,
    ComposeFileEntry,
)

if TYPE_CHECKING:
    pass

HTTP_OK = 200
HTTP_ACCEPTED = 202
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409


@pytest.fixture(autouse=True)
def _mock_vm_lifespan_tick(httpx_mock: HTTPXMock) -> None:  # pyright: ignore[reportUnusedFunction]
    """Same mock pattern as other docker API tests."""
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


def _set_compose_config(app: FastAPI, tmp_path: Path) -> None:
    """Force-set a loaded compose config on the build_sources_loader."""
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "services:\n  caddy:\n    image: caddy:latest\n",
        encoding="utf-8",
    )
    cfg = BuildSourcesConfig(
        compose_files=[ComposeFileEntry(host_path=str(compose), container_path=str(compose))]
    )
    loader = app.state.build_sources_loader
    loader._current_config = cfg  # pyright: ignore[reportPrivateUsage]
    loader._current_error = None  # pyright: ignore[reportPrivateUsage]


async def _seed_caddy_target(repo: SqliteRepository) -> None:
    """Insert a single docker target named 'caddy' into targets so the 404 guard passes."""
    from sqlalchemy import text  # noqa: PLC0415

    await repo.execute(
        text(
            "INSERT INTO targets "
            "(id, name, kind, status, first_seen, last_seen,"
            " hidden_at, labels, source, created_at) "
            "VALUES ('docker:caddy', 'caddy', 'docker_container', 'running',"
            " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',"
            " NULL, '{}', 'docker_socket', '2026-01-01T00:00:00+00:00')"
        )
    )


def _mock_compose_inspect(
    httpx_mock: HTTPXMock,
    *,
    container_name: str = "caddy",
    compose_service: str = "caddy",
    compose_project: str = "test",
) -> None:
    """Add a docker socket inspect mock so ComposeActionRunner can resolve the service.

    Per STAGE-003-010 ADDENDUM Q2, ``resolve_compose`` calls
    ``socket_client.inspect_container(container_name)`` to read the
    ``com.docker.compose.service`` label. Without this mock the inspect call
    raises DockerSocketConnectionError → resolve_compose returns None →
    action terminates with state=failed.
    """
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"http://localhost/containers/{re.escape(container_name)}/json"),
        json={
            "Id": "abc123",
            "Name": f"/{container_name}",
            "Config": {
                "Labels": {
                    "com.docker.compose.service": compose_service,
                    "com.docker.compose.project": compose_project,
                }
            },
        },
        is_reusable=True,
    )


@pytest.mark.asyncio
async def test_pull_and_restart_400_without_confirm_phrase(
    authenticated_client: AsyncClient, repo: SqliteRepository, tmp_path: Path
) -> None:
    app = cast("FastAPI", authenticated_client.app)  # type: ignore[attr-defined]
    _set_compose_config(app, tmp_path)
    await _seed_caddy_target(repo)
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/docker/containers/caddy/pull-and-restart",
        json={"confirm_phrase": ""},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == HTTP_BAD_REQUEST
    assert "confirm_phrase" in resp.text


@pytest.mark.asyncio
async def test_pull_and_restart_400_with_wrong_phrase(
    authenticated_client: AsyncClient, repo: SqliteRepository, tmp_path: Path
) -> None:
    app = cast("FastAPI", authenticated_client.app)  # type: ignore[attr-defined]
    _set_compose_config(app, tmp_path)
    await _seed_caddy_target(repo)
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/docker/containers/caddy/pull-and-restart",
        json={"confirm_phrase": "push"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == HTTP_BAD_REQUEST


@pytest.mark.asyncio
async def test_pull_and_restart_case_insensitive_phrase(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    app = cast("FastAPI", authenticated_client.app)  # type: ignore[attr-defined]
    _set_compose_config(app, tmp_path)
    await _seed_caddy_target(repo)
    _mock_compose_inspect(httpx_mock)
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"", b""))
    fake_proc.returncode = 0
    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        resp = await authenticated_client.post(
            "/api/integrations/docker/containers/caddy/pull-and-restart",
            json={"confirm_phrase": "PULL"},
            headers={"X-CSRF-Token": csrf or ""},
        )
        assert resp.status_code == HTTP_ACCEPTED


@pytest.mark.asyncio
async def test_pull_and_restart_401_without_auth(
    unauthenticated_client: AsyncClient, tmp_path: Path
) -> None:
    resp = await unauthenticated_client.post(
        "/api/integrations/docker/containers/caddy/pull-and-restart",
        json={"confirm_phrase": "pull"},
    )
    assert resp.status_code == HTTP_UNAUTHORIZED


@pytest.mark.asyncio
async def test_pull_and_restart_403_token_without_docker_write_scope(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A token with READ_STATUS only should get 403 insufficient_scope."""
    import base64  # noqa: PLC0415

    from httpx import ASGITransport  # noqa: PLC0415

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415
    from homelab_monitor.kernel.auth.api_tokens import make_api_token  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")
    monkeypatch.setenv("HOMELAB_MONITOR_DISABLE_STARTUP_CRON_DISCOVERY", "1")

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        plaintext, _ = make_api_token()
        await app.state.auth_repo.create_api_token(
            name="readonly",
            scopes={Scope.READ_STATUS},
            plaintext_token=plaintext,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            resp = await client.post(
                "/api/integrations/docker/containers/caddy/pull-and-restart",
                json={"confirm_phrase": "pull"},
            )
            assert resp.status_code == HTTP_FORBIDDEN


@pytest.mark.asyncio
async def test_pull_and_restart_202_with_docker_write_token(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    """A token WITH docker:write scope is accepted (202)."""
    import base64  # noqa: PLC0415

    from httpx import ASGITransport  # noqa: PLC0415

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415
    from homelab_monitor.kernel.auth.api_tokens import make_api_token  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")
    monkeypatch.setenv("HOMELAB_MONITOR_DISABLE_STARTUP_CRON_DISCOVERY", "1")

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        # Seed target + compose config + DB row.
        from sqlalchemy import text  # noqa: PLC0415

        from homelab_monitor.kernel.db.repository import SqliteRepository  # noqa: PLC0415

        repo = SqliteRepository(app.state.repo._engine)  # pyright: ignore[reportPrivateUsage]
        await repo.execute(
            text(
                "INSERT INTO targets"
                " (id, name, kind, status, first_seen, last_seen,"
                " hidden_at, labels, source, created_at) "
                "VALUES ('docker:caddy', 'caddy', 'docker_container', 'running',"
                " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',"
                " NULL, '{}', 'docker_socket', '2026-01-01T00:00:00+00:00')"
            )
        )
        _set_compose_config(app, tmp_path)
        _mock_compose_inspect(httpx_mock)

        plaintext, _ = make_api_token()
        await app.state.auth_repo.create_api_token(
            name="docker-write",
            scopes={Scope.DOCKER_WRITE},
            plaintext_token=plaintext,
        )
        fake_proc = MagicMock()
        fake_proc.communicate = AsyncMock(return_value=(b"", b""))
        fake_proc.returncode = 0
        with patch(
            "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": f"Bearer {plaintext}"},
            ) as client:
                resp = await client.post(
                    "/api/integrations/docker/containers/caddy/pull-and-restart",
                    json={"confirm_phrase": "pull"},
                )
                assert resp.status_code == HTTP_ACCEPTED
                data = resp.json()
                assert "action_id" in data
                assert data["state"] in ("pulling", "restarting", "running", "failed")


@pytest.mark.asyncio
async def test_pull_and_restart_404_for_unknown_container(
    authenticated_client: AsyncClient, tmp_path: Path
) -> None:
    app = cast("FastAPI", authenticated_client.app)  # type: ignore[attr-defined]
    _set_compose_config(app, tmp_path)
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/docker/containers/nonexistent/pull-and-restart",
        json={"confirm_phrase": "pull"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == HTTP_NOT_FOUND


@pytest.mark.asyncio
async def test_pull_and_restart_happy_path_full_lifecycle(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    """Confirm + auth → 202 + action_id → GET → state=success."""
    app = cast("FastAPI", authenticated_client.app)  # type: ignore[attr-defined]
    _set_compose_config(app, tmp_path)
    await _seed_caddy_target(repo)
    _mock_compose_inspect(httpx_mock)
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"Up to date\n", b""))
    fake_proc.returncode = 0

    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        resp = await authenticated_client.post(
            "/api/integrations/docker/containers/caddy/pull-and-restart",
            json={"confirm_phrase": "pull"},
            headers={"X-CSRF-Token": csrf or ""},
        )
        assert resp.status_code == HTTP_ACCEPTED
        action_id = resp.json()["action_id"]

        # Wait for background task to complete.
        from homelab_monitor.kernel.docker.compose_action_runner import (  # noqa: PLC0415
            ComposeActionRunner,
        )

        runner = cast("ComposeActionRunner", app.state.compose_action_runner)
        await asyncio.gather(*runner._active_tasks, return_exceptions=True)  # pyright: ignore[reportPrivateUsage]

        # GET the action.
        detail_resp = await authenticated_client.get(
            f"/api/integrations/docker/compose-actions/{action_id}"
        )
        assert detail_resp.status_code == HTTP_OK
        detail = detail_resp.json()
        assert detail["state"] == "success"
        assert detail["container_name"] == "caddy"
        assert detail["exit_code"] == 0


@pytest.mark.asyncio
async def test_get_compose_action_404_for_unknown_id(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.get("/api/integrations/docker/compose-actions/99999")
    assert resp.status_code == HTTP_NOT_FOUND


@pytest.mark.asyncio
async def test_list_compose_actions_filters_by_container(
    authenticated_client: AsyncClient, repo: SqliteRepository, tmp_path: Path
) -> None:
    """list endpoint returns most-recent first, filtered by container."""
    from homelab_monitor.kernel.db.repositories.compose_actions_repository import (  # noqa: PLC0415
        ComposeActionsRepository,
    )

    r = ComposeActionsRepository(repo)
    await r.insert_running(
        action="pull_and_restart",
        container_name="caddy",
        compose_service="caddy",
        command="cmd",
        started_at="2026-01-01T00:00:00+00:00",
        who="op",
        client_ip=None,
    )
    await r.insert_running(
        action="pull_and_restart",
        container_name="nginx",
        compose_service="nginx",
        command="cmd",
        started_at="2026-01-02T00:00:00+00:00",
        who="op",
        client_ip=None,
    )
    resp = await authenticated_client.get(
        "/api/integrations/docker/compose-actions?container=caddy"
    )
    assert resp.status_code == HTTP_OK
    data = resp.json()
    assert len(data["actions"]) == 1
    assert data["actions"][0]["container_name"] == "caddy"


@pytest.mark.asyncio
async def test_list_compose_actions_requires_container_param(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.get("/api/integrations/docker/compose-actions")
    assert resp.status_code == 422  # fastapi missing-required-query  # noqa: PLR2004


@pytest.mark.asyncio
async def test_pull_and_restart_503_when_runner_not_initialized(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """_get_compose_action_runner raises 503 when compose_action_runner absent from app.state.

    Covers docker.py line 609: the HTTPException(503) branch.
    """
    app = cast("FastAPI", authenticated_client.app)  # type: ignore[attr-defined]
    _set_compose_config(app, tmp_path)
    await _seed_caddy_target(repo)

    # Remove the runner from app.state to trigger the 503 guard.
    # State stores attributes in an internal _state dict; we temporarily
    # overwrite with a sentinel that makes getattr return None.
    original = getattr(app.state, "compose_action_runner", None)
    app.state.compose_action_runner = None  # type: ignore[assignment]

    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    try:
        resp = await authenticated_client.post(
            "/api/integrations/docker/containers/caddy/pull-and-restart",
            json={"confirm_phrase": "pull"},
            headers={"X-CSRF-Token": csrf or ""},
        )
        assert resp.status_code == 503  # noqa: PLR2004
        body = resp.json()
        message = body.get("error", {}).get("message", body.get("detail", ""))
        assert "compose action runner" in message
    finally:
        app.state.compose_action_runner = original


@pytest.mark.asyncio
async def test_pull_and_restart_202_state_failed_when_resolve_returns_none(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    """When resolve_compose returns None, trigger_pull_and_restart inserts a failed row
    synchronously. The 202 response must reflect state='failed'.

    Covers docker.py line 731: the ``state = "failed"`` assignment branch.
    """
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from homelab_monitor.kernel.docker.compose_action_runner import (  # noqa: PLC0415
        ComposeActionRunner,
    )

    app = cast("FastAPI", authenticated_client.app)  # type: ignore[attr-defined]
    _set_compose_config(app, tmp_path)
    await _seed_caddy_target(repo)

    # Mock the docker socket inspect to raise so error-reason detection succeeds cleanly.
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://localhost/containers/caddy/json"),
        status_code=404,
        json={"message": "no such container"},
        is_reusable=True,
    )

    runner = cast("ComposeActionRunner", app.state.compose_action_runner)
    # Patch resolve_compose to return None → pre-resolution failure path.
    original_resolve = runner.resolve_compose
    runner.resolve_compose = AsyncMock(return_value=None)  # type: ignore[method-assign]

    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    try:
        resp = await authenticated_client.post(
            "/api/integrations/docker/containers/caddy/pull-and-restart",
            json={"confirm_phrase": "pull"},
            headers={"X-CSRF-Token": csrf or ""},
        )
        assert resp.status_code == HTTP_ACCEPTED
        data = resp.json()
        assert "action_id" in data
        assert data["state"] == "failed"
    finally:
        runner.resolve_compose = original_resolve  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_pull_and_restart_409_when_action_already_in_flight(
    authenticated_client: AsyncClient,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    """Returns 409 when an active compose_action row exists for the container."""
    from homelab_monitor.kernel.db.repositories.compose_actions_repository import (  # noqa: PLC0415
        ComposeActionsRepository,
    )
    from homelab_monitor.kernel.db.repository import SqliteRepository as _SR  # noqa: PLC0415
    from homelab_monitor.kernel.db.time import utc_now_iso  # noqa: PLC0415

    app = cast("FastAPI", authenticated_client.app)  # type: ignore[attr-defined]
    _set_compose_config(app, tmp_path)

    # Use the app's own repo to seed the target and the in-flight action row.
    app_repo = _SR(app.state.repo._engine)  # pyright: ignore[reportPrivateUsage]
    await _seed_caddy_target(app_repo)

    actions_repo = ComposeActionsRepository(app_repo)
    await actions_repo.insert_running(
        action="pull_and_restart",
        container_name="caddy",
        compose_service="caddy",
        command="docker compose pull caddy",
        started_at=utc_now_iso(),
        who="earlier-user",
        client_ip="10.0.0.1",
    )

    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/docker/containers/caddy/pull-and-restart",
        json={"confirm_phrase": "pull"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == HTTP_CONFLICT
    details = resp.json()["error"]["details"]
    assert details["in_flight_action_id"] is not None
    assert details["container_name"] == "caddy"
    assert details["state"] == "pulling"
