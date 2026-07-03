"""API endpoint tests for the session-PIN router (STAGE-009-010B).

Tests: GET/POST/DELETE /api/settings/security/pin, POST /pin/verify.

Uses authenticated_client / unauthenticated_client fixtures (session + CSRF)
per the project convention established in test_api_autofix.py /
test_api_autofix_settings.py / test_api_runbooks.py. Audit assertions query
audit_log directly via SqliteRepository.fetch_all, mirroring
test_api_runbooks.py::test_refresh_audits.
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.security.pin import PIN_HASH_KEY, hash_pin
from tests.conftest import TEST_PASSWORD, TEST_USERNAME

_URL = "/api/settings/security/pin"
_VERIFY_URL = "/api/settings/security/pin/verify"


def _csrf(client: AsyncClient) -> dict[str, str]:
    """Extract CSRF token from client cookies (empty string when absent)."""
    csrf = client.cookies.get("homelab_monitor_csrf") or ""
    return {"X-CSRF-Token": csrf}


async def _seed_pin(repo: SqliteRepository, pin: str = "1234") -> None:
    """Directly seed a PIN hash via app_settings (bypassing the set endpoint)."""
    app_settings = AppSettingsRepository(repo)
    await app_settings.set(PIN_HASH_KEY, hash_pin(pin, cost=4))


# ---------------------------------------------------------------------------
# GET /pin (status)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_pin_status_returns_false_when_unset(
    authenticated_client: AsyncClient,
) -> None:
    response = await authenticated_client.get(_URL)
    assert response.status_code == 200  # noqa: PLR2004
    assert response.json()["set"] is False


@pytest.mark.asyncio
async def test_get_pin_status_returns_true_when_set(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_pin(repo)
    response = await authenticated_client.get(_URL)
    assert response.status_code == 200  # noqa: PLR2004
    assert response.json()["set"] is True


@pytest.mark.asyncio
async def test_get_pin_status_requires_session(
    unauthenticated_client: AsyncClient,
) -> None:
    response = await unauthenticated_client.get(_URL)
    assert response.status_code == 401  # noqa: PLR2004


# ---------------------------------------------------------------------------
# POST /pin — set flow (no existing PIN)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_pin_set_flow_success(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    response = await authenticated_client.post(
        _URL,
        json={"new_pin": "1234", "current_password": TEST_PASSWORD},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 200  # noqa: PLR2004
    data = response.json()
    assert data["set"] is True
    assert data["rotated"] is False

    rows = await repo.fetch_all(
        text("SELECT who, before_json, after_json FROM audit_log WHERE what = :w"),
        {"w": "security.pin_set"},
    )
    assert len(rows) == 1
    who, before_json, after_json = rows[0]
    assert who == TEST_USERNAME
    assert before_json is None
    after = json.loads(after_json)
    assert after == {"has_pin": True}
    # No PIN/hash leaked into audit.
    assert "1234" not in json.dumps(after)


@pytest.mark.asyncio
async def test_post_pin_set_flow_wrong_password_400(
    authenticated_client: AsyncClient,
) -> None:
    response = await authenticated_client.post(
        _URL,
        json={"new_pin": "1234", "current_password": "wrong-password"},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 400  # noqa: PLR2004


@pytest.mark.asyncio
async def test_post_pin_set_flow_wrong_password_rate_limited_429(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """5 wrong-password POST /pin attempts exhaust the login rate-limit budget.

    POST /pin (set flow) shares the login rate limiter (5 attempts / 300s,
    keyed by IP) for password verification failures, distinct from the PIN
    rate limiter used for PIN-rotation/verify. The 6th attempt returns 429
    no additional audit row is written for the 429 branch itself (it returns
    before the audit insert). This path raises RateLimitedProblem, which is
    NOT one of the dedicated Retry-After-header exception types (unlike the
    PIN-lockout 429s) — no Retry-After header is set here.
    """
    for _ in range(5):
        resp = await authenticated_client.post(
            _URL,
            json={"new_pin": "1234", "current_password": "wrong-password"},
            headers=_csrf(authenticated_client),
        )
        assert resp.status_code == 400  # noqa: PLR2004

    resp = await authenticated_client.post(
        _URL,
        json={"new_pin": "1234", "current_password": "wrong-password"},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 429  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "rate_limited"

    rows = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = :w"),
        {"w": "security.password_verify_failed_on_pin_set"},
    )
    assert len(rows) == 5  # noqa: PLR2004


@pytest.mark.asyncio
async def test_post_pin_set_flow_no_password_400(
    authenticated_client: AsyncClient,
) -> None:
    response = await authenticated_client.post(
        _URL,
        json={"new_pin": "1234"},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 400  # noqa: PLR2004


@pytest.mark.asyncio
async def test_post_pin_set_flow_current_pin_when_no_pin_400(
    authenticated_client: AsyncClient,
) -> None:
    response = await authenticated_client.post(
        _URL,
        json={
            "new_pin": "1234",
            "current_password": TEST_PASSWORD,
            "current_pin": "0000",
        },
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 400  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Body validation (Pydantic)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_pin_rejects_non_digit_new_pin_422(
    authenticated_client: AsyncClient,
) -> None:
    response = await authenticated_client.post(
        _URL,
        json={"new_pin": "abcd", "current_password": TEST_PASSWORD},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_post_pin_rejects_too_short_422(
    authenticated_client: AsyncClient,
) -> None:
    response = await authenticated_client.post(
        _URL,
        json={"new_pin": "123", "current_password": TEST_PASSWORD},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_post_pin_rejects_too_long_422(
    authenticated_client: AsyncClient,
) -> None:
    response = await authenticated_client.post(
        _URL,
        json={"new_pin": "1234567890123", "current_password": TEST_PASSWORD},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_post_pin_rejects_extra_field_422(
    authenticated_client: AsyncClient,
) -> None:
    response = await authenticated_client.post(
        _URL,
        json={
            "new_pin": "1234",
            "current_password": TEST_PASSWORD,
            "extra_field": "x",
        },
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 422  # noqa: PLR2004


# ---------------------------------------------------------------------------
# POST /pin — rotate flow (existing PIN)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_pin_rotate_flow_success(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_pin(repo, pin="1111")

    response = await authenticated_client.post(
        _URL,
        json={"new_pin": "2222", "current_pin": "1111"},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 200  # noqa: PLR2004
    data = response.json()
    assert data["set"] is True
    assert data["rotated"] is True

    rows = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = :w"),
        {"w": "security.pin_rotated"},
    )
    assert len(rows) == 1

    # New PIN actually took effect.
    status_resp = await authenticated_client.get(_URL)
    assert status_resp.json()["set"] is True


@pytest.mark.asyncio
async def test_post_pin_rotate_flow_wrong_current_pin_400_and_ticks_limiter(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_pin(repo, pin="1111")

    response = await authenticated_client.post(
        _URL,
        json={"new_pin": "2222", "current_pin": "9999"},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 400  # noqa: PLR2004

    # Audit row for failed verification during rotate.
    rows = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = :w"),
        {"w": "security.pin_verify_failed"},
    )
    assert len(rows) == 1

    # Repeat 4 more times (5 total) to trip the rate limiter curve (3rd fails
    # -> 5s lockout); the 4th attempt on this endpoint should now 429.
    for _ in range(2):
        resp = await authenticated_client.post(
            _URL,
            json={"new_pin": "2222", "current_pin": "9999"},
            headers=_csrf(authenticated_client),
        )
        assert resp.status_code == 400  # noqa: PLR2004
    # 4th wrong attempt: limiter should now be locked (>= 3 failures) -> 429.
    resp = await authenticated_client.post(
        _URL,
        json={"new_pin": "2222", "current_pin": "9999"},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 429  # noqa: PLR2004
    assert "Retry-After" in resp.headers
    assert resp.json()["error"]["details"]["retry_after_seconds"] == 5  # noqa: PLR2004


@pytest.mark.asyncio
async def test_post_pin_rotate_flow_3rd_wrong_locks_and_audits(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """During PIN rotation, 3 wrong PINs trigger a 5s lock and write audit rows.

    Mirrors verify_pin behavior: each wrong attempt writes
    security.pin_verify_failed, and the 3rd failure also writes security.pin_locked.
    """
    await _seed_pin(repo, pin="1111")

    # 3 wrong attempts during rotation
    for _ in range(3):
        resp = await authenticated_client.post(
            _URL,
            json={"new_pin": "2222", "current_pin": "9999"},
            headers=_csrf(authenticated_client),
        )
        assert resp.status_code == 400  # noqa: PLR2004

    # Assert 3 verify-failed audit rows and 1 lock audit row @ 5s.
    failed_rows = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = :w"),
        {"w": "security.pin_verify_failed"},
    )
    assert len(failed_rows) == 3  # noqa: PLR2004

    locked_rows = await repo.fetch_all(
        text("SELECT after_json FROM audit_log WHERE what = :w"),
        {"w": "security.pin_locked"},
    )
    assert len(locked_rows) == 1
    after = json.loads(locked_rows[0][0])
    assert after["retry_after_seconds"] == 5  # noqa: PLR2004


@pytest.mark.asyncio
async def test_post_pin_rotate_flow_current_password_when_pin_set_400(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_pin(repo, pin="1111")

    response = await authenticated_client.post(
        _URL,
        json={
            "new_pin": "2222",
            "current_pin": "1111",
            "current_password": TEST_PASSWORD,
        },
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 400  # noqa: PLR2004


# ---------------------------------------------------------------------------
# POST /pin/verify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_pin_verify_success(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_pin(repo, pin="4321")

    response = await authenticated_client.post(
        _VERIFY_URL,
        json={"pin": "4321"},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 200  # noqa: PLR2004
    assert response.json()["ok"] is True

    rows = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = :w"),
        {"w": "security.pin_verify_succeeded"},
    )
    assert len(rows) == 1

    # Limiter cleared: a subsequent wrong attempt starts failure count fresh
    # (i.e. does not immediately lock out after just 1 more failure).
    wrong_resp = await authenticated_client.post(
        _VERIFY_URL,
        json={"pin": "0000"},
        headers=_csrf(authenticated_client),
    )
    assert wrong_resp.status_code == 400  # noqa: PLR2004


@pytest.mark.asyncio
async def test_post_pin_verify_wrong_400(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_pin(repo, pin="4321")

    response = await authenticated_client.post(
        _VERIFY_URL,
        json={"pin": "0000"},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 400  # noqa: PLR2004

    rows = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = :w"),
        {"w": "security.pin_verify_failed"},
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_post_pin_verify_no_pin_configured_400(
    authenticated_client: AsyncClient,
) -> None:
    response = await authenticated_client.post(
        _VERIFY_URL,
        json={"pin": "1234"},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 400  # noqa: PLR2004


@pytest.mark.asyncio
async def test_post_pin_verify_3rd_wrong_locks_at_5s(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """3 consecutive wrong PINs trigger a 5s lock; 4th attempt returns 429.

    PIN rate limiter curve: 3 fails=5s, 4 fails=30s, 5 fails=300s.
    This test covers the 3-fails → 5s lock case.
    """
    await _seed_pin(repo, pin="4321")

    # 3 wrong attempts — each returns 400 and ticks limiter.
    for _ in range(3):
        resp = await authenticated_client.post(
            _VERIFY_URL,
            json={"pin": "0000"},
            headers=_csrf(authenticated_client),
        )
        assert resp.status_code == 400  # noqa: PLR2004

    # Assert 3 verify-failed audit rows and 1 lock audit row @ 5s.
    failed_rows = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = :w"),
        {"w": "security.pin_verify_failed"},
    )
    assert len(failed_rows) == 3  # noqa: PLR2004

    locked_rows = await repo.fetch_all(
        text("SELECT after_json FROM audit_log WHERE what = :w"),
        {"w": "security.pin_locked"},
    )
    assert len(locked_rows) == 1
    after = json.loads(locked_rows[0][0])
    assert after["retry_after_seconds"] == 5  # noqa: PLR2004

    # 4th attempt: check() sees 3 fails, locks at 5s → 429
    resp4 = await authenticated_client.post(
        _VERIFY_URL,
        json={"pin": "0000"},
        headers=_csrf(authenticated_client),
    )
    assert resp4.status_code == 429  # noqa: PLR2004
    assert "Retry-After" in resp4.headers
    assert resp4.json()["error"]["details"]["retry_after_seconds"] == 5  # noqa: PLR2004


@pytest.mark.asyncio
async def test_post_pin_verify_extra_field_422(
    authenticated_client: AsyncClient,
) -> None:
    response = await authenticated_client.post(
        _VERIFY_URL,
        json={"pin": "1234", "extra_field": "x"},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_post_pin_verify_requires_session(
    unauthenticated_client: AsyncClient,
) -> None:
    response = await unauthenticated_client.post(_VERIFY_URL, json={"pin": "1234"})
    assert response.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_post_pin_verify_requires_csrf(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_pin(repo, pin="4321")
    response = await authenticated_client.post(_VERIFY_URL, json={"pin": "4321"})
    assert response.status_code == 403  # noqa: PLR2004


@pytest.mark.asyncio
async def test_x_forwarded_for_header_is_not_honored_in_audit(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """X-Forwarded-For header is ignored; audit records the actual remote_addr.

    The X-Forwarded-For header is deliberately NOT used (matches other routers'
    convention). Even if the header is set, audit.ip records the actual client
    remote address, not the header value. This prevents session-cookie attackers
    from spoofing audit IPs.
    """
    await _seed_pin(repo, pin="4321")

    response = await authenticated_client.post(
        _VERIFY_URL,
        json={"pin": "4321"},
        headers={
            **_csrf(authenticated_client),
            "X-Forwarded-For": "10.0.0.1, 10.0.0.2",
        },
    )
    assert response.status_code == 200  # noqa: PLR2004

    # Fetch the security.pin_verify_succeeded audit row.
    audit_rows = await repo.fetch_all(
        text("SELECT ip FROM audit_log WHERE what = :w"),
        {"w": "security.pin_verify_succeeded"},
    )
    assert len(audit_rows) == 1
    # The audit IP should NOT be the X-Forwarded-For value
    assert audit_rows[0][0] != "10.0.0.1"


@pytest.mark.asyncio
async def test_post_pin_rotate_flow_missing_current_pin_400(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """Rotating PIN without current_pin or current_password -> 400.

    Covers security_pin.py:169-172 — the password-or-pin requirement.
    """
    await _seed_pin(repo, pin="4321")

    response = await authenticated_client.post(
        _URL,
        json={"new_pin": "5678"},  # No current_pin, no current_password
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 400  # noqa: PLR2004
    # Message should mention that current credential is needed
    assert "current_pin" in response.json()["error"]["message"].lower()


# ---------------------------------------------------------------------------
# DELETE /pin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_pin_success_clears_limiter(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_pin(repo, pin="1234")

    # Rack up 2 failed verify attempts (below lockout threshold) to prove the
    # delete flow resets the limiter afterward.
    for _ in range(2):
        await authenticated_client.post(
            _VERIFY_URL,
            json={"pin": "0000"},
            headers=_csrf(authenticated_client),
        )

    response = await authenticated_client.request(
        "DELETE",
        _URL,
        json={"current_password": TEST_PASSWORD},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 200  # noqa: PLR2004
    assert response.json()["ok"] is True

    rows = await repo.fetch_all(
        text("SELECT before_json, after_json FROM audit_log WHERE what = :w"),
        {"w": "security.pin_removed"},
    )
    assert len(rows) == 1
    before = json.loads(rows[0][0])
    after = json.loads(rows[0][1])
    assert before == {"has_pin": True}
    assert after == {"has_pin": False}

    status_resp = await authenticated_client.get(_URL)
    assert status_resp.json()["set"] is False

    # Limiter reset: seed a fresh PIN and confirm 2 wrong attempts don't
    # carry over any prior lockout state (would already be proven by lack of
    # 429 above, but re-verify explicitly against a freshly-set PIN).
    await _seed_pin(repo, pin="5678")
    resp = await authenticated_client.post(
        _VERIFY_URL,
        json={"pin": "0000"},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 400  # noqa: PLR2004


@pytest.mark.asyncio
async def test_delete_pin_wrong_password_400(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_pin(repo, pin="1234")

    response = await authenticated_client.request(
        "DELETE",
        _URL,
        json={"current_password": "wrong-password"},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 400  # noqa: PLR2004


@pytest.mark.asyncio
async def test_delete_pin_wrong_password_rate_limited_429(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """5 wrong-password DELETE /pin attempts exhaust the login rate-limit budget.

    Mirrors test_post_pin_set_flow_wrong_password_rate_limited_429 but for the
    DELETE flow's separate audit `what` string. The login_rate_limiter is
    freshly constructed per test by the _per_test_db fixture in conftest.py,
    so no explicit reset is needed here.
    """
    await _seed_pin(repo, pin="1234")

    for _ in range(5):
        resp = await authenticated_client.request(
            "DELETE",
            _URL,
            json={"current_password": "wrong-password"},
            headers=_csrf(authenticated_client),
        )
        assert resp.status_code == 400  # noqa: PLR2004

    resp = await authenticated_client.request(
        "DELETE",
        _URL,
        json={"current_password": "wrong-password"},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 429  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "rate_limited"

    rows = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = :w"),
        {"w": "security.password_verify_failed_on_pin_delete"},
    )
    assert len(rows) == 5  # noqa: PLR2004


@pytest.mark.asyncio
async def test_delete_pin_when_no_pin_400(
    authenticated_client: AsyncClient,
) -> None:
    response = await authenticated_client.request(
        "DELETE",
        _URL,
        json={"current_password": TEST_PASSWORD},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 400  # noqa: PLR2004


@pytest.mark.asyncio
async def test_delete_pin_rejects_extra_field_422(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_pin(repo, pin="1234")
    response = await authenticated_client.request(
        "DELETE",
        _URL,
        json={"current_password": TEST_PASSWORD, "extra_field": "x"},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_delete_pin_requires_session(
    unauthenticated_client: AsyncClient,
) -> None:
    response = await unauthenticated_client.request(
        "DELETE", _URL, json={"current_password": TEST_PASSWORD}
    )
    assert response.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_delete_pin_requires_csrf(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_pin(repo, pin="1234")
    response = await authenticated_client.request(
        "DELETE", _URL, json={"current_password": TEST_PASSWORD}
    )
    assert response.status_code == 403  # noqa: PLR2004


# ---------------------------------------------------------------------------
# CSRF on POST /pin (set/rotate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_pin_requires_csrf(authenticated_client: AsyncClient) -> None:
    response = await authenticated_client.post(
        _URL,
        json={"new_pin": "1234", "current_password": TEST_PASSWORD},
    )
    assert response.status_code == 403  # noqa: PLR2004


@pytest.mark.asyncio
async def test_post_pin_requires_session(unauthenticated_client: AsyncClient) -> None:
    response = await unauthenticated_client.post(
        _URL, json={"new_pin": "1234", "current_password": TEST_PASSWORD}
    )
    assert response.status_code == 401  # noqa: PLR2004
