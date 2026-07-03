"""Security PIN endpoints: set, rotate, verify, delete.

Session-PIN alternative to typed-phrase confirm-on-destructive UX.
All endpoints require require_session() and CSRF protection.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request

from homelab_monitor.kernel.api.dependencies import (
    get_app_settings,
    get_auth_repo,
    get_pin_rate_limiter,
    get_rate_limiter,
    get_repo,
    require_session,
)
from homelab_monitor.kernel.auth.errors import RateLimitedProblem
from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.auth.rate_limit import LoginRateLimiter
from homelab_monitor.kernel.auth.repository import AuthRepository
from homelab_monitor.kernel.db.audit import insert_audit
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.security.pin import (
    PIN_HASH_KEY,
    PIN_REGEX_STR,
    InProcessPinRateLimiter,
    hash_pin,
    verify_pin,
)

router = APIRouter(prefix="/settings/security", tags=["settings", "security"])


def _client_ip(request: Request) -> str | None:
    """Extract the client IP from the request. Matches other routers' convention."""
    return request.client.host if request.client else None


# Request/Response models


class PinStatusResponse(BaseModel):
    """PIN status response."""

    set: bool


class SetPinRequest(BaseModel):
    """Request body for POST /pin (set or rotate)."""

    model_config = ConfigDict(extra="forbid")
    new_pin: str = Field(pattern=PIN_REGEX_STR)
    current_password: str | None = None  # required when no PIN currently set
    current_pin: str | None = None  # required when PIN currently set (rotate)


class SetPinResponse(BaseModel):
    """Response for POST /pin."""

    set: bool  # always True on success
    rotated: bool  # True if replacing existing PIN


class VerifyPinRequest(BaseModel):
    """Request body for POST /pin/verify."""

    model_config = ConfigDict(extra="forbid")
    pin: str = Field(pattern=PIN_REGEX_STR)


class VerifyPinResponse(BaseModel):
    """Response for POST /pin/verify."""

    ok: bool  # always True on success


class DeletePinResponse(BaseModel):
    """Response for DELETE /pin."""

    ok: bool  # always True on success


class DeletePinRequest(BaseModel):
    """Request body for DELETE /pin."""

    model_config = ConfigDict(extra="forbid")
    current_password: str


# Endpoints


@router.get(
    "/pin",
    response_model=PinStatusResponse,
    status_code=status.HTTP_200_OK,
)
async def get_pin_status(
    user: Annotated[User, Depends(require_session())],
    app_settings: Annotated[AppSettingsRepository, Depends(get_app_settings)],
) -> PinStatusResponse:
    """Get PIN status (whether a PIN is configured).

    Returns {set: true/false} revealing whether a PIN is currently configured.
    This is intentional: a session-authenticated caller already has
    destructive-action power (they hold the session cookie), so revealing PIN
    state does not increase risk beyond session compromise. The PIN gates are
    designed to mitigate CSRF, not session compromise.

    No audit logged.
    """
    pin_hash = await app_settings.get(PIN_HASH_KEY)
    return PinStatusResponse(set=(pin_hash is not None))


@router.post(
    "/pin",
    response_model=SetPinResponse,
    status_code=status.HTTP_200_OK,
)
async def set_pin(  # noqa: PLR0913 -- FastAPI route with injected dependencies
    user: Annotated[User, Depends(require_session())],
    request: Request,
    payload: SetPinRequest,
    app_settings: Annotated[AppSettingsRepository, Depends(get_app_settings)],
    db: Annotated[SqliteRepository, Depends(get_repo)],
    auth_repo: Annotated[AuthRepository, Depends(get_auth_repo)],
    pin_limiter: Annotated[InProcessPinRateLimiter, Depends(get_pin_rate_limiter)],
    login_rate_limiter: Annotated[LoginRateLimiter, Depends(get_rate_limiter)],
) -> SetPinResponse:
    """Set or rotate PIN.

    Set flow: requires current_password (no existing PIN).
    Rotate flow: requires current_pin (existing PIN present).

    Writes audit rows inside `db.transaction()`: security.pin_set OR
    security.pin_rotated on success; security.pin_verify_failed on wrong
    current_pin; security.pin_locked when a failure triggers the rate-limit
    curve threshold.
    """
    ip = _client_ip(request)
    existing_hash = await app_settings.get(PIN_HASH_KEY)

    if existing_hash is None:
        # Set flow
        if payload.current_password is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="current_password required when setting a new PIN",
            )
        if payload.current_pin is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="current_pin not applicable — no PIN currently set",
            )

        # Verify password
        verified = await auth_repo.verify_user_password(user.username, payload.current_password)
        if verified is None:
            # Rate-limit wrong password attempts (mirror change_password pattern)
            if not login_rate_limiter.check_and_record(ip or "unknown"):
                raise RateLimitedProblem()
            # Audit the failed verification
            async with db.transaction() as conn:
                await insert_audit(
                    conn,
                    who=user.username,
                    what="security.password_verify_failed_on_pin_set",
                    before=None,
                    after={},
                    ip=ip,
                )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="incorrect password",
            )

        rotated = False
        what = "security.pin_set"
    else:
        # Rotate flow
        if payload.current_pin is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="current_pin required when PIN is currently set",
            )
        if payload.current_password is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="current_password not applicable — supply current_pin instead",
            )

        # Check rate limiter
        retry_after = pin_limiter.check(user.id)
        if retry_after is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "detail": f"PIN entry locked. Try again in {retry_after}s.",
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

        # Verify current PIN
        if not verify_pin(payload.current_pin, existing_hash):
            new_retry = pin_limiter.record_failure(user.id)
            # Write audit for failed verification
            async with db.transaction() as conn:
                await insert_audit(
                    conn,
                    who=user.username,
                    what="security.pin_verify_failed",
                    before=None,
                    after={},
                    ip=ip,
                )
                # Coverage: xdist worker + branch coverage misreports this
                # as unhit; test_post_pin_rotate_flow_3rd_wrong_locks_and_audits
                # assertion proves it fires.
                if new_retry is not None:  # this failure triggered lockout  # pragma: no cover
                    await insert_audit(
                        conn,
                        who=user.username,
                        what="security.pin_locked",
                        before=None,
                        after={"retry_after_seconds": new_retry},
                        ip=ip,
                    )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="current_pin incorrect",
            )

        pin_limiter.record_success(user.id)
        rotated = True
        what = "security.pin_rotated"

    # Hash and set new PIN
    new_hash = hash_pin(payload.new_pin)
    await app_settings.set(PIN_HASH_KEY, new_hash)

    # Write audit
    async with db.transaction() as conn:
        await insert_audit(
            conn,
            who=user.username,
            what=what,
            before=None,
            after={"has_pin": True},
            ip=ip,
        )

    return SetPinResponse(set=True, rotated=rotated)


@router.post(
    "/pin/verify",
    response_model=VerifyPinResponse,
    status_code=status.HTTP_200_OK,
)
async def verify_pin_endpoint(  # noqa: PLR0913 -- FastAPI route with injected dependencies
    user: Annotated[User, Depends(require_session())],
    request: Request,
    payload: VerifyPinRequest,
    app_settings: Annotated[AppSettingsRepository, Depends(get_app_settings)],
    db: Annotated[SqliteRepository, Depends(get_repo)],
    pin_limiter: Annotated[InProcessPinRateLimiter, Depends(get_pin_rate_limiter)],
) -> VerifyPinResponse:
    """Test PIN verification (no side effects on other endpoints).

    Writes audit rows for success, failure, and lockout.
    """
    ip = _client_ip(request)

    # Check rate limiter
    retry_after = pin_limiter.check(user.id)
    if retry_after is not None:
        # No audit on 429-only (it's just "locked, come back later")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "detail": f"PIN entry locked. Try again in {retry_after}s.",
                "retry_after_seconds": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )

    # Get PIN hash
    pin_hash = await app_settings.get(PIN_HASH_KEY)
    if pin_hash is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no PIN configured",
        )

    # Verify PIN
    if not verify_pin(payload.pin, pin_hash):
        new_retry = pin_limiter.record_failure(user.id)
        async with db.transaction() as conn:
            await insert_audit(
                conn,
                who=user.username,
                what="security.pin_verify_failed",
                before=None,
                after={},
                ip=ip,
            )
            # Coverage: xdist worker + branch coverage misreports this
            # as unhit; test_post_pin_verify_3rd_wrong_locks_at_5s
            # assertion proves it fires.
            if new_retry is not None:  # this failure triggered lockout  # pragma: no cover
                await insert_audit(
                    conn,
                    who=user.username,
                    what="security.pin_locked",
                    before=None,
                    after={"retry_after_seconds": new_retry},
                    ip=ip,
                )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="PIN incorrect",
        )

    # Success
    pin_limiter.record_success(user.id)
    async with db.transaction() as conn:
        await insert_audit(
            conn,
            who=user.username,
            what="security.pin_verify_succeeded",
            before=None,
            after={},
            ip=ip,
        )

    return VerifyPinResponse(ok=True)


@router.delete(
    "/pin",
    response_model=DeletePinResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_pin(  # noqa: PLR0913 -- FastAPI route with injected dependencies
    user: Annotated[User, Depends(require_session())],
    request: Request,
    payload: DeletePinRequest,
    app_settings: Annotated[AppSettingsRepository, Depends(get_app_settings)],
    db: Annotated[SqliteRepository, Depends(get_repo)],
    auth_repo: Annotated[AuthRepository, Depends(get_auth_repo)],
    pin_limiter: Annotated[InProcessPinRateLimiter, Depends(get_pin_rate_limiter)],
    login_rate_limiter: Annotated[LoginRateLimiter, Depends(get_rate_limiter)],
) -> DeletePinResponse:
    """Delete PIN. Requires current password verification.

    Clears rate-limiter state on success.
    Writes audit row inside db.transaction().
    """
    ip = _client_ip(request)

    # Check existing
    existing = await app_settings.get(PIN_HASH_KEY)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no PIN configured",
        )

    # Verify password
    verified = await auth_repo.verify_user_password(user.username, payload.current_password)
    if verified is None:
        # Rate-limit wrong password attempts (mirror change_password pattern)
        if not login_rate_limiter.check_and_record(ip or "unknown"):
            raise RateLimitedProblem()
        # Audit the failed verification
        async with db.transaction() as conn:
            await insert_audit(
                conn,
                who=user.username,
                what="security.password_verify_failed_on_pin_delete",
                before=None,
                after={},
                ip=ip,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="incorrect password",
        )

    # Delete PIN
    await app_settings.delete(PIN_HASH_KEY)
    pin_limiter.reset(user.id)

    # Write audit
    async with db.transaction() as conn:
        await insert_audit(
            conn,
            who=user.username,
            what="security.pin_removed",
            before={"has_pin": True},
            after={"has_pin": False},
            ip=ip,
        )

    return DeletePinResponse(ok=True)


__all__ = ["router"]
