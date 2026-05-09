"""Request/response middleware for structured logging, request IDs, and auth resolution.

These are pure ASGI callables (not Starlette ``BaseHTTPMiddleware``), because
``BaseHTTPMiddleware`` buffers streaming response bodies and breaks SSE
(``/api/events``). See STAGE-001-014 for context.
"""

from __future__ import annotations

import time
import uuid

import structlog
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from homelab_monitor.kernel.auth.api_tokens import hash_token
from homelab_monitor.kernel.auth.repository import AuthRepository
from homelab_monitor.kernel.auth.sessions import verify_session_cookie_value

# Module-level logger for AccessLogMiddleware to avoid re-creation on each request.
_access_log = structlog.get_logger("homelab_monitor.kernel.api.access_log")

# Paths that NEVER require auth resolution to populate request.state.user
# (the resolver still runs but produces auth_kind="unauthenticated").
# Enforcement decisions happen in route-level Depends, not here.
AUTH_EXEMPT_PATHS = frozenset(
    {
        "/api/healthz",
        "/api/version",
        "/api/openapi.json",
        "/api/docs",
        "/api/redoc",
        "/api/docs/oauth2-redirect",
        "/api/auth/login",
        # NOT exempt: /api/auth/logout. The route is idempotent (no-op when no
        # session) but it MUST run AuthMiddleware so request.state.session is
        # populated when a valid cookie is present, so the row can be deleted.
    }
)

SESSION_COOKIE_NAME = "homelab_monitor_session"


class RequestIdMiddleware:
    """Generate or validate X-Request-Id header and bind to structlog context.

    Pure-ASGI middleware (not ``BaseHTTPMiddleware``) so streaming responses
    pass through unbuffered. See module docstring.

    Reads X-Request-Id if present (accepts both 32-char hex and 36-char
    canonical UUID ``8-4-4-4-12``), normalizes to 32-char hex internally and
    in responses. If absent or unparseable, generates a fresh uuid4 hex.

    Binds request_id to ``structlog.contextvars`` for the duration of the
    request, and resets the binding after the response completes (no
    cross-request leakage). Sets X-Request-Id response header on the
    ``http.response.start`` message via a wrapped ``send``.

    Why 32-char hex in responses: simpler grep on log lines; clients that
    sent canonical UUIDs see them echoed in 32-char form (acceptable per
    RFC 4122 — the UUID value is the same).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Read or generate request_id. Accept both 32-char hex (uuid4().hex)
        # and 36-char canonical UUID (8-4-4-4-12). Normalize to hex32.
        # Headers built via scope= mutates scope["headers"] to a list, which
        # downstream code is allowed to assume.
        request = Request(scope)
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

        # Store on scope state so downstream Request.state.request_id resolves.
        # Starlette's Request.state property is a State proxy backed by
        # scope["state"], so writing through the dict here is equivalent to
        # request.state.request_id = ... (verified in starlette/requests.py).
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id

        # Bind for the duration of this request only — reset afterward to
        # prevent leakage between requests sharing a task context.
        tokens = structlog.contextvars.bind_contextvars(request_id=request_id)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                # Set X-Request-Id response header on the start message.
                # MutableHeaders(scope=message) mutates message["headers"] to
                # a list and lets us set the header in place before we
                # forward the message.
                headers = MutableHeaders(scope=message)
                headers["X-Request-Id"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            structlog.contextvars.reset_contextvars(**tokens)


class AccessLogMiddleware:
    """Log a structured JSON entry per request: method, path, status, duration, auth.

    Pure-ASGI middleware (not ``BaseHTTPMiddleware``). Captures the response
    status from the ``http.response.start`` message via a wrapped ``send``,
    and emits the log on response completion (or on exception, with
    ``status=0`` and ``exception=True``).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        start = time.perf_counter()
        status_holder: dict[str, int] = {"status": 0}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                # ASGI guarantees status is an int on http.response.start.
                status_holder["status"] = int(message.get("status", 0))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            _access_log.warning(
                "http.request",
                method=request.method,
                path=request.url.path,
                status=0,
                had_exception=True,
                duration_ms=duration_ms,
                query_count=len(request.query_params),
                auth=_auth_log_field(request),
            )
            raise

        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        _access_log.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status=status_holder["status"],
            duration_ms=duration_ms,
            query_count=len(request.query_params),
            auth=_auth_log_field(request),
        )


def _auth_log_field(request: Request) -> str:
    """Build the access-log auth field from request.state.

    Returns:
        - "session(user_id=N)" when AuthMiddleware resolved a session cookie
        - "token:<name>" when an API token was used
        - "unauthenticated" otherwise (including exempt paths)

    The getattr fallbacks are defensive: AccessLogMiddleware sits OUTSIDE
    AuthMiddleware in the stack, so on requests that error out before
    AuthMiddleware runs (e.g., ASGI-level bug), state attributes may be
    absent. The "?" sentinel surfaces that condition in logs without
    crashing the access-log writer.
    """
    kind = getattr(request.state, "auth_kind", "unauthenticated")
    if kind == "session":
        uid = getattr(request.state, "user_id", "?")
        return f"session(user_id={uid})"
    if kind == "token":
        name = getattr(request.state, "token_name", "?")
        return f"token:{name}"
    return "unauthenticated"


class AuthMiddleware:
    """Resolve session cookie OR API token; populate ``request.state``.

    Pure-ASGI middleware (not ``BaseHTTPMiddleware``). Writes through
    ``scope["state"]`` so downstream FastAPI dependencies that access
    ``request.state.*`` see the resolved values.

    NEVER enforces. Route-level ``Depends(require_*)`` handles enforcement.

    Sets:
        - request.state.user (User | None)
        - request.state.user_id (int | None)
        - request.state.session (Session | None)
        - request.state.token (ApiToken | None)
        - request.state.token_name (str | None)
        - request.state.auth_kind ("session" | "token" | "unauthenticated")
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Initialize state defaults so dependencies can rely on attribute
        # presence. RequestIdMiddleware (the innermost wrapper, runs first)
        # already created scope["state"], but use setdefault for safety.
        state = scope.setdefault("state", {})
        state["user"] = None
        state["user_id"] = None
        state["session"] = None
        state["token"] = None
        state["token_name"] = None
        state["auth_kind"] = "unauthenticated"

        request = Request(scope)

        # On exempt paths, skip the (cheap) DB lookups entirely.
        if request.url.path in AUTH_EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        # Auth precedence (locked design D2):
        #   1. Authorization: Bearer <token>  → API token (CSRF-IMMUNE)
        #   2. Else, homelab_monitor_session cookie → session (CSRF ENFORCED on
        #      state-changing methods inside require_session()/require_user_or_token())
        #   3. Else, unauthenticated (route's Depends(require_*) decides 401).
        # If a request sends BOTH a Bearer header AND a session cookie, the
        # token wins and the session is ignored. This is deliberate: a
        # programmatic caller (Alertmanager, cron) explicitly opting into
        # token auth must not be downgraded to a CSRF-protected flow that
        # demands an X-CSRF-Token header it cannot easily produce.
        # If you add a new auth scheme, evaluate it BEFORE the cookie branch
        # and document the precedence here.
        auth_header = request.headers.get("authorization", "")
        bearer_token: str | None = None
        if auth_header.lower().startswith("bearer "):
            candidate = auth_header[len("bearer ") :].strip()
            if candidate:
                bearer_token = candidate
        if bearer_token is not None:
            await self._resolve_token(request, bearer_token)
        elif (cookie_val := request.cookies.get(SESSION_COOKIE_NAME)) is not None:
            await self._resolve_session(request, cookie_val)

        await self.app(scope, receive, send)

    @staticmethod
    async def _resolve_token(request: Request, plaintext: str) -> None:
        # Reject empty plaintext early — `Authorization: Bearer ` (trailing space)
        # otherwise hits the DB with hash_token(""). Constant-time short-circuit.
        if not plaintext:
            return
        auth_repo = getattr(request.app.state, "auth_repo", None)
        if auth_repo is None or not isinstance(auth_repo, AuthRepository):
            return
        sha = hash_token(plaintext)
        token = await auth_repo.get_api_token_by_hash(sha)
        if token is None:
            return
        request.state.token = token
        request.state.token_name = token.name
        request.state.auth_kind = "token"
        # Best-effort last_used update; do not fail the request on a write error
        try:
            from homelab_monitor.kernel.db.time import utc_now_iso  # noqa: PLC0415

            await auth_repo.update_token_last_used(token.id, utc_now_iso())
        except Exception:  # pragma: no cover -- defensive
            pass

    @staticmethod
    async def _resolve_session(request: Request, cookie_val: str) -> None:
        master_key = getattr(request.app.state, "master_key", None)
        if master_key is None:
            return
        auth_repo = getattr(request.app.state, "auth_repo", None)
        if auth_repo is None or not isinstance(auth_repo, AuthRepository):
            return
        session_id = verify_session_cookie_value(cookie_val, master_key)
        if session_id is None:
            return
        session = await auth_repo.get_session(session_id)
        if session is None:
            return
        if AuthRepository.is_session_expired(session.expires_at):
            return
        user = await auth_repo.get_user_by_id(session.user_id)
        if user is None:
            return
        request.state.user = user
        request.state.user_id = user.id
        request.state.session = session
        request.state.auth_kind = "session"


class CspHeadersMiddleware:
    """Inject ``Content-Security-Policy: frame-ancestors 'self'`` on all responses.

    Pure-ASGI middleware (not ``BaseHTTPMiddleware``). The header is the
    modern replacement for ``X-Frame-Options: SAMEORIGIN`` and is required so
    the embedded Karma iframe at ``/alerts`` is permitted to nest under our
    own origin while denying every other origin.

    Why every response (not just HTML): keeping the policy uniform avoids a
    classification step on response content-type and the header is harmless
    on JSON / streaming responses. If a future caller embeds the monitor's
    JSON endpoints from another origin, this header is silently ignored
    (CSP is enforced only on document-loading contexts).

    If a route handler sets its own Content-Security-Policy header,
    this middleware preserves it. To add directives globally, modify
    CSP_VALUE here.
    """

    CSP_VALUE = "frame-ancestors 'self'"

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                # Use append-only semantics: don't clobber a per-route CSP
                # if a future endpoint tightens its own policy. Header name
                # comparison is case-insensitive per RFC 7230.
                if "content-security-policy" not in headers:
                    headers["Content-Security-Policy"] = self.CSP_VALUE
            await send(message)

        await self.app(scope, receive, send_wrapper)
