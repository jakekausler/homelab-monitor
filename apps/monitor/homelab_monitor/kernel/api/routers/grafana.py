"""Grafana metrics-dashboard proxy.

Proxies every request under ``/api/grafana/`` to the upstream Grafana
sidecar container at ``HOMELAB_MONITOR_GRAFANA_URL`` (default
``http://grafana:3000``). The route is mounted in the monitor's same-origin
so the sandboxed iframe at ``/metrics`` can call Grafana's REST endpoints
(datasources, dashboards, query execution) without crossing origins.

Auth: cookie session via ``require_session_no_csrf()`` — the iframe cannot
attach the X-CSRF-Token header (it lives on a same-origin sandboxed
document with ``allow-same-origin`` but no access to our top-frame
JS context that mints CSRF requests). To compensate, every state-changing
method (POST/PUT/PATCH/DELETE) MUST present an Origin or Referer header
that matches the request's own scheme://host. Grafana POSTs ``/api/ds/query``
on every dashboard render, so this is exercised on the read path.

Token auth path: NOT exposed. Grafana is interactive-UI-only here.

Header allow-listing: only a known-safe subset of request headers is
forwarded upstream (no Cookie, no Authorization — those are ours, not
Grafana's), and only a known-safe subset of response headers is relayed
back. Crucially, ``X-Frame-Options`` is NOT in the response allow-list,
so Grafana's default ``X-Frame-Options: DENY`` is silently dropped on the
return path. The CSP ``frame-ancestors 'self'`` injected globally by
``CspHeadersMiddleware`` is the modern same-origin-frame gate.

Sub-path routing: Grafana is configured with ``GF_SERVER_SERVE_FROM_SUB_PATH=true``
and ``GF_SERVER_ROOT_URL=http://127.0.0.1/api/grafana/``. Grafana therefore
expects incoming requests to retain the ``/api/grafana/`` prefix. This proxy
forwards ``/api/grafana/{path}`` upstream as ``http://grafana:3000/api/grafana/{path}``
— double prefix in the target URL is intentional and matches the karma router's
behavior, where karma also has ``listen.prefix: /api/karma/``.

TODO(future): grafana.py and karma.py share ~95% of their proxy
implementation (header allow-lists, _validate_path, _verify_origin,
_aiter_with_close). Rule of three: when a third sidecar proxy lands
(e.g., Netdata in a future epic), extract the shared helpers into
kernel/api/_proxy_utils.py.
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


def _grafana_timeout_s() -> float:
    """Read upstream timeout from env (HOMELAB_MONITOR_GRAFANA_TIMEOUT_S, default 30s)."""
    raw = os.environ.get("HOMELAB_MONITOR_GRAFANA_TIMEOUT_S", "30")
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
#   - percent-encoding: %  (Grafana may emit %xx in static asset URLs and
#     dashboard UIDs, e.g. /api/grafana/d/host-overview/host-overview)
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
# absent: those are ours (the monitor's session/Bearer scheme), not Grafana's.
# Grafana authenticates via its own anonymous-org config (locked at Design).
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
# Strict-Transport-Security are NEVER relayed. X-Frame-Options is
# Grafana's default DENY which would prevent the iframe from rendering;
# stripping it here is what makes /metrics work end-to-end. The CSP
# header injected by CspHeadersMiddleware (frame-ancestors 'self') is
# the modern equivalent and gates same-origin embedding.
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


def _grafana_url() -> str:
    """Return the Grafana base URL from env, defaulting to compose hostname."""
    return os.environ.get("HOMELAB_MONITOR_GRAFANA_URL", "http://grafana:3000")


def _validate_path(path: str) -> None:
    """Reject path traversal and disallowed characters. Raises HttpProblem on failure."""
    if "\x00" in path:
        raise HttpProblem(
            status_code=400,
            code="invalid_path",
            message="path contains null byte",
        )
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
    """Same-origin check for state-changing methods on the grafana proxy.

    Threat model and reverse-proxy semantics: identical to karma's
    ``_verify_origin``. See ``karma.py`` for the full security analysis;
    this function is copied verbatim with only the function name renamed.

    Reverse-proxy aware behavior:
        ``X-Forwarded-Proto`` is only honored when the env var
        ``HOMELAB_MONITOR_TRUST_FORWARDED_HEADERS=1`` is set. Operators
        running behind a trusted reverse proxy (the supported deployment)
        set this; direct-access deployments leave it unset and fall back to
        ``request.url.scheme`` (which the ASGI server derives from the
        actual TCP socket).
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
    # Host header is intentionally NOT in the allow-list; httpx sets
    # Host based on the upstream URL passed to build_request().
    out: dict[str, str] = {}
    for name, value in request.headers.items():
        if name.lower() in _ALLOWED_REQUEST_HEADERS:
            out[name] = value
    return out


def _filter_response_headers(upstream: httpx.Response) -> dict[str, str]:
    """Build the client-bound header dict from the allow-list.

    Note: ``x-frame-options`` is intentionally absent from
    ``_ALLOWED_RESPONSE_HEADERS``, so Grafana's default
    ``X-Frame-Options: DENY`` is dropped here. This is what makes the
    /metrics iframe render. The CSP ``frame-ancestors 'self'`` header
    injected globally by ``CspHeadersMiddleware`` is the modern
    same-origin-frame gate.
    """
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
    "/grafana/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def grafana_proxy(
    path: str,
    request: Request,
    _user: User = Depends(require_session_no_csrf()),  # noqa: B008
    http_client: httpx.AsyncClient = Depends(get_http_client),  # noqa: B008
) -> StreamingResponse:
    """Forward an arbitrary request to the upstream Grafana container.

    Auth: cookie session required (CSRF-EXEMPT — see module docstring).
    Origin/Referer match required on POST/PUT/PATCH/DELETE.
    """
    log: BoundLogger = structlog.get_logger().bind(component="grafana_proxy")  # pyright: ignore[reportAssignmentType]

    _validate_path(path)
    _verify_origin(request)

    # Build the upstream URL. Grafana's GF_SERVER_SERVE_FROM_SUB_PATH=true
    # means it ALSO expects requests under `/api/grafana/...`. So we forward
    # the FULL incoming path (`/api/grafana/<rest>`), not just the captured
    # suffix. Mirrors the karma proxy's pattern.
    upstream_base = _grafana_url().rstrip("/")
    upstream_url = f"{upstream_base}/api/grafana/{path}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    headers = _filter_request_headers(request)
    body = await request.body()

    upstream_req = http_client.build_request(
        method=request.method,
        url=upstream_url,
        headers=headers,
        content=body,
        timeout=_grafana_timeout_s(),
    )
    try:
        upstream: httpx.Response = await http_client.send(
            upstream_req,
            stream=True,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning("grafana_proxy.upstream_error", error=str(exc), path=path)
        raise HttpProblem(
            status_code=502,
            code="grafana_unavailable",
            message="grafana upstream unreachable",
        ) from exc

    relayed_headers = _filter_response_headers(upstream)
    return StreamingResponse(
        _aiter_with_close(upstream),
        status_code=upstream.status_code,
        headers=relayed_headers,
    )
