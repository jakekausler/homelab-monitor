"""Tests for user-rules API endpoints: CRUD, enable/disable, rendering, CSRF."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from httpx import AsyncClient
from sqlalchemy import text

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
            "expr": "_msg:error | stats count() as match_count | filter match_count:>0",
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
    assert data["expr"] == "_msg:error | stats count() as match_count | filter match_count:>0"
    assert data["expr_kind"] == "logsql"
    assert data["enabled"] is True
    assert data["source_kind"] == "manual"
    # Verify render_all wrote the per-rule file for this rule.
    logs_file = tmp_path / "logs" / "TestRule.yaml"
    assert logs_file.exists()
    doc = yaml.safe_load(logs_file.read_text())
    assert any(r["alert"] == "TestRule" for r in doc["groups"][0]["rules"])


async def test_create_user_rule_duplicate_409(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/logs/user-rules with duplicate rule_name returns 409."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    csrf = _csrf(authenticated_client)
    # Create first
    await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "Duplicate",
            "expr": "_msg:error | stats count() as match_count | filter match_count:>0",
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
            "expr": "_msg:other | stats count() as match_count | filter match_count:>0",
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


async def test_create_user_rule_render_failure_still_201(
    authenticated_client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A render failure (unwritable dir) is swallowed: POST still returns 201."""
    # Make the logs render dir uncreatable: its parent is a regular file.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(blocker / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    response = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "RenderFailRule",
            "expr": "_msg:error | stats count() as match_count | filter match_count:>0",
            "expr_kind": "logsql",
            "severity": "warning",
            "summary": "render fail test",
        },
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 201  # noqa: PLR2004


async def test_get_user_rule_200(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /api/logs/user-rules/{id} returns 200 with rule data."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    csrf = _csrf(authenticated_client)
    created = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "GetTest",
            "expr": "_msg:boom | stats count() as match_count | filter match_count:>0",
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
            "expr": "_msg:original | stats count() as match_count | filter match_count:>0",
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
    assert (
        data["expr"] == "_msg:original | stats count() as match_count | filter match_count:>0"
    )  # Unchanged
    # Verify render_all rewrote the per-rule file with the patched severity.
    logs_file = tmp_path / "logs" / "PatchTest.yaml"
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
            "expr": "_msg:error | stats count() as match_count | filter match_count:>0",
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
    # Verify the rule's per-rule file was removed (orphan reconcile).
    logs_file = tmp_path / "logs" / "DeleteTest.yaml"
    assert not logs_file.exists()


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
            "expr": "_msg:error | stats count() as match_count | filter match_count:>0",
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
            "expr": "_msg:error | stats count() as match_count | filter match_count:>0",
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
    # Verify the disabled rule's per-rule file was removed.
    logs_file = tmp_path / "logs" / "DisableTest.yaml"
    assert not logs_file.exists()


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


async def test_list_enabled_filter(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /api/logs/user-rules?enabled=true filters to enabled only."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    csrf = _csrf(authenticated_client)
    # Create enabled rule
    await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "EnabledRule",
            "expr": "_msg:error | stats count() as match_count | filter match_count:>0",
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
            "expr": "_msg:error | stats count() as match_count | filter match_count:>0",
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


async def test_create_user_rule_invalid_expr_400_not_persisted(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST logsql without | stats pipe returns 400 invalid_expr, not persisted."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    response = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "BadExpr",
            "expr": "_msg:error",
            "expr_kind": "logsql",
            "severity": "warning",
            "summary": "Bad expr",
        },
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 400  # noqa: PLR2004
    error = response.json()["error"]
    assert error["code"] == "invalid_expr"
    assert error["details"]["check"] == "missing_stats_pipe"
    # Verify rule was NOT persisted.
    list_resp = await authenticated_client.get("/api/logs/user-rules")
    rule_names = [r["rule_name"] for r in list_resp.json()["rules"]]
    assert "BadExpr" not in rule_names


async def test_create_user_rule_reserved_filter_field_400(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST logsql with reserved | filter field returns 400 invalid_expr."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    response = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "ReservedFilter",
            "expr": "_msg:error | stats count() as c | filter count:>0",
            "expr_kind": "logsql",
            "severity": "warning",
            "summary": "Reserved filter",
        },
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 400  # noqa: PLR2004
    error = response.json()["error"]
    assert error["code"] == "invalid_expr"
    assert error["details"]["check"] == "reserved_filter_field"


async def test_create_user_rule_valid_stats_expr_201(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST logsql with valid stats expr returns 201 (positive guard)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    response = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "ValidExpr",
            "expr": "_msg:error | stats count() as match_count | filter match_count:>0",
            "expr_kind": "logsql",
            "severity": "warning",
            "summary": "Valid expr",
        },
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 201  # noqa: PLR2004
    data = response.json()
    assert data["rule_name"] == "ValidExpr"


async def test_patch_user_rule_invalid_expr_400(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCH to invalid expr returns 400 invalid_expr."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    csrf = _csrf(authenticated_client)
    # Create a valid rule first
    created = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "PatchTest",
            "expr": "_msg:original | stats count() as match_count | filter match_count:>0",
            "expr_kind": "logsql",
            "severity": "warning",
            "summary": "Original",
        },
        headers=csrf,
    )
    rule_id = created.json()["id"]
    # PATCH with invalid expr
    response = await authenticated_client.patch(
        f"/api/logs/user-rules/{rule_id}",
        json={"expr": "_msg:bare"},
        headers=csrf,
    )
    assert response.status_code == 400  # noqa: PLR2004
    error = response.json()["error"]
    assert error["code"] == "invalid_expr"
    assert error["details"]["check"] == "missing_stats_pipe"


async def test_create_with_dryrun_enabled_but_mount_empty(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST with dryrun enabled but mount_source='' -> dryrun skips, rule persists (fail-open)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_DRYRUN_ENABLED", "1")
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_DRYRUN_MOUNT", "")
    response = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "DryRunSkipMount",
            "expr": "_msg:error | stats count() as match_count | filter match_count:>0",
            "expr_kind": "logsql",
            "severity": "warning",
            "summary": "Dryrun skip mount",
        },
        headers=_csrf(authenticated_client),
    )
    # Should still persist (dryrun skipped due to no mount)
    assert response.status_code == 201  # noqa: PLR2004
    assert response.json()["rule_name"] == "DryRunSkipMount"


async def test_create_with_dryrun_disabled_default(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST without HOMELAB_MONITOR_VMALERT_DRYRUN_ENABLED (default off) -> rule persists."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    # Don't set DRYRUN_ENABLED (defaults to off)
    response = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "DryRunDisabledDefault",
            "expr": "_msg:error | stats count() as match_count | filter match_count:>0",
            "expr_kind": "logsql",
            "severity": "warning",
            "summary": "Dryrun disabled default",
        },
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 201  # noqa: PLR2004
    assert response.json()["rule_name"] == "DryRunDisabledDefault"


async def test_create_with_dryrun_mock_failure(
    authenticated_client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST with mocked dryrun returning ok=False -> 400 invalid_expr(check='dryrun')."""
    from unittest.mock import patch  # noqa: PLC0415

    from homelab_monitor.kernel.logs.vmalert_dryrun import DryRunResult  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_DRYRUN_ENABLED", "1")
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_DRYRUN_MOUNT", "vol")

    def mock_dryrun(
        rule_yaml: str, *, image: str, timeout_s: float, mount_source: str, work_dir: str
    ) -> DryRunResult:
        return DryRunResult(skipped=False, ok=False, stderr="invalid expr: boom")

    with patch("homelab_monitor.kernel.api.routers.logs.run_vmalert_dryrun", mock_dryrun):
        response = await authenticated_client.post(
            "/api/logs/user-rules",
            json={
                "rule_name": "DryRunMockFail",
                "expr": "_msg:error | stats count() as match_count | filter match_count:>0",
                "expr_kind": "logsql",
                "severity": "warning",
                "summary": "Dryrun mock fail",
            },
            headers=_csrf(authenticated_client),
        )
    assert response.status_code == 400  # noqa: PLR2004
    error = response.json()["error"]
    assert error["code"] == "invalid_expr"
    assert error["details"]["check"] == "dryrun"
    assert "boom" in error["message"]


async def test_create_user_rule_error_severity_201(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST a metricsql rule with severity='error' returns 201 and echoes it."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    response = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "ErrSev",
            "expr": "up == 0",
            "expr_kind": "metricsql",
            "severity": "error",
            "summary": "host down",
        },
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 201  # noqa: PLR2004
    assert response.json()["severity"] == "error"


async def test_create_metricsql_bare_selector_400(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare metricsql selector is rejected with 400 invalid_expr/missing_threshold."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    response = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "BareSel",
            "expr": "up",
            "expr_kind": "metricsql",
            "severity": "warning",
            "summary": "always fires",
        },
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 400  # noqa: PLR2004
    body = response.json()
    assert body["error"]["code"] == "invalid_expr"
    assert body["error"]["details"]["check"] == "missing_threshold"


async def test_create_user_rule_writes_audit(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful create writes a user_rule.create audit row."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    resp = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "AuditCreate",
            "expr": "up == 0",
            "expr_kind": "metricsql",
            "severity": "warning",
            "summary": "host down",
        },
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 201  # noqa: PLR2004
    audit = await repo.fetch_one(
        text("SELECT what FROM audit_log WHERE what = :w ORDER BY id DESC LIMIT 1"),
        {"w": "user_rule.create"},
    )
    assert audit is not None
    assert audit[0] == "user_rule.create"


async def test_patch_user_rule_writes_audit(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful patch writes a user_rule.update audit row."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    csrf = _csrf(authenticated_client)
    created = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "AuditPatch",
            "expr": "_msg:error | stats count() as match_count | filter match_count:>0",
            "expr_kind": "logsql",
            "severity": "warning",
            "summary": "s",
        },
        headers=csrf,
    )
    rule_id = created.json()["id"]
    resp = await authenticated_client.patch(
        f"/api/logs/user-rules/{rule_id}",
        json={"severity": "error"},
        headers=csrf,
    )
    assert resp.status_code == 200  # noqa: PLR2004
    audit = await repo.fetch_one(
        text("SELECT what FROM audit_log WHERE what = :w ORDER BY id DESC LIMIT 1"),
        {"w": "user_rule.update"},
    )
    assert audit is not None
    assert audit[0] == "user_rule.update"


async def test_delete_user_rule_writes_audit(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful delete writes a user_rule.delete audit row."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    csrf = _csrf(authenticated_client)
    created = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "AuditDelete",
            "expr": "_msg:error | stats count() as match_count | filter match_count:>0",
            "expr_kind": "logsql",
            "severity": "warning",
            "summary": "s",
        },
        headers=csrf,
    )
    rule_id = created.json()["id"]
    resp = await authenticated_client.delete(f"/api/logs/user-rules/{rule_id}", headers=csrf)
    assert resp.status_code == 204  # noqa: PLR2004
    audit = await repo.fetch_one(
        text("SELECT what FROM audit_log WHERE what = :w ORDER BY id DESC LIMIT 1"),
        {"w": "user_rule.delete"},
    )
    assert audit is not None and audit[0] == "user_rule.delete"


async def test_enable_user_rule_writes_audit(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful enable writes a user_rule.enable audit row."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    csrf = _csrf(authenticated_client)
    created = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "AuditEnable",
            "expr": "_msg:error | stats count() as match_count | filter match_count:>0",
            "expr_kind": "logsql",
            "severity": "warning",
            "summary": "s",
        },
        headers=csrf,
    )
    rule_id = created.json()["id"]
    # Disable first
    await authenticated_client.post(
        f"/api/logs/user-rules/{rule_id}/disable",
        headers=csrf,
    )
    # Enable
    resp = await authenticated_client.post(
        f"/api/logs/user-rules/{rule_id}/enable",
        headers=csrf,
    )
    assert resp.status_code == 200  # noqa: PLR2004
    audit = await repo.fetch_one(
        text("SELECT what FROM audit_log WHERE what = :w ORDER BY id DESC LIMIT 1"),
        {"w": "user_rule.enable"},
    )
    assert audit is not None and audit[0] == "user_rule.enable"


async def test_disable_user_rule_writes_audit(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful disable writes a user_rule.disable audit row."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(tmp_path / "metrics"))
    csrf = _csrf(authenticated_client)
    created = await authenticated_client.post(
        "/api/logs/user-rules",
        json={
            "rule_name": "AuditDisable",
            "expr": "_msg:error | stats count() as match_count | filter match_count:>0",
            "expr_kind": "logsql",
            "severity": "warning",
            "summary": "s",
        },
        headers=csrf,
    )
    rule_id = created.json()["id"]
    resp = await authenticated_client.post(
        f"/api/logs/user-rules/{rule_id}/disable",
        headers=csrf,
    )
    assert resp.status_code == 200  # noqa: PLR2004
    audit = await repo.fetch_one(
        text("SELECT what FROM audit_log WHERE what = :w ORDER BY id DESC LIMIT 1"),
        {"w": "user_rule.disable"},
    )
    assert audit is not None and audit[0] == "user_rule.disable"


__all__: list[str] = []
