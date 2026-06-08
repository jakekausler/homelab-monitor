"""Tests for user-rules API endpoints: CRUD, enable/disable, rendering, CSRF."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from httpx import AsyncClient

from homelab_monitor.kernel.db.repository import SqliteRepository


def _csrf(client: AsyncClient) -> dict[str, str]:
    """Extract CSRF token from client cookies."""
    csrf: str = client.cookies.get("homelab_monitor_csrf") or ""
    return {"X-CSRF-Token": csrf}


async def test_list_user_rules_empty(authenticated_client: AsyncClient) -> None:
    """GET /api/logs/user-rules empty returns 200 with rules=[]."""
    response = await authenticated_client.get("/api/logs/user-rules")
    assert response.status_code == 200  # noqa: PLR2004
    data = response.json()
    assert data["rules"] == []


async def test_create_user_rule_201(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST valid rule returns 201 with id>0, enabled=True, source_kind='manual'."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    response = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "TestRule",
            "expr": "_msg:error",
            "expr_kind": "logsql",
            "severity": "warning",
            "summary": "Test alert",
        },
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 201  # noqa: PLR2004
    data = response.json()
    assert data["id"] > 0
    assert data["rule_name"] == "TestRule"
    assert data["expr"] == "_msg:error"
    assert data["expr_kind"] == "logsql"
    assert data["enabled"] is True
    assert data["source_kind"] == "manual"
    # Verify render_all was called: logs.yaml should exist
    logs_file = tmp_path / "logs" / "logs.yaml"
    assert logs_file.exists()
    doc = yaml.safe_load(logs_file.read_text())
    assert any(r["alert"] == "TestRule" for r in doc["groups"][0]["rules"] if doc["groups"])


async def test_create_user_rule_duplicate_409(authenticated_client: AsyncClient) -> None:
    """POST /api/logs/user-rules with duplicate rule_name returns 409."""
    csrf = _csrf(authenticated_client)
    # Create first
    await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "Duplicate",
            "expr": "_msg:error",
            "expr_kind": "logsql",
            "severity": "warning",
            "summary": "First",
        },
        headers=csrf,
    )
    # Attempt duplicate
    response = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "Duplicate",
            "expr": "_msg:other",
            "expr_kind": "logsql",
            "severity": "info",
            "summary": "Second",
        },
        headers=csrf,
    )
    assert response.status_code == 409  # noqa: PLR2004
    assert "already exists" in response.json()["error"]["message"]


async def test_create_user_rule_invalid_name_400(authenticated_client: AsyncClient) -> None:
    """POST /api/logs/user-rules with invalid rule_name returns 400."""
    response = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "1bad",  # Starts with digit; pydantic passes, repo rejects
            "expr": "_msg:error",
            "expr_kind": "logsql",
            "severity": "warning",
            "summary": "Bad name",
        },
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 400  # noqa: PLR2004
    assert "invalid_rule" in response.json()["error"]["code"]


async def test_get_user_rule_200(authenticated_client: AsyncClient) -> None:
    """GET /api/logs/user-rules/{id} returns 200 with rule data."""
    csrf = _csrf(authenticated_client)
    created = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "GetTest",
            "expr": "_msg:boom",
            "expr_kind": "logsql",
            "severity": "critical",
            "summary": "Get test",
        },
        headers=csrf,
    )
    rule_id = created.json()["id"]
    response = await authenticated_client.get(f"/api/logs/user-rules/{rule_id}")
    assert response.status_code == 200  # noqa: PLR2004
    data = response.json()
    assert data["id"] == rule_id
    assert data["rule_name"] == "GetTest"


async def test_get_user_rule_404(authenticated_client: AsyncClient) -> None:
    """GET /api/logs/user-rules/9999 returns 404."""
    response = await authenticated_client.get("/api/logs/user-rules/9999")
    assert response.status_code == 404  # noqa: PLR2004
    assert "not found" in response.json()["error"]["message"]


async def test_patch_user_rule_200(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCH /api/logs/user-rules/{id} changes field and re-renders."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    csrf = _csrf(authenticated_client)
    created = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "PatchTest",
            "expr": "_msg:original",
            "expr_kind": "logsql",
            "severity": "warning",
            "summary": "Original",
        },
        headers=csrf,
    )
    rule_id = created.json()["id"]
    response = await authenticated_client.patch(
        f"/api/logs/user-rules/{rule_id}",
        json={"severity": "critical"},
        headers=csrf,
    )
    assert response.status_code == 200  # noqa: PLR2004
    data = response.json()
    assert data["severity"] == "critical"
    assert data["expr"] == "_msg:original"  # Unchanged
    # Verify render_all was called
    logs_file = tmp_path / "logs" / "logs.yaml"
    doc = yaml.safe_load(logs_file.read_text())
    rendered = next((r for r in doc["groups"][0]["rules"] if r["alert"] == "PatchTest"), None)
    assert rendered is not None
    assert rendered["labels"]["severity"] == "critical"


async def test_patch_user_rule_404(authenticated_client: AsyncClient) -> None:
    """PATCH /api/logs/user-rules/9999 returns 404."""
    response = await authenticated_client.patch(
        "/api/logs/user-rules/9999",
        json={"severity": "info"},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 404  # noqa: PLR2004


async def test_delete_user_rule_204(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELETE /api/logs/user-rules/{id} returns 204; rule then absent; file cleared."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    csrf = _csrf(authenticated_client)
    created = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "DeleteTest",
            "expr": "_msg:error",
            "expr_kind": "logsql",
            "severity": "warning",
            "summary": "Delete test",
        },
        headers=csrf,
    )
    rule_id = created.json()["id"]
    response = await authenticated_client.delete(
        f"/api/logs/user-rules/{rule_id}",
        headers=csrf,
    )
    assert response.status_code == 204  # noqa: PLR2004
    # Verify deleted
    get_response = await authenticated_client.get(f"/api/logs/user-rules/{rule_id}")
    assert get_response.status_code == 404  # noqa: PLR2004
    # Verify file cleared
    logs_file = tmp_path / "logs" / "logs.yaml"
    doc = yaml.safe_load(logs_file.read_text())
    assert doc["groups"] == []


async def test_delete_user_rule_404(authenticated_client: AsyncClient) -> None:
    """DELETE /api/logs/user-rules/9999 returns 404."""
    response = await authenticated_client.delete(
        "/api/logs/user-rules/9999",
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 404  # noqa: PLR2004


async def test_enable_user_rule_200(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/logs/user-rules/{id}/enable returns 200 with enabled=True."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    csrf = _csrf(authenticated_client)
    created = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "EnableTest",
            "expr": "_msg:error",
            "expr_kind": "logsql",
            "severity": "warning",
            "summary": "Enable test",
        },
        headers=csrf,
    )
    rule_id = created.json()["id"]
    # Disable first
    await authenticated_client.post(
        f"/api/logs/user-rules/{rule_id}/disable",
        headers=csrf,
    )
    # Now enable
    response = await authenticated_client.post(
        f"/api/logs/user-rules/{rule_id}/enable",
        headers=csrf,
    )
    assert response.status_code == 200  # noqa: PLR2004
    assert response.json()["enabled"] is True


async def test_disable_user_rule_200(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST .../disable returns 200, enabled=False; file clears when no other rules remain."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    csrf = _csrf(authenticated_client)
    created = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "DisableTest",
            "expr": "_msg:error",
            "expr_kind": "logsql",
            "severity": "warning",
            "summary": "Disable test",
        },
        headers=csrf,
    )
    rule_id = created.json()["id"]
    response = await authenticated_client.post(
        f"/api/logs/user-rules/{rule_id}/disable",
        headers=csrf,
    )
    assert response.status_code == 200  # noqa: PLR2004
    assert response.json()["enabled"] is False
    # Verify file shows no rules
    logs_file = tmp_path / "logs" / "logs.yaml"
    doc = yaml.safe_load(logs_file.read_text())
    assert doc["groups"] == [] or len(doc["groups"][0]["rules"]) == 0


async def test_csrf_protection_post_without_token(authenticated_client: AsyncClient) -> None:
    """POST without X-CSRF-Token returns 403."""
    # Remove CSRF token and attempt POST
    authenticated_client.cookies.delete("homelab_monitor_csrf", domain=None, path=None)
    response = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "NoCSRF",
            "expr": "_msg:error",
            "expr_kind": "logsql",
            "severity": "warning",
            "summary": "No CSRF",
        },
        headers={},
    )
    assert response.status_code == 403  # noqa: PLR2004


async def test_list_enabled_filter(authenticated_client: AsyncClient) -> None:
    """GET /api/logs/user-rules?enabled=true filters to enabled only."""
    csrf = _csrf(authenticated_client)
    # Create enabled rule
    await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "EnabledRule",
            "expr": "_msg:error",
            "expr_kind": "logsql",
            "severity": "warning",
            "summary": "Enabled",
        },
        headers=csrf,
    )
    # Create disabled rule
    disabled_resp = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "DisabledRule",
            "expr": "_msg:error",
            "expr_kind": "logsql",
            "severity": "info",
            "summary": "Disabled",
        },
        headers=csrf,
    )
    disabled_id = disabled_resp.json()["id"]
    # Disable the second rule
    await authenticated_client.post(
        f"/api/logs/user-rules/{disabled_id}/disable",
        headers=csrf,
    )
    # Query with filter
    response = await authenticated_client.get("/api/logs/user-rules?enabled=true")
    assert response.status_code == 200  # noqa: PLR2004
    data = response.json()
    rule_names = [r["rule_name"] for r in data["rules"]]
    assert "EnabledRule" in rule_names
    assert "DisabledRule" not in rule_names


async def test_enable_user_rule_404(authenticated_client: AsyncClient) -> None:
    """POST .../enable with nonexistent id returns 404."""
    response = await authenticated_client.post(
        "/api/logs/user-rules/9999/enable",
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 404  # noqa: PLR2004
    assert "not found" in response.json()["error"]["message"]


async def test_disable_user_rule_404(authenticated_client: AsyncClient) -> None:
    """POST .../disable with nonexistent id returns 404."""
    response = await authenticated_client.post(
        "/api/logs/user-rules/9999/disable",
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 404  # noqa: PLR2004
    assert "not found" in response.json()["error"]["message"]


__all__: list[str] = []
