"""API tests for STAGE-006-019 container lifecycle endpoints (restart/start/stop)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from starlette.requests import Request

from homelab_monitor.kernel.api.routers.docker import _client_ip, _who  # type: ignore[attr-defined]
from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.auth.scopes import Scope
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.docker.socket_client import (
    DockerSocketClient,
    DockerSocketConnectionError,
)

if TYPE_CHECKING:
    pass

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_UNAVAILABLE = 503
HTTP_BAD_GATEWAY = 502


class _FakeSocket(DockerSocketClient):
    """Test double subclassing DockerSocketClient.

    Mimics the behavior of the real socket client without opening a real socket.
    """

    def __init__(
        self,
        *,
        inspect_status: str = "running",
        inspect_raises: bool = False,
        action_raises: bool = False,
    ) -> None:
        # Set attributes the methods touch, without calling parent __init__.
        self._socket_path = "/var/run/docker.sock"
        self._client = AsyncMock()
        self._log = AsyncMock()
        self.inspect_status = inspect_status
        self.inspect_raises = inspect_raises
        self.action_raises = action_raises
        self.calls: list[str] = []

    async def inspect_container(self, container_id: str) -> object:  # type: ignore[override]
        """Return mock inspect data or raise."""
        if self.inspect_raises:
            raise DockerSocketConnectionError("inspect boom")
        return {"State": {"Status": self.inspect_status}}

    async def restart_container(
        self, container_id: str, *, timeout_seconds: int | None = None
    ) -> None:
        """Track call and optionally raise."""
        self.calls.append("restart")
        if self.action_raises:
            raise DockerSocketConnectionError("restart boom")

    async def start_container(self, container_id: str) -> None:
        """Track call and optionally raise."""
        self.calls.append("start")
        if self.action_raises:
            raise DockerSocketConnectionError("start boom")

    async def stop_container(
        self, container_id: str, *, timeout_seconds: int | None = None
    ) -> None:
        """Track call and optionally raise."""
        self.calls.append("stop")
        if self.action_raises:
            raise DockerSocketConnectionError("stop boom")

    async def aclose(self) -> None:
        """No-op close."""
        pass


async def _seed_lifecycle_target(repo: SqliteRepository) -> None:
    """Insert a docker target named 'caddy' with container_id."""
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
    # Insert targets_docker row with container_id so list_docker_containers returns it.
    await repo.execute(
        text(
            "INSERT INTO targets_docker "
            "(target_id, container_id) "
            "VALUES ('docker:caddy', 'abc123container')"
        )
    )


@pytest.mark.asyncio
async def test_restart_container_success(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """POST /containers/{name}/restart with correct confirm → 200 + audit."""
    app = cast("FastAPI", authenticated_client.app)  # type: ignore[attr-defined]
    await _seed_lifecycle_target(repo)
    fake_socket = _FakeSocket(inspect_status="running")
    app.state.docker_socket_client = fake_socket

    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/docker/containers/caddy/restart",
        json={"confirm_phrase": "restart"},
        headers={"X-CSRF-Token": csrf or ""},
    )

    assert resp.status_code == HTTP_OK
    data = resp.json()
    assert data["action"] == "restart"
    assert data["container_name"] == "caddy"
    assert data["container_id"] == "abc123container"
    assert "audit_id" in data
    assert fake_socket.calls == ["restart"]

    # Check audit row
    async with repo.transaction() as conn:
        result = await conn.execute(
            text(
                "SELECT who, what, before_json, after_json, ip "
                "FROM audit_log WHERE what = 'docker.container.restart' "
                'ORDER BY "when" DESC LIMIT 1'
            )
        )
        row = result.fetchone()
    assert row is not None
    _who, _what, before_json, after_json, _ip = row
    assert _who == "testuser"
    assert _what == "docker.container.restart"
    assert "running" in before_json
    assert "restart" in after_json


@pytest.mark.asyncio
async def test_start_container_success(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """POST /containers/{name}/start with correct confirm → 200 + audit."""
    app = cast("FastAPI", authenticated_client.app)  # type: ignore[attr-defined]
    await _seed_lifecycle_target(repo)
    fake_socket = _FakeSocket(inspect_status="exited")
    app.state.docker_socket_client = fake_socket

    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/docker/containers/caddy/start",
        json={"confirm_phrase": "start"},
        headers={"X-CSRF-Token": csrf or ""},
    )

    assert resp.status_code == HTTP_OK
    data = resp.json()
    assert data["action"] == "start"
    assert data["container_name"] == "caddy"
    assert fake_socket.calls == ["start"]

    # Check audit row
    async with repo.transaction() as conn:
        result = await conn.execute(
            text(
                "SELECT who, what, before_json, after_json "
                "FROM audit_log WHERE what = 'docker.container.start' "
                'ORDER BY "when" DESC LIMIT 1'
            )
        )
        row = result.fetchone()
    assert row is not None
    _who, _what, before_json, after_json = row
    assert _who == "testuser"
    assert "exited" in before_json
    assert "start" in after_json


@pytest.mark.asyncio
async def test_stop_container_success(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """POST /containers/{name}/stop with correct confirm → 200 + audit."""
    app = cast("FastAPI", authenticated_client.app)  # type: ignore[attr-defined]
    await _seed_lifecycle_target(repo)
    fake_socket = _FakeSocket(inspect_status="running")
    app.state.docker_socket_client = fake_socket

    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/docker/containers/caddy/stop",
        json={"confirm_phrase": "stop"},
        headers={"X-CSRF-Token": csrf or ""},
    )

    assert resp.status_code == HTTP_OK
    data = resp.json()
    assert data["action"] == "stop"
    assert fake_socket.calls == ["stop"]

    # Check audit row
    async with repo.transaction() as conn:
        result = await conn.execute(
            text(
                "SELECT what FROM audit_log WHERE what = 'docker.container.stop' "
                'ORDER BY "when" DESC LIMIT 1'
            )
        )
        row = result.fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_restart_container_confirm_fail_400(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """POST /containers/{name}/restart with wrong confirm phrase → 400."""
    app = cast("FastAPI", authenticated_client.app)  # type: ignore[attr-defined]
    await _seed_lifecycle_target(repo)
    fake_socket = _FakeSocket()
    app.state.docker_socket_client = fake_socket

    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/docker/containers/caddy/restart",
        json={"confirm_phrase": "wrong"},
        headers={"X-CSRF-Token": csrf or ""},
    )

    assert resp.status_code == HTTP_BAD_REQUEST
    assert fake_socket.calls == []

    # No audit row
    async with repo.transaction() as conn:
        result = await conn.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE what = 'docker.container.restart'")
        )
        count = result.scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_start_container_confirm_fail_400(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """POST /containers/{name}/start with wrong confirm → 400."""
    app = cast("FastAPI", authenticated_client.app)  # type: ignore[attr-defined]
    await _seed_lifecycle_target(repo)
    fake_socket = _FakeSocket()
    app.state.docker_socket_client = fake_socket

    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/docker/containers/caddy/start",
        json={"confirm_phrase": "nope"},
        headers={"X-CSRF-Token": csrf or ""},
    )

    assert resp.status_code == HTTP_BAD_REQUEST
    assert fake_socket.calls == []


@pytest.mark.asyncio
async def test_confirm_fail_beats_503_socket_unavailable(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Wrong confirm phrase → 400 even if socket is unavailable (ordering check)."""
    app = cast("FastAPI", authenticated_client.app)  # type: ignore[attr-defined]
    await _seed_lifecycle_target(repo)
    # Socket not available
    if hasattr(app.state, "docker_socket_client"):
        delattr(app.state, "docker_socket_client")

    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/docker/containers/caddy/restart",
        json={"confirm_phrase": "wrong"},
        headers={"X-CSRF-Token": csrf or ""},
    )

    assert resp.status_code == HTTP_BAD_REQUEST


@pytest.mark.asyncio
async def test_restart_container_403_scope_rejection(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch, repo: SqliteRepository
) -> None:
    """Token without DOCKER_WRITE scope → 403."""
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
        await _seed_lifecycle_target(repo)
        fake_socket = _FakeSocket()
        app.state.docker_socket_client = fake_socket

        plaintext, _ = make_api_token()
        await app.state.auth_repo.create_api_token(
            name="read-only-token",
            scopes={Scope.READ_STATUS},
            plaintext_token=plaintext,
        )

        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        resp = await client.post(
            "/api/integrations/docker/containers/caddy/restart",
            json={"confirm_phrase": "restart"},
            headers={"Authorization": f"Bearer {plaintext}"},
        )
        assert resp.status_code == HTTP_FORBIDDEN
        assert fake_socket.calls == []


@pytest.mark.asyncio
async def test_restart_container_404_not_found(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """POST /containers/unknown/restart → 404."""
    app = cast("FastAPI", authenticated_client.app)  # type: ignore[attr-defined]
    await _seed_lifecycle_target(repo)
    fake_socket = _FakeSocket()
    app.state.docker_socket_client = fake_socket

    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/docker/containers/nonexistent/restart",
        json={"confirm_phrase": "restart"},
        headers={"X-CSRF-Token": csrf or ""},
    )

    assert resp.status_code == HTTP_NOT_FOUND
    assert fake_socket.calls == []

    # No audit row
    async with repo.transaction() as conn:
        result = await conn.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE what LIKE 'docker.container.%'")
        )
        count = result.scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_restart_container_502_inspect_error(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """POST /containers/{name}/restart with inspect failure → 502, no audit."""
    app = cast("FastAPI", authenticated_client.app)  # type: ignore[attr-defined]
    await _seed_lifecycle_target(repo)
    fake_socket = _FakeSocket(inspect_raises=True)
    app.state.docker_socket_client = fake_socket

    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/docker/containers/caddy/restart",
        json={"confirm_phrase": "restart"},
        headers={"X-CSRF-Token": csrf or ""},
    )

    assert resp.status_code == HTTP_BAD_GATEWAY
    assert fake_socket.calls == []

    # No audit row
    async with repo.transaction() as conn:
        result = await conn.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE what = 'docker.container.restart'")
        )
        count = result.scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_stop_container_502_action_error(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """POST /containers/{name}/stop with action failure → 502, no audit."""
    app = cast("FastAPI", authenticated_client.app)  # type: ignore[attr-defined]
    await _seed_lifecycle_target(repo)
    fake_socket = _FakeSocket(action_raises=True)
    app.state.docker_socket_client = fake_socket

    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/docker/containers/caddy/stop",
        json={"confirm_phrase": "stop"},
        headers={"X-CSRF-Token": csrf or ""},
    )

    assert resp.status_code == HTTP_BAD_GATEWAY
    # Inspect was called, action was attempted
    assert "stop" in fake_socket.calls

    # No audit row
    async with repo.transaction() as conn:
        result = await conn.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE what = 'docker.container.stop'")
        )
        count = result.scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_restart_container_503_socket_unavailable(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """POST /containers/{name}/restart with no socket → 503."""
    app = cast("FastAPI", authenticated_client.app)  # type: ignore[attr-defined]
    await _seed_lifecycle_target(repo)
    if hasattr(app.state, "docker_socket_client"):
        delattr(app.state, "docker_socket_client")

    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/docker/containers/caddy/restart",
        json={"confirm_phrase": "restart"},
        headers={"X-CSRF-Token": csrf or ""},
    )

    assert resp.status_code == HTTP_UNAVAILABLE


def test_who_user_branch() -> None:
    """_who helper: User principal → username."""
    user = User(id=1, username="testuser", created_at="2026-01-01T00:00:00")
    assert _who(user) == "testuser"


@pytest.mark.asyncio
async def test_who_token_branch(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch, repo: SqliteRepository
) -> None:
    """_who helper: ApiToken principal → 'token:<name>'."""
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
        await _seed_lifecycle_target(repo)
        fake_socket = _FakeSocket()
        app.state.docker_socket_client = fake_socket

        plaintext, _ = make_api_token()
        await app.state.auth_repo.create_api_token(
            name="test-docker-token",
            scopes={Scope.DOCKER_WRITE},
            plaintext_token=plaintext,
        )

        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        resp = await client.post(
            "/api/integrations/docker/containers/caddy/restart",
            json={"confirm_phrase": "restart"},
            headers={"Authorization": f"Bearer {plaintext}"},
        )
        assert resp.status_code == HTTP_OK
        data = resp.json()
        assert "audit_id" in data

        # Check audit row has token:<name> in who field
        async with repo.transaction() as conn:
            result = await conn.execute(
                text(
                    "SELECT who FROM audit_log WHERE what = 'docker.container.restart' "
                    'ORDER BY "when" DESC LIMIT 1'
                )
            )
            row = result.fetchone()
        assert row is not None
        (_who,) = row
        assert _who == "token:test-docker-token"


def test_client_ip_present() -> None:
    """_client_ip helper: request.client present → returns host."""
    scope: dict[str, object] = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [],
        "client": ("1.2.3.4", 12345),
    }
    request = Request(scope)  # pyright: ignore[reportArgumentType]
    assert _client_ip(request) == "1.2.3.4"


def test_client_ip_absent() -> None:
    """_client_ip helper: request.client None → returns None."""
    scope: dict[str, object] = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [],
    }
    request = Request(scope)  # pyright: ignore[reportArgumentType]
    assert _client_ip(request) is None


@pytest.mark.asyncio
async def test_restart_container_case_insensitive_confirm(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Confirm phrase is case-insensitive and whitespace-tolerant."""
    app = cast("FastAPI", authenticated_client.app)  # type: ignore[attr-defined]
    await _seed_lifecycle_target(repo)
    fake_socket = _FakeSocket()
    app.state.docker_socket_client = fake_socket

    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/docker/containers/caddy/restart",
        json={"confirm_phrase": "  RESTART  "},
        headers={"X-CSRF-Token": csrf or ""},
    )

    assert resp.status_code == HTTP_OK
    assert fake_socket.calls == ["restart"]
