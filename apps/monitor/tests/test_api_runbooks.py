"""Tests for the runbook registry API."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml
from httpx import AsyncClient
from sqlalchemy import text

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.runbooks.loader import RUNBOOK_CONFIG_FILENAME, RUNBOOK_PROMPT_FILENAME


def _valid_config_dict(name: str = "test-runbook") -> dict[str, object]:
    """Create a minimal valid runbook config."""
    return {
        "runbook": 1,
        "name": name,
        "match_patterns": [{"alertname": "HighCPU"}],
        "risk_tag": "safe",
        "dry_run_required": True,
        "rate_limit_per_hour": 5,
        "cooldown_seconds": 300,
        "scoped_capabilities": {"docker": {"container": "c1", "allowed_actions": ["restart"]}},
    }


def _csrf(client: AsyncClient) -> dict[str, str]:
    """Extract CSRF token from client cookies."""
    csrf = client.cookies.get("homelab_monitor_csrf") or ""
    return {"X-CSRF-Token": csrf}


def _write_runbook(
    folder: Path, *, config: Mapping[str, object] | None = None, claude: str | None = "do the thing"
) -> None:
    """Write a test runbook folder."""
    folder.mkdir(parents=True, exist_ok=True)
    if config is not None:
        (folder / RUNBOOK_CONFIG_FILENAME).write_text(yaml.safe_dump(config))
    if claude is not None:
        (folder / RUNBOOK_PROMPT_FILENAME).write_text(claude)


@pytest.mark.asyncio
async def test_list_requires_session(unauthenticated_client: AsyncClient) -> None:
    """Unauthenticated GET /api/runbooks -> 401."""
    resp = await unauthenticated_client.get("/api/runbooks")
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_refresh_requires_session(unauthenticated_client: AsyncClient) -> None:
    """Unauthenticated POST /api/runbooks/refresh -> 401."""
    resp = await unauthenticated_client.post("/api/runbooks/refresh")
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_patch_requires_session(unauthenticated_client: AsyncClient) -> None:
    """Unauthenticated PATCH /api/runbooks/x -> 401."""
    resp = await unauthenticated_client.patch("/api/runbooks/x", json={})
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_token_client_401(api_token_client: AsyncClient) -> None:
    """api_token_client GET /api/runbooks -> 401 (session-only route)."""
    resp = await api_token_client.get("/api/runbooks")
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_empty(authenticated_client: AsyncClient) -> None:
    """Authed GET on empty registry -> {items: []}."""
    resp = await authenticated_client.get("/api/runbooks")
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert data["items"] == []


@pytest.mark.asyncio
async def test_refresh_registers_new(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write a valid folder, set env, POST refresh -> 200, registered has path."""
    monkeypatch.setenv("HOMELAB_MONITOR_RUNBOOKS_DIR", str(tmp_path))

    folder = tmp_path / "my-runbook"
    _write_runbook(folder, config=_valid_config_dict("my-runbook"))

    resp = await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert str(folder) in data["registered"]
    assert data["refreshed"] == []
    assert data["skipped"] == []
    assert data["errors"] == []

    # Verify enabled/auto_trigger default to False
    resp = await authenticated_client.get("/api/runbooks")
    assert resp.status_code == 200  # noqa: PLR2004
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["enabled"] is False
    assert items[0]["auto_trigger"] is False


@pytest.mark.asyncio
async def test_refresh_reports_malformed_not_fatal(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tmp dir with one valid + one invalid-config folder -> 200, mixed result."""
    monkeypatch.setenv("HOMELAB_MONITOR_RUNBOOKS_DIR", str(tmp_path))

    valid = tmp_path / "good-one"
    _write_runbook(valid, config=_valid_config_dict("good"))

    bad = tmp_path / "bad-one"
    bad_config = {"name": "x"}  # invalid
    _write_runbook(bad, config=bad_config)

    resp = await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert str(valid) in data["registered"]
    assert len(data["errors"]) == 1
    assert str(bad) in data["errors"][0]["path"]


@pytest.mark.asyncio
async def test_refresh_skips_underscore(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tmp dir with _examples/ + real folder -> only real one registered."""
    monkeypatch.setenv("HOMELAB_MONITOR_RUNBOOKS_DIR", str(tmp_path))

    examples = tmp_path / "_examples"
    _write_runbook(examples, config=_valid_config_dict("example"))

    real = tmp_path / "real-rb"
    _write_runbook(real, config=_valid_config_dict("real"))

    resp = await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert str(real) in data["registered"]
    assert str(examples) not in data["registered"]
    assert len(data["errors"]) == 0


@pytest.mark.asyncio
async def test_refresh_unchanged_skipped(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refresh twice unchanged -> 2nd response skipped has path."""
    monkeypatch.setenv("HOMELAB_MONITOR_RUNBOOKS_DIR", str(tmp_path))

    folder = tmp_path / "my-runbook"
    _write_runbook(folder, config=_valid_config_dict())

    resp1 = await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )
    assert resp1.status_code == 200  # noqa: PLR2004
    assert str(folder) in resp1.json()["registered"]

    resp2 = await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )
    assert resp2.status_code == 200  # noqa: PLR2004
    data = resp2.json()
    assert str(folder) in data["skipped"]
    assert data["registered"] == []


@pytest.mark.asyncio
async def test_refresh_changed_updates_hash(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refresh, mutate folder's runbook.yaml, refresh again -> hash updated."""
    monkeypatch.setenv("HOMELAB_MONITOR_RUNBOOKS_DIR", str(tmp_path))

    folder = tmp_path / "my-runbook"
    _write_runbook(folder, config=_valid_config_dict())

    resp1 = await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )
    assert resp1.status_code == 200  # noqa: PLR2004
    data1 = resp1.json()
    assert str(folder) in data1["registered"]

    # Get the registered runbook to check hash
    list_resp = await authenticated_client.get("/api/runbooks")
    items = list_resp.json()["items"]
    hash1 = items[0]["content_hash"]

    # Mutate config
    new_config: dict[str, object] = _valid_config_dict()
    new_config["cooldown_seconds"] = 999
    _write_runbook(folder, config=new_config)

    resp2 = await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )
    assert resp2.status_code == 200  # noqa: PLR2004
    data2 = resp2.json()
    assert str(folder) in data2["refreshed"]

    list_resp2 = await authenticated_client.get("/api/runbooks")
    items2 = list_resp2.json()["items"]
    hash2 = items2[0]["content_hash"]
    assert hash2 != hash1
    assert items2[0]["cooldown_seconds"] == 999  # noqa: PLR2004


@pytest.mark.asyncio
async def test_patch_enabled(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Register, PATCH enabled=true with CSRF -> 200, enabled is True."""
    monkeypatch.setenv("HOMELAB_MONITOR_RUNBOOKS_DIR", str(tmp_path))

    folder = tmp_path / "my-runbook"
    _write_runbook(folder, config=_valid_config_dict())

    await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )
    list_resp = await authenticated_client.get("/api/runbooks")
    runbook_id = list_resp.json()["items"][0]["id"]

    resp = await authenticated_client.patch(
        f"/api/runbooks/{runbook_id}",
        json={"enabled": True},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert data["enabled"] is True
    assert data["auto_trigger"] is False


@pytest.mark.asyncio
async def test_patch_auto_trigger(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCH auto_trigger=true -> auto_trigger is True, enabled is False."""
    monkeypatch.setenv("HOMELAB_MONITOR_RUNBOOKS_DIR", str(tmp_path))

    folder = tmp_path / "my-runbook"
    _write_runbook(folder, config=_valid_config_dict())

    await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )
    list_resp = await authenticated_client.get("/api/runbooks")
    runbook_id = list_resp.json()["items"][0]["id"]

    resp = await authenticated_client.patch(
        f"/api/runbooks/{runbook_id}",
        json={"auto_trigger": True},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert data["auto_trigger"] is True
    assert data["enabled"] is False


@pytest.mark.asyncio
async def test_patch_gates_independent(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enable, then PATCH auto_trigger only -> both True."""
    monkeypatch.setenv("HOMELAB_MONITOR_RUNBOOKS_DIR", str(tmp_path))

    folder = tmp_path / "my-runbook"
    _write_runbook(folder, config=_valid_config_dict())

    await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )
    list_resp = await authenticated_client.get("/api/runbooks")
    runbook_id = list_resp.json()["items"][0]["id"]

    # Enable
    await authenticated_client.patch(
        f"/api/runbooks/{runbook_id}",
        json={"enabled": True},
        headers=_csrf(authenticated_client),
    )

    # PATCH auto_trigger only
    resp = await authenticated_client.patch(
        f"/api/runbooks/{runbook_id}",
        json={"auto_trigger": True},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert data["enabled"] is True
    assert data["auto_trigger"] is True


@pytest.mark.asyncio
async def test_patch_unknown_id_404(authenticated_client: AsyncClient) -> None:
    """PATCH /api/runbooks/does-not-exist with CSRF -> 404."""
    resp = await authenticated_client.patch(
        "/api/runbooks/does-not-exist",
        json={"enabled": True},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_patch_extra_field_422(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCH with extra field (risk_tag) -> 422."""
    monkeypatch.setenv("HOMELAB_MONITOR_RUNBOOKS_DIR", str(tmp_path))

    folder = tmp_path / "my-runbook"
    _write_runbook(folder, config=_valid_config_dict())

    await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )
    list_resp = await authenticated_client.get("/api/runbooks")
    runbook_id = list_resp.json()["items"][0]["id"]

    resp = await authenticated_client.patch(
        f"/api/runbooks/{runbook_id}",
        json={"risk_tag": "safe"},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_refresh_audits(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After refresh, audit_log has runbook_registered with who == user:...."""
    monkeypatch.setenv("HOMELAB_MONITOR_RUNBOOKS_DIR", str(tmp_path))

    folder = tmp_path / "my-runbook"
    _write_runbook(folder, config=_valid_config_dict())

    await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )

    rows = await repo.fetch_all(
        text("SELECT who, what FROM audit_log WHERE what = :w"),
        {"w": "runbook_registered"},
    )
    assert len(rows) == 1
    who = rows[0][0]
    assert who.startswith("user:")

    # Refresh with config change -> runbook_refreshed audit has before/after
    new_config: dict[str, object] = _valid_config_dict()
    new_config["cooldown_seconds"] = 999
    _write_runbook(folder, config=new_config)

    await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )

    refresh_rows = await repo.fetch_all(
        text("SELECT before_json, after_json FROM audit_log WHERE what = :w"),
        {"w": "runbook_refreshed"},
    )
    assert len(refresh_rows) == 1
    before_json_str = refresh_rows[0][0]
    after_json_str = refresh_rows[0][1]
    assert before_json_str is not None
    before = json.loads(before_json_str)
    after = json.loads(after_json_str)
    assert "content_hash" in before
    assert "content_hash" in after
    assert before["content_hash"] is not None
    assert before["content_hash"] != after["content_hash"]


@pytest.mark.asyncio
async def test_patch_audits(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After PATCH enabled, audit_log has runbook_gates_changed."""
    monkeypatch.setenv("HOMELAB_MONITOR_RUNBOOKS_DIR", str(tmp_path))

    folder = tmp_path / "my-runbook"
    _write_runbook(folder, config=_valid_config_dict())

    await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )
    list_resp = await authenticated_client.get("/api/runbooks")
    runbook_id = list_resp.json()["items"][0]["id"]

    await authenticated_client.patch(
        f"/api/runbooks/{runbook_id}",
        json={"enabled": True},
        headers=_csrf(authenticated_client),
    )

    rows = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = :w"),
        {"w": "runbook_gates_changed"},
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_refresh_missing_root_reported_not_fatal(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing runbooks root is reported gracefully (200), never fatal (500)."""
    monkeypatch.setenv("HOMELAB_MONITOR_RUNBOOKS_DIR", str(tmp_path / "does-not-exist"))

    resp = await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert len(data["errors"]) == 1
    assert "is not a directory" in data["errors"][0]["message"]
    assert data["registered"] == []
