"""Tests for the silence allowlist API endpoints (STAGE-004-038).

Covers:
  GET  /api/logs/signatures/silence-allowlist      (list)
  POST /api/logs/signatures/silence-allowlist      (create)
  DELETE /api/logs/signatures/silence-allowlist/{id} (delete)

Project test conventions:
- Framework: pytest-asyncio (asyncio_mode=auto)
- CSRF: _csrf() reads homelab_monitor_csrf cookie → X-CSRF-Token header
"""

from __future__ import annotations

from httpx import AsyncClient

# ---------------------------------------------------------------------------
# CSRF helper
# ---------------------------------------------------------------------------


def _csrf(client: AsyncClient) -> dict[str, str]:
    """Return X-CSRF-Token header extracted from the session cookie."""
    csrf: str = client.cookies.get("homelab_monitor_csrf") or ""
    return {"X-CSRF-Token": csrf}


# ===========================================================================
# GET /api/logs/signatures/silence-allowlist — list
# ===========================================================================


async def test_list_empty_returns_200_empty(
    authenticated_client: AsyncClient,
) -> None:
    """GET silence-allowlist with no entries returns 200, empty entries list."""
    resp = await authenticated_client.get("/api/logs/signatures/silence-allowlist")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["entries"] == []


# ===========================================================================
# POST /api/logs/signatures/silence-allowlist — create
# ===========================================================================


async def test_post_always_201_and_listed(
    authenticated_client: AsyncClient,
) -> None:
    """POST with always schedule returns 201."""
    resp = await authenticated_client.post(
        "/api/logs/signatures/silence-allowlist",
        json={
            "service_key": "svc1",
            "schedule_kind": "always",
            "schedule_value": "",
            "reason": "quiet",
        },
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 201  # noqa: PLR2004
    body = resp.json()
    assert body["schedule_kind"] == "always"
    assert body["id"] > 0
    assert body["service_key"] == "svc1"
    assert body["template_hash"] is None
    assert "T" in body["created_at"]

    # Verify it appears in list
    list_resp = await authenticated_client.get("/api/logs/signatures/silence-allowlist")
    assert list_resp.status_code == 200  # noqa: PLR2004
    list_body = list_resp.json()
    assert len(list_body["entries"]) == 1
    assert list_body["entries"][0]["reason"] == "quiet"


async def test_post_cron_canonicalizes(
    authenticated_client: AsyncClient,
) -> None:
    """POST with @hourly cron is canonicalized to 0 * * * *."""
    resp = await authenticated_client.post(
        "/api/logs/signatures/silence-allowlist",
        json={
            "service_key": "svc1",
            "schedule_kind": "cron",
            "schedule_value": "@hourly",
            "reason": "job",
        },
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 201  # noqa: PLR2004
    body = resp.json()
    assert body["schedule_value"] == "0 * * * *"


async def test_post_window_201(
    authenticated_client: AsyncClient,
) -> None:
    """POST with window schedule returns 201."""
    resp = await authenticated_client.post(
        "/api/logs/signatures/silence-allowlist",
        json={
            "service_key": "svc1",
            "schedule_kind": "window",
            "schedule_value": "2026-06-07T00:00:00+00:00/2026-06-08T00:00:00+00:00",
            "reason": "maint",
        },
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 201  # noqa: PLR2004
    body = resp.json()
    assert body["schedule_kind"] == "window"


async def test_post_per_service_null_hash(
    authenticated_client: AsyncClient,
) -> None:
    """POST without template_hash creates service-wide entry."""
    resp = await authenticated_client.post(
        "/api/logs/signatures/silence-allowlist",
        json={
            "service_key": "svc1",
            "schedule_kind": "always",
            "schedule_value": "",
            "reason": "all",
        },
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 201  # noqa: PLR2004
    body = resp.json()
    assert body["template_hash"] is None


async def test_post_422_always_with_value(
    authenticated_client: AsyncClient,
) -> None:
    """POST always with non-empty schedule_value returns 422."""
    resp = await authenticated_client.post(
        "/api/logs/signatures/silence-allowlist",
        json={
            "service_key": "svc1",
            "schedule_kind": "always",
            "schedule_value": "0 * * * *",
            "reason": "bad",
        },
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 422  # noqa: PLR2004


async def test_post_422_cron_empty(
    authenticated_client: AsyncClient,
) -> None:
    """POST cron with empty schedule_value returns 422."""
    resp = await authenticated_client.post(
        "/api/logs/signatures/silence-allowlist",
        json={
            "service_key": "svc1",
            "schedule_kind": "cron",
            "schedule_value": "",
            "reason": "bad",
        },
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 422  # noqa: PLR2004


async def test_post_422_cron_invalid(
    authenticated_client: AsyncClient,
) -> None:
    """POST cron with invalid expression returns 422."""
    resp = await authenticated_client.post(
        "/api/logs/signatures/silence-allowlist",
        json={
            "service_key": "svc1",
            "schedule_kind": "cron",
            "schedule_value": "not a cron",
            "reason": "bad",
        },
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 422  # noqa: PLR2004


async def test_post_422_window_bad_range(
    authenticated_client: AsyncClient,
) -> None:
    """POST window with end before start returns 422."""
    resp = await authenticated_client.post(
        "/api/logs/signatures/silence-allowlist",
        json={
            "service_key": "svc1",
            "schedule_kind": "window",
            "schedule_value": "2026-06-08T00:00:00+00:00/2026-06-07T00:00:00+00:00",
            "reason": "bad",
        },
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 422  # noqa: PLR2004


async def test_post_422_window_malformed(
    authenticated_client: AsyncClient,
) -> None:
    """POST window with only one ISO part returns 422."""
    resp = await authenticated_client.post(
        "/api/logs/signatures/silence-allowlist",
        json={
            "service_key": "svc1",
            "schedule_kind": "window",
            "schedule_value": "only-one-part",
            "reason": "bad",
        },
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 422  # noqa: PLR2004


async def test_post_422_bad_expires_at(
    authenticated_client: AsyncClient,
) -> None:
    """POST with invalid expires_at returns 422."""
    resp = await authenticated_client.post(
        "/api/logs/signatures/silence-allowlist",
        json={
            "service_key": "svc1",
            "schedule_kind": "always",
            "schedule_value": "",
            "reason": "bad",
            "expires_at": "not-a-date",
        },
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 422  # noqa: PLR2004


async def test_post_422_extra_field(
    authenticated_client: AsyncClient,
) -> None:
    """POST with extra unknown field returns 422 (extra=forbid)."""
    resp = await authenticated_client.post(
        "/api/logs/signatures/silence-allowlist",
        json={
            "service_key": "svc1",
            "schedule_kind": "always",
            "schedule_value": "",
            "reason": "bad",
            "extra_field": "not allowed",
        },
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 422  # noqa: PLR2004


async def test_post_requires_csrf(
    authenticated_client: AsyncClient,
) -> None:
    """POST without X-CSRF-Token header returns 403."""
    resp = await authenticated_client.post(
        "/api/logs/signatures/silence-allowlist",
        json={
            "service_key": "svc1",
            "schedule_kind": "always",
            "schedule_value": "",
            "reason": "no csrf",
        },
    )
    assert resp.status_code == 403  # noqa: PLR2004


async def test_post_401_anon(unauthenticated_client: AsyncClient) -> None:
    """POST without session returns 401."""
    resp = await unauthenticated_client.post(
        "/api/logs/signatures/silence-allowlist",
        json={
            "service_key": "svc1",
            "schedule_kind": "always",
            "schedule_value": "",
            "reason": "anon",
        },
    )
    assert resp.status_code == 401  # noqa: PLR2004


# ===========================================================================
# DELETE /api/logs/signatures/silence-allowlist/{entry_id}
# ===========================================================================


async def test_delete_204_and_gone(
    authenticated_client: AsyncClient,
) -> None:
    """DELETE existing entry returns 204, entry is gone."""
    # Create an entry
    resp = await authenticated_client.post(
        "/api/logs/signatures/silence-allowlist",
        json={
            "service_key": "svc1",
            "schedule_kind": "always",
            "schedule_value": "",
            "reason": "delete me",
        },
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 201  # noqa: PLR2004
    entry_id = resp.json()["id"]

    # Delete it
    del_resp = await authenticated_client.delete(
        f"/api/logs/signatures/silence-allowlist/{entry_id}",
        headers=_csrf(authenticated_client),
    )
    assert del_resp.status_code == 204  # noqa: PLR2004

    # Verify it's gone from list
    list_resp = await authenticated_client.get("/api/logs/signatures/silence-allowlist")
    assert list_resp.status_code == 200  # noqa: PLR2004
    body = list_resp.json()
    assert body["entries"] == []


async def test_delete_404_miss(
    authenticated_client: AsyncClient,
) -> None:
    """DELETE nonexistent entry returns 404."""
    resp = await authenticated_client.delete(
        "/api/logs/signatures/silence-allowlist/9999",
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 404  # noqa: PLR2004
    body = resp.json()
    assert body["error"]["code"] == "not_found"


async def test_delete_requires_csrf(
    authenticated_client: AsyncClient,
) -> None:
    """DELETE without X-CSRF-Token header returns 403."""
    # Create an entry first
    resp = await authenticated_client.post(
        "/api/logs/signatures/silence-allowlist",
        json={
            "service_key": "svc1",
            "schedule_kind": "always",
            "schedule_value": "",
            "reason": "test",
        },
        headers=_csrf(authenticated_client),
    )
    entry_id = resp.json()["id"]

    # Try to delete without CSRF
    del_resp = await authenticated_client.delete(
        f"/api/logs/signatures/silence-allowlist/{entry_id}"
    )
    assert del_resp.status_code == 403  # noqa: PLR2004


async def test_post_201_with_valid_expires_at(
    authenticated_client: AsyncClient,
) -> None:
    """POST with a valid expires_at ISO-8601 datetime returns 201."""
    resp = await authenticated_client.post(
        "/api/logs/signatures/silence-allowlist",
        json={
            "service_key": "svc1",
            "schedule_kind": "always",
            "schedule_value": "",
            "reason": "expiry-test",
            "expires_at": "2026-06-08T12:00:00+00:00",
        },
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 201  # noqa: PLR2004
    body = resp.json()
    assert body["expires_at"] == "2026-06-08T12:00:00+00:00"


async def test_post_201_with_explicit_null_expires_at(
    authenticated_client: AsyncClient,
) -> None:
    """POST with explicit expires_at=null returns 201 (the validator's None branch)."""
    resp = await authenticated_client.post(
        "/api/logs/signatures/silence-allowlist",
        json={
            "service_key": "svc1",
            "schedule_kind": "always",
            "schedule_value": "",
            "reason": "null-expiry",
            "expires_at": None,
        },
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 201  # noqa: PLR2004
    body = resp.json()
    assert body["expires_at"] is None


async def test_post_422_window_invalid_iso_datetime(
    authenticated_client: AsyncClient,
) -> None:
    """POST window with 2-part value but non-ISO parts returns 422 (hits except ValueError)."""
    resp = await authenticated_client.post(
        "/api/logs/signatures/silence-allowlist",
        json={
            "service_key": "svc1",
            "schedule_kind": "window",
            "schedule_value": "badstart/badend",
            "reason": "bad",
        },
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 422  # noqa: PLR2004


async def test_post_422_cron_reboot(
    authenticated_client: AsyncClient,
) -> None:
    """POST cron with @reboot returns 422 (unsupported for silence entries)."""
    resp = await authenticated_client.post(
        "/api/logs/signatures/silence-allowlist",
        json={
            "service_key": "svc1",
            "schedule_kind": "cron",
            "schedule_value": "@reboot",
            "reason": "bad",
        },
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 422  # noqa: PLR2004
    body = resp.json()
    assert "@reboot" in str(body)


__all__: list[str] = []
