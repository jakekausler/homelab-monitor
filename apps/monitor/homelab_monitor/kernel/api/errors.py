"""Error envelope and domain exceptions with HTTP mapping."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from homelab_monitor.kernel.db.migrations import MigrationsPendingError
from homelab_monitor.kernel.secrets.errors import MasterKeyError
from homelab_monitor.kernel.secrets.repository import SecretIntegrityError


class ErrorPayload(BaseModel):
    """Error details within an ErrorEnvelope."""

    model_config = ConfigDict(extra="forbid")
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorEnvelope(BaseModel):
    """Uniform error response envelope."""

    model_config = ConfigDict(extra="forbid")
    error: ErrorPayload


class HttpProblem(Exception):
    """Base domain exception that maps to HTTP responses."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


class NotFoundProblem(HttpProblem):
    """404 Not Found."""

    DEFAULT_STATUS = 404
    DEFAULT_CODE = "not_found"

    def __init__(
        self,
        *,
        message: str,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code if status_code is not None else self.DEFAULT_STATUS,
            code=code if code is not None else self.DEFAULT_CODE,
            message=message,
            details=details,
        )


class UnauthorizedProblem(HttpProblem):
    """401 Unauthorized."""

    DEFAULT_STATUS = 401
    DEFAULT_CODE = "unauthorized"

    def __init__(
        self,
        *,
        message: str,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code if status_code is not None else self.DEFAULT_STATUS,
            code=code if code is not None else self.DEFAULT_CODE,
            message=message,
            details=details,
        )


class ForbiddenProblem(HttpProblem):
    """403 Forbidden."""

    DEFAULT_STATUS = 403
    DEFAULT_CODE = "forbidden"

    def __init__(
        self,
        *,
        message: str,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code if status_code is not None else self.DEFAULT_STATUS,
            code=code if code is not None else self.DEFAULT_CODE,
            message=message,
            details=details,
        )


class ConflictProblem(HttpProblem):
    """409 Conflict."""

    DEFAULT_STATUS = 409
    DEFAULT_CODE = "conflict"

    def __init__(
        self,
        *,
        message: str,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code if status_code is not None else self.DEFAULT_STATUS,
            code=code if code is not None else self.DEFAULT_CODE,
            message=message,
            details=details,
        )


class TooManyRequestsProblem(HttpProblem):
    """429 Too Many Requests."""

    DEFAULT_STATUS = 429
    DEFAULT_CODE = "too_many_requests"

    def __init__(
        self,
        *,
        message: str,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code if status_code is not None else self.DEFAULT_STATUS,
            code=code if code is not None else self.DEFAULT_CODE,
            message=message,
            details=details,
        )


class DependencyUnavailableProblem(HttpProblem):
    """503 Service Unavailable."""

    DEFAULT_STATUS = 503
    DEFAULT_CODE = "dependency_unavailable"

    def __init__(
        self,
        *,
        message: str,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code if status_code is not None else self.DEFAULT_STATUS,
            code=code if code is not None else self.DEFAULT_CODE,
            message=message,
            details=details,
        )


def envelope_response(
    status: int, code: str, message: str, details: dict[str, Any] | None = None
) -> JSONResponse:
    """Build an error envelope response (public helper used by middleware)."""
    payload = ErrorEnvelope(error=ErrorPayload(code=code, message=message, details=details))
    return JSONResponse(status_code=status, content=payload.model_dump(mode="json"))


async def _handle_too_many_requests(request: Request, exc: Exception) -> JSONResponse:
    """Handle TooManyRequestsProblem with Retry-After header."""
    del request
    assert isinstance(exc, TooManyRequestsProblem)
    response = envelope_response(exc.status_code, exc.code, exc.message, exc.details)
    # Extract retry_after_seconds from details if present
    if exc.details:
        retry_after = exc.details.get("retry_after_seconds")
        if retry_after is not None:
            response.headers["Retry-After"] = str(retry_after)
    return response


async def _handle_http_problem(request: Request, exc: Exception) -> JSONResponse:
    """Handle HttpProblem exceptions."""
    del request
    assert isinstance(exc, HttpProblem)
    return envelope_response(exc.status_code, exc.code, exc.message, exc.details)


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Handle pydantic ValidationError."""
    del request
    assert isinstance(exc, ValidationError)
    return envelope_response(422, "validation_error", "validation error", {"errors": exc.errors()})


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    """Handle FastAPI HTTPException."""
    del request
    assert isinstance(exc, HTTPException)
    # Map status codes to domain codes
    status_to_code = {
        400: "invalid_input",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        422: "validation_error",
        429: "too_many_requests",
        503: "service_unavailable",
    }
    code = status_to_code.get(exc.status_code, "internal_error")
    # Preserve dict-shaped details (FastAPI validation errors); coerce string
    # detail into the message field.
    if isinstance(exc.detail, dict):
        return envelope_response(exc.status_code, code, code, exc.detail)
    return envelope_response(exc.status_code, code, str(exc.detail) if exc.detail else "")


def _make_dependency_unavailable_handler(
    message: str,
) -> Callable[[Request, Exception], Any]:
    async def handler(request: Request, exc: Exception) -> JSONResponse:
        del request, exc
        return envelope_response(503, "dependency_unavailable", message)

    return handler


_handle_migration_pending_error = _make_dependency_unavailable_handler("migrations pending")
_handle_master_key_error = _make_dependency_unavailable_handler("master key unavailable")
_handle_secret_integrity_error = _make_dependency_unavailable_handler(
    "secret integrity check failed"
)


async def _handle_generic_exception(request: Request, exc: Exception) -> JSONResponse:
    """Handle unhandled exceptions."""
    del request
    log = structlog.get_logger()
    log.error(
        "unhandled_exception",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return envelope_response(
        500,
        "internal_error",
        "internal server error",
    )


def register_error_handlers(app: FastAPI) -> None:
    """Register all exception handlers on the FastAPI app."""
    app.add_exception_handler(TooManyRequestsProblem, _handle_too_many_requests)
    app.add_exception_handler(HttpProblem, _handle_http_problem)
    app.add_exception_handler(ValidationError, _handle_validation_error)
    app.add_exception_handler(HTTPException, _handle_http_exception)
    app.add_exception_handler(MigrationsPendingError, _handle_migration_pending_error)
    app.add_exception_handler(MasterKeyError, _handle_master_key_error)
    app.add_exception_handler(SecretIntegrityError, _handle_secret_integrity_error)
    app.add_exception_handler(Exception, _handle_generic_exception)
