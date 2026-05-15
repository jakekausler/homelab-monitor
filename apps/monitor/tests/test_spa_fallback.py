"""Tests for SPA fallback behavior.

Verifies that the catch-all route correctly:
- Returns index.html for non-existent client-side routes (SPA fallback)
- Preserves real asset files
- Does not shadow API routes
- Rejects API/observability paths that don't exist
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from homelab_monitor.kernel.api.app import create_app

TEST_INDEX_MARKER = "<!-- SPA Test Index -->"
TEST_ASSET_CONTENT = "/* test asset file */"


@pytest.mark.asyncio
async def test_spa_fallback_nonexistent_route(
    tmp_path: Path, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /inventory/crons (non-existent route) returns index.html (200, HTML)."""
    # Setup: create minimal UI directory with index.html and asset
    ui_dir = tmp_path / "ui"
    ui_dir.mkdir()
    index_html = ui_dir / "index.html"
    index_html.write_text(f"<html>{TEST_INDEX_MARKER}</html>")

    db_url = "sqlite+aiosqlite:///:memory:"
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_UI_DIR", str(ui_dir))
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/inventory/crons")
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.headers.get("content-type") == "text/html; charset=utf-8"
        assert TEST_INDEX_MARKER in resp.text


@pytest.mark.asyncio
async def test_spa_fallback_login_route(
    tmp_path: Path, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /login (non-existent route) returns index.html (200, HTML)."""
    ui_dir = tmp_path / "ui"
    ui_dir.mkdir()
    index_html = ui_dir / "index.html"
    index_html.write_text(f"<html>{TEST_INDEX_MARKER}</html>")

    db_url = "sqlite+aiosqlite:///:memory:"
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_UI_DIR", str(ui_dir))
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/login")
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.headers.get("content-type") == "text/html; charset=utf-8"
        assert TEST_INDEX_MARKER in resp.text


@pytest.mark.asyncio
async def test_spa_fallback_root_route(
    tmp_path: Path, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET / returns index.html (200, HTML)."""
    ui_dir = tmp_path / "ui"
    ui_dir.mkdir()
    index_html = ui_dir / "index.html"
    index_html.write_text(f"<html>{TEST_INDEX_MARKER}</html>")

    db_url = "sqlite+aiosqlite:///:memory:"
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_UI_DIR", str(ui_dir))
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/")
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.headers.get("content-type") == "text/html; charset=utf-8"
        assert TEST_INDEX_MARKER in resp.text


@pytest.mark.asyncio
async def test_spa_fallback_preserves_real_assets(
    tmp_path: Path, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /assets/<real-file> returns the file (not index.html)."""
    ui_dir = tmp_path / "ui"
    ui_dir.mkdir()
    index_html = ui_dir / "index.html"
    index_html.write_text(f"<html>{TEST_INDEX_MARKER}</html>")

    assets_dir = ui_dir / "assets"
    assets_dir.mkdir()
    asset_file = assets_dir / "app.css"
    asset_file.write_text(TEST_ASSET_CONTENT)

    db_url = "sqlite+aiosqlite:///:memory:"
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_UI_DIR", str(ui_dir))
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/assets/app.css")
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.text == TEST_ASSET_CONTENT
        assert TEST_INDEX_MARKER not in resp.text


@pytest.mark.asyncio
async def test_spa_fallback_rejects_api_misses(
    tmp_path: Path, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /api/<nonexistent> returns 404 JSON (not HTML fallback)."""
    ui_dir = tmp_path / "ui"
    ui_dir.mkdir()
    index_html = ui_dir / "index.html"
    index_html.write_text(f"<html>{TEST_INDEX_MARKER}</html>")

    db_url = "sqlite+aiosqlite:///:memory:"
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_UI_DIR", str(ui_dir))
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/nonexistent-endpoint")
        assert resp.status_code == 404  # noqa: PLR2004
        # Should be JSON (FastAPI default), not HTML
        assert resp.headers.get("content-type") == "application/json"
        assert TEST_INDEX_MARKER not in resp.text


@pytest.mark.asyncio
async def test_spa_fallback_rejects_metrics_misses(
    tmp_path: Path, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /metrics (nonexistent) returns 404 JSON (not HTML fallback)."""
    ui_dir = tmp_path / "ui"
    ui_dir.mkdir()
    index_html = ui_dir / "index.html"
    index_html.write_text(f"<html>{TEST_INDEX_MARKER}</html>")

    db_url = "sqlite+aiosqlite:///:memory:"
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_UI_DIR", str(ui_dir))
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/metrics/nonexistent")
        assert resp.status_code == 404  # noqa: PLR2004
        # Should be JSON (FastAPI default), not HTML
        assert resp.headers.get("content-type") == "application/json"
        assert TEST_INDEX_MARKER not in resp.text


@pytest.mark.asyncio
async def test_spa_fallback_rejects_post_nonexistent(
    tmp_path: Path, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /inventory/crons (non-existent route, wrong method) returns 404 or 405."""
    ui_dir = tmp_path / "ui"
    ui_dir.mkdir()
    index_html = ui_dir / "index.html"
    index_html.write_text(f"<html>{TEST_INDEX_MARKER}</html>")

    db_url = "sqlite+aiosqlite:///:memory:"
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_UI_DIR", str(ui_dir))
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/inventory/crons")
        # Catch-all is GET-only, so POST should fail (405 Method Not Allowed is typical)
        assert resp.status_code in (404, 405)
        # Should not return HTML (SPA fallback is GET-only)
        assert TEST_INDEX_MARKER not in resp.text


@pytest.mark.asyncio
async def test_spa_fallback_ui_directory_missing(
    tmp_path: Path, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When UI directory is missing, app still starts (no SPA fallback)."""
    nonexistent_ui_dir = tmp_path / "nonexistent"

    db_url = "sqlite+aiosqlite:///:memory:"
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_UI_DIR", str(nonexistent_ui_dir))
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    # Should not raise an exception
    app = create_app(lifespan_enabled=False)
    assert app is not None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Without UI mounted, /nonexistent-route should return 404
        resp = await client.get("/nonexistent-route")
        assert resp.status_code == 404  # noqa: PLR2004
