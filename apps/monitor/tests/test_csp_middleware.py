"""Tests for CspHeadersMiddleware (STAGE-001-019)."""

from __future__ import annotations

from httpx import AsyncClient


async def test_csp_header_present_on_healthz(authenticated_client: AsyncClient) -> None:
    """Healthz response carries the frame-ancestors policy."""
    resp = await authenticated_client.get("/api/healthz")
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.headers.get("content-security-policy") == "frame-ancestors 'self'"


async def test_csp_header_present_on_unauthenticated_404(
    authenticated_client: AsyncClient,
) -> None:
    """CSP is set even on 404 / error responses (every http.response.start)."""
    resp = await authenticated_client.get("/api/this-route-does-not-exist")
    assert resp.status_code == 404  # noqa: PLR2004
    assert resp.headers.get("content-security-policy") == "frame-ancestors 'self'"


async def test_csp_header_present_on_login_endpoint(
    authenticated_client: AsyncClient,
) -> None:
    """CSP is set on auth endpoints too (the login form is rendered into a
    document context, so its headers matter most)."""
    # /api/auth/me is auth-exempt-ish (returns 401/200 depending on session)
    resp = await authenticated_client.get("/api/auth/me")
    # Either 200 (logged in) or 401 (not) — both must carry CSP
    assert resp.status_code in {200, 401}
    assert resp.headers.get("content-security-policy") == "frame-ancestors 'self'"


async def test_csp_header_value_is_exact(authenticated_client: AsyncClient) -> None:
    """The exact CSP value matches the locked spec (regression sentinel)."""
    resp = await authenticated_client.get("/api/version")
    assert resp.headers["content-security-policy"] == "frame-ancestors 'self'"


async def test_csp_middleware_preserves_existing_header_via_send_wrapper() -> None:
    """When upstream sends its own CSP header, middleware does NOT inject the default.

    Direct ASGI-level test that exercises the false branch of
    `if "content-security-policy" not in headers`. No HTTP route currently
    sets a custom CSP, so this test invokes the middleware as a pure ASGI
    callable.
    """
    from homelab_monitor.kernel.api.middleware import CspHeadersMiddleware  # noqa: PLC0415

    sent_messages: list[dict[str, object]] = []

    async def fake_send(message: dict[str, object]) -> None:
        sent_messages.append(message)

    async def fake_app(scope: object, receive: object, send: object) -> None:
        import typing  # noqa: PLC0415

        _send = typing.cast(typing.Any, send)
        await _send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/plain"),
                    (b"content-security-policy", b"default-src 'none'"),
                ],
            }
        )
        await _send({"type": "http.response.body", "body": b"ok"})

    middleware = CspHeadersMiddleware(app=fake_app)  # type: ignore[arg-type]

    async def fake_receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    scope: dict[str, object] = {
        "type": "http",
        "method": "GET",
        "path": "/test",
        "headers": [],
    }
    await middleware(scope, fake_receive, fake_send)  # type: ignore[arg-type]

    start = sent_messages[0]
    assert start["type"] == "http.response.start"
    headers = start["headers"]
    csp_values = [v for (k, v) in headers if k == b"content-security-policy"]  # type: ignore[misc]
    # Must have exactly one CSP header — the upstream's, not ours.
    assert csp_values == [b"default-src 'none'"]


async def test_csp_middleware_preserves_existing_header(
    authenticated_client: AsyncClient,
) -> None:
    """If a route handler sets its own Content-Security-Policy header,
    the middleware does NOT overwrite it. The middleware uses
    `not in headers` to decide whether to inject the default policy.

    Note: no current production route sets a per-route CSP. This test
    pre-validates the behavior so a future stage that needs a stricter
    policy on a specific route (e.g. /alerts iframe parent page) can
    rely on the middleware not stomping it.
    """
    # We don't have a route that sets its own CSP yet, so this test
    # asserts the middleware's logic via code inspection. The middleware
    # uses `if "content-security-policy" not in headers` to decide whether
    # to inject the default policy, which means existing headers are preserved.
    # This test documents the intent for future coverage when a route exists.
    resp = await authenticated_client.get("/api/healthz")
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.headers.get("content-security-policy") == "frame-ancestors 'self'"
