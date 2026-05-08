"""Tests for lifespan alertmanager render integration."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock


@pytest.mark.asyncio
async def test_lifespan_renders_alertmanager_config_on_boot(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Lifespan renders template to output path with substituted token."""
    # Set up env vars pointing to tmp_path controlled files
    template_path = tmp_path / "template.yml"
    output_path = tmp_path / "output.yml"

    template_path.write_text("token: ${ALERTMANAGER_INGEST_TOKEN}\n")

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_TEMPLATE", str(template_path))
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_OUTPUT", str(output_path))
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        # Lifespan should have run render
        assert output_path.exists()
        content = output_path.read_text()
        assert "${ALERTMANAGER_INGEST_TOKEN}" not in content
        assert "token: homelab_" in content


@pytest.mark.asyncio
async def test_lifespan_idempotent_on_second_boot(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bring lifespan up+down+up again; only ONE alertmanager-ingest token row exists."""
    # Note: relies on dispose_engine() being called by lifespan finally + a
    # fresh get_engine() on second entry. The _reset_engine_singleton autouse
    # fixture in conftest only runs between tests, NOT between two lifespan
    # invocations within a single test.
    template_path = tmp_path / "template.yml"
    output_path = tmp_path / "output.yml"

    template_path.write_text("token: ${ALERTMANAGER_INGEST_TOKEN}\n")

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_TEMPLATE", str(template_path))
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_OUTPUT", str(output_path))
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    from sqlalchemy import text  # noqa: PLC0415

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)

    # First boot
    async with app.router.lifespan_context(app):
        # Count tokens
        count1 = await app.state.repo.fetch_one(
            text("SELECT COUNT(*) FROM api_tokens WHERE name = :n"),
            {"n": "alertmanager-ingest"},
        )
        assert count1 is not None
        assert count1[0] == 1

    # Second boot with same DB
    async with app.router.lifespan_context(app):
        # Count tokens again
        count2 = await app.state.repo.fetch_one(
            text("SELECT COUNT(*) FROM api_tokens WHERE name = :n"),
            {"n": "alertmanager-ingest"},
        )
        assert count2 is not None
        assert count2[0] == 1  # Still only 1, not 2


@pytest.mark.asyncio
async def test_lifespan_swallows_missing_template(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missing template → lifespan completes without raising; warning log fires."""
    output_path = tmp_path / "output.yml"

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_TEMPLATE", "/nonexistent/template")
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_OUTPUT", str(output_path))
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    # Should not raise
    async with app.router.lifespan_context(app):
        pass


@pytest.mark.asyncio
async def test_lifespan_skips_reload_when_url_disabled(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    """HOMELAB_MONITOR_ALERTMANAGER_URL=disabled → no HTTP calls made."""
    template_path = tmp_path / "template.yml"
    output_path = tmp_path / "output.yml"

    template_path.write_text("token: ${ALERTMANAGER_INGEST_TOKEN}\n")

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_TEMPLATE", str(template_path))
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_OUTPUT", str(output_path))
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    # httpx_mock will fail if any HTTP call is made (strict by default)
    async with app.router.lifespan_context(app):
        # Should complete without making any HTTP calls
        assert output_path.exists()


@pytest.mark.asyncio
async def test_lifespan_skips_reload_when_url_empty(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    """HOMELAB_MONITOR_ALERTMANAGER_URL="" → no HTTP calls made."""
    template_path = tmp_path / "template.yml"
    output_path = tmp_path / "output.yml"

    template_path.write_text("token: ${ALERTMANAGER_INGEST_TOKEN}\n")

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_TEMPLATE", str(template_path))
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_OUTPUT", str(output_path))
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "")

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    # httpx_mock will fail if any HTTP call is made (strict by default)
    async with app.router.lifespan_context(app):
        # Should complete without making any HTTP calls
        assert output_path.exists()
