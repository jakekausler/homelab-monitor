"""Domain exceptions for auth subsystem with HTTP problem mapping."""

from __future__ import annotations

from typing import Any

from homelab_monitor.kernel.api.errors import (
    ConflictProblem,
    ForbiddenProblem,
    HttpProblem,
    NotFoundProblem,
    UnauthorizedProblem,
)


class UnauthenticatedProblem(UnauthorizedProblem):
    """401 with code `unauthenticated` — distinct from generic `unauthorized`."""

    def __init__(
        self,
        *,
        message: str = "authentication required",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, details=details, code="unauthenticated")


class WrongPasswordProblem(UnauthorizedProblem):
    """401 with code `wrong_password` — login failed."""

    def __init__(
        self,
        *,
        message: str = "invalid username or password",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, details=details, code="wrong_password")


class CsrfMismatchProblem(ForbiddenProblem):
    """403 with code `csrf_mismatch`."""

    def __init__(
        self,
        *,
        message: str = "missing or invalid CSRF token",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, details=details, code="csrf_mismatch")


class InsufficientScopeProblem(ForbiddenProblem):
    """403 with code `insufficient_scope`."""

    def __init__(
        self,
        *,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, details=details, code="insufficient_scope")


class RateLimitedProblem(HttpProblem):
    """429 with code `rate_limited`."""

    def __init__(
        self,
        *,
        message: str = "too many login attempts; try again later",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            status_code=429,
            code="rate_limited",
            message=message,
            details=details,
        )


class WeakPasswordProblem(HttpProblem):
    """400 with code `weak_password`."""

    def __init__(
        self,
        *,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            status_code=400,
            code="weak_password",
            message=message,
            details=details,
        )


class UserExistsProblem(ConflictProblem):
    """409 with code `user_exists`."""

    def __init__(
        self,
        *,
        message: str = "username already exists",
    ) -> None:
        super().__init__(message=message, code="user_exists")


class UserNotFoundProblem(NotFoundProblem):
    """404 with code `user_not_found`."""

    def __init__(
        self,
        *,
        message: str = "user not found",
    ) -> None:
        super().__init__(message=message, code="user_not_found")


class TokenNotFoundProblem(NotFoundProblem):
    """404 with code `token_not_found`."""

    def __init__(
        self,
        *,
        message: str = "api token not found",
    ) -> None:
        super().__init__(message=message, code="token_not_found")
