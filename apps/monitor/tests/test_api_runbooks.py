"""Tests for the runbook registry API."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from httpx import AsyncClient
from sqlalchemy import text

from homelab_monitor.kernel.autofix.orchestrator import AutoFixOrchestrator
from homelab_monitor.kernel.autofix.runs_repository import RunbookRunsRepository
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.docker.socket_client import ExecResult
from homelab_monitor.kernel.runbooks.loader import RUNBOOK_CONFIG_FILENAME, RUNBOOK_PROMPT_FILENAME
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository
from homelab_monitor.kernel.security.pin import PIN_HASH_KEY, hash_pin

# Reuse the underscore-private factories from the orchestrator test suite —
# the codebase idiom for cross-test-file helper reuse (see test_api_autofix.py).
from tests.test_autofix_orchestrator import (
    _FakeDockerClient,  # pyright: ignore[reportPrivateUsage]
    _insert_runbook,  # pyright: ignore[reportPrivateUsage]
    _make_orchestrator,  # pyright: ignore[reportPrivateUsage]
    _make_risky_runbook_record,  # pyright: ignore[reportPrivateUsage]
    _make_runbook_record,  # pyright: ignore[reportPrivateUsage]
)


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


@pytest.mark.asyncio
async def test_reconcile_v1_hash_becomes_v2_on_next_refresh(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-stage bare-hex content_hash is rewritten to v2 on the next refresh."""
    monkeypatch.setenv("HOMELAB_MONITOR_RUNBOOKS_DIR", str(tmp_path))

    # 1. Write a runbook folder
    folder = tmp_path / "test-runbook"
    _write_runbook(folder, config=_valid_config_dict("test-runbook"))

    # 2. Initial refresh to create the row (with v2 hash computed)
    resp = await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 200  # noqa: PLR2004

    # 3. Manually insert/update the row with bare-hex v1 hash
    async with repo.transaction() as conn:
        await conn.execute(
            text("UPDATE runbooks SET content_hash = :hash WHERE path = :path"),
            {"path": str(folder), "hash": "a" * 64},
        )

    # 4. Refresh again
    resp = await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()

    # 5. Assert the row's content_hash now starts with v2:sha256:
    items = data.get("registered", []) + data.get("refreshed", [])
    assert str(folder) in items

    # Verify the hash was updated in the DB
    resp = await authenticated_client.get("/api/runbooks")
    runbooks = resp.json()["items"]
    assert len(runbooks) == 1
    assert runbooks[0]["content_hash"].startswith("v2:sha256:")


@pytest.mark.asyncio
async def test_reconcile_hash_error_skips_folder_and_logs(
    authenticated_client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A folder with a hard-reject condition (symlink) is skipped; other folders
    still reconcile; the error is surfaced in the refresh response's errors list.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_RUNBOOKS_DIR", str(tmp_path))

    # 1. Write one valid runbook
    valid_folder = tmp_path / "good-runbook"
    _write_runbook(valid_folder, config=_valid_config_dict("good"))

    # 2. Write another with a symlink (will cause hash error)
    bad_folder = tmp_path / "bad-runbook"
    _write_runbook(bad_folder, config=_valid_config_dict("bad"))
    target = tmp_path / "outside.txt"
    target.write_text("x", encoding="utf-8")
    (bad_folder / "link.txt").symlink_to(target)

    # 3. POST refresh
    resp = await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()

    # 4. Assert valid folder registered, bad folder error reported
    assert str(valid_folder) in data["registered"]
    errors = data["errors"]
    assert len(errors) > 0
    error = next((e for e in errors if str(bad_folder) in e["path"]), None)
    assert error is not None
    assert "symlink" in error["message"]


# ---------------------------------------------------------------------------
# POST /runbooks/{runbook_id}/trigger (STAGE-009-010A)
# ---------------------------------------------------------------------------


def _wire_orchestrator(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    docker: object,
    **kwargs: object,
) -> AutoFixOrchestrator:
    orch = _make_orchestrator(repo, kwargs.pop("secrets_repo"), docker, **kwargs)  # type: ignore[arg-type]
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]
    return orch


@pytest.mark.asyncio
async def test_trigger_endpoint_dry_success(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """POST trigger, mode='dry_run', safe runbook -> 200 dry_run_stored."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="plan", stderr=""))
    _wire_orchestrator(
        authenticated_client,
        repo,
        docker,
        secrets_repo=secrets_repo,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        resp = await authenticated_client.post(
            f"/api/runbooks/{rb.id}/trigger",
            json={"mode": "dry_run"},
            headers=_csrf(authenticated_client),
        )
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert data["outcome"] == "dry_run_stored"
    assert data["run_id"] is not None
    assert data["approval_id"] is not None


@pytest.mark.asyncio
async def test_trigger_endpoint_real_success_on_safe(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """POST trigger, mode='real', safe runbook -> 200 outcome=ran."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="done", stderr=""))
    _wire_orchestrator(
        authenticated_client,
        repo,
        docker,
        secrets_repo=secrets_repo,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        resp = await authenticated_client.post(
            f"/api/runbooks/{rb.id}/trigger",
            json={"mode": "real", "confirm_phrase": "runbook"},
            headers=_csrf(authenticated_client),
        )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["outcome"] == "ran"


@pytest.mark.asyncio
async def test_trigger_endpoint_400_dry_run_required_for_risky(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Risky runbook, mode='real' -> 400, code=dry_run_required_for_risky."""
    rb = _make_risky_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient()
    _wire_orchestrator(authenticated_client, repo, docker, secrets_repo=secrets_repo)

    resp = await authenticated_client.post(
        f"/api/runbooks/{rb.id}/trigger",
        json={"mode": "real", "confirm_phrase": "risky-runbook"},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "dry_run_required_for_risky"


@pytest.mark.asyncio
async def test_trigger_endpoint_404_runbook_not_found(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Unknown runbook_id -> 404, code=not_found."""
    docker = _FakeDockerClient()
    _wire_orchestrator(authenticated_client, repo, docker, secrets_repo=secrets_repo)

    resp = await authenticated_client.post(
        "/api/runbooks/does-not-exist/trigger",
        json={"mode": "dry_run"},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_trigger_endpoint_real_missing_runbook_404(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Real mode with nonexistent runbook_id -> 404, code=not_found.

    Covers runbooks.py:245 branch: missing runbook in handle_operator_trigger.
    """
    docker = _FakeDockerClient()
    _wire_orchestrator(authenticated_client, repo, docker, secrets_repo=secrets_repo)

    resp = await authenticated_client.post(
        "/api/runbooks/nonexistent-id/trigger",
        json={"mode": "real", "confirm_phrase": "anything"},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 404  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_trigger_endpoint_401_unauthenticated(
    unauthenticated_client: AsyncClient,
) -> None:
    """No session cookie -> 401."""
    resp = await unauthenticated_client.post(
        "/api/runbooks/some-id/trigger", json={"mode": "dry_run"}
    )
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_trigger_endpoint_403_csrf_missing(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Session cookie present, CSRF header missing -> 403."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)

    docker = _FakeDockerClient()
    _wire_orchestrator(authenticated_client, repo, docker, secrets_repo=secrets_repo)

    resp = await authenticated_client.post(
        f"/api/runbooks/{rb.id}/trigger",
        json={"mode": "dry_run"},
    )
    assert resp.status_code == 403  # noqa: PLR2004


@pytest.mark.asyncio
async def test_trigger_endpoint_409_kill_switch(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Kill-switch engaged (unset) -> 409, code=kill_switch."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)

    docker = _FakeDockerClient()
    _wire_orchestrator(authenticated_client, repo, docker, secrets_repo=secrets_repo)

    resp = await authenticated_client.post(
        f"/api/runbooks/{rb.id}/trigger",
        json={"mode": "dry_run"},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 409  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "kill_switch"


@pytest.mark.asyncio
async def test_trigger_endpoint_409_runbook_disabled(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """enabled=False -> 409, code=runbook_disabled."""
    rb = _make_runbook_record(alertname="TestAlert", enabled=False)
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient()
    _wire_orchestrator(authenticated_client, repo, docker, secrets_repo=secrets_repo)

    resp = await authenticated_client.post(
        f"/api/runbooks/{rb.id}/trigger",
        json={"mode": "dry_run"},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 409  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "runbook_disabled"


@pytest.mark.asyncio
async def test_trigger_endpoint_409_rate_limit(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Rate limit tripped -> 409, code=rate_limit."""
    rb = _make_runbook_record(alertname="TestAlert", rate_limit_per_hour=1)
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient()
    _wire_orchestrator(authenticated_client, repo, docker, secrets_repo=secrets_repo)

    with patch.object(RunbookRunsRepository, "count_started_since", new=AsyncMock(return_value=1)):
        resp = await authenticated_client.post(
            f"/api/runbooks/{rb.id}/trigger",
            json={"mode": "dry_run"},
            headers=_csrf(authenticated_client),
        )
    assert resp.status_code == 409  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "rate_limit"


@pytest.mark.asyncio
async def test_trigger_endpoint_409_cooldown(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Cooldown active -> 409, code=cooldown."""
    rb = _make_runbook_record(alertname="TestAlert", cooldown_seconds=3600)
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient()
    _wire_orchestrator(authenticated_client, repo, docker, secrets_repo=secrets_repo)

    with patch.object(
        RunbookRunsRepository,
        "latest_ended_at",
        new=AsyncMock(return_value=utc_now_iso()),
    ):
        resp = await authenticated_client.post(
            f"/api/runbooks/{rb.id}/trigger",
            json={"mode": "dry_run"},
            headers=_csrf(authenticated_client),
        )
    assert resp.status_code == 409  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "cooldown"


@pytest.mark.asyncio
async def test_trigger_endpoint_409_already_running(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Per-runbook lock already held -> 409, code=already_running."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient()
    orch = _wire_orchestrator(authenticated_client, repo, docker, secrets_repo=secrets_repo)

    lock = orch._lock_for(rb.id)  # pyright: ignore[reportPrivateUsage]
    await lock.acquire()
    try:
        resp = await authenticated_client.post(
            f"/api/runbooks/{rb.id}/trigger",
            json={"mode": "dry_run"},
            headers=_csrf(authenticated_client),
        )
    finally:
        lock.release()
    assert resp.status_code == 409  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "already_running"


@pytest.mark.asyncio
async def test_trigger_endpoint_rejects_unknown_mode(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """mode='foo' -> 422 (pydantic Literal validation)."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)

    docker = _FakeDockerClient()
    _wire_orchestrator(authenticated_client, repo, docker, secrets_repo=secrets_repo)

    resp = await authenticated_client.post(
        f"/api/runbooks/{rb.id}/trigger",
        json={"mode": "foo"},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_trigger_endpoint_rejects_extra_fields(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Extra field in body -> 422 (extra='forbid')."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)

    docker = _FakeDockerClient()
    _wire_orchestrator(authenticated_client, repo, docker, secrets_repo=secrets_repo)

    resp = await authenticated_client.post(
        f"/api/runbooks/{rb.id}/trigger",
        json={"mode": "dry_run", "extra_field": True},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 422  # noqa: PLR2004


# ---------------------------------------------------------------------------
# PATCH /runbooks/{runbook_id}: risky auto_trigger pre-check (STAGE-009-010A)
# ---------------------------------------------------------------------------


async def _register_risky_runbook(
    authenticated_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    """Register a risk_tag='risky' runbook via refresh; returns its id."""
    monkeypatch.setenv("HOMELAB_MONITOR_RUNBOOKS_DIR", str(tmp_path))
    folder = tmp_path / "risky-runbook"
    config = _valid_config_dict("risky-runbook")
    config["risk_tag"] = "risky"
    _write_runbook(folder, config=config)

    await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )
    list_resp = await authenticated_client.get("/api/runbooks")
    items = list_resp.json()["items"]
    for item in items:
        if item["path"] == str(folder):
            runbook_id: str = item["id"]
            assert item["risk_tag"] == "risky"
            return runbook_id
    raise AssertionError("risky runbook not found after refresh")


@pytest.mark.asyncio
async def test_patch_risky_auto_trigger_true_returns_400(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATCH {auto_trigger: true} on risky runbook -> 400; no audit; auto_trigger stays False."""
    runbook_id = await _register_risky_runbook(authenticated_client, tmp_path, monkeypatch)

    resp = await authenticated_client.patch(
        f"/api/runbooks/{runbook_id}",
        json={"auto_trigger": True},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "risky_auto_trigger_denied"

    rows = await repo.fetch_all(
        text("SELECT id FROM audit_log WHERE what = :w"), {"w": "runbook_gates_changed"}
    )
    assert rows == []

    list_resp = await authenticated_client.get("/api/runbooks")
    item = next(i for i in list_resp.json()["items"] if i["id"] == runbook_id)
    assert item["auto_trigger"] is False


@pytest.mark.asyncio
async def test_patch_risky_enabled_toggle_still_works(
    authenticated_client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATCH {enabled: false} on risky runbook -> 200."""
    runbook_id = await _register_risky_runbook(authenticated_client, tmp_path, monkeypatch)

    resp = await authenticated_client.patch(
        f"/api/runbooks/{runbook_id}",
        json={"enabled": False},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["enabled"] is False


@pytest.mark.asyncio
async def test_patch_safe_auto_trigger_true_still_works(
    authenticated_client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATCH {auto_trigger: true} on safe runbook -> 200."""
    monkeypatch.setenv("HOMELAB_MONITOR_RUNBOOKS_DIR", str(tmp_path))
    folder = tmp_path / "safe-runbook"
    _write_runbook(folder, config=_valid_config_dict("safe-runbook"))

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
    assert resp.json()["auto_trigger"] is True


@pytest.mark.asyncio
async def test_patch_risky_auto_trigger_false_still_works(
    authenticated_client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATCH {auto_trigger: false} on risky runbook -> 200."""
    runbook_id = await _register_risky_runbook(authenticated_client, tmp_path, monkeypatch)

    resp = await authenticated_client.patch(
        f"/api/runbooks/{runbook_id}",
        json={"auto_trigger": False},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["auto_trigger"] is False


@pytest.mark.asyncio
async def test_patch_missing_runbook_returns_404(
    authenticated_client: AsyncClient,
) -> None:
    """PATCH unknown id with auto_trigger:true -> 404 (record lookup happens first)."""
    resp = await authenticated_client.patch(
        "/api/runbooks/does-not-exist",
        json={"auto_trigger": True},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 404  # noqa: PLR2004


# ---------------------------------------------------------------------------
# STAGE-009-010B: server-side credential gate on POST /trigger mode='real'
# ---------------------------------------------------------------------------


async def _seed_pin(repo: SqliteRepository, pin: str = "1234") -> None:
    """Seed a PIN hash directly via app_settings (bypassing the security_pin router)."""
    app_settings = AppSettingsRepository(repo)
    await app_settings.set(PIN_HASH_KEY, hash_pin(pin, cost=4))


@pytest.mark.asyncio
async def test_trigger_dry_run_needs_no_credential(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """mode='dry_run' with an empty body (no confirm_phrase/confirm_pin)
    still succeeds — dry runs stay confirmation-free (regression baseline;
    mirrors test_trigger_endpoint_dry_success but explicitly names the
    credential-free contract this stage must preserve)."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="plan", stderr=""))
    _wire_orchestrator(
        authenticated_client,
        repo,
        docker,
        secrets_repo=secrets_repo,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        resp = await authenticated_client.post(
            f"/api/runbooks/{rb.id}/trigger",
            json={"mode": "dry_run"},
            headers=_csrf(authenticated_client),
        )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["outcome"] == "dry_run_stored"


@pytest.mark.asyncio
async def test_trigger_real_without_credential_returns_400(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """mode='real' with empty confirm fields -> 400 (NEW behavior: closes the
    010A gap where real-mode trigger required no server-side credential)."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="done", stderr=""))
    _wire_orchestrator(authenticated_client, repo, docker, secrets_repo=secrets_repo)

    resp = await authenticated_client.post(
        f"/api/runbooks/{rb.id}/trigger",
        json={"mode": "real"},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 400  # noqa: PLR2004


@pytest.mark.asyncio
async def test_trigger_real_with_confirm_phrase_matching_basename_success(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """mode='real' with confirm_phrase == Path(runbook.path).name succeeds.

    runbooks.py derives expected_basename via ``Path(record.path).name``; with
    ``runbook_dir=tmp_path / "runbook"`` the basename is literally "runbook".
    """
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)
    assert Path(rb.path).name == "runbook"

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="done", stderr=""))
    _wire_orchestrator(
        authenticated_client,
        repo,
        docker,
        secrets_repo=secrets_repo,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        resp = await authenticated_client.post(
            f"/api/runbooks/{rb.id}/trigger",
            json={"mode": "real", "confirm_phrase": "runbook"},
            headers=_csrf(authenticated_client),
        )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["outcome"] == "ran"


@pytest.mark.asyncio
async def test_trigger_real_with_confirm_phrase_case_fold_variations(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """mode='real' with confirm_phrase using different case + surrounding
    whitespace still matches (trigger uses CASE_FOLD mode, unlike approve's
    EXACT mode)."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="done", stderr=""))
    _wire_orchestrator(
        authenticated_client,
        repo,
        docker,
        secrets_repo=secrets_repo,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        resp = await authenticated_client.post(
            f"/api/runbooks/{rb.id}/trigger",
            json={"mode": "real", "confirm_phrase": "  RUNBOOK  "},
            headers=_csrf(authenticated_client),
        )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["outcome"] == "ran"


@pytest.mark.asyncio
async def test_trigger_real_with_confirm_pin_success(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """mode='real' with a configured PIN + correct confirm_pin succeeds."""
    await _seed_pin(repo, pin="1234")

    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="done", stderr=""))
    _wire_orchestrator(
        authenticated_client,
        repo,
        docker,
        secrets_repo=secrets_repo,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        resp = await authenticated_client.post(
            f"/api/runbooks/{rb.id}/trigger",
            json={"mode": "real", "confirm_pin": "1234"},
            headers=_csrf(authenticated_client),
        )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["outcome"] == "ran"


@pytest.mark.asyncio
async def test_trigger_audit_includes_credential_type(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """``handle_operator_trigger``'s ``credential_type`` kwarg is threaded into
    both the pre-lock rejection audit (``autofix.trigger_rejected``, written
    by ``_audit_trigger_rejected`` when a risky runbook is triggered with
    mode='real') AND into the ``autofix.ran`` audit written by
    ``_claim_and_exec``/``_persist_outcome`` for a successful real run.

    credential_type surfaces in ``autofix.trigger_rejected`` for both the
    phrase and pin paths on a risky+real rejection. A successful real-mode
    trigger's ``autofix.ran`` row now INCLUDES credential_type, mirroring the
    behavior of the approval path (autofix.approved also includes it).
    """
    # Phrase path: risky runbook + mode=real is rejected before any exec.
    risky_rb = _make_risky_runbook_record(alertname="TestAlertRisky")
    await _insert_runbook(repo, risky_rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="done", stderr=""))
    _wire_orchestrator(
        authenticated_client,
        repo,
        docker,
        secrets_repo=secrets_repo,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
    )

    resp_phrase = await authenticated_client.post(
        f"/api/runbooks/{risky_rb.id}/trigger",
        json={"mode": "real", "confirm_phrase": "risky-runbook"},
        headers=_csrf(authenticated_client),
    )
    assert resp_phrase.status_code == 400  # noqa: PLR2004
    assert resp_phrase.json()["error"]["code"] == "dry_run_required_for_risky"

    rejected_rows = await repo.fetch_all(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.trigger_rejected'"),
        {},
    )
    assert len(rejected_rows) == 1
    after_phrase = json.loads(rejected_rows[0][0])
    assert after_phrase["credential_type"] == "phrase"

    # PIN path: same rejection, different runbook to keep audit rows distinct.
    await _seed_pin(repo, pin="1234")
    risky_rb2 = _make_risky_runbook_record(alertname="TestAlertRisky2")
    await _insert_runbook(repo, risky_rb2)

    resp_pin = await authenticated_client.post(
        f"/api/runbooks/{risky_rb2.id}/trigger",
        json={"mode": "real", "confirm_pin": "1234"},
        headers=_csrf(authenticated_client),
    )
    assert resp_pin.status_code == 400  # noqa: PLR2004

    rejected_rows2 = await repo.fetch_all(
        text(
            "SELECT after_json FROM audit_log WHERE what = 'autofix.trigger_rejected' "
            "AND after_json LIKE :like"
        ),
        {"like": f'%"runbook_id": "{risky_rb2.id}"%'},
    )
    assert len(rejected_rows2) == 1
    after_pin = json.loads(rejected_rows2[0][0])
    assert after_pin["credential_type"] == "pin"

    # Now test: a SUCCESSFUL real-mode trigger's autofix.ran row MUST include
    # credential_type (now fixed to match the approval audit behavior).
    safe_rb = _make_runbook_record(
        alertname="TestAlertSafe", runbook_dir=tmp_path / "runbook", content_hash="h-safe"
    )
    await _insert_runbook(repo, safe_rb)

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        resp_safe = await authenticated_client.post(
            f"/api/runbooks/{safe_rb.id}/trigger",
            json={"mode": "real", "confirm_phrase": "runbook"},
            headers=_csrf(authenticated_client),
        )
    assert resp_safe.status_code == 200  # noqa: PLR2004

    ran_rows = await repo.fetch_all(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.ran'"),
        {},
    )
    assert len(ran_rows) == 1
    after_ran = json.loads(ran_rows[0][0])
    assert after_ran["credential_type"] == "phrase"


@pytest.mark.asyncio
async def test_refresh_prunes_removed_folder_and_surfaces_in_response(
    authenticated_client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Register folder, delete from disk, refresh. Response surfaces in pruned."""
    monkeypatch.setenv("HOMELAB_MONITOR_RUNBOOKS_DIR", str(tmp_path))

    # Register a folder
    folder = tmp_path / "foo"
    _write_runbook(folder, config=_valid_config_dict("foo"))

    resp = await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert str(folder) in data["registered"]

    # Delete folder from disk
    shutil.rmtree(folder)

    # Refresh again
    resp = await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()

    # Verify pruned in response
    assert str(folder) in data["pruned"]
    assert data["prune_skipped"] == []
    assert data["registered"] == []
    assert data["refreshed"] == []
    assert data["skipped"] == []

    # Verify GET /api/runbooks no longer has the folder
    resp = await authenticated_client.get("/api/runbooks")
    assert resp.status_code == 200  # noqa: PLR2004
    items = resp.json()["items"]
    assert not any(item["path"] == str(folder) for item in items)
