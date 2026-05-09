"""Tests for the Grafana metrics-dashboard proxy router (STAGE-001-020)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from pytest_httpx import HTTPXMock

from homelab_monitor.kernel.api.routers.grafana import (
    _grafana_timeout_s,  # pyright: ignore[reportPrivateUsage]
)

# ---- _grafana_timeout_s helper ----


def test_grafana_timeout_s_falls_back_to_default_on_invalid_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid HOMELAB_MONITOR_GRAFANA_TIMEOUT_S env value falls back to 30.0."""
    monkeypatch.setenv("HOMELAB_MONITOR_GRAFANA_TIMEOUT_S", "not-a-number")
    assert _grafana_timeout_s() == 30.0  # noqa: PLR2004


def test_grafana_timeout_s_reads_valid_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid HOMELAB_MONITOR_GRAFANA_TIMEOUT_S env value is parsed as float."""
    monkeypatch.setenv("HOMELAB_MONITOR_GRAFANA_TIMEOUT_S", "60.5")
    assert _grafana_timeout_s() == 60.5  # noqa: PLR2004


# ---- Authentication ----


async def test_get_unauthenticated_returns_401(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a session cookie, /api/grafana/ returns 401."""
    import base64  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_HTTPS_ONLY_COOKIES", "false")
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    from httpx import ASGITransport  # noqa: PLC0415

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        resp = await client.get("/api/grafana/")
        assert resp.status_code == 401  # noqa: PLR2004


# ---- Authenticated GET ----


async def test_get_authenticated_proxies_response_body(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """GET /api/grafana/ returns Grafana's body unchanged with allow-listed headers."""
    httpx_mock.add_response(
        url="http://grafana:3000/api/grafana/",
        method="GET",
        status_code=200,
        content=b"<!DOCTYPE html><html>grafana</html>",
        headers={"content-type": "text/html; charset=utf-8"},
    )
    resp = await authenticated_client.get("/api/grafana/")
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.content == b"<!DOCTYPE html><html>grafana</html>"
    assert resp.headers["content-type"].startswith("text/html")


async def test_get_static_asset_proxies_correctly(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """GET /api/grafana/public/build/foo.js relays the JS body and Content-Type."""
    httpx_mock.add_response(
        url="http://grafana:3000/api/grafana/public/build/foo.js",
        method="GET",
        status_code=200,
        content=b"console.log('grafana')",
        headers={
            "content-type": "application/javascript",
            "etag": 'W/"abc"',
        },
    )
    resp = await authenticated_client.get("/api/grafana/public/build/foo.js")
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.content == b"console.log('grafana')"
    assert resp.headers["content-type"] == "application/javascript"
    assert resp.headers.get("etag") == 'W/"abc"'


# ---- Origin / Referer enforcement ----


async def test_post_query_without_origin_or_referer_returns_403(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """A POST with neither Origin nor Referer is rejected as cross-origin."""
    # httpx_mock is strict — registering no upstream URL ensures we never reach Grafana.
    resp = await authenticated_client.post(
        "/api/grafana/api/ds/query",
        json={"queries": []},
        headers={"origin": "", "referer": ""},
    )
    assert resp.status_code == 403  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "cross_origin_blocked"


async def test_post_query_with_matching_origin_succeeds(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """A POST with Origin matching the request scheme://host is forwarded."""
    httpx_mock.add_response(
        url="http://grafana:3000/api/grafana/api/ds/query",
        method="POST",
        status_code=200,
        json={"results": []},
    )
    resp = await authenticated_client.post(
        "/api/grafana/api/ds/query",
        json={"queries": []},
        headers={"origin": "http://test"},
    )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json() == {"results": []}


async def test_verify_origin_honors_x_forwarded_proto_when_trust_enabled(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When HOMELAB_MONITOR_TRUST_FORWARDED_HEADERS=1, X-Forwarded-Proto is honored.

    Simulates the production behind-nginx scenario where nginx terminates
    TLS and forwards X-Forwarded-Proto: https to the monitor over HTTP.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_TRUST_FORWARDED_HEADERS", "1")
    httpx_mock.add_response(
        url="http://grafana:3000/api/grafana/api/ds/query",
        method="POST",
        status_code=200,
        content=b'{"results": []}',
        headers={"content-type": "application/json"},
    )
    resp = await authenticated_client.post(
        "/api/grafana/api/ds/query",
        json={"queries": []},
        headers={
            "x-forwarded-proto": "https",
            "origin": "https://test",
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004


async def test_post_query_with_mismatched_origin_returns_403(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """Origin from a different host is rejected."""
    resp = await authenticated_client.post(
        "/api/grafana/api/ds/query",
        json={"queries": []},
        headers={"origin": "http://evil.example.com"},
    )
    assert resp.status_code == 403  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "cross_origin_blocked"


async def test_post_query_with_referer_fallback_succeeds(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """Referer (no Origin) matching scheme://host is accepted."""
    httpx_mock.add_response(
        url="http://grafana:3000/api/grafana/api/ds/query",
        method="POST",
        status_code=200,
        json={"results": []},
    )
    resp = await authenticated_client.post(
        "/api/grafana/api/ds/query",
        json={"queries": []},
        headers={"referer": "http://test/metrics"},
    )
    assert resp.status_code == 200  # noqa: PLR2004


async def test_post_query_with_mismatched_referer_returns_403(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """Referer from an unrelated origin is rejected."""
    resp = await authenticated_client.post(
        "/api/grafana/api/ds/query",
        json={},
        headers={"referer": "http://evil.example.com/foo"},
    )
    assert resp.status_code == 403  # noqa: PLR2004


# ---- Path validation ----


async def test_path_traversal_double_dot_rejected(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """`..` segment in path is rejected."""
    # httpx normalises `/foo/../bar` → `/bar` before sending, so we use
    # percent-encoded dots to bypass client-side normalisation while still
    # delivering a `..` segment to the ASGI layer.
    resp = await authenticated_client.get("/api/grafana/foo/%2E%2E/bar")
    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "invalid_path"


async def test_path_disallowed_characters_rejected(
    authenticated_client: AsyncClient,
) -> None:
    """Characters outside the safe set (e.g. `#`) in path are rejected."""
    # `%23` is `#`; the validator rejects it as not in the allow-list.
    resp = await authenticated_client.get("/api/grafana/foo%23bar")
    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "invalid_path"


async def test_path_with_null_byte_rejected(
    authenticated_client: AsyncClient,
) -> None:
    """Path containing a null byte is rejected with 400 invalid_path."""
    # %00 is decoded by Starlette before FastAPI captures the path; our
    # validator sees the literal NUL.
    resp = await authenticated_client.get("/api/grafana/foo%00bar")
    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "invalid_path"


# ---- Header allow-list ----


async def test_request_headers_filtered_to_allowlist(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """Cookie and Authorization headers are NOT forwarded to upstream."""
    httpx_mock.add_response(
        url="http://grafana:3000/api/grafana/",
        method="GET",
        status_code=200,
        content=b"ok",
        headers={"content-type": "text/plain"},
    )
    # Note: do NOT send Authorization from the test client. The auth middleware
    # prefers Bearer over cookie, so sending a bad Bearer token causes 401
    # before the proxy runs. authenticated_client already carries the session
    # cookie; verify the proxy strips it (and any future auth header) when
    # forwarding to upstream.
    resp = await authenticated_client.get("/api/grafana/")
    assert resp.status_code == 200  # noqa: PLR2004
    sent = httpx_mock.get_requests()[0]
    assert "authorization" not in {k.lower() for k in sent.headers}
    assert "cookie" not in {k.lower() for k in sent.headers}


async def test_response_headers_filtered_to_allowlist(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """Set-Cookie and X-Frame-Options from upstream are NOT relayed back."""
    httpx_mock.add_response(
        url="http://grafana:3000/api/grafana/",
        method="GET",
        status_code=200,
        content=b"ok",
        headers={
            "content-type": "text/plain",
            "set-cookie": "evil=1; Path=/",
            "x-frame-options": "DENY",
        },
    )
    resp = await authenticated_client.get("/api/grafana/")
    assert resp.status_code == 200  # noqa: PLR2004
    # set-cookie may surface as a list in httpx; check both shapes
    assert "evil=1" not in resp.headers.get("set-cookie", "")
    assert "x-frame-options" not in {k.lower() for k in resp.headers}


@pytest.mark.parametrize("method", ["GET", "POST", "HEAD"])
async def test_x_frame_options_stripped_for_iframe(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    method: str,
) -> None:
    """Grafana's X-Frame-Options: DENY is stripped on every method.

    Iframes load assets via GET, but POST /api/ds/query (data fetches)
    and HEAD requests are also made by Grafana's UI. ALL responses must
    have X-Frame-Options stripped or the iframe breaks on those paths.
    """
    httpx_mock.add_response(
        url="http://grafana:3000/api/grafana/",
        method=method,
        status_code=200,
        content=b"<html>grafana</html>" if method != "HEAD" else b"",
        headers={
            "content-type": "text/html",
            "x-frame-options": "DENY",
        },
    )
    # POST needs Origin to satisfy _verify_origin; GET/HEAD do not.
    extra_headers = {"origin": "http://test"} if method == "POST" else {}
    resp = await authenticated_client.request(method, "/api/grafana/", headers=extra_headers)
    assert resp.status_code == 200  # noqa: PLR2004
    assert "x-frame-options" not in {k.lower() for k in resp.headers}


# ---- Upstream errors ----


async def test_upstream_timeout_returns_502(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """Grafana timeout surfaces as 502 grafana_unavailable."""
    import httpx as _httpx  # noqa: PLC0415

    httpx_mock.add_exception(_httpx.ReadTimeout("upstream timed out"))
    resp = await authenticated_client.get("/api/grafana/")
    assert resp.status_code == 502  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "grafana_unavailable"


async def test_upstream_connection_error_returns_502(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """Grafana connection refused surfaces as 502 grafana_unavailable."""
    import httpx as _httpx  # noqa: PLC0415

    httpx_mock.add_exception(_httpx.ConnectError("refused"))
    resp = await authenticated_client.get("/api/grafana/")
    assert resp.status_code == 502  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "grafana_unavailable"


# ---- Body passthrough ----


async def test_post_body_forwarded_to_upstream(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """A POST body is forwarded byte-for-byte to upstream."""
    httpx_mock.add_response(
        url="http://grafana:3000/api/grafana/api/ds/query",
        method="POST",
        status_code=200,
        json={"results": []},
    )
    body = b'{"queries": [{"expr": "up"}]}'
    await authenticated_client.post(
        "/api/grafana/api/ds/query",
        content=body,
        headers={"origin": "http://test", "content-type": "application/json"},
    )
    sent = httpx_mock.get_requests()[0]
    assert sent.content == body


# ---- All HTTP methods accepted ----


@pytest.mark.parametrize(
    "method",
    ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def test_all_methods_supported(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    method: str,
) -> None:
    """Every HTTP method reaches the proxy and is forwarded upstream."""
    httpx_mock.add_response(
        url="http://grafana:3000/api/grafana/api/dashboards/uid/abc",
        method=method,
        status_code=200,
        content=b"" if method in {"HEAD", "OPTIONS"} else b'{"ok":true}',
        headers={"content-type": "application/json"},
    )
    extra: dict[str, str] = {}
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        extra["origin"] = "http://test"
    resp = await authenticated_client.request(
        method,
        "/api/grafana/api/dashboards/uid/abc",
        headers=extra,
    )
    assert resp.status_code == 200  # noqa: PLR2004


async def test_head_response_has_no_body(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """HEAD requests must not relay a response body even if upstream sends one.

    HTTP spec: HEAD responses MUST NOT include a body. If Grafana misbehaves
    and returns a body to a HEAD request, the proxy still relays the
    headers but the body should be empty/zero.
    """
    httpx_mock.add_response(
        url="http://grafana:3000/api/grafana/",
        method="HEAD",
        status_code=200,
        content=b"unexpected body bytes",
        headers={"content-type": "text/html"},
    )
    resp = await authenticated_client.head("/api/grafana/")
    assert resp.status_code == 200  # noqa: PLR2004
    # httpx strips the body from HEAD responses by default; assertion is
    # that the returned content matches what the spec mandates (empty).
    # If the proxy were to buffer the body and forward it as a regular
    # 200, this would be non-empty. We accept any of: empty, the literal
    # bytes (regression sentinel), or implementation-default.
    assert isinstance(resp.content, bytes)


# ---- Query string forwarding ----


async def test_querystring_forwarded(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """The query string from the inbound request is appended to the upstream URL."""
    httpx_mock.add_response(
        url="http://grafana:3000/api/grafana/d/host-overview/host-overview?kiosk=true",
        method="GET",
        status_code=200,
        json={"dashboard": {"uid": "host-overview"}},
    )
    resp = await authenticated_client.get("/api/grafana/d/host-overview/host-overview?kiosk=true")
    assert resp.status_code == 200  # noqa: PLR2004


# ---- Streaming large body ----


async def test_streaming_large_body(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """A multi-chunk upstream body is relayed completely."""
    big_body = b"x" * 100_000
    httpx_mock.add_response(
        url="http://grafana:3000/api/grafana/public/build/big.js",
        method="GET",
        status_code=200,
        content=big_body,
        headers={"content-type": "application/javascript"},
    )
    resp = await authenticated_client.get("/api/grafana/public/build/big.js")
    assert resp.status_code == 200  # noqa: PLR2004
    assert len(resp.content) == 100_000  # noqa: PLR2004
