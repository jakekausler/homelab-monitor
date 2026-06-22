"""Tests for PiholeRestClient + PiholeError (STAGE-006-001).

Covers: session login -> SID stored -> info_version returns parsed payload + took;
the X-FTL-SID header; 401 single re-auth + retry (success and persistent-401);
every PiholeError mapping (no-password auth, 429 rate_limited, 5xx http_error,
connect -> unreachable, timeout -> timeout, bad JSON -> bad_response); aclose
logout (DELETE + suppress + no-session no-op); single-flight login under concurrency;
the password never leaking into a PiholeError.message; load_pihole_config default /
env override / trailing-slash strip.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from homelab_monitor.kernel.config import PiholeConfig, load_pihole_config
from homelab_monitor.kernel.pihole.client import (
    PiholeResponse,
    PiholeRestClient,
    _extract_took,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.pihole.errors import PiholeError
from homelab_monitor.kernel.plugins.io import PiholeClient

_PW = "super-secret-pihole-app-password-xyz"
_SID = "sid-abc-123"
_SID_2 = "sid-def-456"
_BASE = "http://localhost:8080"
_HTTP_OK = 200
_HTTP_UNAUTHORIZED = 401
_HTTP_TOO_MANY = 429
_HTTP_SERVER_ERROR = 503


def _resp(
    *, json_value: object = None, status: int = _HTTP_OK, headers: dict[str, str] | None = None
) -> AsyncMock:
    """Build a mocked httpx.Response with the given json()/status/headers."""
    resp = AsyncMock()
    resp.status_code = status
    resp.headers = headers if headers is not None else {}
    resp.json = MagicMock(return_value=json_value)
    return resp


def _auth_ok(sid: str = _SID) -> AsyncMock:
    """A successful /api/auth response body."""
    return _resp(
        json_value={
            "session": {
                "valid": True,
                "totp": False,
                "sid": sid,
                "validity": 1800,
                "message": "password correct",
            }
        }
    )


def _client(pw: str | None = _PW, base_url: str = _BASE) -> tuple[PiholeRestClient, AsyncMock]:
    """Build a client with a mocked shared httpx client; return (client, mock_http)."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    client = PiholeRestClient(base_url=base_url, http=mock_http, password_provider=lambda: pw)
    return client, mock_http


# ---- conformance ----


def test_client_satisfies_protocol() -> None:
    """PiholeRestClient structurally satisfies the PiholeClient Protocol."""
    client, _ = _client()
    assert isinstance(client, PiholeClient)


# ---- success: login + info_version + took ----


@pytest.mark.asyncio
async def test_info_version_logs_in_then_returns_response() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [
        _auth_ok(),
        _resp(json_value={"version": {"core": {"local": {"version": "v6.0"}}}, "took": 0.0123}),
    ]
    result = await client.info_version()
    assert isinstance(result, PiholeResponse)
    assert result.endpoint == "info/version"
    assert result.took_seconds == 0.0123  # noqa: PLR2004 -- exact mock value
    assert client._sid == _SID  # pyright: ignore[reportPrivateUsage]
    # First call was the login POST.
    login_call = mock_http.request.call_args_list[0]
    assert login_call.args[0] == "POST"
    assert login_call.args[1] == "http://localhost:8080/api/auth"
    assert login_call.kwargs["json"] == {"password": _PW}


@pytest.mark.asyncio
async def test_get_attaches_x_ftl_sid_header() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_auth_ok(), _resp(json_value={"took": 0.0})]
    await client.info_version()
    get_call = mock_http.request.call_args_list[1]
    assert get_call.args[0] == "GET"
    assert get_call.args[1] == "http://localhost:8080/api/info/version"
    assert get_call.kwargs["headers"]["X-FTL-SID"] == _SID


@pytest.mark.asyncio
async def test_took_missing_defaults_zero() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_auth_ok(), _resp(json_value={"version": {}})]
    result = await client.info_version()
    assert isinstance(result, PiholeResponse)
    assert result.took_seconds == 0.0


# ---- 401 re-auth ----


@pytest.mark.asyncio
async def test_401_triggers_single_reauth_then_succeeds() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [
        _auth_ok(_SID),  # initial login
        _resp(status=_HTTP_UNAUTHORIZED),  # first GET -> 401
        _auth_ok(_SID_2),  # re-login
        _resp(json_value={"took": 0.5}),  # retry GET -> ok
    ]
    result = await client.info_version()
    assert isinstance(result, PiholeResponse)
    assert client._sid == _SID_2  # pyright: ignore[reportPrivateUsage]
    # The retried GET carried the NEW sid.
    retry_call = mock_http.request.call_args_list[3]
    assert retry_call.kwargs["headers"]["X-FTL-SID"] == _SID_2


@pytest.mark.asyncio
async def test_401_persists_after_reauth_returns_auth_error() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [
        _auth_ok(_SID),
        _resp(status=_HTTP_UNAUTHORIZED),  # first GET -> 401
        _auth_ok(_SID_2),  # re-login ok
        _resp(status=_HTTP_UNAUTHORIZED),  # retry GET -> STILL 401
    ]
    result = await client.info_version()
    assert isinstance(result, PiholeError)
    assert result.reason == "auth"
    assert result.status == _HTTP_UNAUTHORIZED


@pytest.mark.asyncio
async def test_401_then_reauth_fails_returns_login_error() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [
        _auth_ok(_SID),
        _resp(status=_HTTP_UNAUTHORIZED),  # first GET -> 401
        _resp(
            json_value={"session": {"valid": False, "sid": None, "message": "rejected"}}
        ),  # re-login rejected
    ]
    result = await client.info_version()
    assert isinstance(result, PiholeError)
    assert result.reason == "auth"


# ---- status mappings ----


@pytest.mark.asyncio
async def test_429_maps_rate_limited() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_auth_ok(), _resp(status=_HTTP_TOO_MANY)]
    result = await client.info_version()
    assert isinstance(result, PiholeError)
    assert result.reason == "rate_limited"
    assert result.status == _HTTP_TOO_MANY


@pytest.mark.asyncio
async def test_429_with_retry_after_header() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [
        _auth_ok(),
        _resp(status=_HTTP_TOO_MANY, headers={"Retry-After": "30"}),
    ]
    result = await client.info_version()
    assert isinstance(result, PiholeError)
    assert result.reason == "rate_limited"
    assert "30" in result.message


@pytest.mark.asyncio
async def test_5xx_maps_http_error_not_propagated() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_auth_ok(), _resp(status=_HTTP_SERVER_ERROR)]
    result = await client.info_version()
    assert isinstance(result, PiholeError)
    assert result.reason == "http_error"
    assert result.status == _HTTP_SERVER_ERROR


@pytest.mark.asyncio
async def test_connect_error_maps_unreachable() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_auth_ok(), httpx.ConnectError("refused")]
    result = await client.info_version()
    assert isinstance(result, PiholeError)
    assert result.reason == "unreachable"


@pytest.mark.asyncio
async def test_connect_timeout_maps_unreachable() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_auth_ok(), httpx.ConnectTimeout("connect timed out")]
    result = await client.info_version()
    assert isinstance(result, PiholeError)
    assert result.reason == "unreachable"


@pytest.mark.asyncio
async def test_read_timeout_maps_timeout() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_auth_ok(), httpx.ReadTimeout("read timed out")]
    result = await client.info_version()
    assert isinstance(result, PiholeError)
    assert result.reason == "timeout"


@pytest.mark.asyncio
async def test_generic_timeout_exception_maps_timeout() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_auth_ok(), httpx.TimeoutException("pool timeout")]
    result = await client.info_version()
    assert isinstance(result, PiholeError)
    assert result.reason == "timeout"


@pytest.mark.asyncio
async def test_non_json_body_is_bad_response() -> None:
    client, mock_http = _client()
    bad = _resp()
    bad.json = MagicMock(side_effect=ValueError("no json"))
    mock_http.request.side_effect = [_auth_ok(), bad]
    result = await client.info_version()
    assert isinstance(result, PiholeError)
    assert result.reason == "bad_response"


# ---- login failures ----


@pytest.mark.asyncio
async def test_no_password_returns_auth_without_network_call() -> None:
    client, mock_http = _client(pw=None)
    result = await client.info_version()
    assert isinstance(result, PiholeError)
    assert result.reason == "auth"
    assert result.message == "no pihole password configured"
    mock_http.request.assert_not_called()


@pytest.mark.asyncio
async def test_login_rejected_invalid_session_returns_auth() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _resp(
        json_value={"session": {"valid": False, "sid": None, "message": "password incorrect"}}
    )
    result = await client.info_version()
    assert isinstance(result, PiholeError)
    assert result.reason == "auth"
    assert "password incorrect" in result.message


@pytest.mark.asyncio
async def test_login_connect_error_maps_unreachable() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = httpx.ConnectError("refused")
    result = await client.info_version()
    assert isinstance(result, PiholeError)
    assert result.reason == "unreachable"


@pytest.mark.asyncio
async def test_login_timeout_maps_timeout() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = httpx.ReadTimeout("timed out")
    result = await client.info_version()
    assert isinstance(result, PiholeError)
    assert result.reason == "timeout"


@pytest.mark.asyncio
async def test_login_429_maps_rate_limited() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _resp(status=_HTTP_TOO_MANY)
    result = await client.info_version()
    assert isinstance(result, PiholeError)
    assert result.reason == "rate_limited"


@pytest.mark.asyncio
async def test_login_5xx_maps_auth() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _resp(status=_HTTP_SERVER_ERROR)
    result = await client.info_version()
    assert isinstance(result, PiholeError)
    assert result.reason == "auth"
    assert result.status == _HTTP_SERVER_ERROR


@pytest.mark.asyncio
async def test_login_non_json_is_bad_response() -> None:
    client, mock_http = _client()
    bad = _resp()
    bad.json = MagicMock(side_effect=ValueError("no json"))
    mock_http.request.return_value = bad
    result = await client.info_version()
    assert isinstance(result, PiholeError)
    assert result.reason == "bad_response"


@pytest.mark.asyncio
async def test_login_body_not_object_is_bad_response() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _resp(json_value=["not", "an", "object"])
    result = await client.info_version()
    assert isinstance(result, PiholeError)
    assert result.reason == "bad_response"


# ---- password never leaks ----


@pytest.mark.asyncio
async def test_password_never_in_error_message_on_rejected_login() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _resp(
        json_value={"session": {"valid": False, "sid": None, "message": "nope"}}
    )
    result = await client.info_version()
    assert isinstance(result, PiholeError)
    assert _PW not in result.message


@pytest.mark.asyncio
async def test_password_never_in_error_message_on_unreachable() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = httpx.ConnectError(f"refused {_PW}")
    result = await client.info_version()
    assert isinstance(result, PiholeError)
    assert _PW not in result.message


# ---- aclose ----


@pytest.mark.asyncio
async def test_aclose_logs_out_with_sid() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_auth_ok(), _resp(json_value={"took": 0.0})]
    await client.info_version()
    mock_http.request.reset_mock()
    mock_http.request.side_effect = None
    mock_http.request.return_value = _resp(json_value={})
    await client.aclose()
    logout_call = mock_http.request.call_args
    assert logout_call.args[0] == "DELETE"
    assert logout_call.args[1] == "http://localhost:8080/api/auth"
    assert logout_call.kwargs["headers"]["X-FTL-SID"] == _SID
    assert client._sid is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_aclose_no_session_is_noop() -> None:
    client, mock_http = _client()
    await client.aclose()
    mock_http.request.assert_not_called()


@pytest.mark.asyncio
async def test_aclose_suppresses_transport_error() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_auth_ok(), _resp(json_value={"took": 0.0})]
    await client.info_version()
    mock_http.request.reset_mock()
    mock_http.request.side_effect = httpx.ConnectError("pihole is down")
    # Must NOT raise.
    await client.aclose()
    assert client._sid is None  # pyright: ignore[reportPrivateUsage]


# ---- single-flight login ----


@pytest.mark.asyncio
async def test_concurrent_first_calls_login_once() -> None:
    client, mock_http = _client()
    login_count = 0

    async def fake_request(method: str, url: str, **kwargs: object) -> AsyncMock:
        nonlocal login_count
        if url.endswith("/api/auth") and method == "POST":
            login_count += 1
            await asyncio.sleep(0)  # yield so both coros queue on the lock
            return _auth_ok()
        return _resp(json_value={"took": 0.0})

    mock_http.request.side_effect = fake_request
    results = await asyncio.gather(client.info_version(), client.info_version())
    assert all(isinstance(r, PiholeResponse) for r in results)
    assert login_count == 1


# ---- load_pihole_config ----


def test_load_pihole_config_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_MONITOR_PIHOLE_URL", raising=False)
    cfg = load_pihole_config()
    assert isinstance(cfg, PiholeConfig)
    assert cfg.base_url == "http://192.168.2.148:8080"


def test_load_pihole_config_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_PIHOLE_URL", "http://192.168.2.5:8081")
    cfg = load_pihole_config()
    assert cfg.base_url == "http://192.168.2.5:8081"


def test_load_pihole_config_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_PIHOLE_URL", "http://192.168.2.5:8081/")
    cfg = load_pihole_config()
    assert cfg.base_url == "http://192.168.2.5:8081"


# ---- scaffolding helpers ----


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "expected_endpoint", "call_kwargs"),
    [
        ("info_ftl", "info/ftl", {}),
        ("info_database", "info/database", {}),
        ("info_messages", "info/messages", {}),
        ("info_system", "info/system", {}),
        ("stats_summary", "stats/summary", {}),
        ("stats_upstreams", "stats/upstreams", {}),
        ("stats_query_types", "stats/query_types", {}),
        ("stats_top_clients", "stats/top_clients", {}),
        ("stats_top_domains", "stats/top_domains", {}),
        ("stats_recent_blocked", "stats/recent_blocked", {}),
        ("dns_blocking", "dns/blocking", {}),
        ("lists", "lists", {}),
        ("network_devices", "network/devices", {}),
        ("queries", "queries", {"params": {"n": "10"}}),
    ],
)
async def test_scaffolding_helper_returns_pihole_response(
    method_name: str, expected_endpoint: str, call_kwargs: dict[str, object]
) -> None:
    """Each scaffolding GET helper calls _get with the correct endpoint label."""
    client, mock_http = _client()
    mock_http.request.side_effect = [
        _auth_ok(),
        _resp(json_value={"took": 0.001}),
    ]
    method = getattr(client, method_name)
    result = await method(**call_kwargs)
    assert isinstance(result, PiholeResponse)
    assert result.endpoint == expected_endpoint


# ---- _get: 401 re-auth succeeds but retry GET hits transport error (line 276) ----


@pytest.mark.asyncio
async def test_401_reauth_then_retry_transport_error_returns_error() -> None:
    """After 401, re-auth succeeds, but the retry GET raises a transport error.

    Covers client.py line 276: `return resp` when _do_get returns a PiholeError
    on the post-reauth retry.
    """
    client, mock_http = _client()
    mock_http.request.side_effect = [
        _auth_ok(_SID),  # initial login (SID is None on fresh client)
        _resp(status=_HTTP_UNAUTHORIZED),  # first GET -> 401
        _auth_ok(_SID_2),  # re-auth succeeds
        httpx.ReadTimeout("retry timed out"),  # retry GET raises transport error
    ]
    result = await client.info_version()
    assert isinstance(result, PiholeError)
    assert result.reason == "timeout"


# ---- _extract_took: non-dict payload returns 0.0 (branch 341→345) ----


def test_extract_took_non_dict_payload_returns_zero() -> None:
    """_extract_took returns 0.0 for any non-dict payload."""
    assert _extract_took([1, 2, 3]) == 0.0
    assert _extract_took("not a dict") == 0.0
    assert _extract_took(None) == 0.0


# ---- _get skip-login when SID already set ----


@pytest.mark.asyncio
async def test_second_call_skips_login() -> None:
    """After a first successful call sets _sid, the second call does NOT re-login.

    Exercises _get branch: self._sid is not None → skip _ensure_session entirely.
    """
    client, mock_http = _client()
    # Prime: auth + first payload + second payload (NO second auth).
    mock_http.request.side_effect = [
        _auth_ok(),
        _resp(json_value={"took": 0.0}),
        _resp(json_value={"took": 0.0}),
    ]
    # First call: logs in (1 POST /api/auth) + issues GET.
    result1 = await client.info_version()
    assert isinstance(result1, PiholeResponse)
    # Second call: SID is set → skip login, issue GET directly.
    result2 = await client.info_version()
    assert isinstance(result2, PiholeResponse)

    # Count POST /api/auth calls — must be exactly 1.
    auth_calls = [
        c
        for c in mock_http.request.call_args_list
        if c.args[0] == "POST" and "/api/auth" in c.args[1]
    ]
    assert len(auth_calls) == 1
