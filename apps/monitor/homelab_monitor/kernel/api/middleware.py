"""Request/response middleware for structured logging, request IDs, and dev auth."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from homelab_monitor.kernel.api.errors import envelope_response

_AUTH_EXEMPT_PATHS = {
    "/api/healthz",
    "/api/version",
    "/api/openapi.json",
    "/api/docs",
    "/api/redoc",
    "/api/docs/oauth2-redirect",
}


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Generate or validate X-Request-Id header and bind to structlog context.

    Reads X-Request-Id if present and valid (hex32), else generates fresh uuid.
    Binds request_id to structlog.contextvars for the duration of the request,
    and unbinds it after the response completes (no cross-request leakage).
    Sets X-Request-Id response header.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[..., Awaitable[Response]]
    ) -> Response:
        # Read or generate request_id. Accept both 32-char hex (uuid4().hex)
        # and 36-char canonical UUID (8-4-4-4-12). Normalize to hex32.
        raw = request.headers.get("X-Request-Id", "").strip().lower()
        request_id: str | None = None
        if raw:
            try:
                # uuid.UUID accepts both 32-char and 36-char forms.
                request_id = uuid.UUID(raw).hex
            except ValueError:
                request_id = None
        if request_id is None:
            request_id = uuid.uuid4().hex

        # Store on request for downstream access
        request.state.request_id = request_id

        # Bind for the duration of this request only — unbind afterward to
        # prevent leakage between requests sharing a task context.
        tokens = structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.reset_contextvars(**tokens)

        # Set response header
        response.headers["X-Request-Id"] = request_id
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Log structured JSON entry for each request (method, path, status, duration)."""

    async def dispatch(
        self, request: Request, call_next: Callable[..., Awaitable[Response]]
    ) -> Response:
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            log = structlog.get_logger()
            # Status is unknown here; the exception handler will emit the actual
            # response. Use status=0 to signal "exception escaped".
            log.warning(
                "http.request",
                method=request.method,
                path=request.url.path,
                status=0,
                exception=True,
                duration_ms=duration_ms,
                query_count=len(request.query_params),
            )
            raise
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        log = structlog.get_logger()
        log.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
            query_count=len(request.query_params),
        )
        return response


class DevAuthMiddleware(BaseHTTPMiddleware):
    """Gate requests to protected endpoints with X-Auth: dev header (dev placeholder).

    Checks HOMELAB_MONITOR_DEV_AUTH env var. If unset, returns 401 for protected
    endpoints. If set to "1", requires X-Auth: dev header (case-insensitive).
    Certain paths are auth-exempt (healthz, version, docs).
    """

    async def dispatch(
        self, request: Request, call_next: Callable[..., Awaitable[Response]]
    ) -> Response:
        # Exempt paths never require auth
        if request.url.path in _AUTH_EXEMPT_PATHS:
            return await call_next(request)

        # Check if auth is enabled
        if os.environ.get("HOMELAB_MONITOR_DEV_AUTH") != "1":
            return envelope_response(401, "unauthorized", "auth not enabled in this build")

        # Check X-Auth header
        auth_header = request.headers.get("X-Auth", "").lower()
        if auth_header != "dev":
            return envelope_response(401, "unauthorized", "missing or invalid X-Auth header")

        return await call_next(request)
