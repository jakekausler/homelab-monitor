"""Tests for kernel/alertmanager/render.py — token bootstrap, render, reload."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from unittest import mock

import httpx
import pytest
import structlog
from pytest_httpx import HTTPXMock
from sqlalchemy import text

from homelab_monitor.kernel.alertmanager.render import (
    BOOTSTRAP_WHO,
    SECRET_NAME,
    TEMPLATE_PLACEHOLDER,
    TOKEN_NAME,
    AlertmanagerReloader,
    ensure_ingest_token,
    render_config,
    render_on_boot,
)
from homelab_monitor.kernel.auth.repository import AuthRepository
from homelab_monitor.kernel.auth.scopes import Scope
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository

# Two audit rows expected: api_token.create + secret.set, both with who=BOOTSTRAP_WHO.
_MIN_AUDIT_ROWS = 2


@pytest.mark.asyncio
async def test_ensure_ingest_token_mints_when_absent(
    repo: SqliteRepository, master_key: bytes
) -> None:
    """Fresh DB, no token, no secret → mint both api_token + secret rows."""
    auth_repo = AuthRepository(repo)
    secrets_repo = AsyncSecretsRepository(repo, master_key)
    log = structlog.get_logger()

    token_plaintext = await ensure_ingest_token(
        auth_repo,
        secrets_repo,
        log=log,  # type: ignore
    )

    # Verify api_tokens row exists
    token_row = await auth_repo.get_api_token_by_name(TOKEN_NAME)
    assert token_row is not None
    assert token_row.name == TOKEN_NAME
    assert Scope.ALERTS_INGEST_WRITE.value in token_row.scopes

    # Verify secret row exists
    secret_value = await secrets_repo.get(SECRET_NAME)
    assert secret_value == token_plaintext

    # Verify audit rows exist
    audit_count = await repo.fetch_one(
        text("SELECT COUNT(*) FROM audit_log WHERE who = :who"),
        {"who": BOOTSTRAP_WHO},
    )
    assert audit_count is not None
    assert audit_count[0] >= _MIN_AUDIT_ROWS  # token.create + secret.set


@pytest.mark.asyncio
async def test_ensure_ingest_token_reuses_when_both_present(
    repo: SqliteRepository, master_key: bytes
) -> None:
    """Pre-seed token + secret, call ensure → no new audit rows."""
    auth_repo = AuthRepository(repo)
    secrets_repo = AsyncSecretsRepository(repo, master_key)
    log = structlog.get_logger()

    # Pre-seed token and secret
    await ensure_ingest_token(auth_repo, secrets_repo, log=log)  # type: ignore

    # Count audit rows before second call
    audit_before = await repo.fetch_one(
        text("SELECT COUNT(*) FROM audit_log WHERE who = :who"),
        {"who": BOOTSTRAP_WHO},
    )
    before_count = audit_before[0] if audit_before else 0

    # Call ensure again
    token_plaintext = await ensure_ingest_token(
        auth_repo,
        secrets_repo,
        log=log,  # type: ignore
    )

    # Count audit rows after second call
    audit_after = await repo.fetch_one(
        text("SELECT COUNT(*) FROM audit_log WHERE who = :who"),
        {"who": BOOTSTRAP_WHO},
    )
    after_count = audit_after[0] if audit_after else 0

    # Should not have created new audit rows (both should equal)
    assert before_count == after_count
    # Plaintext should be the same
    secret_value = await secrets_repo.get(SECRET_NAME)
    assert secret_value == token_plaintext


@pytest.mark.asyncio
async def test_ensure_ingest_token_remints_when_secret_missing(
    repo: SqliteRepository, master_key: bytes
) -> None:
    """Token row present, secret absent → re-mints both."""
    auth_repo = AuthRepository(repo)
    secrets_repo = AsyncSecretsRepository(repo, master_key)
    log = structlog.get_logger()

    # First, mint a token
    await ensure_ingest_token(
        auth_repo,
        secrets_repo,
        log=log,  # type: ignore
    )

    # Now delete the secret (simulate operator cleanup)
    async with repo.transaction() as conn:
        await conn.execute(text("DELETE FROM secrets WHERE name = :n"), {"n": SECRET_NAME})

    # Call ensure again
    plaintext2 = await ensure_ingest_token(
        auth_repo,
        secrets_repo,
        log=log,  # type: ignore
    )

    # Should have created a new plaintext token
    assert plaintext2 is not None
    # Secret should exist now
    secret_value = await secrets_repo.get(SECRET_NAME)
    assert secret_value == plaintext2


@pytest.mark.asyncio
async def test_ensure_ingest_token_remints_when_token_row_missing(
    repo: SqliteRepository, master_key: bytes
) -> None:
    """Secret present, token row absent → re-mints."""
    auth_repo = AuthRepository(repo)
    secrets_repo = AsyncSecretsRepository(repo, master_key)
    log = structlog.get_logger()

    # First, mint a token
    await ensure_ingest_token(
        auth_repo,
        secrets_repo,
        log=log,  # type: ignore
    )

    # Now delete the token row (simulate operator cleanup)
    async with repo.transaction() as conn:
        await conn.execute(text("DELETE FROM api_tokens WHERE name = :n"), {"n": TOKEN_NAME})

    # Call ensure again
    await ensure_ingest_token(
        auth_repo,
        secrets_repo,
        log=log,  # type: ignore
    )

    # Token should exist now
    token_row = await auth_repo.get_api_token_by_name(TOKEN_NAME)
    assert token_row is not None


@pytest.mark.asyncio
async def test_ensure_ingest_token_never_logs_plaintext(
    repo: SqliteRepository, master_key: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    """Mint a token; plaintext never appears in any log record."""
    auth_repo = AuthRepository(repo)
    secrets_repo = AsyncSecretsRepository(repo, master_key)

    # Capture logs at INFO level for homelab_monitor
    with caplog.at_level(logging.INFO, logger="homelab_monitor"):
        log = structlog.get_logger()
        plaintext = await ensure_ingest_token(
            auth_repo,
            secrets_repo,
            log=log,  # type: ignore
        )

    # Plaintext should not appear in any log record
    for record in caplog.records:
        assert plaintext not in record.message


def test_render_config_substitutes_placeholder(tmp_path: Path) -> None:
    """Template has placeholder, rendered output contains the actual token."""
    template_file = tmp_path / "template.yml"
    output_file = tmp_path / "output.yml"
    template_file.write_text(f"token: {TEMPLATE_PLACEHOLDER}\n")

    log = structlog.get_logger()
    token = "test_token_12345"

    render_config(
        template_path=template_file,
        output_path=output_file,
        token=token,
        log=log,
    )

    # Verify output file exists and contains the token
    assert output_file.exists()
    content = output_file.read_text()
    assert token in content
    assert TEMPLATE_PLACEHOLDER not in content


def test_render_config_template_missing_raises(tmp_path: Path) -> None:
    """Template path absent → FileNotFoundError raised + warning log."""
    template_file = tmp_path / "nonexistent.yml"
    output_file = tmp_path / "output.yml"

    log = structlog.get_logger()

    with pytest.raises(FileNotFoundError):
        render_config(
            template_path=template_file,
            output_path=output_file,
            token="token",
            log=log,
        )


def test_render_config_creates_parent_dirs(tmp_path: Path) -> None:
    """Output parent doesn't exist → directory created and file written."""
    template_file = tmp_path / "template.yml"
    output_file = tmp_path / "nested" / "dir" / "output.yml"
    template_file.write_text(f"key: {TEMPLATE_PLACEHOLDER}\n")

    log = structlog.get_logger()
    token = "test_token"

    # Parent directory doesn't exist yet
    assert not output_file.parent.exists()

    render_config(
        template_path=template_file,
        output_path=output_file,
        token=token,
        log=log,
    )

    # Directory should be created
    assert output_file.parent.exists()
    assert output_file.exists()


@pytest.mark.asyncio
async def test_alertmanager_reloader_success(httpx_mock: HTTPXMock) -> None:
    """POST /-/reload returns 200 → returns True, emits ok log."""
    httpx_mock.add_response(status_code=200)

    log = structlog.get_logger()
    client = httpx.AsyncClient()
    reloader = AlertmanagerReloader(
        am_url="http://alertmanager:9093",
        http_client=client,
        log=log,
    )

    result = await reloader.reload()
    assert result is True
    await client.aclose()


@pytest.mark.asyncio
async def test_alertmanager_reloader_unreachable_returns_false(httpx_mock: HTTPXMock) -> None:
    """HTTP error (e.g., ECONNREFUSED) → returns False, emits unreachable log."""
    httpx_mock.add_exception(httpx.ConnectError("Connection refused"))

    log = structlog.get_logger()
    client = httpx.AsyncClient()
    reloader = AlertmanagerReloader(
        am_url="http://alertmanager:9093",
        http_client=client,
        log=log,
    )

    result = await reloader.reload()
    assert result is False
    await client.aclose()


@pytest.mark.asyncio
async def test_alertmanager_reloader_non_200_returns_false(httpx_mock: HTTPXMock) -> None:
    """503 response → returns False, emits non_200 log."""
    httpx_mock.add_response(status_code=503)

    log = structlog.get_logger()
    client = httpx.AsyncClient()
    reloader = AlertmanagerReloader(
        am_url="http://alertmanager:9093",
        http_client=client,
        log=log,
    )

    result = await reloader.reload()
    assert result is False
    await client.aclose()


@pytest.mark.asyncio
async def test_render_on_boot_swallows_render_errors(
    repo: SqliteRepository, master_key: bytes, tmp_path: Path
) -> None:
    """Missing template → function returns None (doesn't raise), logs warning."""
    auth_repo = AuthRepository(repo)
    secrets_repo = AsyncSecretsRepository(repo, master_key)
    log = structlog.get_logger()

    # Template path doesn't exist
    template_path = tmp_path / "nonexistent.yml"
    output_path = tmp_path / "output.yml"

    client = httpx.AsyncClient()
    result = await render_on_boot(
        auth_repo=auth_repo,
        secrets_repo=secrets_repo,
        template_path=template_path,
        output_path=output_path,
        am_url="http://alertmanager:9093",
        http_client=client,
        log=log,
    )
    await client.aclose()

    # Should not raise, should return None
    assert result is None


@pytest.mark.asyncio
async def test_render_on_boot_skips_reload_when_am_url_none(
    repo: SqliteRepository, master_key: bytes, tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    """am_url=None → no HTTP call made."""
    auth_repo = AuthRepository(repo)
    secrets_repo = AsyncSecretsRepository(repo, master_key)
    log = structlog.get_logger()

    # Create template file
    template_path = tmp_path / "template.yml"
    output_path = tmp_path / "output.yml"
    template_path.write_text(f"token: {TEMPLATE_PLACEHOLDER}\n")

    # Add strict httpx_mock with no URL matcher (will fail if any call is made)
    # httpx_mock is configured to reject unregistered URLs by default

    client = httpx.AsyncClient()
    await render_on_boot(
        auth_repo=auth_repo,
        secrets_repo=secrets_repo,
        template_path=template_path,
        output_path=output_path,
        am_url=None,  # No reload
        http_client=client,
        log=log,
    )
    await client.aclose()

    # Verify output file was created (token was rendered)
    assert output_path.exists()
    # Verify no HTTP requests were made (httpx_mock would fail if any were)


@pytest.mark.asyncio
async def test_render_on_boot_full_happy_path(
    repo: SqliteRepository, master_key: bytes, tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    """Fresh DB + valid template + mocked AM 200 → renders + reloads."""
    auth_repo = AuthRepository(repo)
    secrets_repo = AsyncSecretsRepository(repo, master_key)
    log = structlog.get_logger()

    # Create template file
    template_path = tmp_path / "template.yml"
    output_path = tmp_path / "output.yml"
    template_path.write_text(f"token: {TEMPLATE_PLACEHOLDER}\n")

    # Mock AM reload endpoint
    httpx_mock.add_response(status_code=200)

    client = httpx.AsyncClient()
    await render_on_boot(
        auth_repo=auth_repo,
        secrets_repo=secrets_repo,
        template_path=template_path,
        output_path=output_path,
        am_url="http://alertmanager:9093",
        http_client=client,
        log=log,
    )
    await client.aclose()

    # Verify output file exists with placeholder substituted
    assert output_path.exists()
    content = output_path.read_text()
    assert TEMPLATE_PLACEHOLDER not in content
    # Token should be in the file
    assert "token: homelab_" in content


def test_render_config_raises_on_write_failure(tmp_path: Path) -> None:
    """OSError during atomic replace → log warning + re-raise."""
    template_file = tmp_path / "alertmanager.yml.template"
    template_file.write_text(f"token: {TEMPLATE_PLACEHOLDER}\n")
    output_file = tmp_path / "alertmanager.yml"
    log = structlog.get_logger()

    with (
        mock.patch("os.replace", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        render_config(
            template_path=template_file,
            output_path=output_file,
            token="test-token-plaintext",
            log=log,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_render_on_boot_swallows_ensure_token_failure(tmp_path: Path) -> None:
    """ensure_ingest_token raising → render_on_boot logs error and returns (no raise)."""
    from unittest.mock import AsyncMock  # noqa: PLC0415

    log = structlog.get_logger()
    template_file = tmp_path / "alertmanager.yml.template"
    template_file.write_text(f"token: {TEMPLATE_PLACEHOLDER}\n")
    output_file = tmp_path / "alertmanager.yml"

    client = httpx.AsyncClient()
    with mock.patch(
        "homelab_monitor.kernel.alertmanager.render.ensure_ingest_token",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        result = await render_on_boot(
            auth_repo=None,  # type: ignore[arg-type]
            secrets_repo=None,  # type: ignore[arg-type]
            template_path=template_file,
            output_path=output_file,
            am_url=None,
            http_client=client,
            log=log,  # type: ignore[arg-type]
        )
    await client.aclose()

    assert result is None


def test_render_config_amconfig_group_missing(tmp_path: Path) -> None:
    """grp.getgrnam raises KeyError (amconfig group absent) → file still written."""
    template_file = tmp_path / "alertmanager.yml.template"
    output_file = tmp_path / "alertmanager.yml"
    template_file.write_text(f"token: {TEMPLATE_PLACEHOLDER}\n")
    log = structlog.get_logger()

    with (
        mock.patch(
            "homelab_monitor.kernel.alertmanager.render.grp.getgrnam",
            side_effect=KeyError("amconfig"),
        ),
        mock.patch("homelab_monitor.kernel.alertmanager.render.os.chown"),
    ):
        render_config(
            template_path=template_file,
            output_path=output_file,
            token="test-token",
            log=log,  # type: ignore[arg-type]
        )

    assert output_file.exists()
    assert "test-token" in output_file.read_text()


def test_render_config_amconfig_group_present_chowns_file(tmp_path: Path) -> None:
    """grp.getgrnam returns a group → file is chowned to that group's gid (line 136)."""
    template_file = tmp_path / "alertmanager.yml.template"
    output_file = tmp_path / "alertmanager.yml"
    template_file.write_text(f"token: {TEMPLATE_PLACEHOLDER}\n")
    log = structlog.get_logger()

    fake_group = mock.MagicMock()
    fake_group.gr_gid = 2000

    with (
        mock.patch(
            "homelab_monitor.kernel.alertmanager.render.grp.getgrnam",
            return_value=fake_group,
        ),
        mock.patch("homelab_monitor.kernel.alertmanager.render.os.chown") as mock_chown,
    ):
        render_config(
            template_path=template_file,
            output_path=output_file,
            token="test-token",
            log=log,  # type: ignore[arg-type]
        )

    mock_chown.assert_called_once_with(output_file, -1, 2000)
    assert output_file.exists()
    assert "test-token" in output_file.read_text()


def test_render_config_chown_oserror_logged(tmp_path: Path) -> None:
    """os.chown raises OSError → warning logged, file still written, no exception raised."""
    template_file = tmp_path / "alertmanager.yml.template"
    output_file = tmp_path / "alertmanager.yml"
    template_file.write_text(f"token: {TEMPLATE_PLACEHOLDER}\n")
    log = structlog.get_logger()

    fake_group = mock.MagicMock()
    fake_group.gr_gid = 2000

    with (
        mock.patch(
            "homelab_monitor.kernel.alertmanager.render.grp.getgrnam",
            return_value=fake_group,
        ),
        mock.patch(
            "homelab_monitor.kernel.alertmanager.render.os.chown",
            side_effect=PermissionError("test no chown cap"),
        ),
    ):
        render_config(
            template_path=template_file,
            output_path=output_file,
            token="test-token-chown-fail",
            log=log,  # type: ignore[arg-type]
        )

    assert output_file.exists()
    assert "test-token-chown-fail" in output_file.read_text()


# ---------------------------------------------------------------------------
# T6 — monitoring-health receiver in alertmanager template (STAGE-002-010)
# ---------------------------------------------------------------------------


def _alertmanager_template_path() -> Path:
    """Resolve the alertmanager template relative to the repo root."""
    # tests/ → apps/monitor/ → apps/ → homelab-monitor/ → deploy/alertmanager/
    return (
        Path(__file__).parent.parent.parent.parent
        / "deploy"
        / "alertmanager"
        / "alertmanager.yml.template"
    )


def test_render_config_monitoring_health_receiver_present(tmp_path: Path) -> None:
    """Rendered alertmanager config contains the monitoring-health-channel receiver."""
    import yaml  # noqa: PLC0415

    template_path = _alertmanager_template_path()
    output_file = tmp_path / "alertmanager.yml"
    log = structlog.get_logger()
    token = "test-token-t6"

    render_config(
        template_path=template_path,
        output_path=output_file,
        token=token,
        log=log,
    )

    content = output_file.read_text()

    # Rendered YAML must parse
    parsed = yaml.safe_load(content)
    assert parsed is not None

    # monitoring-health-channel receiver must be present
    receiver_names = {r["name"] for r in parsed.get("receivers", [])}
    assert "monitoring-health-channel" in receiver_names

    # Child route with routing_channel="monitoring-health" must be present
    routes = parsed.get("route", {}).get("routes", [])
    assert any(
        any(
            "routing_channel" in str(m) and "monitoring-health" in str(m)
            for m in r.get("matchers", [])
        )
        for r in routes
    ), "No child route with routing_channel=monitoring-health found"


def test_render_config_token_substituted_in_both_receivers(tmp_path: Path) -> None:
    """${ALERTMANAGER_INGEST_TOKEN} is replaced in BOTH receiver blocks."""
    template_path = _alertmanager_template_path()
    output_file = tmp_path / "alertmanager.yml"
    log = structlog.get_logger()
    token = "unique-test-token-xyz"

    render_config(
        template_path=template_path,
        output_path=output_file,
        token=token,
        log=log,
    )

    content = output_file.read_text()

    # No literal placeholder remains
    assert "${ALERTMANAGER_INGEST_TOKEN}" not in content
    # The token appears at least twice (once per receiver)
    assert content.count(token) >= 2  # noqa: PLR2004


def test_render_config_cleans_up_tmp_file_on_replace_failure(tmp_path: Path) -> None:
    """OSError during os.replace → temp file is unlinked, then re-raised."""
    template_file = tmp_path / "alertmanager.yml.template"
    template_file.write_text(f"token: {TEMPLATE_PLACEHOLDER}\n")
    output_file = tmp_path / "alertmanager.yml"
    log = structlog.get_logger()

    # Track tmp file paths created.
    tmp_files_seen: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def _spy_mkstemp(
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | None = None,
        text: bool = False,
    ) -> tuple[int, str]:
        fd, path = real_mkstemp(suffix=suffix, prefix=prefix, dir=dir, text=text)
        tmp_files_seen.append(path)
        return fd, path

    with (
        mock.patch("tempfile.mkstemp", side_effect=_spy_mkstemp),
        mock.patch("os.replace", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        render_config(
            template_path=template_file,
            output_path=output_file,
            token="test-token",
            log=log,  # type: ignore
        )

    # Verify the tmp file was created AND cleaned up
    assert len(tmp_files_seen) == 1
    assert not Path(tmp_files_seen[0]).exists(), "temp file should have been unlinked"
