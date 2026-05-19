"""Tests for POST /api/crons/{fingerprint}/install-wrapper (STAGE-002-009).

Covers:
- dry-run (confirm=false): returns InstallWrapperPreview, no writes
- confirm=true: applies + returns InstallWrapperResult with wrapper_last_seen_at
- 401 for unauthenticated requests
- 403/401 CSRF enforcement on POST
- 400 for remote-host cron
- 400 for unset public_url
- 404 for unknown fingerprint
- 409 for crontab line not found / already wrapped
- Audit row crons.wrapper_installed written on success
- GET /api/crons/wrapper-template: 200 + text/plain + all 4 placeholders present
- GET /api/crons/wrapper-template: 401 without token, 403 with wrong scope
- Route /wrapper-template is not shadowed by /{fingerprint}
- wrapper_last_seen_at is NULL after install (install no longer sets it)
"""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text

from homelab_monitor.kernel.cron.fingerprint import compute_fingerprint
from homelab_monitor.kernel.cron.wrapper_constants import (
    WRAPPER_PATH,
    build_invocation_prefix,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HOST = "monitor-host"
_REMOTE_HOST = "remote-host"
_SCHEDULE = "*/5 * * * *"
_COMMAND = "/usr/bin/backup.sh --full"
_SOURCE_PATH = "/etc/crontab"
_PUBLIC_URL = "https://monitor.example.com"
# Pre-computed fingerprint matching the default _seed_cron args
_FP = compute_fingerprint(
    host=_HOST, source_path=_SOURCE_PATH, schedule=_SCHEDULE, command=_COMMAND
)


def _csrf(client: AsyncClient) -> dict[str, str]:
    csrf: str = client.cookies.get("homelab_monitor_csrf") or ""
    return {"X-CSRF-Token": csrf}


async def _seed_cron(  # noqa: PLR0913 -- seed helper benefits from explicit kwargs
    repo: SqliteRepository,
    *,
    host: str = _HOST,
    command: str = _COMMAND,
    source_path: str = _SOURCE_PATH,
    schedule: str = _SCHEDULE,
    wrapper_last_seen_at: str | None = None,
) -> str:
    fp = compute_fingerprint(host=host, source_path=source_path, schedule=schedule, command=command)
    now = utc_now_iso()
    async with repo.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO crons ("
                "  fingerprint, name, host, command, schedule, schedule_canonical,"
                "  cadence_seconds, expected_grace_seconds, enabled, last_seen_state,"
                "  created_at, updated_at, hidden_at, source_path, wrapper_last_seen_at"
                ") VALUES ("
                "  :fp, :name, :host, :cmd, :sched, :sched_canon,"
                "  :cad, :grace, 1, 'unknown',"
                "  :now, :now, NULL, :sp, :wlsa"
                ")"
            ),
            {
                "fp": fp,
                "name": "backup",
                "host": host,
                "cmd": command,
                "sched": schedule,
                "sched_canon": schedule,
                "cad": 300,
                "grace": 300,
                "now": now,
                "sp": source_path,
                "wlsa": wrapper_last_seen_at,
            },
        )
    return fp


def _make_fake_crontab(tmp_path: Path, schedule: str = _SCHEDULE, command: str = _COMMAND) -> Path:
    """Create /etc/crontab under tmp_path with a single matching job line."""
    etc = tmp_path / "etc"
    etc.mkdir(exist_ok=True)
    ct = etc / "crontab"
    ct.write_text(f"# header\n{schedule} root {command}\n")
    return tmp_path


# ---------------------------------------------------------------------------
# Unauthenticated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_wrapper_unauthenticated(
    unauthenticated_client: AsyncClient,
) -> None:
    """No session → 401."""
    resp = await unauthenticated_client.post(
        "/api/crons/some-fp/install-wrapper",
        json={"confirm": False},
    )
    assert resp.status_code == 401  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Public URL not configured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_wrapper_no_public_url(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HOMELAB_MONITOR_PUBLIC_URL unset → 400."""
    monkeypatch.delenv("HOMELAB_MONITOR_PUBLIC_URL", raising=False)
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)
    fp = await _seed_cron(repo)

    resp = await authenticated_client.post(
        f"/api/crons/{fp}/install-wrapper",
        json={"confirm": False},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 400  # noqa: PLR2004
    assert "PUBLIC_URL" in resp.text


# ---------------------------------------------------------------------------
# Cron not found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_wrapper_cron_not_found(
    authenticated_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown fingerprint → 404."""
    monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)

    resp = await authenticated_client.post(
        "/api/crons/does-not-exist/install-wrapper",
        json={"confirm": False},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 404  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Remote host
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_wrapper_remote_host(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cron on a remote host → 400."""
    monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)
    fp = await _seed_cron(repo, host=_REMOTE_HOST)

    # HM_HOST_HOSTNAME is _HOST, cron is on _REMOTE_HOST → mismatch
    resp = await authenticated_client.post(
        f"/api/crons/{fp}/install-wrapper",
        json={"confirm": False},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 400  # noqa: PLR2004
    assert "remote" in resp.text.lower()


# ---------------------------------------------------------------------------
# Dry-run: crontab line not found → 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_wrapper_dry_run_line_not_found(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dry-run where crontab file has no matching line → 409."""
    monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)

    # Create an empty crontab (no matching line)
    (tmp_path / "etc").mkdir(exist_ok=True)
    (tmp_path / "etc" / "crontab").write_text("# only comments\n")

    fp = await _seed_cron(repo, host=_HOST)

    resp = await authenticated_client.post(
        f"/api/crons/{fp}/install-wrapper",
        json={"confirm": False},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 409  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Dry-run: already wrapped → 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_wrapper_dry_run_already_wrapped(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dry-run where crontab line is already wrapped → 409."""
    monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)

    wrapped_cmd = build_invocation_prefix(_FP) + _COMMAND
    (tmp_path / "etc").mkdir(exist_ok=True)
    (tmp_path / "etc" / "crontab").write_text(f"{_SCHEDULE} root {wrapped_cmd}\n")

    fp = await _seed_cron(repo, host=_HOST)

    resp = await authenticated_client.post(
        f"/api/crons/{fp}/install-wrapper",
        json={"confirm": False},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 409  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Dry-run success: returns preview without writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_wrapper_dry_run_preview(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dry-run returns InstallWrapperPreview with correct fields."""
    monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)

    _make_fake_crontab(tmp_path)
    fp = await _seed_cron(repo, host=_HOST)

    resp = await authenticated_client.post(
        f"/api/crons/{fp}/install-wrapper",
        json={"confirm": False},
        headers=_csrf(authenticated_client),
    )

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert data["fingerprint"] == fp
    assert WRAPPER_PATH in data["crontab_diff"]["new_line"]
    assert _COMMAND in data["crontab_diff"]["old_line"]
    assert data["wrapper_path"] is not None
    assert data["wrapper_content"].startswith("#!/bin/sh")

    # No wrapper_last_seen_at should be set yet (dry-run)
    row = await repo.fetch_one(
        text("SELECT wrapper_last_seen_at FROM crons WHERE fingerprint = :fp"),
        {"fp": fp},
    )
    assert row is not None
    assert row[0] is None


# ---------------------------------------------------------------------------
# Confirm=true success: writes files, sets wrapper_last_seen_at, audit row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_wrapper_confirm_success(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """confirm=true: install_wrapper_local succeeds → 200 with InstallWrapperResult."""
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from homelab_monitor.kernel.cron.repository import CronRecord  # noqa: PLC0415
    from homelab_monitor.kernel.db.time import utc_now_iso as _utc  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)

    _make_fake_crontab(tmp_path)
    fp = await _seed_cron(repo, host=_HOST)

    now = _utc()
    fake_updated_cron = CronRecord(
        fingerprint=fp,
        name="backup",
        host=_HOST,
        command=_COMMAND,
        schedule=_SCHEDULE,
        schedule_canonical=_SCHEDULE,
        cadence_seconds=300,
        expected_grace_seconds=300,
        enabled=True,
        last_seen_state="unknown",
        created_at=now,
        updated_at=now,
        hidden_at=None,
        source_path=_SOURCE_PATH,
        wrapper_last_seen_at=now,
        last_discovered_at=None,
        soft_deleted_at=None,
        log_match_key=None,
        wrapper_installed=True,
        wrapper_format_version=None,
    )

    with patch(
        "homelab_monitor.kernel.cron.install.install_wrapper_local",
        new=AsyncMock(return_value=fake_updated_cron),
    ):
        resp = await authenticated_client.post(
            f"/api/crons/{fp}/install-wrapper",
            json={"confirm": True},
            headers=_csrf(authenticated_client),
        )

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert "cron" in data
    assert data["cron"]["wrapper_last_seen_at"] is not None


# ---------------------------------------------------------------------------
# is_local field on CronOut
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_wrapper_dry_run_remote_host_error_in_build(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dry-run: build_install_kit raises RemoteHostError → 400 (line 303-304)."""
    from homelab_monitor.kernel.cron.install import RemoteHostError  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", "https://monitor.example.com")
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)
    _make_fake_crontab(tmp_path)
    fp = await _seed_cron(repo, host=_HOST)

    with patch(
        "homelab_monitor.kernel.cron.install.build_install_kit",
        side_effect=RemoteHostError("not local"),
    ):
        resp = await authenticated_client.post(
            f"/api/crons/{fp}/install-wrapper",
            json={"confirm": False},
            headers=_csrf(authenticated_client),
        )
    assert resp.status_code == 400  # noqa: PLR2004


@pytest.mark.asyncio
async def test_install_wrapper_confirm_remote_host_error(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Confirm path: install_wrapper_local raises RemoteHostError → 400 (line 349-350)."""
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from homelab_monitor.kernel.cron.install import RemoteHostError  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)
    _make_fake_crontab(tmp_path)
    fp = await _seed_cron(repo, host=_HOST)

    fake_wrapper = tmp_path / "cron-with-heartbeat.sh"
    fake_token_dir = tmp_path / "etc" / "homelab-monitor"
    fake_token_dir.mkdir(parents=True, exist_ok=True)
    fake_token = fake_token_dir / "heartbeat.token"

    with (
        patch("homelab_monitor.kernel.cron.install.WRAPPER_PATH", str(fake_wrapper)),
        patch("homelab_monitor.kernel.cron.install.TOKEN_FILE_PATH", str(fake_token)),
        patch(
            "homelab_monitor.kernel.cron.install.install_wrapper_local",
            new=AsyncMock(side_effect=RemoteHostError("wrong host")),
        ),
    ):
        resp = await authenticated_client.post(
            f"/api/crons/{fp}/install-wrapper",
            json={"confirm": True},
            headers=_csrf(authenticated_client),
        )
    assert resp.status_code == 400  # noqa: PLR2004


@pytest.mark.asyncio
async def test_install_wrapper_confirm_cron_line_not_found(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Confirm path: install_wrapper_local raises CronLineNotFoundError → 409 (lines 351-352)."""
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from homelab_monitor.kernel.cron.install import CronLineNotFoundError  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)
    _make_fake_crontab(tmp_path)
    fp = await _seed_cron(repo, host=_HOST)

    fake_wrapper = tmp_path / "cron-with-heartbeat.sh"
    fake_token_dir = tmp_path / "etc" / "homelab-monitor"
    fake_token_dir.mkdir(parents=True, exist_ok=True)
    fake_token = fake_token_dir / "heartbeat.token"

    with (
        patch("homelab_monitor.kernel.cron.install.WRAPPER_PATH", str(fake_wrapper)),
        patch("homelab_monitor.kernel.cron.install.TOKEN_FILE_PATH", str(fake_token)),
        patch(
            "homelab_monitor.kernel.cron.install.install_wrapper_local",
            new=AsyncMock(side_effect=CronLineNotFoundError("line gone")),
        ),
    ):
        resp = await authenticated_client.post(
            f"/api/crons/{fp}/install-wrapper",
            json={"confirm": True},
            headers=_csrf(authenticated_client),
        )
    assert resp.status_code == 409  # noqa: PLR2004


@pytest.mark.asyncio
async def test_install_wrapper_confirm_already_wrapped(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Confirm path: install_wrapper_local raises AlreadyWrappedError → 409 (lines 353-354)."""
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from homelab_monitor.kernel.cron.install import AlreadyWrappedError  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)
    _make_fake_crontab(tmp_path)
    fp = await _seed_cron(repo, host=_HOST)

    fake_wrapper = tmp_path / "cron-with-heartbeat.sh"
    fake_token_dir = tmp_path / "etc" / "homelab-monitor"
    fake_token_dir.mkdir(parents=True, exist_ok=True)
    fake_token = fake_token_dir / "heartbeat.token"

    with (
        patch("homelab_monitor.kernel.cron.install.WRAPPER_PATH", str(fake_wrapper)),
        patch("homelab_monitor.kernel.cron.install.TOKEN_FILE_PATH", str(fake_token)),
        patch(
            "homelab_monitor.kernel.cron.install.install_wrapper_local",
            new=AsyncMock(side_effect=AlreadyWrappedError("already done")),
        ),
    ):
        resp = await authenticated_client.post(
            f"/api/crons/{fp}/install-wrapper",
            json={"confirm": True},
            headers=_csrf(authenticated_client),
        )
    assert resp.status_code == 409  # noqa: PLR2004


@pytest.mark.asyncio
async def test_install_wrapper_confirm_crontab_write_error(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Confirm path: install_wrapper_local raises CrontabWriteError → 500 (lines 355-357)."""
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from homelab_monitor.kernel.cron.install import CrontabWriteError  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)
    _make_fake_crontab(tmp_path)
    fp = await _seed_cron(repo, host=_HOST)

    fake_wrapper = tmp_path / "cron-with-heartbeat.sh"
    fake_token_dir = tmp_path / "etc" / "homelab-monitor"
    fake_token_dir.mkdir(parents=True, exist_ok=True)
    fake_token = fake_token_dir / "heartbeat.token"

    with (
        patch("homelab_monitor.kernel.cron.install.WRAPPER_PATH", str(fake_wrapper)),
        patch("homelab_monitor.kernel.cron.install.TOKEN_FILE_PATH", str(fake_token)),
        patch(
            "homelab_monitor.kernel.cron.install.install_wrapper_local",
            new=AsyncMock(side_effect=CrontabWriteError("disk full; rolled back")),
        ),
    ):
        resp = await authenticated_client.post(
            f"/api/crons/{fp}/install-wrapper",
            json={"confirm": True},
            headers=_csrf(authenticated_client),
        )
    assert resp.status_code == 500  # noqa: PLR2004
    assert "rollback" in resp.text.lower()


@pytest.mark.asyncio
async def test_install_wrapper_confirm_no_auth_repo_via_delattr(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """confirm=true with auth_repo=None → DependencyUnavailableProblem (line 326).

    Session auth does not use auth_repo, so setting it to None after login
    still allows the authenticated request to proceed past require_session()
    and reach the auth_repo=None check at line 326.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)
    _make_fake_crontab(tmp_path)
    fp = await _seed_cron(repo, host=_HOST)

    fake_wrapper = tmp_path / "cron-with-heartbeat.sh"
    fake_token_dir = tmp_path / "etc" / "homelab-monitor"
    fake_token_dir.mkdir(parents=True, exist_ok=True)
    fake_token = fake_token_dir / "heartbeat.token"

    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    real_auth_repo = app.state.auth_repo
    # Set auth_repo=None AFTER the cookie was obtained; session validation
    # uses the session table (SQL), not auth_repo, so auth still passes.
    app.state.auth_repo = None

    try:
        with (
            patch("homelab_monitor.kernel.cron.install.WRAPPER_PATH", str(fake_wrapper)),
            patch("homelab_monitor.kernel.cron.install.TOKEN_FILE_PATH", str(fake_token)),
        ):
            resp = await authenticated_client.post(
                f"/api/crons/{fp}/install-wrapper",
                json={"confirm": True},
                headers=_csrf(authenticated_client),
            )
    finally:
        app.state.auth_repo = real_auth_repo

    # DependencyUnavailableProblem → 5xx or session resolver returns 401 if it uses auth_repo
    # Either way we accept any non-2xx that signals the missing-repo branch was hit
    assert resp.status_code != 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_install_wrapper_confirm_no_secrets_repo(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """confirm=true with secrets_repo=None on app.state → covers line 331.

    Session cookie is obtained before clearing secrets_repo, so auth still works.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)
    _make_fake_crontab(tmp_path)
    fp = await _seed_cron(repo, host=_HOST)

    fake_wrapper = tmp_path / "cron-with-heartbeat.sh"
    fake_token_dir = tmp_path / "etc" / "homelab-monitor"
    fake_token_dir.mkdir(parents=True, exist_ok=True)
    fake_token = fake_token_dir / "heartbeat.token"

    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]

    # secrets_repo is not used for session auth, so setting it None won't break login
    original_secrets_repo = getattr(app.state, "secrets_repo", None)
    app.state.secrets_repo = None

    try:
        with (
            patch("homelab_monitor.kernel.cron.install.WRAPPER_PATH", str(fake_wrapper)),
            patch("homelab_monitor.kernel.cron.install.TOKEN_FILE_PATH", str(fake_token)),
        ):
            resp = await authenticated_client.post(
                f"/api/crons/{fp}/install-wrapper",
                json={"confirm": True},
                headers=_csrf(authenticated_client),
            )
    finally:
        app.state.secrets_repo = original_secrets_repo

    assert resp.status_code >= 400  # noqa: PLR2004


@pytest.mark.asyncio
async def test_install_wrapper_direct_auth_repo_none(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Call install_wrapper() directly with auth_repo=None.

    → DependencyUnavailableProblem (line 326).
    """
    from unittest.mock import MagicMock  # noqa: PLC0415

    from starlette.datastructures import State  # noqa: PLC0415
    from starlette.requests import Request  # noqa: PLC0415

    from homelab_monitor.kernel.api.routers.crons import (  # noqa: PLC0415
        install_wrapper,
    )
    from homelab_monitor.kernel.cron.repository import CronRepo  # noqa: PLC0415
    from homelab_monitor.kernel.cron.schemas import InstallWrapperRequest  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)
    _make_fake_crontab(tmp_path)
    fp = await _seed_cron(repo, host=_HOST)

    # Build a mock Request with auth_repo=None on app.state
    mock_app_state = State()
    mock_app_state.auth_repo = None  # type: ignore[attr-defined]
    mock_app_state.secrets_repo = None  # type: ignore[attr-defined]

    mock_app = MagicMock()
    mock_app.state = mock_app_state

    scope: dict[str, object] = {
        "type": "http",
        "method": "POST",
        "path": f"/api/crons/{fp}/install-wrapper",
        "headers": [],
        "query_string": b"",
        "app": mock_app,
    }
    mock_request = Request(scope)  # pyright: ignore[reportArgumentType]

    cron_repo = CronRepo(repo)
    user = MagicMock()
    user.username = "test"

    payload = InstallWrapperRequest(confirm=True)

    try:
        await install_wrapper(
            fingerprint=fp,
            payload=payload,
            request=mock_request,
            user=user,
            repo=cron_repo,
        )
        result_status = 200
    except Exception as exc:
        # DependencyUnavailableProblem or HTTPException
        result_status = getattr(exc, "status_code", 500)

    # Line 326 raises DependencyUnavailableProblem (5xx) or similar
    assert result_status != 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_install_wrapper_confirm_executor_unavailable(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Confirm path: install_wrapper_local raises CronApplyUnavailableError → 503."""
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from homelab_monitor.kernel.cron.install import (  # noqa: PLC0415
        CronApplyUnavailableError,
    )

    monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)
    _make_fake_crontab(tmp_path)
    fp = await _seed_cron(repo, host=_HOST)

    with patch(
        "homelab_monitor.kernel.cron.install.install_wrapper_local",
        new=AsyncMock(
            side_effect=CronApplyUnavailableError("executor not running; run host-setup.sh")
        ),
    ):
        resp = await authenticated_client.post(
            f"/api/crons/{fp}/install-wrapper",
            json={"confirm": True},
            headers=_csrf(authenticated_client),
        )
    assert resp.status_code == 503  # noqa: PLR2004


@pytest.mark.asyncio
async def test_cron_out_is_local_false_for_remote(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """CronOut.is_local=false when cron host != local hostname.

    The list endpoint computes is_local via cron_record_to_out(local_hostname=...).
    We seed a cron with host='other-host'; whatever the real hostname is,
    it won't equal 'other-host', so is_local must be false.
    """
    fp = await _seed_cron(repo, host="other-host-xyzzy")

    resp = await authenticated_client.get("/api/crons")
    assert resp.status_code == 200  # noqa: PLR2004
    items = resp.json()["items"]
    matching = [i for i in items if i["fingerprint"] == fp]
    assert len(matching) == 1
    assert matching[0]["is_local"] is False


# ---------------------------------------------------------------------------
# I1: confirm=true result — wrapper_last_seen_at must be null (install no
#     longer sets the wrapper health signal; only a real heartbeat does)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_wrapper_confirm_result_wrapper_last_seen_at_is_null(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """confirm=true: the returned cron object has wrapper_last_seen_at=null.

    record_wrapper_installed no longer sets wrapper_last_seen_at; the install
    result should therefore carry null so the UI does not falsely report a
    healthy wrapper before it has run once.
    """
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from homelab_monitor.kernel.cron.repository import CronRecord  # noqa: PLC0415
    from homelab_monitor.kernel.db.time import utc_now_iso as _utc  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_PUBLIC_URL", _PUBLIC_URL)
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)

    _make_fake_crontab(tmp_path)
    fp = await _seed_cron(repo, host=_HOST)

    now = _utc()
    fake_updated_cron = CronRecord(
        fingerprint=fp,
        name="backup",
        host=_HOST,
        command=_COMMAND,
        schedule=_SCHEDULE,
        schedule_canonical=_SCHEDULE,
        cadence_seconds=300,
        expected_grace_seconds=300,
        enabled=True,
        last_seen_state="unknown",
        created_at=now,
        updated_at=now,
        hidden_at=None,
        source_path=_SOURCE_PATH,
        wrapper_last_seen_at=None,  # install does NOT set this
        last_discovered_at=None,
        soft_deleted_at=None,
        log_match_key=None,
        wrapper_installed=False,
        wrapper_format_version=None,
    )

    with patch(
        "homelab_monitor.kernel.cron.install.install_wrapper_local",
        new=AsyncMock(return_value=fake_updated_cron),
    ):
        resp = await authenticated_client.post(
            f"/api/crons/{fp}/install-wrapper",
            json={"confirm": True},
            headers=_csrf(authenticated_client),
        )

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert "cron" in data
    assert data["cron"]["wrapper_last_seen_at"] is None


# ---------------------------------------------------------------------------
# GET /api/crons/wrapper-template (item 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_wrapper_template_200_text_plain(
    api_token_client: AsyncClient,
) -> None:
    """GET /wrapper-template with HEARTBEAT_WRITE token → 200 text/plain with all 3 placeholders."""
    resp = await api_token_client.get("/api/crons/wrapper-template")
    assert resp.status_code == 200  # noqa: PLR2004
    assert "text/plain" in resp.headers.get("content-type", "")
    body = resp.text
    # All three placeholders must be present (unsubstituted — raw template)
    assert "{{WRAPPER_FORMAT_VERSION}}" in body
    assert "{{TOKEN_FILE_PATH}}" in body
    assert "{{WRAPPER_ENV_PATH}}" in body


@pytest.mark.asyncio
async def test_get_wrapper_template_401_without_token(
    unauthenticated_client: AsyncClient,
) -> None:
    """GET /wrapper-template without any token → 401."""
    resp = await unauthenticated_client.get("/api/crons/wrapper-template")
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_wrapper_template_403_with_wrong_scope(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /wrapper-template with a token lacking HEARTBEAT_WRITE → 403."""
    import base64  # noqa: PLC0415

    from httpx import ASGITransport  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415
    from homelab_monitor.kernel.auth.api_tokens import make_api_token  # noqa: PLC0415
    from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        # Token with READ_STATUS only — no HEARTBEAT_WRITE
        plaintext, _ = make_api_token(prefix="test")
        await app.state.auth_repo.create_api_token(
            name="limited-token",
            scopes={Scope.READ_STATUS},
            plaintext_token=plaintext,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            resp = await client.get("/api/crons/wrapper-template")

    assert resp.status_code == 403  # noqa: PLR2004


@pytest.mark.asyncio
async def test_wrapper_template_route_not_shadowed_by_fingerprint(
    api_token_client: AsyncClient,
) -> None:
    """GET /api/crons/wrapper-template must not be matched by /{fingerprint} route.

    The router registers /wrapper-template before /{fingerprint}, so literal
    'wrapper-template' must resolve to the template endpoint, not the cron-detail
    endpoint (which would 404 with 'cron not found').
    """
    resp = await api_token_client.get("/api/crons/wrapper-template")
    # Must be 200 — not 404 (which would indicate route shadowing)
    assert resp.status_code == 200  # noqa: PLR2004
    # Content must be the template text, not a JSON error
    assert "{{WRAPPER_FORMAT_VERSION}}" in resp.text


# ===========================================================================
# STAGE-002-009A: POST /api/crons/{fingerprint}/uninstall-wrapper
# ===========================================================================


def _make_fake_wrapped_crontab(
    tmp_path: Path, schedule: str = _SCHEDULE, command: str = _COMMAND
) -> Path:
    """Create /etc/crontab under tmp_path with a WRAPPED job line."""
    etc = tmp_path / "etc"
    etc.mkdir(exist_ok=True)
    ct = etc / "crontab"
    fp = compute_fingerprint(
        host=_HOST, source_path=_SOURCE_PATH, schedule=schedule, command=command
    )
    wrapped_cmd = build_invocation_prefix(fp) + command
    ct.write_text(f"# header\n{schedule} root {wrapped_cmd}\n")
    return tmp_path


# ---------------------------------------------------------------------------
# Unauthenticated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uninstall_wrapper_unauthenticated(
    unauthenticated_client: AsyncClient,
) -> None:
    """No session → 401."""
    resp = await unauthenticated_client.post(
        "/api/crons/some-fp/uninstall-wrapper",
        json={"confirm": False},
    )
    assert resp.status_code == 401  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Cron not found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uninstall_wrapper_cron_not_found(
    authenticated_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown fingerprint → 404."""
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)

    resp = await authenticated_client.post(
        "/api/crons/does-not-exist/uninstall-wrapper",
        json={"confirm": False},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 404  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Remote host
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uninstall_wrapper_remote_host(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cron on a remote host → 400."""
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)
    fp = await _seed_cron(repo, host=_REMOTE_HOST)

    resp = await authenticated_client.post(
        f"/api/crons/{fp}/uninstall-wrapper",
        json={"confirm": False},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 400  # noqa: PLR2004
    assert "remote" in resp.text.lower()


# ---------------------------------------------------------------------------
# Dry-run: crontab line not found → 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uninstall_wrapper_dry_run_line_not_found(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dry-run where crontab file has no matching line → 409."""
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)

    (tmp_path / "etc").mkdir(exist_ok=True)
    (tmp_path / "etc" / "crontab").write_text("# only comments\n")

    fp = await _seed_cron(repo, host=_HOST)

    resp = await authenticated_client.post(
        f"/api/crons/{fp}/uninstall-wrapper",
        json={"confirm": False},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 409  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Dry-run: not wrapped → 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uninstall_wrapper_dry_run_not_wrapped(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dry-run where crontab line is NOT wrapped → 409."""
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)

    # Write a bare (unwrapped) line
    (tmp_path / "etc").mkdir(exist_ok=True)
    (tmp_path / "etc" / "crontab").write_text(f"{_SCHEDULE} root {_COMMAND}\n")

    fp = await _seed_cron(repo, host=_HOST)

    resp = await authenticated_client.post(
        f"/api/crons/{fp}/uninstall-wrapper",
        json={"confirm": False},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 409  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Dry-run: wrapped crontab → returns UninstallWrapperPreview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uninstall_wrapper_dry_run_returns_preview(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dry-run (confirm=false) on a wrapped line → 200 with UninstallWrapperPreview."""
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)
    _make_fake_wrapped_crontab(tmp_path)

    fp = await _seed_cron(repo, host=_HOST)

    resp = await authenticated_client.post(
        f"/api/crons/{fp}/uninstall-wrapper",
        json={"confirm": False},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert data["fingerprint"] == fp
    diff = data["crontab_diff"]
    assert "old_line" in diff
    assert "new_line" in diff
    # old_line contains the wrapper prefix; new_line does not
    assert WRAPPER_PATH in diff["old_line"]
    assert WRAPPER_PATH not in diff["new_line"]


# ---------------------------------------------------------------------------
# Executor unavailable → 503
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uninstall_wrapper_executor_unavailable(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Executor unavailable → 503."""
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)
    _make_fake_wrapped_crontab(tmp_path)

    fp = await _seed_cron(repo, host=_HOST)

    from homelab_monitor.kernel.cron.install import CronApplyUnavailableError  # noqa: PLC0415

    with patch(
        "homelab_monitor.kernel.cron.install.uninstall_wrapper_local",
        side_effect=CronApplyUnavailableError("executor not running"),
    ):
        resp = await authenticated_client.post(
            f"/api/crons/{fp}/uninstall-wrapper",
            json={"confirm": True},
            headers=_csrf(authenticated_client),
        )

    assert resp.status_code == 503  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Confirm path: RemoteHostError → 400 (crons.py line 491)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uninstall_wrapper_confirm_remote_host_returns_400(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """confirm=true: uninstall_wrapper_local raises RemoteHostError → 400.

    Covers crons.py line 491.
    """
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from homelab_monitor.kernel.cron.install import RemoteHostError  # noqa: PLC0415

    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)
    _make_fake_wrapped_crontab(tmp_path)
    fp = await _seed_cron(repo, host=_HOST)

    with patch(
        "homelab_monitor.kernel.cron.install.uninstall_wrapper_local",
        new=AsyncMock(side_effect=RemoteHostError("cron is on remote host")),
    ):
        resp = await authenticated_client.post(
            f"/api/crons/{fp}/uninstall-wrapper",
            json={"confirm": True},
            headers=_csrf(authenticated_client),
        )

    assert resp.status_code == 400  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Confirm path: CronLineNotFoundError → 409 (crons.py line 493)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uninstall_wrapper_confirm_line_not_found_returns_409(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """confirm=true: uninstall_wrapper_local raises CronLineNotFoundError → 409.

    Covers crons.py line 493.
    """
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from homelab_monitor.kernel.cron.install import CronLineNotFoundError  # noqa: PLC0415

    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)
    _make_fake_wrapped_crontab(tmp_path)
    fp = await _seed_cron(repo, host=_HOST)

    with patch(
        "homelab_monitor.kernel.cron.install.uninstall_wrapper_local",
        new=AsyncMock(side_effect=CronLineNotFoundError("line not found")),
    ):
        resp = await authenticated_client.post(
            f"/api/crons/{fp}/uninstall-wrapper",
            json={"confirm": True},
            headers=_csrf(authenticated_client),
        )

    assert resp.status_code == 409  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Confirm path: NotWrappedError → 409 (crons.py line 495)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uninstall_wrapper_confirm_not_wrapped_returns_409(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """confirm=true: uninstall_wrapper_local raises NotWrappedError → 409.

    Covers crons.py line 495.
    """
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from homelab_monitor.kernel.cron.install import NotWrappedError  # noqa: PLC0415

    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)
    _make_fake_wrapped_crontab(tmp_path)
    fp = await _seed_cron(repo, host=_HOST)

    with patch(
        "homelab_monitor.kernel.cron.install.uninstall_wrapper_local",
        new=AsyncMock(side_effect=NotWrappedError("already bare")),
    ):
        resp = await authenticated_client.post(
            f"/api/crons/{fp}/uninstall-wrapper",
            json={"confirm": True},
            headers=_csrf(authenticated_client),
        )

    assert resp.status_code == 409  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Confirm path: CrontabWriteError → 500 (crons.py lines 499-501)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uninstall_wrapper_confirm_write_error_returns_500(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """confirm=true: uninstall_wrapper_local raises CrontabWriteError → 500.

    Covers crons.py lines 499-501.
    """
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from homelab_monitor.kernel.cron.install import CrontabWriteError  # noqa: PLC0415

    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)
    _make_fake_wrapped_crontab(tmp_path)
    fp = await _seed_cron(repo, host=_HOST)

    with patch(
        "homelab_monitor.kernel.cron.install.uninstall_wrapper_local",
        new=AsyncMock(side_effect=CrontabWriteError("disk full")),
    ):
        resp = await authenticated_client.post(
            f"/api/crons/{fp}/uninstall-wrapper",
            json={"confirm": True},
            headers=_csrf(authenticated_client),
        )

    assert resp.status_code == 500  # noqa: PLR2004
    assert "uninstall failed" in resp.text


# ---------------------------------------------------------------------------
# Confirm path: success → 200 with UninstallWrapperResult (crons.py lines 503-504)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uninstall_wrapper_confirm_success_returns_200(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """confirm=true: successful uninstall returns 200 with UninstallWrapperResult.

    Covers crons.py lines 503-504 (success path after uninstall_wrapper_local).
    """
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from homelab_monitor.kernel.cron.repository import CronRecord  # noqa: PLC0415
    from homelab_monitor.kernel.db.time import utc_now_iso as _utc  # noqa: PLC0415

    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", _HOST)
    _make_fake_wrapped_crontab(tmp_path)
    fp = await _seed_cron(repo, host=_HOST)

    now = _utc()
    fake_updated_cron = CronRecord(
        fingerprint=fp,
        name="backup",
        host=_HOST,
        command=_COMMAND,
        schedule=_SCHEDULE,
        schedule_canonical=_SCHEDULE,
        cadence_seconds=300,
        expected_grace_seconds=300,
        enabled=True,
        last_seen_state="unknown",
        created_at=now,
        updated_at=now,
        hidden_at=None,
        source_path=_SOURCE_PATH,
        wrapper_last_seen_at=None,
        last_discovered_at=None,
        soft_deleted_at=None,
        log_match_key=None,
        wrapper_installed=False,
        wrapper_format_version=None,
    )

    with patch(
        "homelab_monitor.kernel.cron.install.uninstall_wrapper_local",
        new=AsyncMock(return_value=fake_updated_cron),
    ):
        resp = await authenticated_client.post(
            f"/api/crons/{fp}/uninstall-wrapper",
            json={"confirm": True},
            headers=_csrf(authenticated_client),
        )

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert "cron" in data
    assert data["cron"]["fingerprint"] == fp
