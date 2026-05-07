"""POST /api/auth/login, /logout, /change-password; GET /api/auth/me."""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request

from homelab_monitor.kernel.api.dependencies import (
    get_auth_repo,
    get_master_key,
    get_rate_limiter,
    require_session,
)
from homelab_monitor.kernel.api.middleware import SESSION_COOKIE_NAME
from homelab_monitor.kernel.auth.csrf import make_csrf_token
from homelab_monitor.kernel.auth.errors import (
    RateLimitedProblem,
    WeakPasswordProblem,
    WrongPasswordProblem,
)
from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.auth.passwords import (
    MIN_PASSWORD_LENGTH,
    hash_password,
)
from homelab_monitor.kernel.auth.rate_limit import LoginRateLimiter
from homelab_monitor.kernel.auth.repository import AuthRepository
from homelab_monitor.kernel.auth.sessions import make_session_cookie_value

CSRF_COOKIE_NAME = "homelab_monitor_csrf"
SESSION_TTL_ENV = "HOMELAB_MONITOR_SESSION_TTL_DAYS"
DEFAULT_SESSION_TTL_DAYS = 7
HTTPS_ONLY_ENV = "HOMELAB_MONITOR_HTTPS_ONLY_COOKIES"


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=128)
    # password.min_length is intentionally 1 (not 12 like
    # MIN_PASSWORD_LENGTH): refusing short inputs at LOGIN would leak the
    # policy and narrow attackers' brute-force search space. The 12-char
    # floor is enforced at HASH time (passwords.py:hash_password).
    password: str = Field(min_length=1)


class UserResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    username: str


class MeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user: UserResponse


class LoginResponse(BaseModel):
    """Response for POST /api/auth/login. Same shape as MeResponse but kept
    distinct so OpenAPI consumers see endpoint-specific schema names."""

    model_config = ConfigDict(extra="forbid")
    user: UserResponse


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=1)


router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    """Return the request peer IP, or "unknown" when starlette omits request.client.

    request.client is None only when the ASGI scope omits the `client` key
    (some lifespan-internal calls, ASGI test harnesses for HTTP/2 push, or
    Unix-socket transports that do not propagate a client tuple). Tests use
    httpx ASGITransport which always sets it, so the fallback is a defensive
    constant that we deliberately do not exercise.
    """
    if request.client is not None:
        return request.client.host
    return "unknown"  # pragma: no cover -- defensive fallback documented above


def _https_only_cookies() -> bool:
    val = os.environ.get(HTTPS_ONLY_ENV, "true").strip().lower()
    return val not in ("false", "0", "no")


def _session_ttl_seconds() -> int:
    raw = os.environ.get(SESSION_TTL_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_SESSION_TTL_DAYS * 24 * 3600
    try:
        days = int(raw)
    except ValueError:  # pragma: no cover -- env-var bad-int is logged once at startup elsewhere
        return DEFAULT_SESSION_TTL_DAYS * 24 * 3600
    if days < 1:
        import structlog  # noqa: PLC0415

        structlog.get_logger().warning(
            "auth.session_ttl_clamped",
            env_var=SESSION_TTL_ENV,
            requested_days=days,
            clamped_to_days=1,
        )
        days = 1
    return days * 24 * 3600


def _set_auth_cookies(
    response: Response,
    cookie_val: str,
    csrf_token: str,
    ttl_seconds: int,
) -> None:
    secure = _https_only_cookies()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=cookie_val,
        max_age=ttl_seconds,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        max_age=ttl_seconds,
        httponly=False,  # JS reads this and echoes in X-CSRF-Token header
        secure=secure,
        samesite="lax",
        path="/",
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")


@router.post("/login", response_model=LoginResponse)
async def login(  # noqa: PLR0913 -- FastAPI Depends parameters
    request: Request,
    body: LoginRequest,
    response: Response,
    auth_repo: Annotated[AuthRepository, Depends(get_auth_repo)],
    master_key: Annotated[bytes, Depends(get_master_key)],
    rate_limiter: Annotated[LoginRateLimiter, Depends(get_rate_limiter)],
) -> LoginResponse:
    """Authenticate username/password; issue session + CSRF cookies.

    Rate limiting: only failed attempts count against the budget. Successful
    logins do NOT consume a slot, so a legitimate user repeatedly logging in
    is never throttled.
    """
    ip = _client_ip(request)
    user = await auth_repo.verify_user_password(body.username, body.password)
    if user is None:
        # Failed attempt — record and enforce.
        if not rate_limiter.check_and_record(ip):
            raise RateLimitedProblem()
        raise WrongPasswordProblem()
    # Concurrent sessions allowed: a successful login mints a NEW session
    # without revoking other devices' sessions. Sessions are revocable from
    # Settings -> Auth (later stage) and expire on TTL. Change-password
    # still revokes all sessions (post-incident posture).
    csrf = make_csrf_token()
    ttl = _session_ttl_seconds()
    session = await auth_repo.create_session(user.id, ip, ttl, csrf)
    cookie_val = make_session_cookie_value(session.id, master_key)
    _set_auth_cookies(response, cookie_val, csrf, ttl)
    return LoginResponse(user=UserResponse(id=user.id, username=user.username))


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_session())],
    auth_repo: Annotated[AuthRepository, Depends(get_auth_repo)],
) -> Response:
    """Delete session row + clear cookies. Requires session + CSRF."""
    session = (
        request.state.session
    )  # populated by AuthMiddleware; guaranteed non-None by require_session()
    await auth_repo.delete_session(session.id, who=str(user.id), ip=_client_ip(request))
    _clear_auth_cookies(response)
    response.status_code = 204
    return response


@router.get("/me", response_model=MeResponse)
async def me(
    user: Annotated[User, Depends(require_session())],
) -> MeResponse:
    """Return the current authenticated user."""
    return MeResponse(user=UserResponse(id=user.id, username=user.username))


@router.post("/change-password", response_model=MeResponse)
async def change_password(  # noqa: PLR0913 -- FastAPI Depends parameters
    request: Request,
    body: ChangePasswordRequest,
    response: Response,
    user: Annotated[User, Depends(require_session())],
    auth_repo: Annotated[AuthRepository, Depends(get_auth_repo)],
    master_key: Annotated[bytes, Depends(get_master_key)],
    rate_limiter: Annotated[LoginRateLimiter, Depends(get_rate_limiter)],
) -> MeResponse:
    """Verify current password; change to new; rotate session."""
    if len(body.new_password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordProblem(
            message=f"new password must be at least {MIN_PASSWORD_LENGTH} characters",
        )
    # Re-verify current password (defense in depth: session compromise alone
    # must not yield a password change).
    verified = await auth_repo.verify_user_password(user.username, body.current_password)
    if verified is None:
        # A failed re-verify counts against the same per-IP login budget — a
        # session-stealing attacker cannot brute-force the password through
        # change-password. Use the same rate_limiter dependency as login.
        if not rate_limiter.check_and_record(_client_ip(request)):
            raise RateLimitedProblem()
        raise WrongPasswordProblem()
    new_hash = hash_password(body.new_password)
    ip = _client_ip(request)
    await auth_repo.change_password(user.id, new_hash, who=str(user.id), ip=ip)
    # Rotate sessions: kill all old, mint a fresh one. Concurrent in-flight
    # requests using the OLD session cookie will see 401 until the client
    # reads this response and updates its cookie jar — acceptable for a
    # single-user homelab; if a concurrent-request fleet ever matters, swap
    # for grace-period invalidation (mark old sessions as soft-revoked).
    await auth_repo.delete_all_user_sessions(user.id)
    csrf = make_csrf_token()
    ttl = _session_ttl_seconds()
    session = await auth_repo.create_session(user.id, ip, ttl, csrf)
    cookie_val = make_session_cookie_value(session.id, master_key)
    _set_auth_cookies(response, cookie_val, csrf, ttl)
    return MeResponse(user=UserResponse(id=user.id, username=user.username))
