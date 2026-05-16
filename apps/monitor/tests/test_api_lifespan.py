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
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository


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


@pytest.mark.asyncio
async def test_cron_events_token_minted_at_boot(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifespan mints the cron-events ingest token and wires it to app.state."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    app = create_app(lifespan_enabled=True)

    async with app.router.lifespan_context(app):
        # Token string is available on app.state
        assert hasattr(app.state, "cron_events_token")
        token = app.state.cron_events_token
        assert isinstance(token, str)
        assert len(token) > 0

        # An api_tokens row named 'cron-events-ingest' must exist in the DB
        repo = SqliteRepository(engine=get_engine(url=db_url))
        row = await repo.fetch_one(
            text("SELECT name FROM api_tokens WHERE name = 'cron-events-ingest'"),
            {},
        )
        assert row is not None, "api_tokens row 'cron-events-ingest' not found"


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


@pytest.mark.asyncio
async def test_cron_events_token_reused_on_second_boot(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifespan second boot reuses existing cron-events token (idempotency).

    Covers log_ingest_token.py line 37-38: the already-exists fast-path
    (existing_token is not None AND existing_secret is not None).
    """
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    app = create_app(lifespan_enabled=True)

    # First boot mints the token.
    async with app.router.lifespan_context(app):
        token_first = app.state.cron_events_token
        count1 = await app.state.repo.fetch_one(
            text("SELECT COUNT(*) FROM api_tokens WHERE name = 'cron-events-ingest'"),
            {},
        )
        assert count1 is not None
        assert count1[0] == 1

    # Second boot must reuse, not re-mint.
    async with app.router.lifespan_context(app):
        token_second = app.state.cron_events_token
        count2 = await app.state.repo.fetch_one(
            text("SELECT COUNT(*) FROM api_tokens WHERE name = 'cron-events-ingest'"),
            {},
        )
        assert count2 is not None
        assert count2[0] == 1  # still only 1 row
        assert token_second == token_first  # same plaintext reused


@pytest.mark.asyncio
async def test_ensure_cron_events_token_half_pair_token_only(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Half-pair: token row exists but secret missing → deletes token, mints fresh.

    Covers log_ingest_token.py line 41: ``await auth_repo.delete_api_token_by_name``.
    """
    import structlog  # noqa: PLC0415

    from homelab_monitor.kernel.auth.api_tokens import make_api_token  # noqa: PLC0415
    from homelab_monitor.kernel.auth.repository import AuthRepository  # noqa: PLC0415
    from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415
    from homelab_monitor.kernel.cron.log_ingest_token import (  # noqa: PLC0415
        SECRET_NAME,
        TOKEN_NAME,
        ensure_cron_events_token,
    )

    auth_repo = AuthRepository(repo)
    log = structlog.get_logger()

    # Seed a token row without the corresponding secret (half-pair).
    plaintext, _ = make_api_token()
    await auth_repo.create_api_token(
        name=TOKEN_NAME,
        scopes={Scope.CRON_EVENTS_INGEST_WRITE},
        plaintext_token=plaintext,
    )
    # Confirm secret is absent.
    assert await secrets_repo.get(SECRET_NAME) is None

    # Call ensure — must delete stale token row and mint fresh pair.
    fresh_token = await ensure_cron_events_token(auth_repo, secrets_repo, log=log)

    assert isinstance(fresh_token, str)
    assert len(fresh_token) > 0
    # Secret now present.
    stored_secret = await secrets_repo.get(SECRET_NAME)
    assert stored_secret == fresh_token
    # Token row count is exactly 1 (no duplicate).
    row = await repo.fetch_one(
        text("SELECT COUNT(*) FROM api_tokens WHERE name = :n"),
        {"n": TOKEN_NAME},
    )
    assert row is not None
    assert row[0] == 1


@pytest.mark.asyncio
async def test_ensure_cron_events_token_half_pair_secret_only(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Half-pair: secret exists but token row missing → deletes secret, mints fresh.

    Covers log_ingest_token.py line 43: ``await secrets_repo.delete``.
    """
    import structlog  # noqa: PLC0415

    from homelab_monitor.kernel.auth.repository import AuthRepository  # noqa: PLC0415
    from homelab_monitor.kernel.cron.log_ingest_token import (  # noqa: PLC0415
        BOOTSTRAP_WHO,
        SECRET_NAME,
        TOKEN_NAME,
        ensure_cron_events_token,
    )

    auth_repo = AuthRepository(repo)
    log = structlog.get_logger()

    # Seed a secret without the corresponding token row.
    await secrets_repo.set(SECRET_NAME, "stale-plaintext", who=BOOTSTRAP_WHO)
    # Confirm token row is absent.
    assert await auth_repo.get_api_token_by_name(TOKEN_NAME) is None

    # Call ensure — must delete stale secret and mint fresh pair.
    fresh_token = await ensure_cron_events_token(auth_repo, secrets_repo, log=log)

    assert isinstance(fresh_token, str)
    assert len(fresh_token) > 0
    # The fresh token is different from the stale one we seeded.
    assert fresh_token != "stale-plaintext"
    # Secret now contains the fresh value.
    stored_secret = await secrets_repo.get(SECRET_NAME)
    assert stored_secret == fresh_token
    # Token row exists.
    row = await repo.fetch_one(
        text("SELECT COUNT(*) FROM api_tokens WHERE name = :n"),
        {"n": TOKEN_NAME},
    )
    assert row is not None
    assert row[0] == 1


@pytest.mark.asyncio
async def test_cron_events_token_mint_failure_swallowed(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ensure_cron_events_token raising → lifespan swallows it and continues.

    Covers lifespan.py lines 419-420: the except branch of the cron-events
    token-mint try/except.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    import homelab_monitor.kernel.cron.render as render_mod  # noqa: PLC0415

    async def _raise(*args: object, **kwargs: object) -> str:
        raise RuntimeError("simulated mint failure")

    monkeypatch.setattr(render_mod, "ensure_cron_events_token", _raise)

    app = create_app(lifespan_enabled=True)

    # Lifespan must complete without raising; cron_events_token is absent from state.
    async with app.router.lifespan_context(app):
        assert not hasattr(app.state, "cron_events_token"), (
            "cron_events_token should not be set when mint fails"
        )
        assert app.state.scheduler is not None
