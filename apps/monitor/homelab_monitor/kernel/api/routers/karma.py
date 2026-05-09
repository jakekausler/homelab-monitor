"""Karma alert-console proxy.

Proxies every request under ``/api/karma/`` to the upstream Karma sidecar
container at ``HOMELAB_MONITOR_KARMA_URL`` (default ``http://karma:8081``).
The route is mounted in the monitor's same-origin so the sandboxed iframe
at ``/alerts`` can call Karma's REST endpoints without crossing origins.

Auth: cookie session via ``require_session_no_csrf()`` — the iframe cannot
attach the X-CSRF-Token header (it lives on a same-origin sandboxed
document with ``allow-same-origin`` but no access to our top-frame
JS context that mints CSRF requests). To compensate, every state-changing
method (POST/PUT/PATCH/DELETE) MUST present an Origin or Referer header
that matches the request's own scheme://host.

Token auth path: NOT exposed. Karma is interactive-UI-only; if a caller
needs programmatic AM access, they should hit Alertmanager's API directly
(internal network) or, in the future, a dedicated AM-token endpoint.

Header allow-listing: only a known-safe subset of request headers is
forwarded upstream (no Cookie, no Authorization — those are ours, not
Karma's), and only a known-safe subset of response headers is relayed
back (no Set-Cookie, no X-Frame-Options).
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator

import httpx
import structlog
from fastapi import APIRouter, Depends, Request
from starlette.responses import StreamingResponse
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.api.dependencies import (
    get_http_client,
    require_session_no_csrf,
)
from homelab_monitor.kernel.api.errors import HttpProblem
from homelab_monitor.kernel.auth.models import User

router = APIRouter()


def _karma_timeout_s() -> float:
    """Read upstream timeout from env (HOMELAB_MONITOR_KARMA_TIMEOUT_S, default 30s)."""
    raw = os.environ.get("HOMELAB_MONITOR_KARMA_TIMEOUT_S", "30")
    try:
        return float(raw)
    except ValueError:
        return 30.0


_STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Path validation regex.
#
# Allowed (per segment, post-FastAPI capture but pre-decode):
#   - alphanumeric: [A-Za-z0-9]
#   - common URL-safe: . _ - ~ +
#   - percent-encoding: %  (Karma may emit %xx in static asset URLs)
#   - path separator: /
#
# Rejected (defense-in-depth, even though FastAPI's path matcher already
# strips leading /):
#   - `..` anywhere → traversal attempt
#   - `\x00` → null-byte injection
#   - any other character (e.g., `?`, `#`, space) → suspicious
#
# Querystring is not seen here (FastAPI captures it via request.url.query),
# so `?`, `=`, `&` characters never reach this validator.
_PATH_RE = re.compile(r"^[A-Za-z0-9._/~+\-%]*$")

# Headers we forward upstream. Cookie and Authorization are deliberately
# absent: those are ours (the monitor's session/Bearer scheme), not Karma's.
# Karma authenticates via its own `listen.prefix` + AM config.
_ALLOWED_REQUEST_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "accept-language",
        "cache-control",
        "content-type",
        "content-length",
        "if-modified-since",
        "if-none-match",
        "user-agent",
    }
)

# Headers we relay back to the client. Set-Cookie / X-Frame-Options /
# Strict-Transport-Security are NEVER relayed (Karma doesn't normally set
# them, but defensive — they could leak upstream config or override ours).
_ALLOWED_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "content-length",
        "content-encoding",
        "cache-control",
        "etag",
        "last-modified",
        "vary",
    }
)


def _karma_url() -> str:
    """Return the Karma base URL from env, defaulting to compose hostname."""
    return os.environ.get("HOMELAB_MONITOR_KARMA_URL", "http://karma:8081")


def _validate_path(path: str) -> None:
    """Reject path traversal and disallowed characters. Raises HttpProblem on failure."""
    # FastAPI's {path:path} captures the URL path AFTER `/api/karma/`. Empty
    # is OK (root request → /api/karma/). Reject any `..` segment, control
    # chars, and characters outside the safe set.
    if "\x00" in path:
        raise HttpProblem(
            status_code=400,
            code="invalid_path",
            message="path contains null byte",
        )
    # Defense in depth: reject any literal ".." segment regardless of where
    # it sits, even though _PATH_RE allows dots.
    for seg in path.split("/"):
        if seg == "..":
            raise HttpProblem(
                status_code=400,
                code="invalid_path",
                message="path traversal not permitted",
            )
    if not _PATH_RE.match(path):
        raise HttpProblem(
            status_code=400,
            code="invalid_path",
            message="path contains disallowed characters",
        )


def _verify_origin(request: Request) -> None:
    """Same-origin check for state-changing methods on the karma proxy.

    Threat model:
        Browsers cannot let scripted JS forge the ``Origin`` header on
        cross-origin requests, so a same-origin assertion gives meaningful
        protection against CSRF in the standard browser-attack model.

        However, this check derives the "expected" origin from the request
        itself (via ``Host`` + ``X-Forwarded-Proto``, with fallbacks to
        ``request.url.{netloc, scheme}``). That means the check IS NOT a
        defense against:
        - A reverse-proxy misconfiguration that forwards a client-supplied
          ``X-Forwarded-Proto`` or ``X-Forwarded-Host`` header verbatim.
        - A man-in-the-middle attacker who can rewrite the ``Host`` header
          on incoming requests.
        - Direct (no-proxy) access where a malicious client controls the
          ``Host`` and ``X-Forwarded-Proto`` headers it sends.

        Operators MUST configure their reverse proxy to set
        ``X-Forwarded-Proto`` from server-side state (NOT from a client
        header) and to either set or strip ``X-Forwarded-Host``. nginx's
        default ``proxy_set_header X-Forwarded-Proto $scheme;`` is correct;
        Caddy's automatic forwarding is also correct.

    Reverse-proxy aware behavior:
        ``X-Forwarded-Proto`` is only honored when the env var
        ``HOMELAB_MONITOR_TRUST_FORWARDED_HEADERS=1`` is set. Operators
        running behind a trusted reverse proxy (the supported deployment)
        set this; direct-access deployments leave it unset and fall back to
        ``request.url.scheme`` (which the ASGI server derives from the
        actual TCP socket). The ``Host`` header is always read because
        HTTP/1.1 requires it; the ASGI server's ``request.url.netloc``
        derives from it anyway.
    """
    if request.method.upper() not in _STATE_CHANGING:
        return
    trust_forwarded = os.environ.get("HOMELAB_MONITOR_TRUST_FORWARDED_HEADERS", "0") == "1"
    host = request.headers.get("host", "").strip() or request.url.netloc
    if trust_forwarded:
        scheme = request.headers.get("x-forwarded-proto", "").strip() or request.url.scheme
    else:
        scheme = request.url.scheme
    expected = f"{scheme}://{host}"
    origin = request.headers.get("origin", "").strip()
    if origin:
        if origin != expected:
            raise HttpProblem(
                status_code=403,
                code="cross_origin_blocked",
                message="origin does not match request origin",
            )
        return
    referer = request.headers.get("referer", "").strip()
    if not referer:
        raise HttpProblem(
            status_code=403,
            code="cross_origin_blocked",
            message="missing Origin and Referer headers on state-changing request",
        )
    if not (referer == expected or referer.startswith(expected + "/")):
        raise HttpProblem(
            status_code=403,
            code="cross_origin_blocked",
            message="referer does not match request origin",
        )


def _filter_request_headers(request: Request) -> dict[str, str]:
    """Build the upstream-bound header dict from the allow-list."""
    out: dict[str, str] = {}
    for name, value in request.headers.items():
        if name.lower() in _ALLOWED_REQUEST_HEADERS:
            out[name] = value
    # Host header is intentionally NOT in the allow-list; httpx sets
    # Host based on the upstream URL passed to build_request().
    return out


def _filter_response_headers(upstream: httpx.Response) -> dict[str, str]:
    """Build the client-bound header dict from the allow-list."""
    out: dict[str, str] = {}
    for name, value in upstream.headers.items():
        if name.lower() in _ALLOWED_RESPONSE_HEADERS:
            out[name] = value
    return out


async def _aiter_with_close(
    upstream: httpx.Response,
) -> AsyncIterator[bytes]:
    """Yield raw chunks; close the upstream response when iteration ends."""
    try:
        async for chunk in upstream.aiter_raw():
            yield chunk
    finally:
        await upstream.aclose()


@router.api_route(
    "/karma/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def karma_proxy(
    path: str,
    request: Request,
    _user: User = Depends(require_session_no_csrf()),  # noqa: B008
    http_client: httpx.AsyncClient = Depends(get_http_client),  # noqa: B008
) -> StreamingResponse:
    """Forward an arbitrary request to the upstream Karma container.

    Auth: cookie session required (CSRF-EXEMPT — see module docstring).
    Origin/Referer match required on POST/PUT/PATCH/DELETE.
    """
    log: BoundLogger = structlog.get_logger().bind(component="karma_proxy")  # pyright: ignore[reportAssignmentType]

    _validate_path(path)
    _verify_origin(request)

    # Build the upstream URL. Karma's `listen.prefix: /api/karma/` means it
    # ALSO expects requests under `/api/karma/...`. So we forward the FULL
    # incoming path (`/api/karma/<rest>`), not just the captured suffix.
    upstream_base = _karma_url().rstrip("/")
    upstream_url = f"{upstream_base}/api/karma/{path}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    headers = _filter_request_headers(request)
    # TODO(STAGE-021+): For Karma silence-creation payloads (~200 bytes) this
    # is fine; if a future use case proxies large uploads, switch to
    # `content=request.stream()` to avoid buffering. Sized cap is currently
    # implicit (httpx's default + ASGI server's max-request-size).
    body = await request.body()

    upstream_req = http_client.build_request(
        method=request.method,
        url=upstream_url,
        headers=headers,
        content=body,
        timeout=_karma_timeout_s(),
    )
    try:
        upstream: httpx.Response = await http_client.send(
            upstream_req,
            stream=True,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning("karma_proxy.upstream_error", error=str(exc), path=path)
        raise HttpProblem(
            status_code=502,
            code="karma_unavailable",
            message="karma upstream unreachable",
        ) from exc

    relayed_headers = _filter_response_headers(upstream)
    return StreamingResponse(
        _aiter_with_close(upstream),
        status_code=upstream.status_code,
        headers=relayed_headers,
    )
