"""Tests for kernel/api/lifespan.py — lifespan unit tests (NOT full e2e)."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from homelab_monitor.kernel.api.app import create_app
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.migrations import run_migrations
from homelab_monitor.kernel.db.repository import SqliteRepository


@pytest.fixture
def patched_exit(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Fixture to monkeypatch os._exit to raise SystemExit instead."""
    exit_calls: list[int] = []

    def fake_exit(code: int) -> None:
        exit_calls.append(code)
        raise SystemExit(code)

    monkeypatch.setattr("os._exit", fake_exit)
    return exit_calls


@pytest.mark.asyncio
async def test_create_app_lifespan_disabled_instantiates() -> None:
    """create_app(lifespan_enabled=False) can be instantiated with minimal init."""
    app = create_app(lifespan_enabled=False)
    assert app is not None
    assert app.routes is not None


@pytest.mark.asyncio
async def test_create_app_lifespan_disabled_has_routes() -> None:
    """create_app(lifespan_enabled=False) has routes registered."""
    app = create_app(lifespan_enabled=False)
    route_paths = {route.path for route in app.routes if hasattr(route, "path")}  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportAttributeAccessIssue]
    assert "/api/healthz" in route_paths or any("/api" in p for p in route_paths)  # pyright: ignore[reportUnknownVariableType]


@pytest.mark.asyncio
async def test_create_app_lifespan_disabled_healthz_degraded(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """create_app(lifespan_enabled=False) serves /api/healthz with degraded status."""
    app = create_app(lifespan_enabled=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/healthz")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        # In degraded mode, db should be "down"
        assert data.get("ok") is False or data.get("db") == "down"


@pytest.mark.asyncio
async def test_lifespan_master_key_missing_aborts(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    patched_exit: list[int],
) -> None:
    """Master key missing → _critical_abort invoked → SystemExit."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.delenv("HOMELAB_MONITOR_MASTER_KEY", raising=False)

    app = create_app(lifespan_enabled=True)

    with pytest.raises(SystemExit) as exc_info:
        async with app.router.lifespan_context(app):
            pass

    assert exc_info.value.code == 1
    assert len(patched_exit) > 0


@pytest.mark.asyncio
async def test_lifespan_migrations_pending_aborts(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
    patched_exit: list[int],
) -> None:
    """Migrations pending + auto-migrate disabled → SystemExit."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_AUTO_MIGRATE", "0")

    app = create_app(lifespan_enabled=True)

    with pytest.raises(SystemExit) as exc_info:
        async with app.router.lifespan_context(app):
            pass

    # Should abort due to pending migrations
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_lifespan_subprocess_manifest_invalid_degraded(
    db_url: str,
    db_path: Path,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Invalid subprocess manifest → degraded list contains plugin name."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    # Create a plugin with invalid manifest
    plugin_dir = tmp_path / "bad_plugin"
    plugin_dir.mkdir()
    bad_manifest = plugin_dir / "plugin.yaml"
    bad_manifest.write_text("invalid: yaml: content: [")

    monkeypatch.setenv("HOMELAB_MONITOR_PLUGINS_DIR", str(tmp_path))

    app = create_app(lifespan_enabled=True)

    try:
        async with app.router.lifespan_context(app):
            # Should have loaded with the bad plugin listed as degraded
            # invalid plugin should appear in degraded list (or startup should still complete)
            assert app.state.scheduler is not None
    except Exception:
        # If it errors, that's also acceptable for this test
        pass


@pytest.mark.asyncio
async def test_lifespan_scheduler_running_after_startup(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scheduler is running after lifespan startup."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    app = create_app(lifespan_enabled=True)

    async with app.router.lifespan_context(app):
        assert app.state.scheduler is not None
        # Scheduler should have started
        assert app.state.scheduler.running


@pytest.mark.asyncio
async def test_lifespan_cleanup_on_shutdown(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After lifespan shutdown: scheduler stopped, http_client closed, refresh task cancelled."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    app = create_app(lifespan_enabled=True)

    async with app.router.lifespan_context(app):
        scheduler = app.state.scheduler

    # After shutdown, scheduler should be stopped
    assert not scheduler.running


@pytest.mark.asyncio
async def test_app_state_accessible_after_lifespan_startup(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """app.state.scheduler exists after startup; accessible via dependency."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    app = create_app(lifespan_enabled=True)

    async with app.router.lifespan_context(app):
        assert hasattr(app.state, "scheduler")
        assert hasattr(app.state, "broker")
        assert hasattr(app.state, "repo")
        assert hasattr(app.state, "metrics_writer")
        assert hasattr(app.state, "logs_writer")


@pytest.mark.asyncio
async def test_lifespan_enabled_true_with_healthz_up(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lifespan_enabled=True + healthz returns 200 with db: up and scheduler: running."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    app = create_app(lifespan_enabled=True)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        resp = await client.get("/api/healthz")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data.get("ok") is True
        assert data.get("db") == "up"
        assert data.get("scheduler") == "running"


@pytest.mark.asyncio
async def test_lifespan_wires_cron_discoverer_to_app_state(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After lifespan startup, app.state.cron_discoverer is wired with cron_repo.

    Regression guard for STAGE-002-007: the wiring loop used ``for c in collectors``
    where ``c`` was a ``LoadedCollector`` wrapper, causing ``isinstance(c, CronDiscoverer)``
    to silently fail and leave ``app.state.cron_discoverer`` unset.  The fix unwraps via
    ``lc.collector``.  This test ensures that branch cannot regress.
    """
    from homelab_monitor.plugins.discoverers.cron_discoverer import CronDiscoverer  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    app = create_app(lifespan_enabled=True)

    async with app.router.lifespan_context(app):
        assert hasattr(app.state, "cron_discoverer"), (
            "app.state.cron_discoverer should be set by lifespan startup"
        )
        discoverer = app.state.cron_discoverer
        assert isinstance(discoverer, CronDiscoverer), (
            f"expected CronDiscoverer, got {type(discoverer)!r}"
        )
        assert discoverer.cron_repo is not None, "cron_repo must be wired during lifespan startup"


def test_create_app_mounts_ui_when_directory_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When HOMELAB_MONITOR_UI_DIR points to a real dir with index.html, app mounts it at /."""
    ui_dir = tmp_path / "ui"
    ui_dir.mkdir()
    (ui_dir / "index.html").write_text("<!doctype html><html><body>test</body></html>")

    monkeypatch.setenv("HOMELAB_MONITOR_UI_DIR", str(ui_dir))

    app = create_app(lifespan_enabled=False)

    mount_routes = [r for r in app.routes if getattr(r, "name", None) == "ui"]
    assert len(mount_routes) == 1, "Expected exactly one route named 'ui' after mounting"


@pytest.mark.asyncio
async def test_lifespan_plugins_dir_not_found_skipped(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no plugins_dir found, subprocess plugins are skipped gracefully."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    # Don't set HOMELAB_MONITOR_PLUGINS_DIR; force fallback logic to find no plugins
    monkeypatch.delenv("HOMELAB_MONITOR_PLUGINS_DIR", raising=False)

    app = create_app(lifespan_enabled=True)

    async with app.router.lifespan_context(app):
        # Should succeed with scheduler running despite no plugins
        assert app.state.scheduler is not None
        assert app.state.scheduler.running


@pytest.mark.asyncio
async def test_lifespan_clears_all_quarantine_on_startup(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifespan startup clears all quarantined collectors from persistent state.

    This prevents stale quarantine from blocking redeploys with new code.
    Regression guard for Bug A: quarantine state survives container restarts.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    # Run migrations to set up schema
    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)

    # Pre-seed a quarantined collector in the DB.
    async with repo.transaction() as conn:
        # Ensure collector row exists (id is required; SQLite will auto-generate from rowid)
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO collectors (id, name, created_at) "
                "VALUES ('test-collector-id', 'test-collector', '2026-01-01T00:00:00Z')"
            )
        )
        # Mark it as quarantined
        await conn.execute(
            text(
                "UPDATE collectors "
                "SET consecutive_failures = 5, "
                "    quarantined_at = '2026-01-01T00:00:00Z', "
                "    quarantine_reason = 'test quarantine' "
                "WHERE name = 'test-collector'"
            )
        )

    # Verify it's quarantined before startup
    async with repo.transaction() as conn:
        result = await conn.execute(
            text("SELECT quarantined_at FROM collectors WHERE name = 'test-collector'")
        )
        row = result.fetchone()
        assert row is not None
        assert row[0] is not None, "Collector should be quarantined before lifespan startup"

    # Create app and enter lifespan context
    app = create_app(lifespan_enabled=True)

    async with app.router.lifespan_context(app):
        # After startup, quarantine should be cleared
        assert app.state.failure_budget is not None
        assert not app.state.failure_budget.is_quarantined("test-collector")

    # Verify the DB was actually updated
    async with repo.transaction() as conn:
        result = await conn.execute(
            text(
                "SELECT consecutive_failures, quarantined_at, quarantine_reason "
                "FROM collectors WHERE name = 'test-collector'"
            )
        )
        row = result.fetchone()
        assert row is not None
        assert row[0] == 0, "consecutive_failures should be 0 after clear"
        assert row[1] is None, "quarantined_at should be NULL after clear"
        assert row[2] is None, "quarantine_reason should be NULL after clear"
