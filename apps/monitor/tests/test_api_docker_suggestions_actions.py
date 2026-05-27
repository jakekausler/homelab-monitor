"""Tests for STAGE-003-012 Accept/Customize/Ignore endpoints."""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from pytest_httpx import HTTPXMock
from sqlalchemy import text

from homelab_monitor.kernel.api.routers.docker import (
    _default_probes_from_inspect,  # pyright: ignore[reportPrivateUsage]
    _get_docker_socket_client,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.db.repositories.probe_targets_repository import (
    ProbeTargetsRepository,
)
from homelab_monitor.kernel.db.repositories.suggestions_repository import (
    SuggestionsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.docker.socket_client import DockerSocketClient

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_UNPROCESSABLE_ENTITY = 422

# Expected probe count: 2 tcp probes (ports 8080, 443) + 1 exec probe (healthcheck)
_EXPECTED_PROBES_COUNT = 3


@pytest.fixture(autouse=True)
def _mock_vm_lifespan_tick(httpx_mock: HTTPXMock) -> None:  # pyright: ignore[reportUnusedFunction]
    """Copy of the mock from test_api_docker_suggestions.py — prevents lifespan contamination."""
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
    container_id: str = "cid_test",
    container_name: str = "test-container",
    image_ref: str = "test:latest",
    kind: str = "docker_container_discovered",
    detection_reason: str = "no_homelab_monitor_label",
    state: str = "pending",
    labels: dict[str, str] | None = None,
) -> str:
    """Seed a suggestion via the production upsert path."""
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
            compose_project=None,
            compose_service=None,
            compose_file_path=None,
            detection_reason=detection_reason,
            now=now,
        )
        if state != "pending":
            await conn.execute(
                text("UPDATE suggestions SET state = :state WHERE id = :id"),
                {"state": state, "id": suggestion_id},
            )
    return suggestion_id


def _inspect_with_ports_and_healthcheck() -> dict[str, Any]:
    return {
        "Id": "cid_test",
        "Name": "/test-container",
        "Image": "test:latest",
        "State": {"Status": "running"},
        "RestartCount": 0,
        "Config": {
            "ExposedPorts": {"8080/tcp": {}, "443/tcp": {}},
            "Healthcheck": {
                "Test": ["CMD-SHELL", "curl -f http://localhost:8080/health"],
            },
        },
        "HostConfig": {
            "PortBindings": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]},
            "NetworkMode": "default",
        },
    }


def _inspect_no_ports() -> dict[str, Any]:
    inspect = _inspect_with_ports_and_healthcheck()
    inspect["Config"]["ExposedPorts"] = {}
    inspect["HostConfig"]["PortBindings"] = {}
    return inspect


# ============================================================================
# Accept endpoint tests
# ============================================================================


@pytest.mark.asyncio
async def test_accept_pending_no_ports_no_healthcheck_creates_no_probes(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Accept on container with no ports or no healthcheck creates no probes."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_1")

    with patch(
        "homelab_monitor.kernel.api.routers.docker._get_docker_socket_client"
    ) as mock_get_client:
        mock_client = AsyncMock()
        mock_client.inspect_container = AsyncMock(return_value=_inspect_no_ports())
        mock_get_client.return_value = mock_client

        response = await authenticated_client.post(
            f"/api/integrations/docker/suggestions/{suggestion_id}/accept",
            json={"apply_default_probes": True},
            headers={
                "X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""
            },
        )

    assert response.status_code == HTTP_OK
    data = response.json()
    assert data["suggestion"]["state"] == "accepted"
    assert data["probes_created"] == 0


@pytest.mark.asyncio
async def test_accept_pending_with_ports_and_healthcheck_creates_probes(
    authenticated_client: AsyncClient, repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patch DockerSocketClient.inspect_container; expect 2 tcp + 1 exec = 3 probes."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_2")

    mock_client = AsyncMock()
    mock_client.inspect_container = AsyncMock(return_value=_inspect_with_ports_and_healthcheck())
    monkeypatch.setattr(
        "homelab_monitor.kernel.api.routers.docker._get_docker_socket_client",
        lambda _: mock_client,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )

    response = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/accept",
        json={"apply_default_probes": True},
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_OK
    data = response.json()
    assert data["suggestion"]["state"] == "accepted"
    assert data["probes_created"] == _EXPECTED_PROBES_COUNT  # 2 tcp + 1 exec


@pytest.mark.asyncio
async def test_accept_with_apply_default_probes_false_creates_no_probes(
    authenticated_client: AsyncClient, repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When apply_default_probes=false, no probes created even if docker socket available."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_3")

    mock_client = AsyncMock()
    mock_client.inspect_container = AsyncMock(return_value=_inspect_with_ports_and_healthcheck())
    monkeypatch.setattr(
        "homelab_monitor.kernel.api.routers.docker._get_docker_socket_client",
        lambda _: mock_client,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )

    response = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/accept",
        json={"apply_default_probes": False},
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_OK
    data = response.json()
    assert data["suggestion"]["state"] == "accepted"
    assert data["probes_created"] == 0


@pytest.mark.asyncio
async def test_accept_already_accepted_is_noop(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Accept twice — second call returns 200, no duplicate probe rows."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_4", state="accepted")

    response = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/accept",
        json={"apply_default_probes": True},
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_OK
    data = response.json()
    assert data["suggestion"]["state"] == "accepted"
    assert data["probes_created"] == 0


@pytest.mark.asyncio
async def test_accept_ignored_returns_409(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Accept on ignored suggestion returns 409."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_5", state="ignored")

    response = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/accept",
        json={"apply_default_probes": True},
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_CONFLICT


@pytest.mark.asyncio
async def test_accept_container_gone_returns_409(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Accept on container_gone suggestion returns 409."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_6", state="container_gone")

    response = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/accept",
        json={"apply_default_probes": True},
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_CONFLICT


@pytest.mark.asyncio
async def test_accept_missing_returns_404(authenticated_client: AsyncClient) -> None:
    """Accept on non-existent suggestion returns 404."""
    response = await authenticated_client.post(
        "/api/integrations/docker/suggestions/nonexistent/accept",
        json={"apply_default_probes": True},
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_NOT_FOUND


@pytest.mark.asyncio
async def test_accept_unauthenticated_returns_401(
    unauthenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Accept without authentication returns 401."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_7")

    response = await unauthenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/accept",
        json={"apply_default_probes": True},
    )

    assert response.status_code == HTTP_UNAUTHORIZED


@pytest.mark.asyncio
async def test_accept_writes_audit_log(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """SELECT FROM audit_log WHERE what='docker.suggestion.accept' → 1 row."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_8")

    response = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/accept",
        json={"apply_default_probes": False},
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_OK

    # Check audit log
    async with repo.transaction() as conn:
        rows = await conn.execute(
            text("SELECT what, who FROM audit_log WHERE what = 'docker.suggestion.accept'")
        )
        audit_rows = rows.fetchall()
        assert len(audit_rows) >= 1


# ============================================================================
# Customize endpoint tests
# ============================================================================


@pytest.mark.asyncio
async def test_customize_pending_creates_probes_and_accepts(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Customize on pending suggestion creates probes and transitions to accepted."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_9")

    response = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/customize",
        json={
            "probes": [
                {
                    "kind": "http",
                    "name": "health",
                    "target_value": "http://localhost:8080/health",
                    "interval_seconds": 60,
                    "timeout_seconds": 10,
                }
            ]
        },
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_OK
    data = response.json()
    assert data["suggestion"]["state"] == "accepted"
    assert data["probes_created"] == 1
    assert data["probes_updated"] == 0


@pytest.mark.asyncio
async def test_customize_already_accepted_upserts(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Customize twice with same probe (kind,name) but different target_value.

    Second call should reflect updated target_value via probes_updated=1.
    """
    suggestion_id = await _seed_suggestion(repo, container_id="cid_10", state="accepted")

    # First customize
    response1 = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/customize",
        json={
            "probes": [
                {
                    "kind": "http",
                    "name": "health",
                    "target_value": "http://localhost:8080/health",
                    "interval_seconds": 60,
                    "timeout_seconds": 10,
                }
            ]
        },
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response1.status_code == HTTP_OK
    data1 = response1.json()
    assert data1["probes_created"] == 1

    # Second customize with same (kind, name) but different target
    response2 = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/customize",
        json={
            "probes": [
                {
                    "kind": "http",
                    "name": "health",
                    "target_value": "http://localhost:8080/healthz",
                    "interval_seconds": 30,
                    "timeout_seconds": 5,
                }
            ]
        },
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response2.status_code == HTTP_OK
    data2 = response2.json()
    assert data2["probes_created"] == 0
    assert data2["probes_updated"] == 1


@pytest.mark.asyncio
async def test_customize_duplicate_kind_name_in_body_returns_422(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Duplicate (kind, name) in request body returns 422."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_11")

    response = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/customize",
        json={
            "probes": [
                {
                    "kind": "http",
                    "name": "health",
                    "target_value": "http://localhost:8080/health",
                    "interval_seconds": 60,
                    "timeout_seconds": 10,
                },
                {
                    "kind": "http",
                    "name": "health",
                    "target_value": "http://localhost:8080/healthz",
                    "interval_seconds": 60,
                    "timeout_seconds": 10,
                },
            ]
        },
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY


@pytest.mark.asyncio
async def test_customize_invalid_name_regex_returns_422(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Names with spaces, dots, or > 64 chars rejected at FastAPI validation layer."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_12")

    response = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/customize",
        json={
            "probes": [
                {
                    "kind": "http",
                    "name": "health probe",  # Space not allowed
                    "target_value": "http://localhost:8080/health",
                    "interval_seconds": 60,
                    "timeout_seconds": 10,
                }
            ]
        },
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY


@pytest.mark.asyncio
async def test_customize_interval_out_of_range_returns_422(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Interval outside 1-3600 returns 422."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_13")

    response = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/customize",
        json={
            "probes": [
                {
                    "kind": "http",
                    "name": "health",
                    "target_value": "http://localhost:8080/health",
                    "interval_seconds": 5000,  # > 3600
                    "timeout_seconds": 10,
                }
            ]
        },
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY


@pytest.mark.asyncio
async def test_customize_empty_probes_list_returns_422(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """ProbeSpec list min_length=1."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_14")

    response = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/customize",
        json={"probes": []},
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY


@pytest.mark.asyncio
async def test_customize_ignored_returns_409(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Customize on ignored suggestion returns 409."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_15", state="ignored")

    response = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/customize",
        json={
            "probes": [
                {
                    "kind": "http",
                    "name": "health",
                    "target_value": "http://localhost:8080/health",
                    "interval_seconds": 60,
                    "timeout_seconds": 10,
                }
            ]
        },
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_CONFLICT


@pytest.mark.asyncio
async def test_customize_container_gone_returns_409(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Customize on container_gone suggestion returns 409."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_16", state="container_gone")

    response = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/customize",
        json={
            "probes": [
                {
                    "kind": "http",
                    "name": "health",
                    "target_value": "http://localhost:8080/health",
                    "interval_seconds": 60,
                    "timeout_seconds": 10,
                }
            ]
        },
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_CONFLICT


@pytest.mark.asyncio
async def test_customize_missing_returns_404(authenticated_client: AsyncClient) -> None:
    """Customize on non-existent suggestion returns 404."""
    response = await authenticated_client.post(
        "/api/integrations/docker/suggestions/nonexistent/customize",
        json={
            "probes": [
                {
                    "kind": "http",
                    "name": "health",
                    "target_value": "http://localhost:8080/health",
                    "interval_seconds": 60,
                    "timeout_seconds": 10,
                }
            ]
        },
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_NOT_FOUND


@pytest.mark.asyncio
async def test_customize_unauthenticated_returns_401(
    unauthenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Customize without authentication returns 401."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_17")

    response = await unauthenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/customize",
        json={
            "probes": [
                {
                    "kind": "http",
                    "name": "health",
                    "target_value": "http://localhost:8080/health",
                    "interval_seconds": 60,
                    "timeout_seconds": 10,
                }
            ]
        },
    )

    assert response.status_code == HTTP_UNAUTHORIZED


@pytest.mark.asyncio
async def test_customize_writes_audit_log(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """SELECT FROM audit_log WHERE what='docker.suggestion.customize' → 1 row."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_18")

    response = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/customize",
        json={
            "probes": [
                {
                    "kind": "http",
                    "name": "health",
                    "target_value": "http://localhost:8080/health",
                    "interval_seconds": 60,
                    "timeout_seconds": 10,
                }
            ]
        },
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_OK

    # Check audit log
    async with repo.transaction() as conn:
        rows = await conn.execute(
            text("SELECT what, who FROM audit_log WHERE what = 'docker.suggestion.customize'")
        )
        audit_rows = rows.fetchall()
        assert len(audit_rows) >= 1


# ============================================================================
# Ignore endpoint tests
# ============================================================================


@pytest.mark.asyncio
async def test_ignore_pending_transitions_to_ignored(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Ignore on pending suggestion transitions it to ignored."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_19")

    response = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/ignore",
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_OK
    data = response.json()
    assert data["suggestion"]["state"] == "ignored"


@pytest.mark.asyncio
async def test_ignore_accepted_transitions_but_keeps_probes(
    authenticated_client: AsyncClient, repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-NO-PROBE-MISSING-ON-IGNORE — accept first (creating probes), ignore,
    verify probe_targets rows are still present and not hidden."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_20", state="accepted")

    # First create a probe manually (simulating accept)
    now = utc_now_iso()
    async with repo.transaction() as conn:
        await ProbeTargetsRepository.upsert_probe_target_conn(
            conn,
            container_name="test-container",
            kind="http",
            name="health",
            target_value="http://localhost:8080/health",
            config_source="discovered_accepted",
            enabled=True,
            interval_seconds=60,
            timeout_seconds=10,
            now=now,
        )

    # Now ignore
    response = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/ignore",
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_OK
    data = response.json()
    assert data["suggestion"]["state"] == "ignored"

    # Verify probes still exist (not hidden)
    probes_repo = ProbeTargetsRepository(repo)
    probes = await probes_repo.list_for_container(
        container_name="test-container", include_hidden=False
    )
    assert len(probes) == 1
    assert probes[0].name == "health"


@pytest.mark.asyncio
async def test_ignore_already_ignored_is_noop(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Ignore twice — second call returns 200 no-op."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_21", state="ignored")

    response = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/ignore",
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_OK
    data = response.json()
    assert data["suggestion"]["state"] == "ignored"


@pytest.mark.asyncio
async def test_ignore_container_gone_transitions_to_ignored(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Ignore on container_gone suggestion transitions it to ignored."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_22", state="container_gone")

    response = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/ignore",
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_OK
    data = response.json()
    assert data["suggestion"]["state"] == "ignored"


@pytest.mark.asyncio
async def test_ignore_missing_returns_404(authenticated_client: AsyncClient) -> None:
    """Ignore on non-existent suggestion returns 404."""
    response = await authenticated_client.post(
        "/api/integrations/docker/suggestions/nonexistent/ignore",
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_NOT_FOUND


@pytest.mark.asyncio
async def test_ignore_unauthenticated_returns_401(
    unauthenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Ignore without authentication returns 401."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_23")

    response = await unauthenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/ignore",
    )

    assert response.status_code == HTTP_UNAUTHORIZED


@pytest.mark.asyncio
async def test_ignore_writes_audit_log(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """SELECT FROM audit_log WHERE what='docker.suggestion.ignore' → 1 row."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid_24")

    response = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/ignore",
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_OK

    # Check audit log
    async with repo.transaction() as conn:
        rows = await conn.execute(
            text("SELECT what, who FROM audit_log WHERE what = 'docker.suggestion.ignore'")
        )
        audit_rows = rows.fetchall()
        assert len(audit_rows) >= 1


# ============================================================================
# Default-probes endpoint tests (GET /suggestions/{id}/default-probes)
# ============================================================================


@pytest.mark.asyncio
async def test_default_probes_returns_available_when_container_has_ports_and_healthcheck(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default-probes returns probes when container has exposed ports + healthcheck."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid-defaults-1")

    mock_client = AsyncMock()
    mock_client.inspect_container = AsyncMock(return_value=_inspect_with_ports_and_healthcheck())
    monkeypatch.setattr(
        "homelab_monitor.kernel.api.routers.docker._get_docker_socket_client",
        lambda _: mock_client,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )

    response = await authenticated_client.get(
        f"/api/integrations/docker/suggestions/{suggestion_id}/default-probes",
    )

    assert response.status_code == HTTP_OK
    data = response.json()
    assert data["reason"] == "available"
    assert len(data["probes"]) >= 1
    assert data["probes"][0]["kind"] in ("tcp", "exec")


@pytest.mark.asyncio
async def test_default_probes_returns_no_ports_no_healthcheck_when_inspect_empty(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default-probes returns no_ports_no_healthcheck when inspect has no ports or healthcheck."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid-defaults-2")

    inspect_no_hc: dict[str, Any] = {
        "Config": {"ExposedPorts": {}, "Healthcheck": None},
        "HostConfig": {"PortBindings": {}},
    }
    mock_client = AsyncMock()
    mock_client.inspect_container = AsyncMock(return_value=inspect_no_hc)
    monkeypatch.setattr(
        "homelab_monitor.kernel.api.routers.docker._get_docker_socket_client",
        lambda _: mock_client,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )

    response = await authenticated_client.get(
        f"/api/integrations/docker/suggestions/{suggestion_id}/default-probes",
    )

    assert response.status_code == HTTP_OK
    data = response.json()
    assert data["reason"] == "no_ports_no_healthcheck"
    assert data["probes"] == []


@pytest.mark.asyncio
async def test_default_probes_returns_docker_unavailable_when_no_client(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default-probes returns docker_unavailable when _get_docker_socket_client returns None."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid-defaults-3")

    monkeypatch.setattr(
        "homelab_monitor.kernel.api.routers.docker._get_docker_socket_client",
        lambda _: None,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )

    response = await authenticated_client.get(
        f"/api/integrations/docker/suggestions/{suggestion_id}/default-probes",
    )

    assert response.status_code == HTTP_OK
    data = response.json()
    assert data["reason"] == "docker_unavailable"
    assert data["probes"] == []


@pytest.mark.asyncio
async def test_default_probes_returns_container_gone_when_inspect_raises(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default-probes returns container_gone when inspect_container raises."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid-defaults-4")

    mock_client = AsyncMock()
    mock_client.inspect_container = AsyncMock(side_effect=RuntimeError("docker error"))
    monkeypatch.setattr(
        "homelab_monitor.kernel.api.routers.docker._get_docker_socket_client",
        lambda _: mock_client,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )

    response = await authenticated_client.get(
        f"/api/integrations/docker/suggestions/{suggestion_id}/default-probes",
    )

    assert response.status_code == HTTP_OK
    data = response.json()
    assert data["reason"] == "container_gone"
    assert data["probes"] == []


@pytest.mark.asyncio
async def test_default_probes_returns_404_for_missing_suggestion(
    authenticated_client: AsyncClient,
) -> None:
    """Default-probes returns 404 for a non-existent suggestion_id."""
    response = await authenticated_client.get(
        "/api/integrations/docker/suggestions/nonexistent/default-probes",
    )

    assert response.status_code == HTTP_NOT_FOUND


@pytest.mark.asyncio
async def test_default_probes_unauthenticated_returns_401(
    unauthenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Default-probes without authentication returns 401."""
    suggestion_id = await _seed_suggestion(repo, container_id="cid-defaults-5")

    response = await unauthenticated_client.get(
        f"/api/integrations/docker/suggestions/{suggestion_id}/default-probes",
    )

    assert response.status_code == HTTP_UNAUTHORIZED


# ============================================================================
# Unit tests for _default_probes_from_inspect (branch coverage)
# ============================================================================

_EXPECTED_PORT_AND_HC = 2  # 1 tcp + 1 exec


def test_probes_from_inspect_missing_config_returns_empty() -> None:
    """Config or HostConfig not a dict → returns [] immediately."""
    probes = _default_probes_from_inspect({"Config": None, "HostConfig": None})
    assert probes == []


def test_probes_from_inspect_portbindings_none() -> None:
    """PortBindings=None → falls back to ExposedPorts only."""
    inspect: dict[str, Any] = {
        "Config": {
            "ExposedPorts": {"8080/tcp": {}},
            "Healthcheck": {"Test": ["CMD-SHELL", "curl localhost"]},
        },
        "HostConfig": {"PortBindings": None},
    }
    probes = _default_probes_from_inspect(inspect)
    assert len(probes) == _EXPECTED_PORT_AND_HC


def test_probes_from_inspect_invalid_port_in_bindings() -> None:
    """PortBindings with an un-parseable key → that key is skipped, valid key used."""
    inspect: dict[str, Any] = {
        "Config": {
            "ExposedPorts": {},
            "Healthcheck": {"Test": ["CMD-SHELL", "curl localhost"]},
        },
        "HostConfig": {"PortBindings": {"invalid": [{}], "8080/tcp": [{}]}},
    }
    probes = _default_probes_from_inspect(inspect)
    assert len(probes) == _EXPECTED_PORT_AND_HC


def test_probes_from_inspect_exposedports_none() -> None:
    """ExposedPorts=None → falls back to PortBindings only."""
    inspect: dict[str, Any] = {
        "Config": {
            "ExposedPorts": None,
            "Healthcheck": {"Test": ["CMD-SHELL", "curl localhost"]},
        },
        "HostConfig": {"PortBindings": {"8080/tcp": [{}]}},
    }
    probes = _default_probes_from_inspect(inspect)
    assert len(probes) == _EXPECTED_PORT_AND_HC


def test_probes_from_inspect_invalid_port_in_exposed() -> None:
    """ExposedPorts with an un-parseable key → that key is skipped, valid key used."""
    inspect: dict[str, Any] = {
        "Config": {
            "ExposedPorts": {"invalid": {}, "8080/tcp": {}},
            "Healthcheck": {"Test": ["CMD-SHELL", "curl localhost"]},
        },
        "HostConfig": {"PortBindings": None},
    }
    probes = _default_probes_from_inspect(inspect)
    assert len(probes) == _EXPECTED_PORT_AND_HC


def test_probes_from_inspect_no_healthcheck_dict() -> None:
    """Healthcheck=None → gate fails, returns []."""
    inspect: dict[str, Any] = {
        "Config": {
            "ExposedPorts": {"8080/tcp": {}},
            "Healthcheck": None,
        },
        "HostConfig": {"PortBindings": None},
    }
    probes = _default_probes_from_inspect(inspect)
    assert probes == []


def test_probes_from_inspect_healthcheck_test_none() -> None:
    """Healthcheck.Test=None → gate fails, returns []."""
    inspect: dict[str, Any] = {
        "Config": {
            "ExposedPorts": {"8080/tcp": {}},
            "Healthcheck": {"Test": None},
        },
        "HostConfig": {"PortBindings": None},
    }
    probes = _default_probes_from_inspect(inspect)
    assert probes == []


def test_probes_from_inspect_healthcheck_test_empty() -> None:
    """Healthcheck.Test=[] → gate fails, returns []."""
    inspect: dict[str, Any] = {
        "Config": {
            "ExposedPorts": {"8080/tcp": {}},
            "Healthcheck": {"Test": []},
        },
        "HostConfig": {"PortBindings": None},
    }
    probes = _default_probes_from_inspect(inspect)
    assert probes == []


def test_probes_from_inspect_healthcheck_cmd_form() -> None:
    """Healthcheck Test[0]=='CMD' → exec probe with shlex-quoted joined args."""
    inspect: dict[str, Any] = {
        "Config": {
            "ExposedPorts": {"8080/tcp": {}},
            "Healthcheck": {"Test": ["CMD", "/bin/sh", "-c", "curl localhost"]},
        },
        "HostConfig": {"PortBindings": None},
    }
    probes = _default_probes_from_inspect(inspect)
    assert len(probes) == _EXPECTED_PORT_AND_HC
    exec_probes = [p for p in probes if p.kind == "exec"]
    assert len(exec_probes) == 1
    assert "curl" in exec_probes[0].target_value


def test_probes_from_inspect_healthcheck_none_form() -> None:
    """Healthcheck Test[0]=='NONE' → explicit no-healthcheck, gate fails, returns []."""
    inspect: dict[str, Any] = {
        "Config": {
            "ExposedPorts": {"8080/tcp": {}},
            "Healthcheck": {"Test": ["NONE"]},
        },
        "HostConfig": {"PortBindings": None},
    }
    probes = _default_probes_from_inspect(inspect)
    assert probes == []


# ============================================================================
# Unit test for _get_docker_socket_client (lines 1051-1056)
# ============================================================================


def test_get_docker_socket_client_all_return_paths() -> None:
    """Direct unit test covering all 3 return paths of _get_docker_socket_client.

    Covers lines 1051-1056: attribute missing → None, wrong type → None,
    correct DockerSocketClient instance → returned as-is.
    """
    # Case 1: app.state has no docker_socket_client attribute → None
    request_no_attr = MagicMock()
    request_no_attr.app.state = MagicMock(spec=[])  # spec=[] → no attributes
    assert _get_docker_socket_client(request_no_attr) is None

    # Case 2: attribute is wrong type → None
    request_wrong_type = MagicMock()
    request_wrong_type.app.state.docker_socket_client = "not-a-client"
    assert _get_docker_socket_client(request_wrong_type) is None

    # Case 3: attribute is a DockerSocketClient → returned
    real_client = MagicMock(spec=DockerSocketClient)
    request_ok = MagicMock()
    request_ok.app.state.docker_socket_client = real_client
    result = _get_docker_socket_client(request_ok)
    assert result is real_client


# ============================================================================
# Endpoint test: inspect_container raises (lines 1199-1200)
# ============================================================================


@pytest.mark.asyncio
async def test_accept_inspect_container_exception_swallowed_probes_zero(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """inspect_container raises RuntimeError → exception caught, probes_created=0, accepted.

    Covers lines 1199-1200 (except Exception: probes_to_insert = []).
    """
    suggestion_id = await _seed_suggestion(
        repo,
        container_id="cid-exc-1",
        container_name="test-inspect-raises",
        state="pending",
    )

    mock_client = AsyncMock()
    mock_client.inspect_container = AsyncMock(side_effect=RuntimeError("docker socket error"))
    monkeypatch.setattr(
        "homelab_monitor.kernel.api.routers.docker._get_docker_socket_client",
        lambda _: mock_client,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )

    response = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/accept",
        json={"apply_default_probes": True},
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_OK
    data = response.json()
    assert data["suggestion"]["state"] == "accepted"
    assert data["probes_created"] == 0


@pytest.mark.asyncio
async def test_accept_apply_default_probes_true_but_no_docker_client_creates_no_probes(
    authenticated_client: AsyncClient, repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When apply_default_probes=True but _get_docker_socket_client returns None, no probes created.

    Covers line 1195->1202 branch (client is None path).
    """
    suggestion_id = await _seed_suggestion(
        repo,
        container_id="cid-no-client-1",
        container_name="test-no-client",
        state="pending",
    )

    monkeypatch.setattr(
        "homelab_monitor.kernel.api.routers.docker._get_docker_socket_client",
        lambda _: None,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )

    response = await authenticated_client.post(
        f"/api/integrations/docker/suggestions/{suggestion_id}/accept",
        json={"apply_default_probes": True},
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf") or ""},
    )

    assert response.status_code == HTTP_OK
    data = response.json()
    assert data["suggestion"]["state"] == "accepted"
    assert data["probes_created"] == 0
