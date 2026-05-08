"""Tests for POST /api/admin/backup."""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text


def _make_vm_data_dir(tmp_path: Path, snapshot_name: str) -> Path:
    """Create a fake VM data dir with one snapshot tree."""
    vm = tmp_path / "vm-data"
    target = vm / "snapshots" / snapshot_name
    target.mkdir(parents=True)
    (target / "part-0001.bin").write_bytes(b"x" * 32)
    return vm


@pytest.mark.asyncio
async def test_backup_requires_auth() -> None:
    """POST without session/token returns 401."""
    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/admin/backup")
        assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_backup_cookie_auth_csrf_required(authenticated_client: AsyncClient) -> None:
    """Cookie-authed POST without CSRF header returns 403."""
    resp = await authenticated_client.post("/api/admin/backup")
    assert resp.status_code == 403  # noqa: PLR2004


@pytest.mark.asyncio
async def test_backup_token_without_admin_scope_403(api_token_client: AsyncClient) -> None:
    """Token without admin:backup:write returns 403."""
    resp = await api_token_client.post("/api/admin/backup")
    assert resp.status_code == 403  # noqa: PLR2004


@pytest.mark.asyncio
async def test_backup_with_admin_token_succeeds(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Token with admin:backup:write produces a backup and an audit row."""
    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415
    from homelab_monitor.kernel.auth.api_tokens import make_api_token  # noqa: PLC0415
    from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415
    from homelab_monitor.kernel.backup.service import BackupService  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    snapshot_name = "20260508_084812-test"
    vm_data_dir = _make_vm_data_dir(tmp_path, snapshot_name)
    backup_root = tmp_path / "backups"

    # Override the lifespan-built BackupService with a test one that uses a MockTransport
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "snapshot": snapshot_name})

    transport = httpx.MockTransport(handler)
    test_http_client = httpx.AsyncClient(transport=transport, base_url="http://x")

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        # Replace the lifespan-built backup_service with a test instance pointing at tmp dirs.
        # Use the SQLite DB the lifespan opened (sqlite_path inferred from db_url).
        prefix = "sqlite+aiosqlite:///"
        sqlite_db_path = Path(db_url[len(prefix) :]) if db_url.startswith(prefix) else Path(db_url)
        app.state.backup_service = BackupService(
            db_path=sqlite_db_path,
            vm_url="http://x",
            vm_data_dir=vm_data_dir,
            backup_root=backup_root,
            http_client=test_http_client,
        )

        plaintext, _ = make_api_token(prefix="adm")
        await app.state.auth_repo.create_api_token(
            name="admin-token",
            scopes={Scope.ADMIN_BACKUP_WRITE},
            plaintext_token=plaintext,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            resp = await client.post("/api/admin/backup")
            assert resp.status_code == 200, resp.text  # noqa: PLR2004
            body = resp.json()
            assert body["snapshot_id"]
            assert body["sqlite_path"]
            assert body["vm_snapshot_path"]
            assert body["errors"] == []

            # Audit row written
            async with app.state.repo.engine.begin() as conn:
                rows = (
                    await conn.execute(
                        text("SELECT who, what, after_json FROM audit_log WHERE what = :w"),
                        {"w": "admin.backup_run"},
                    )
                ).all()
            assert len(rows) == 1
            assert rows[0].who.startswith("api-token:")

    await test_http_client.aclose()


@pytest.mark.asyncio
async def test_backup_cookie_with_csrf_succeeds(
    authenticated_client: AsyncClient,
    tmp_path: Path,
) -> None:
    """Cookie-authed POST with valid CSRF + admin grant succeeds (operator path)."""
    from homelab_monitor.kernel.backup.service import BackupService  # noqa: PLC0415

    snapshot_name = "20260508_084813-cook"
    vm_data_dir = _make_vm_data_dir(tmp_path, snapshot_name)
    backup_root = tmp_path / "backups-cookie"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "snapshot": snapshot_name})

    transport = httpx.MockTransport(handler)
    test_http_client = httpx.AsyncClient(transport=transport, base_url="http://x")

    # Replace the BackupService on the live app
    app = authenticated_client._transport.app  # pyright: ignore[reportPrivateUsage,reportUnknownMemberType,reportAttributeAccessIssue,reportUnknownVariableType]
    sqlite_db_path = Path(
        app.state.repo.engine.url.database  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownArgumentType,reportUnknownVariableType]
    )
    app.state.backup_service = BackupService(  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
        db_path=sqlite_db_path,
        vm_url="http://x",
        vm_data_dir=vm_data_dir,
        backup_root=backup_root,
        http_client=test_http_client,
    )

    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/admin/backup",
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == 200, resp.text  # noqa: PLR2004
    body = resp.json()
    assert body["snapshot_id"]

    await test_http_client.aclose()
