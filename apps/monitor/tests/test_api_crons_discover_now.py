"""Tests for POST /api/crons/discover-now endpoint (STAGE-002-007)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from homelab_monitor.kernel.cron.discovery_types import CronScanError, CronScanResult


def _csrf(client: AsyncClient) -> dict[str, str]:
    """Return X-CSRF-Token header from the session cookie."""
    csrf: str = client.cookies.get("homelab_monitor_csrf") or ""
    return {"X-CSRF-Token": csrf}


@pytest.mark.asyncio
async def test_discover_now_returns_401_without_auth(unauthenticated_client: AsyncClient) -> None:
    """POST /api/crons/discover-now without session auth returns 401."""
    resp = await unauthenticated_client.post("/api/crons/discover-now")
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_discover_now_returns_403_without_csrf(authenticated_client: AsyncClient) -> None:
    """POST /api/crons/discover-now without CSRF token returns 403."""
    resp = await authenticated_client.post("/api/crons/discover-now")
    assert resp.status_code == 403  # noqa: PLR2004


@pytest.mark.asyncio
async def test_discover_now_returns_202_on_success(
    authenticated_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/crons/discover-now with valid session+CSRF returns 202 with scan result."""
    # Mock the discoverer
    mock_discoverer = AsyncMock()
    mock_result = CronScanResult(
        found_fingerprints=frozenset(["fp1", "fp2", "fp3"]),
        inserted_count=2,
        updated_count=1,
        bump_only_count=0,
        partial=False,
        errors=[],
    )
    mock_discoverer.scan = AsyncMock(return_value=mock_result)

    # Inject the mock into app.state
    authenticated_client._transport.app.state.cron_discoverer = mock_discoverer  # type: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportUnknownMemberType]

    resp = await authenticated_client.post(
        "/api/crons/discover-now",
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 202  # noqa: PLR2004
    body = resp.json()
    assert body["found_count"] == 3  # noqa: PLR2004
    assert body["inserted_count"] == 2  # noqa: PLR2004
    assert body["updated_count"] == 1
    assert body["bump_only_count"] == 0
    assert body["partial"] is False
    assert body["error_count"] == 0
    assert body["errors"] == []


@pytest.mark.asyncio
async def test_discover_now_returns_429_when_throttled(
    authenticated_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second POST within 10s throttle window returns 429 with Retry-After header."""
    import homelab_monitor.kernel.api.routers.crons as _crons_mod  # noqa: PLC0415

    monkeypatch.setattr(_crons_mod, "_discover_now_last_call", 0.0)

    # Mock the discoverer
    mock_discoverer = AsyncMock()
    mock_result = CronScanResult(
        found_fingerprints=frozenset(),
        inserted_count=0,
        updated_count=0,
        bump_only_count=0,
        partial=False,
        errors=[],
    )
    mock_discoverer.scan = AsyncMock(return_value=mock_result)
    authenticated_client._transport.app.state.cron_discoverer = mock_discoverer  # type: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportUnknownMemberType]

    # First call succeeds
    resp1 = await authenticated_client.post(
        "/api/crons/discover-now",
        headers=_csrf(authenticated_client),
    )
    assert resp1.status_code == 202  # noqa: PLR2004

    # Second call within 10s returns 429
    resp2 = await authenticated_client.post(
        "/api/crons/discover-now",
        headers=_csrf(authenticated_client),
    )
    assert resp2.status_code == 429  # noqa: PLR2004
    body = resp2.json()
    assert body["error"]["code"] == "discover_now_throttled"
    assert "Retry-After" in resp2.headers


@pytest.mark.asyncio
async def test_discover_now_returns_503_when_discoverer_unavailable(
    authenticated_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/crons/discover-now returns 503 if cron_discoverer not on app.state."""
    import homelab_monitor.kernel.api.routers.crons as _crons_mod  # noqa: PLC0415

    monkeypatch.setattr(_crons_mod, "_discover_now_last_call", 0.0)

    # Remove the discoverer from app.state
    if hasattr(authenticated_client._transport.app.state, "cron_discoverer"):  # type: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportUnknownMemberType]
        delattr(authenticated_client._transport.app.state, "cron_discoverer")  # type: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportUnknownMemberType]

    resp = await authenticated_client.post(
        "/api/crons/discover-now",
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 503  # noqa: PLR2004
    body = resp.json()
    assert body["error"]["code"] == "cron_discoverer_unavailable"


@pytest.mark.asyncio
async def test_discover_now_returns_partial_with_errors(
    authenticated_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/crons/discover-now with partial=True includes errors in response."""
    import homelab_monitor.kernel.api.routers.crons as _crons_mod  # noqa: PLC0415

    monkeypatch.setattr(_crons_mod, "_discover_now_last_call", 0.0)

    # Mock the discoverer with errors
    mock_discoverer = AsyncMock()
    mock_result = CronScanResult(
        found_fingerprints=frozenset(["fp1"]),
        inserted_count=1,
        updated_count=0,
        bump_only_count=0,
        partial=True,
        errors=[
            CronScanError(host_source_path="/etc/cron.d/malformed", error="Invalid syntax"),
        ],
    )
    mock_discoverer.scan = AsyncMock(return_value=mock_result)
    authenticated_client._transport.app.state.cron_discoverer = mock_discoverer  # type: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportUnknownMemberType]

    resp = await authenticated_client.post(
        "/api/crons/discover-now",
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 202  # noqa: PLR2004
    body = resp.json()
    assert body["partial"] is True
    assert body["error_count"] == 1
    assert len(body["errors"]) == 1
    assert body["errors"][0]["host_source_path"] == "/etc/cron.d/malformed"
    assert body["errors"][0]["error"] == "Invalid syntax"
