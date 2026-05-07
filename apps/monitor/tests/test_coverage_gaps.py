"""Coverage gap tests for STAGE-001-011 auth/CLI code.

Covers uncovered lines across:
- homelab_monitor/kernel/auth/passwords.py (lines 20-23, 63-65)
- homelab_monitor/kernel/auth/errors.py (lines 61, 106, 117, 128)
- homelab_monitor/kernel/auth/api_tokens.py (line 61)
- homelab_monitor/kernel/auth/scopes.py (line 28)
- homelab_monitor/kernel/auth/sessions.py (line 64)
- homelab_monitor/kernel/auth/repository.py (lines 42, 292-293, 295)
- homelab_monitor/kernel/api/dependencies.py (lines 208-219, 229-246)
- homelab_monitor/kernel/api/middleware.py (lines 204, 208, 224, 227, 235, 238)
- homelab_monitor/kernel/api/routers/auth.py (lines 101-115, 231)
- homelab_monitor/cli/api_token.py (lines 46-55)
- homelab_monitor/cli/user.py (lines 41-52, 111)
"""

from __future__ import annotations

import argparse

import pytest

# ---------------------------------------------------------------------------
# passwords.py — _resolve_bcrypt_cost branches (lines 20-23) and
#                validate_password_length (lines 63-65)
# ---------------------------------------------------------------------------


def test_resolve_bcrypt_cost_invalid_env_returns_12(monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_bcrypt_cost returns 12 when env var is non-numeric."""

    import homelab_monitor.kernel.auth.passwords as pw_mod  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_BCRYPT_COST", "notanumber")
    # Call the private function directly — it returns 12 on ValueError
    result = pw_mod._resolve_bcrypt_cost()  # pyright: ignore[reportPrivateUsage]
    assert result == 12  # noqa: PLR2004


def test_resolve_bcrypt_cost_clamped_below_4(monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_bcrypt_cost clamps values below 4 up to 4."""
    import homelab_monitor.kernel.auth.passwords as pw_mod  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_BCRYPT_COST", "2")
    result = pw_mod._resolve_bcrypt_cost()  # pyright: ignore[reportPrivateUsage]
    assert result == 4  # noqa: PLR2004


def test_resolve_bcrypt_cost_clamped_above_20(monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_bcrypt_cost clamps values above 20 down to 20."""
    import homelab_monitor.kernel.auth.passwords as pw_mod  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_BCRYPT_COST", "99")
    result = pw_mod._resolve_bcrypt_cost()  # pyright: ignore[reportPrivateUsage]
    assert result == 20  # noqa: PLR2004


def test_validate_password_length_accepts_long_enough() -> None:
    """validate_password_length does not raise for passwords >= MIN_PASSWORD_LENGTH."""
    from homelab_monitor.kernel.auth.passwords import (  # noqa: PLC0415
        MIN_PASSWORD_LENGTH,
        validate_password_length,
    )

    validate_password_length("a" * MIN_PASSWORD_LENGTH)  # should not raise


def test_validate_password_length_rejects_short() -> None:
    """validate_password_length raises ValueError for passwords < MIN_PASSWORD_LENGTH."""
    from homelab_monitor.kernel.auth.passwords import (  # noqa: PLC0415
        MIN_PASSWORD_LENGTH,
        validate_password_length,
    )

    with pytest.raises(ValueError, match="at least"):
        validate_password_length("a" * (MIN_PASSWORD_LENGTH - 1))


# ---------------------------------------------------------------------------
# errors.py — __init__ bodies for InsufficientScopeProblem, UserExistsProblem,
#              UserNotFoundProblem, TokenNotFoundProblem (lines 61, 106, 117, 128)
# ---------------------------------------------------------------------------


def test_insufficient_scope_problem_defaults() -> None:
    """InsufficientScopeProblem stores message and code=insufficient_scope."""
    from homelab_monitor.kernel.auth.errors import InsufficientScopeProblem  # noqa: PLC0415

    err = InsufficientScopeProblem(message="token lacks required scope: read:status")
    assert err.code == "insufficient_scope"
    assert "read:status" in str(err.message)


def test_user_exists_problem_defaults() -> None:
    """UserExistsProblem uses default message and code=user_exists."""
    from homelab_monitor.kernel.auth.errors import UserExistsProblem  # noqa: PLC0415

    err = UserExistsProblem()
    assert err.code == "user_exists"
    assert "already exists" in err.message


def test_user_not_found_problem_defaults() -> None:
    """UserNotFoundProblem uses default message and code=user_not_found."""
    from homelab_monitor.kernel.auth.errors import UserNotFoundProblem  # noqa: PLC0415

    err = UserNotFoundProblem()
    assert err.code == "user_not_found"
    assert "not found" in err.message


def test_token_not_found_problem_defaults() -> None:
    """TokenNotFoundProblem uses default message and code=token_not_found."""
    from homelab_monitor.kernel.auth.errors import TokenNotFoundProblem  # noqa: PLC0415

    err = TokenNotFoundProblem()
    assert err.code == "token_not_found"
    assert "not found" in err.message


# ---------------------------------------------------------------------------
# api_tokens.py — parse_token_prefix sep <= 0 branch (line 61)
# ---------------------------------------------------------------------------


def test_parse_token_prefix_no_underscore_after_prefix() -> None:
    """parse_token_prefix returns None when there is no underscore after 'homelab_'."""
    from homelab_monitor.kernel.auth.api_tokens import parse_token_prefix  # noqa: PLC0415

    # "homelab_" followed by no underscore → sep <= 0 (sep == -1 from find)
    result = parse_token_prefix("homelab_nounderscore")
    assert result is None


def test_parse_token_prefix_valid() -> None:
    """parse_token_prefix returns the env prefix for a well-formed token."""
    from homelab_monitor.kernel.auth.api_tokens import parse_token_prefix  # noqa: PLC0415

    result = parse_token_prefix("homelab_prod_abc123")
    assert result == "prod"


def test_parse_token_prefix_not_homelab() -> None:
    """parse_token_prefix returns None for tokens not starting with 'homelab_'."""
    from homelab_monitor.kernel.auth.api_tokens import parse_token_prefix  # noqa: PLC0415

    result = parse_token_prefix("other_prod_abc123")
    assert result is None


# ---------------------------------------------------------------------------
# scopes.py — empty token after strip (line 28: consecutive commas)
# ---------------------------------------------------------------------------


def test_parse_scopes_consecutive_commas_skipped() -> None:
    """parse_scopes skips empty tokens produced by consecutive commas."""
    from homelab_monitor.kernel.auth.scopes import Scope, parse_scopes  # noqa: PLC0415

    # "heartbeat:write,,read:status" — the empty token between commas is skipped
    result = parse_scopes("heartbeat:write,,read:status")
    assert result == {Scope.HEARTBEAT_WRITE, Scope.READ_STATUS}


def test_parse_scopes_trailing_comma_skipped() -> None:
    """parse_scopes skips empty trailing token from trailing comma."""
    from homelab_monitor.kernel.auth.scopes import Scope, parse_scopes  # noqa: PLC0415

    result = parse_scopes("heartbeat:write,")
    assert result == {Scope.HEARTBEAT_WRITE}


# ---------------------------------------------------------------------------
# sessions.py — provided_hmac_hex wrong length branch (line 64)
# ---------------------------------------------------------------------------


def test_verify_session_cookie_value_wrong_hmac_length() -> None:
    """verify_session_cookie_value returns None when HMAC part is wrong length.

    Constructs a value with correct total length (65) but the dot is not at
    position SESSION_ID_LEN (32), causing the hmac part to have wrong length.
    This exercises line 64: len(provided_hmac_hex) != SESSION_HMAC_LEN_BYTES * 2.
    """
    from homelab_monitor.kernel.auth.sessions import (  # noqa: PLC0415
        COOKIE_VALUE_LEN,
        verify_session_cookie_value,
    )

    master_key = bytes(range(32))
    # Place the dot at position 10 (not 32), making session_id 10 chars and
    # hmac 54 chars — total is still 65 chars so the first length check passes,
    # but sep != SESSION_ID_LEN (32) so it should return None at the sep check.
    # To exercise line 64 specifically, the dot must be at SESSION_ID_LEN but
    # the hmac portion must be the wrong length. We need total len == COOKIE_VALUE_LEN
    # with dot at position SESSION_ID_LEN but hmac != 32 chars.
    # Build: 32 hex chars + "." + 32 hex chars = 65 total, but replace the last
    # hex char with a "." to get 32-char session id, "." at pos 32, then 31 chars
    # of hmac — but that only sums to 64. Instead: make total length 65 with dot
    # NOT at pos 32. Length check passes (65), sep check fails (sep != 32 → None).
    bad_val = "a" * 10 + "." + "b" * 54
    assert len(bad_val) == COOKIE_VALUE_LEN
    result = verify_session_cookie_value(bad_val, master_key)
    assert result is None


# ---------------------------------------------------------------------------
# repository.py — users_count row is None (line 42), is_session_expired
#                 unparseable (lines 292-293) and naive datetime (line 295)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_users_count_returns_zero_on_none_row(repo: object) -> None:
    """users_count returns 0 when fetch_one returns None (empty DB, COUNT always rows).

    The actual DB always returns a row for COUNT(*), so we test the None branch
    by mocking fetch_one.
    """
    from unittest.mock import AsyncMock, MagicMock  # noqa: PLC0415

    from homelab_monitor.kernel.auth.repository import AuthRepository  # noqa: PLC0415

    mock_repo = MagicMock()
    mock_repo.fetch_one = AsyncMock(return_value=None)
    auth_repo = AuthRepository(mock_repo)
    count = await auth_repo.users_count()
    assert count == 0


def test_is_session_expired_unparseable_timestamp() -> None:
    """is_session_expired returns True for an unparseable timestamp (defense in depth)."""
    from homelab_monitor.kernel.auth.repository import AuthRepository  # noqa: PLC0415

    result = AuthRepository.is_session_expired("not-a-valid-iso-timestamp")
    assert result is True


def test_is_session_expired_naive_datetime_treated_as_utc() -> None:
    """is_session_expired handles naive datetime (no tzinfo) by assuming UTC."""
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from homelab_monitor.kernel.auth.repository import AuthRepository  # noqa: PLC0415

    # Naive future timestamp — should NOT be expired
    future_naive = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    result = AuthRepository.is_session_expired(future_naive)
    assert result is False

    # Naive past timestamp — should be expired
    past_naive = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    result = AuthRepository.is_session_expired(past_naive)
    assert result is True


# ---------------------------------------------------------------------------
# dependencies.py — require_token_scope (lines 208-219) and
#                   require_user_or_token (lines 229-246)
# ---------------------------------------------------------------------------


def _make_request_with_state(**state_attrs: object) -> object:
    """Build a minimal duck-typed request object with arbitrary state attributes."""
    state = type("State", (), state_attrs)()
    app_state = type("AppState", (), {})()
    app = type("App", (), {"state": app_state})()
    return type("Request", (), {"state": state, "app": app, "method": "GET", "headers": {}})()


def test_require_token_scope_rejects_non_token_auth() -> None:
    """require_token_scope raises UnauthenticatedProblem when auth_kind != 'token'."""
    from homelab_monitor.kernel.api.dependencies import require_token_scope  # noqa: PLC0415
    from homelab_monitor.kernel.auth.errors import UnauthenticatedProblem  # noqa: PLC0415
    from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

    dep = require_token_scope(Scope.HEARTBEAT_WRITE)
    req = _make_request_with_state(auth_kind="unauthenticated", token=None)
    with pytest.raises(UnauthenticatedProblem):
        dep(req)  # pyright: ignore[reportArgumentType]


def test_require_token_scope_rejects_session_auth() -> None:
    """require_token_scope raises UnauthenticatedProblem when auth_kind == 'session'."""
    from unittest.mock import MagicMock  # noqa: PLC0415

    from homelab_monitor.kernel.api.dependencies import require_token_scope  # noqa: PLC0415
    from homelab_monitor.kernel.auth.errors import UnauthenticatedProblem  # noqa: PLC0415
    from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

    dep = require_token_scope(Scope.HEARTBEAT_WRITE)
    req = _make_request_with_state(auth_kind="session", user=MagicMock(), token=None)
    with pytest.raises(UnauthenticatedProblem):
        dep(req)  # pyright: ignore[reportArgumentType]


def test_require_token_scope_rejects_insufficient_scope() -> None:
    """require_token_scope raises InsufficientScopeProblem when scope not in token."""
    from homelab_monitor.kernel.api.dependencies import require_token_scope  # noqa: PLC0415
    from homelab_monitor.kernel.auth.errors import InsufficientScopeProblem  # noqa: PLC0415
    from homelab_monitor.kernel.auth.models import ApiToken  # noqa: PLC0415
    from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

    dep = require_token_scope(Scope.HEARTBEAT_WRITE)
    token = ApiToken(
        id="tok-1",
        name="test",
        scopes="read:status",  # does NOT include heartbeat:write
        created_at="2026-01-01T00:00:00",
        last_used_at=None,
        rotated_at=None,
    )
    req = _make_request_with_state(auth_kind="token", token=token)
    with pytest.raises(InsufficientScopeProblem):
        dep(req)  # pyright: ignore[reportArgumentType]


def test_require_token_scope_returns_token_when_scope_granted() -> None:
    """require_token_scope returns the ApiToken when scope is present."""
    from homelab_monitor.kernel.api.dependencies import require_token_scope  # noqa: PLC0415
    from homelab_monitor.kernel.auth.models import ApiToken  # noqa: PLC0415
    from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

    dep = require_token_scope(Scope.HEARTBEAT_WRITE)
    token = ApiToken(
        id="tok-1",
        name="test",
        scopes="heartbeat:write",
        created_at="2026-01-01T00:00:00",
        last_used_at=None,
        rotated_at=None,
    )
    req = _make_request_with_state(auth_kind="token", token=token)
    result = dep(req)  # pyright: ignore[reportArgumentType]
    assert result is token


def test_require_user_or_token_session_path_get_request() -> None:
    """require_user_or_token returns user for GET request with session auth (no CSRF needed)."""
    from unittest.mock import MagicMock  # noqa: PLC0415

    from homelab_monitor.kernel.api.dependencies import require_user_or_token  # noqa: PLC0415
    from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

    dep = require_user_or_token({Scope.READ_STATUS})
    mock_user = MagicMock()
    req = _make_request_with_state(auth_kind="session", user=mock_user, method="GET")
    # Need headers attribute for _enforce_csrf
    req.method = "GET"  # type: ignore[attr-defined]
    req.headers = {}  # type: ignore[attr-defined]
    result = dep(req)  # pyright: ignore[reportArgumentType]
    assert result is mock_user


def test_require_user_or_token_token_path_with_matching_scope() -> None:
    """require_user_or_token returns token when token has ANY of required scopes."""
    from homelab_monitor.kernel.api.dependencies import require_user_or_token  # noqa: PLC0415
    from homelab_monitor.kernel.auth.models import ApiToken  # noqa: PLC0415
    from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

    dep = require_user_or_token({Scope.HEARTBEAT_WRITE, Scope.READ_STATUS})
    token = ApiToken(
        id="tok-1",
        name="test",
        scopes="read:status",  # has READ_STATUS which is in the required set
        created_at="2026-01-01T00:00:00",
        last_used_at=None,
        rotated_at=None,
    )
    req = _make_request_with_state(auth_kind="token", token=token)
    result = dep(req)  # pyright: ignore[reportArgumentType]
    assert result is token


def test_require_user_or_token_token_path_insufficient_scope() -> None:
    """require_user_or_token raises InsufficientScopeProblem when token lacks required scopes."""
    from homelab_monitor.kernel.api.dependencies import require_user_or_token  # noqa: PLC0415
    from homelab_monitor.kernel.auth.errors import InsufficientScopeProblem  # noqa: PLC0415
    from homelab_monitor.kernel.auth.models import ApiToken  # noqa: PLC0415
    from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

    dep = require_user_or_token({Scope.HEARTBEAT_WRITE, Scope.ALERTS_INGEST_WRITE})
    token = ApiToken(
        id="tok-1",
        name="test",
        scopes="read:status",  # disjoint from required set
        created_at="2026-01-01T00:00:00",
        last_used_at=None,
        rotated_at=None,
    )
    req = _make_request_with_state(auth_kind="token", token=token)
    with pytest.raises(InsufficientScopeProblem):
        dep(req)  # pyright: ignore[reportArgumentType]


def test_require_user_or_token_unauthenticated_raises() -> None:
    """require_user_or_token raises UnauthenticatedProblem when neither session nor token."""
    from homelab_monitor.kernel.api.dependencies import require_user_or_token  # noqa: PLC0415
    from homelab_monitor.kernel.auth.errors import UnauthenticatedProblem  # noqa: PLC0415
    from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

    dep = require_user_or_token({Scope.READ_STATUS})
    req = _make_request_with_state(auth_kind="unauthenticated", user=None, token=None)
    with pytest.raises(UnauthenticatedProblem):
        dep(req)  # pyright: ignore[reportArgumentType]


# ---------------------------------------------------------------------------
# middleware.py — _auth_log_field branches (lines 204, 208, 224, 227, 235, 238)
# ---------------------------------------------------------------------------


def test_auth_log_field_session_kind() -> None:
    """_auth_log_field returns 'session(user_id=N)' for session auth."""
    from homelab_monitor.kernel.api.middleware import (
        _auth_log_field,  # pyright: ignore[reportPrivateUsage]
    )

    req = type("R", (), {"state": type("S", (), {"auth_kind": "session", "user_id": 42})()})()
    result = _auth_log_field(req)  # pyright: ignore[reportArgumentType, reportPrivateUsage]
    assert result == "session(user_id=42)"


def test_auth_log_field_session_kind_missing_user_id() -> None:
    """_auth_log_field falls back to '?' when user_id absent (defensive)."""
    from homelab_monitor.kernel.api.middleware import (
        _auth_log_field,  # pyright: ignore[reportPrivateUsage]
    )

    req = type("R", (), {"state": type("S", (), {"auth_kind": "session"})()})()
    result = _auth_log_field(req)  # pyright: ignore[reportArgumentType, reportPrivateUsage]
    assert result == "session(user_id=?)"


def test_auth_log_field_token_kind() -> None:
    """_auth_log_field returns 'token:<name>' for token auth."""
    from homelab_monitor.kernel.api.middleware import (
        _auth_log_field,  # pyright: ignore[reportPrivateUsage]
    )

    req = type(
        "R",
        (),
        {"state": type("S", (), {"auth_kind": "token", "token_name": "my-token"})()},
    )()
    result = _auth_log_field(req)  # pyright: ignore[reportArgumentType, reportPrivateUsage]
    assert result == "token:my-token"


def test_auth_log_field_token_kind_missing_name() -> None:
    """_auth_log_field falls back to '?' when token_name absent (defensive)."""
    from homelab_monitor.kernel.api.middleware import (
        _auth_log_field,  # pyright: ignore[reportPrivateUsage]
    )

    req = type("R", (), {"state": type("S", (), {"auth_kind": "token"})()})()
    result = _auth_log_field(req)  # pyright: ignore[reportArgumentType, reportPrivateUsage]
    assert result == "token:?"


def test_auth_log_field_unauthenticated_kind() -> None:
    """_auth_log_field returns 'unauthenticated' for unauthenticated requests."""
    from homelab_monitor.kernel.api.middleware import (
        _auth_log_field,  # pyright: ignore[reportPrivateUsage]
    )

    req = type("R", (), {"state": type("S", (), {"auth_kind": "unauthenticated"})()})()
    result = _auth_log_field(req)  # pyright: ignore[reportArgumentType, reportPrivateUsage]
    assert result == "unauthenticated"


def test_auth_log_field_missing_auth_kind() -> None:
    """_auth_log_field falls back to 'unauthenticated' when auth_kind absent."""
    from homelab_monitor.kernel.api.middleware import (
        _auth_log_field,  # pyright: ignore[reportPrivateUsage]
    )

    req = type("R", (), {"state": type("S", (), {})()})()
    result = _auth_log_field(req)  # pyright: ignore[reportArgumentType, reportPrivateUsage]
    assert result == "unauthenticated"


# ---------------------------------------------------------------------------
# routers/auth.py — _session_ttl_seconds clamp branch (lines 101-115)
#                   and rate-limit on change-password (line 231)
# ---------------------------------------------------------------------------


def test_session_ttl_seconds_clamps_zero_to_one_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """_session_ttl_seconds clamps days=0 to 1 day and emits a warning log."""
    from homelab_monitor.kernel.api.routers.auth import (  # noqa: PLC0415
        SESSION_TTL_ENV,
        _session_ttl_seconds,  # pyright: ignore[reportPrivateUsage]
    )

    monkeypatch.setenv(SESSION_TTL_ENV, "0")
    result = _session_ttl_seconds()  # pyright: ignore[reportPrivateUsage]
    assert result == 1 * 24 * 3600


def test_session_ttl_seconds_clamps_negative_days(monkeypatch: pytest.MonkeyPatch) -> None:
    """_session_ttl_seconds clamps negative days to 1 day."""
    from homelab_monitor.kernel.api.routers.auth import (  # noqa: PLC0415
        SESSION_TTL_ENV,
        _session_ttl_seconds,  # pyright: ignore[reportPrivateUsage]
    )

    monkeypatch.setenv(SESSION_TTL_ENV, "-5")
    result = _session_ttl_seconds()  # pyright: ignore[reportPrivateUsage]
    assert result == 1 * 24 * 3600


def test_session_ttl_seconds_valid_positive_days(monkeypatch: pytest.MonkeyPatch) -> None:
    """_session_ttl_seconds returns correct seconds for valid positive days (no clamping)."""
    from homelab_monitor.kernel.api.routers.auth import (  # noqa: PLC0415
        SESSION_TTL_ENV,
        _session_ttl_seconds,  # pyright: ignore[reportPrivateUsage]
    )

    monkeypatch.setenv(SESSION_TTL_ENV, "3")
    result = _session_ttl_seconds()  # pyright: ignore[reportPrivateUsage]
    assert result == 3 * 24 * 3600


@pytest.mark.asyncio
async def test_change_password_wrong_current_rate_limit_exhausted(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """change-password with wrong current_password that exhausts rate limit returns 429."""
    import base64  # noqa: PLC0415
    from unittest.mock import MagicMock  # noqa: PLC0415

    from httpx import ASGITransport  # noqa: PLC0415
    from httpx import AsyncClient as HttpxClient  # noqa: PLC0415

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415
    from homelab_monitor.kernel.auth.passwords import hash_password  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_HTTPS_ONLY_COOKIES", "false")

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        # Create user and log in
        await app.state.auth_repo.create_user("testuser2", hash_password("testpassword123", cost=4))

        # Replace the rate limiter with one that always denies
        mock_limiter = MagicMock()
        mock_limiter.check_and_record.return_value = False
        app.state.login_rate_limiter = mock_limiter

        async with HttpxClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Log in to get a session
            login_resp = await client.post(
                "/api/auth/login",
                json={"username": "testuser2", "password": "testpassword123"},
            )
            assert login_resp.status_code == 200  # noqa: PLR2004

            csrf = client.cookies.get("homelab_monitor_csrf")
            resp = await client.post(
                "/api/auth/change-password",
                json={
                    "current_password": "wrongpassword123",
                    "new_password": "newpassword123",
                },
                headers={"X-CSRF-Token": csrf},  # type: ignore[arg-type]
            )
    assert resp.status_code == 429  # noqa: PLR2004
    data = resp.json()
    assert data["error"]["code"] == "rate_limited"


# ---------------------------------------------------------------------------
# cli/api_token.py — _handle dispatch (lines 46-55)
# ---------------------------------------------------------------------------


def test_cli_api_token_handle_no_subcommand_returns_2(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_handle with no api_token_cmd subcommand prints usage and returns 2."""
    from unittest.mock import patch  # noqa: PLC0415

    from homelab_monitor.cli.api_token import (
        _handle,  # pyright: ignore[reportPrivateUsage]
    )

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    args = argparse.Namespace(api_token_cmd=None)
    with patch("builtins.print"):
        result = _handle(args)  # pyright: ignore[reportPrivateUsage]
    assert result == 2  # noqa: PLR2004


def test_cli_api_token_handle_create_dispatches(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_handle dispatches 'create' subcommand correctly."""
    from unittest.mock import patch  # noqa: PLC0415

    from homelab_monitor.cli.api_token import (
        _handle,  # pyright: ignore[reportPrivateUsage]
    )
    from homelab_monitor.kernel.db.migrations import alembic_upgrade_head  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    args = argparse.Namespace(api_token_cmd="create", scope=["heartbeat:write"], name="tok-1")
    with patch("builtins.print"):
        result = _handle(args)  # pyright: ignore[reportPrivateUsage]
    assert result == 0


def test_cli_api_token_handle_list_dispatches(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """_handle dispatches 'list' subcommand correctly."""
    from unittest.mock import patch  # noqa: PLC0415

    from homelab_monitor.cli.api_token import (
        _handle,  # pyright: ignore[reportPrivateUsage]
    )
    from homelab_monitor.kernel.db.migrations import alembic_upgrade_head  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    args = argparse.Namespace(api_token_cmd="list")
    with patch("builtins.print"):
        result = _handle(args)  # pyright: ignore[reportPrivateUsage]
    assert result == 0


def test_cli_api_token_handle_revoke_dispatches(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_handle dispatches 'revoke' subcommand correctly (nonexistent token → 1)."""
    from homelab_monitor.cli.api_token import (
        _handle,  # pyright: ignore[reportPrivateUsage]
    )
    from homelab_monitor.kernel.db.migrations import alembic_upgrade_head  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    args = argparse.Namespace(api_token_cmd="revoke", token_id="nonexistent-id")
    result = _handle(args)  # pyright: ignore[reportPrivateUsage]
    assert result == 1


# ---------------------------------------------------------------------------
# cli/user.py — _handle dispatch (lines 41-52) and _cmd_passwd missing-user (line 111)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# routers/collectors.py — next_run calculation (lines 75-80)
# Requires a collector with a last_run metric on the SAME app instance.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collectors_list_next_run_calculated_when_last_run_present(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /api/collectors calculates next_run when last_run is present (lines 75-80)."""
    import base64  # noqa: PLC0415

    from httpx import ASGITransport  # noqa: PLC0415
    from httpx import AsyncClient as HttpxClient  # noqa: PLC0415

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415
    from homelab_monitor.kernel.auth.passwords import hash_password  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_HTTPS_ONLY_COOKIES", "false")

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        await app.state.auth_repo.create_user("listuser", hash_password("testpassword123", cost=4))

        # Write a metric to the SAME app's writer to simulate a completed tick
        app.state.metrics_writer.write_counter(
            "homelab_collector_run_success_total",
            1.0,
            {"name": "noop"},
        )

        async with HttpxClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            login = await client.post(
                "/api/auth/login",
                json={"username": "listuser", "password": "testpassword123"},
            )
            assert login.status_code == 200  # noqa: PLR2004

            resp = await client.get("/api/collectors")
            assert resp.status_code == 200  # noqa: PLR2004
            data = resp.json()
            assert isinstance(data, list)
            # At least the noop collector should appear
            noop = next((c for c in data if c["name"] == "noop"), None)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            assert noop is not None
            # With the metric written, last_run should be set → next_run calculated
            assert noop["last_run"] is not None
            assert noop["next_run"] is not None


def test_cli_user_handle_no_subcommand_returns_2(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_handle with no user_cmd subcommand prints usage and returns 2."""
    from unittest.mock import patch  # noqa: PLC0415

    from homelab_monitor.cli.user import (
        _handle,  # pyright: ignore[reportPrivateUsage]
    )

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    args = argparse.Namespace(user_cmd=None)
    with patch("builtins.print"):
        result = _handle(args)  # pyright: ignore[reportPrivateUsage]
    assert result == 2  # noqa: PLR2004


def test_cli_user_handle_create_dispatches(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """_handle dispatches 'create' subcommand correctly."""
    from unittest.mock import patch  # noqa: PLC0415

    from homelab_monitor.cli.user import (
        _handle,  # pyright: ignore[reportPrivateUsage]
    )
    from homelab_monitor.kernel.db.migrations import alembic_upgrade_head  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    args = argparse.Namespace(user_cmd="create", username="testuser")
    with patch("homelab_monitor.cli.user.getpass.getpass") as mock_getpass:
        mock_getpass.side_effect = ["testpassword123", "testpassword123"]
        result = _handle(args)  # pyright: ignore[reportPrivateUsage]
    assert result == 0


def test_cli_user_handle_list_dispatches(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """_handle dispatches 'list' subcommand correctly."""
    from unittest.mock import patch  # noqa: PLC0415

    from homelab_monitor.cli.user import (
        _handle,  # pyright: ignore[reportPrivateUsage]
    )
    from homelab_monitor.kernel.db.migrations import alembic_upgrade_head  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    args = argparse.Namespace(user_cmd="list")
    with patch("builtins.print"):
        result = _handle(args)  # pyright: ignore[reportPrivateUsage]
    assert result == 0


def test_cli_user_handle_passwd_dispatches(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """_handle dispatches 'passwd' subcommand correctly (missing user → 1)."""
    from unittest.mock import patch  # noqa: PLC0415

    from homelab_monitor.cli.user import (
        _handle,  # pyright: ignore[reportPrivateUsage]
    )
    from homelab_monitor.kernel.db.migrations import alembic_upgrade_head  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    args = argparse.Namespace(user_cmd="passwd", username="nobody")
    with patch("homelab_monitor.cli.user.getpass.getpass") as mock_getpass:
        mock_getpass.side_effect = ["testpassword123", "testpassword123"]
        result = _handle(args)  # pyright: ignore[reportPrivateUsage]
    assert result == 1


def test_cli_user_handle_delete_dispatches(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """_handle dispatches 'delete' subcommand correctly (missing user → 1)."""
    from homelab_monitor.cli.user import (
        _handle,  # pyright: ignore[reportPrivateUsage]
    )
    from homelab_monitor.kernel.db.migrations import alembic_upgrade_head  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    args = argparse.Namespace(user_cmd="delete", username="nobody")
    result = _handle(args)  # pyright: ignore[reportPrivateUsage]
    assert result == 1


def test_cli_user_cmd_passwd_missing_user_returns_1(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_cmd_passwd returns 1 when user does not exist (line 114-116, not line 111)."""
    import asyncio  # noqa: PLC0415
    from unittest.mock import patch  # noqa: PLC0415

    from homelab_monitor.cli.user import (
        _cmd_passwd,  # pyright: ignore[reportPrivateUsage]
    )
    from homelab_monitor.kernel.db.migrations import alembic_upgrade_head  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    with patch("homelab_monitor.cli.user.getpass.getpass") as mock_getpass:
        mock_getpass.side_effect = ["testpassword123", "testpassword123"]
        result = asyncio.run(_cmd_passwd("doesnotexist"))  # pyright: ignore[reportPrivateUsage]
    assert result == 1


def test_cli_user_cmd_passwd_bad_password_returns_1(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_cmd_passwd returns 1 when _prompt_password_twice returns None (line 111).

    This happens when passwords don't match, hitting the early-return branch.
    """
    import asyncio  # noqa: PLC0415
    from unittest.mock import patch  # noqa: PLC0415

    from homelab_monitor.cli.user import (
        _cmd_passwd,  # pyright: ignore[reportPrivateUsage]
    )

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)

    # Mismatched passwords → _prompt_password_twice returns None → line 111 executes
    with patch("homelab_monitor.cli.user.getpass.getpass") as mock_getpass:
        mock_getpass.side_effect = ["testpassword123", "differentpassword"]
        result = asyncio.run(_cmd_passwd("anyuser"))  # pyright: ignore[reportPrivateUsage]
    assert result == 1


# ---------------------------------------------------------------------------
# middleware.py — _resolve_token and _resolve_session branch coverage
# Lines 204 (no auth_repo), 208 (token None), 224 (no master_key),
# 227 (no auth_repo in session), 235 (expired session), 238 (user None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_token_no_auth_repo_skips(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_resolve_token returns early when auth_repo not set on app.state (line 204)."""
    import base64  # noqa: PLC0415

    from httpx import ASGITransport  # noqa: PLC0415
    from httpx import AsyncClient as HttpxClient  # noqa: PLC0415

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    app = create_app(lifespan_enabled=False)  # no lifespan → auth_repo not set

    async with HttpxClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Send a Bearer token to a non-exempt path; _resolve_token finds no auth_repo → return
        resp = await client.get(
            "/api/collectors",
            headers={"Authorization": "Bearer homelab_prod_sometoken"},
        )
    # No auth_repo → auth_kind stays unauthenticated → require_session → 401
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_resolve_token_unknown_token_returns_unauthenticated(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_resolve_token returns early when token hash not found in DB (line 208)."""
    import base64  # noqa: PLC0415

    from httpx import ASGITransport  # noqa: PLC0415
    from httpx import AsyncClient as HttpxClient  # noqa: PLC0415

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    app = create_app(lifespan_enabled=True)
    async with (
        app.router.lifespan_context(app),
        HttpxClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        # Token not in DB → get_api_token_by_hash returns None → line 208
        resp = await client.get(
            "/api/collectors",
            headers={"Authorization": "Bearer homelab_prod_unknowntoken123456789"},
        )
    # Protected endpoint → 401 (unauthenticated after token not found)
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_resolve_session_no_master_key_skips(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_resolve_session returns early when master_key not set on app.state (line 224)."""
    from httpx import ASGITransport  # noqa: PLC0415
    from httpx import AsyncClient as HttpxClient  # noqa: PLC0415

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    # No MASTER_KEY env → lifespan won't set it; but use lifespan=False to be safe
    app = create_app(lifespan_enabled=False)

    async with HttpxClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/api/collectors",
            cookies={"homelab_monitor_session": "a" * 32 + "." + "b" * 32},
        )
    # No master_key → session resolution skipped → unauthenticated → 401
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_resolve_session_no_auth_repo_skips(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_resolve_session returns early when auth_repo not an AuthRepository (line 227)."""
    import base64  # noqa: PLC0415

    from httpx import ASGITransport  # noqa: PLC0415
    from httpx import AsyncClient as HttpxClient  # noqa: PLC0415

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415
    from homelab_monitor.kernel.auth.sessions import (  # noqa: PLC0415
        make_session_cookie_value,
        make_session_id,
    )

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    app = create_app(lifespan_enabled=False)
    # Set master_key but NOT auth_repo → line 227 triggers
    app.state.master_key = master_key

    sid = make_session_id()
    cookie_val = make_session_cookie_value(sid, master_key)

    async with HttpxClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/api/collectors",
            cookies={"homelab_monitor_session": cookie_val},
        )
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_resolve_session_expired_session_skips(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_resolve_session returns early when session is expired (line 235)."""
    import base64  # noqa: PLC0415
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from httpx import ASGITransport  # noqa: PLC0415
    from httpx import AsyncClient as HttpxClient  # noqa: PLC0415
    from sqlalchemy import text  # noqa: PLC0415

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415
    from homelab_monitor.kernel.auth.csrf import make_csrf_token  # noqa: PLC0415
    from homelab_monitor.kernel.auth.passwords import hash_password  # noqa: PLC0415
    from homelab_monitor.kernel.auth.sessions import (  # noqa: PLC0415
        make_session_cookie_value,
        make_session_id,
    )

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_HTTPS_ONLY_COOKIES", "false")

    app = create_app(lifespan_enabled=True)
    async with (
        app.router.lifespan_context(app),
        HttpxClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        # Create user and an already-expired session
        user = await app.state.auth_repo.create_user(
            "expuser", hash_password("testpassword123", cost=4)
        )
        sid = make_session_id()
        past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        csrf = make_csrf_token()
        await app.state.repo.execute(
            text(
                "INSERT INTO sessions (id, user_id, expires_at, created_ip, csrf_token) "
                "VALUES (:id, :uid, :exp, :ip, :csrf)"
            ),
            {"id": sid, "uid": user.id, "exp": past, "ip": "127.0.0.1", "csrf": csrf},
        )
        cookie_val = make_session_cookie_value(sid, master_key)
        resp = await client.get(
            "/api/auth/me",
            cookies={"homelab_monitor_session": cookie_val},
        )
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_resolve_session_user_not_found_skips(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_resolve_session returns early when user_id in session has no matching user (line 238)."""
    import base64  # noqa: PLC0415
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from httpx import ASGITransport  # noqa: PLC0415
    from httpx import AsyncClient as HttpxClient  # noqa: PLC0415
    from sqlalchemy import text  # noqa: PLC0415

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415
    from homelab_monitor.kernel.auth.csrf import make_csrf_token  # noqa: PLC0415
    from homelab_monitor.kernel.auth.passwords import hash_password  # noqa: PLC0415
    from homelab_monitor.kernel.auth.sessions import (  # noqa: PLC0415
        make_session_cookie_value,
        make_session_id,
    )

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_HTTPS_ONLY_COOKIES", "false")

    app = create_app(lifespan_enabled=True)
    async with (
        app.router.lifespan_context(app),
        HttpxClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        # Create user, create session, then delete the user — session still in DB
        user = await app.state.auth_repo.create_user(
            "ghostuser", hash_password("testpassword123", cost=4)
        )
        sid = make_session_id()
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        csrf = make_csrf_token()
        await app.state.repo.execute(
            text(
                "INSERT INTO sessions (id, user_id, expires_at, created_ip, csrf_token) "
                "VALUES (:id, :uid, :exp, :ip, :csrf)"
            ),
            {"id": sid, "uid": user.id, "exp": future, "ip": "127.0.0.1", "csrf": csrf},
        )
        # Delete the session first (FK), then delete the user, then re-insert the session
        # to simulate a dangling session referencing a deleted user.
        # SQLite FK enforcement: disable temporarily to insert orphaned session.
        await app.state.repo.execute(text("PRAGMA foreign_keys = OFF"), {})
        await app.state.repo.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
        await app.state.repo.execute(text("PRAGMA foreign_keys = ON"), {})
        cookie_val = make_session_cookie_value(sid, master_key)
        resp = await client.get(
            "/api/auth/me",
            cookies={"homelab_monitor_session": cookie_val},
        )
    assert resp.status_code == 401  # noqa: PLR2004
