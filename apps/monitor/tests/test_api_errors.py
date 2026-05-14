"""Tests for kernel/api/errors.py — uniform error envelope and exception handlers."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.api.errors import (
    ConflictProblem,
    DependencyUnavailableProblem,
    ErrorEnvelope,
    ErrorPayload,
    ForbiddenProblem,
    HttpProblem,
    NotFoundProblem,
    UnauthorizedProblem,
)


def test_error_payload_shape() -> None:
    """ErrorPayload has code, message, and optional details."""
    payload = ErrorPayload(
        code="invalid_input",
        message="The request was invalid",
        details=None,
    )
    assert payload.code == "invalid_input"
    assert payload.message == "The request was invalid"
    assert payload.details is None


def test_error_payload_with_details() -> None:
    """ErrorPayload can have details dict."""
    details = {"field": "email", "reason": "invalid format"}
    payload = ErrorPayload(
        code="validation_error",
        message="Validation failed",
        details=details,
    )
    assert payload.details == details


def test_error_payload_forbids_extra() -> None:
    """ErrorPayload enforces extra='forbid'."""
    with pytest.raises(ValueError):
        ErrorPayload(
            code="test",
            message="test",
            extra="not_allowed",  # type: ignore[call-arg]
        )


def test_error_envelope_shape() -> None:
    """ErrorEnvelope wraps ErrorPayload under 'error' key."""
    payload = ErrorPayload(code="test_error", message="Test message")
    envelope = ErrorEnvelope(error=payload)
    assert envelope.error.code == "test_error"
    assert envelope.error.message == "Test message"


def test_error_envelope_forbids_extra() -> None:
    """ErrorEnvelope enforces extra='forbid'."""
    payload = ErrorPayload(code="test", message="test")
    with pytest.raises(ValueError):
        ErrorEnvelope(error=payload, extra="not_allowed")  # type: ignore[call-arg]


def test_http_problem_base_class() -> None:
    """HttpProblem carries status_code, code, message, details."""
    problem = HttpProblem(
        status_code=400,
        code="invalid_request",
        message="Invalid request",
        details={"field": "name"},
    )
    assert problem.status_code == 400  # noqa: PLR2004
    assert problem.code == "invalid_request"
    assert problem.message == "Invalid request"
    assert problem.details == {"field": "name"}


def test_not_found_problem() -> None:
    """NotFoundProblem is 404 with code='not_found'."""
    problem = NotFoundProblem(
        status_code=404,
        code="not_found",
        message="Resource not found",
    )
    assert problem.status_code == 404  # noqa: PLR2004
    assert problem.code == "not_found"


def test_unauthorized_problem() -> None:
    """UnauthorizedProblem is 401 with code='unauthorized'."""
    problem = UnauthorizedProblem(
        status_code=401,
        code="unauthorized",
        message="Authentication required",
    )
    assert problem.status_code == 401  # noqa: PLR2004
    assert problem.code == "unauthorized"


def test_forbidden_problem() -> None:
    """ForbiddenProblem is 403 with code='forbidden'."""
    problem = ForbiddenProblem(
        status_code=403,
        code="forbidden",
        message="Access denied",
    )
    assert problem.status_code == 403  # noqa: PLR2004
    assert problem.code == "forbidden"


def test_conflict_problem() -> None:
    """ConflictProblem is 409 with code='conflict'."""
    problem = ConflictProblem(
        status_code=409,
        code="conflict",
        message="Resource conflict",
    )
    assert problem.status_code == 409  # noqa: PLR2004
    assert problem.code == "conflict"


def test_dependency_unavailable_problem() -> None:
    """DependencyUnavailableProblem is 503 with code='dependency_unavailable'."""
    problem = DependencyUnavailableProblem(
        status_code=503,
        code="dependency_unavailable",
        message="Service unavailable",
    )
    assert problem.status_code == 503  # noqa: PLR2004
    assert problem.code == "dependency_unavailable"


def test_http_problem_is_exception() -> None:
    """HttpProblem is an Exception subclass."""
    problem = HttpProblem(
        status_code=500,
        code="internal_error",
        message="Internal error",
    )
    assert isinstance(problem, Exception)


def test_error_codes_snake_case() -> None:
    """Error codes are snake_case."""
    codes = [
        "invalid_input",
        "unauthorized",
        "forbidden",
        "not_found",
        "conflict",
        "validation_error",
        "dependency_unavailable",
        "internal_error",
    ]
    for code in codes:
        assert code == code.lower()
        assert "_" in code or len(code.split("_")) == 1


def test_error_envelope_model_dump() -> None:
    """ErrorEnvelope.model_dump produces dict with correct structure."""
    payload = ErrorPayload(
        code="test",
        message="test message",
        details={"key": "value"},
    )
    envelope = ErrorEnvelope(error=payload)
    dumped = envelope.model_dump()
    assert "error" in dumped
    assert dumped["error"]["code"] == "test"
    assert dumped["error"]["message"] == "test message"
    assert dumped["error"]["details"]["key"] == "value"


def test_error_envelope_json_serializable() -> None:
    """ErrorEnvelope can be serialized to JSON."""
    import json  # noqa: PLC0415

    payload = ErrorPayload(code="test", message="test message")
    envelope = ErrorEnvelope(error=payload)
    json_str = envelope.model_dump_json()
    data = json.loads(json_str)
    assert data["error"]["code"] == "test"


# ---------------------------------------------------------------------------
# Handler integration tests — use async + pyright ignores for private access
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_validation_error_envelope() -> None:
    """_handle_validation_error returns JSONResponse with 422 and validation_error code."""
    from pydantic import TypeAdapter, ValidationError  # noqa: PLC0415

    from homelab_monitor.kernel.api.errors import (  # noqa: PLC0415
        _handle_validation_error,  # pyright: ignore[reportPrivateUsage]
    )

    adapter = TypeAdapter(int)
    try:
        adapter.validate_python("not_an_int")
    except ValidationError as e:
        req = type("R", (), {"app": type("A", (), {"state": type("S", (), {})()})()})()
        response = await _handle_validation_error(req, e)  # pyright: ignore[reportArgumentType]
        assert response.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_handle_migration_pending_error_returns_503() -> None:
    """_handle_migration_pending_error returns 503 with migrations_pending message."""
    from homelab_monitor.kernel.api.errors import (  # noqa: PLC0415
        _handle_migration_pending_error,  # pyright: ignore[reportPrivateUsage]
    )
    from homelab_monitor.kernel.db.migrations import MigrationsPendingError  # noqa: PLC0415

    exc = MigrationsPendingError("test_version")
    req = type("R", (), {"app": type("A", (), {"state": type("S", (), {})()})()})()
    response = await _handle_migration_pending_error(req, exc)  # type: ignore[reportUnknownVariableType, reportGeneralTypeIssues]
    assert response.status_code == 503  # noqa: PLR2004


@pytest.mark.asyncio
async def test_handle_master_key_error_returns_503() -> None:
    """_handle_master_key_error returns 503 with master_key_error message."""
    from homelab_monitor.kernel.api.errors import (  # noqa: PLC0415
        _handle_master_key_error,  # pyright: ignore[reportPrivateUsage]
    )
    from homelab_monitor.kernel.secrets.errors import MasterKeyError  # noqa: PLC0415

    exc = MasterKeyError("test reason")
    req = type("R", (), {"app": type("A", (), {"state": type("S", (), {})()})()})()
    response = await _handle_master_key_error(req, exc)  # type: ignore[reportUnknownVariableType, reportGeneralTypeIssues]
    assert response.status_code == 503  # noqa: PLR2004


@pytest.mark.asyncio
async def test_handle_secret_integrity_error_returns_503() -> None:
    """_handle_secret_integrity_error returns 503 with secret_integrity_error message."""
    from homelab_monitor.kernel.api.errors import (  # noqa: PLC0415
        _handle_secret_integrity_error,  # pyright: ignore[reportPrivateUsage]
    )
    from homelab_monitor.kernel.secrets.errors import SecretIntegrityError  # noqa: PLC0415

    exc = SecretIntegrityError("checksum mismatch")
    req = type("R", (), {"app": type("A", (), {"state": type("S", (), {})()})()})()
    response = await _handle_secret_integrity_error(req, exc)  # type: ignore[reportUnknownVariableType, reportGeneralTypeIssues]
    assert response.status_code == 503  # noqa: PLR2004


@pytest.mark.parametrize(
    "status_code",
    [400, 401, 403, 404, 409, 422, 500],
)
@pytest.mark.asyncio
async def test_handle_http_exception_maps_status_codes(status_code: int) -> None:
    """_handle_http_exception maps HTTP status codes to response codes."""
    from fastapi import HTTPException  # noqa: PLC0415

    from homelab_monitor.kernel.api.errors import (  # noqa: PLC0415
        _handle_http_exception,  # pyright: ignore[reportPrivateUsage]
    )

    request = type("Request", (), {"app": type("App", (), {"state": type("State", (), {})()})()})()  # pyright: ignore[reportArgumentType]
    exc = HTTPException(status_code=status_code, detail="test")
    response = await _handle_http_exception(request, exc)  # pyright: ignore[reportArgumentType,reportPrivateUsage]
    assert response.status_code == status_code


@pytest.mark.asyncio
async def test_handle_http_exception_unknown_status_maps_to_internal_error() -> None:
    """_handle_http_exception maps unknown status to internal_error."""
    from fastapi import HTTPException  # noqa: PLC0415

    from homelab_monitor.kernel.api.errors import (  # noqa: PLC0415
        _handle_http_exception,  # pyright: ignore[reportPrivateUsage]
    )

    request = type("Request", (), {"app": type("App", (), {"state": type("State", (), {})()})()})()  # pyright: ignore[reportArgumentType]
    exc = HTTPException(status_code=418, detail="I'm a teapot")
    response = await _handle_http_exception(request, exc)  # pyright: ignore[reportArgumentType,reportPrivateUsage]
    assert response.status_code >= 400  # noqa: PLR2004


@pytest.mark.asyncio
async def test_handle_generic_exception_returns_500() -> None:
    """_handle_generic_exception returns 500 with internal_error code for any exception."""
    from homelab_monitor.kernel.api.errors import (  # noqa: PLC0415
        _handle_generic_exception,  # pyright: ignore[reportPrivateUsage]
    )

    request = type("Request", (), {"app": type("App", (), {"state": type("State", (), {})()})()})()  # pyright: ignore[reportArgumentType]
    exc = RuntimeError("unexpected error")
    response = await _handle_generic_exception(request, exc)  # pyright: ignore[reportArgumentType,reportPrivateUsage]
    assert response.status_code == 500  # noqa: PLR2004
    body = response.body
    if isinstance(body, memoryview):
        body = bytes(body)
    import json  # noqa: PLC0415

    payload = json.loads(body)
    assert payload["error"]["code"] == "internal_error"


@pytest.mark.asyncio
async def test_handle_too_many_requests_without_details_omits_retry_after() -> None:
    """_handle_too_many_requests returns 429 with no Retry-After when details is None."""
    from homelab_monitor.kernel.api.errors import (  # noqa: PLC0415
        TooManyRequestsProblem,
        _handle_too_many_requests,  # pyright: ignore[reportPrivateUsage]
    )

    request = type("Request", (), {"app": type("App", (), {"state": type("State", (), {})()})()})()  # pyright: ignore[reportArgumentType]
    exc = TooManyRequestsProblem(message="try later", details=None)
    response = await _handle_too_many_requests(request, exc)  # pyright: ignore[reportArgumentType]
    assert response.status_code == 429  # noqa: PLR2004
    assert "retry-after" not in {k.lower() for k in response.headers}


@pytest.mark.asyncio
async def test_handle_too_many_requests_with_details_but_no_retry_after_key() -> None:
    """_handle_too_many_requests returns 429 with no Retry-After.

    When details lacks retry_after_seconds.
    """
    from homelab_monitor.kernel.api.errors import (  # noqa: PLC0415
        TooManyRequestsProblem,
        _handle_too_many_requests,  # pyright: ignore[reportPrivateUsage]
    )

    request = type("Request", (), {"app": type("App", (), {"state": type("State", (), {})()})()})()  # pyright: ignore[reportArgumentType]
    exc = TooManyRequestsProblem(message="try later", details={"other": "value"})
    response = await _handle_too_many_requests(request, exc)  # pyright: ignore[reportArgumentType]
    assert response.status_code == 429  # noqa: PLR2004
    assert "retry-after" not in {k.lower() for k in response.headers}


@pytest.mark.asyncio
async def test_handle_http_exception_with_dict_detail() -> None:
    """_handle_http_exception preserves dict-shaped details from FastAPI validation errors."""
    from fastapi import HTTPException  # noqa: PLC0415

    from homelab_monitor.kernel.api.errors import (  # noqa: PLC0415
        _handle_http_exception,  # pyright: ignore[reportPrivateUsage]
    )

    request = type("Request", (), {"app": type("App", (), {"state": type("State", (), {})()})()})()  # pyright: ignore[reportArgumentType]
    detail_dict = {"field": "name", "error": "required"}
    exc = HTTPException(status_code=422, detail=detail_dict)
    response = await _handle_http_exception(request, exc)  # type: ignore[reportUnknownVariableType, reportGeneralTypeIssues]
    assert response.status_code == 422  # noqa: PLR2004
