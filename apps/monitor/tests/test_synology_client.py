"""Tests for SynologyRestClient + SynologyError (STAGE-008-001).

Covers: login (no session= param) -> SID stored -> system_info returns parsed data
+ took + endpoint label; the _sid query param on subsequent calls; DSM 119 single
re-auth + retry (success and persistent-119); every SynologyError mapping (no-password
auth, DSM 400 auth, DSM 105 api_error, DSM 402 api_error, other DSM code api_error,
HTTP 429 rate_limited, HTTP 5xx http_error, connect -> unreachable, timeout ->
timeout, bad JSON -> bad_response, body-not-object -> bad_response, missing-success
-> bad_response); aclose logout (+ suppress + no-session no-op); single-flight login
under concurrency; the password / sid never leaking into a SynologyError.message;
load_synology_config defaults / env overrides / trailing-slash strip / blank-account
fallback.
"""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from homelab_monitor.kernel.config import SynologyConfig, load_synology_config
from homelab_monitor.kernel.plugins.io import SynologyClient
from homelab_monitor.kernel.synology.client import (
    SynologyResponse,
    SynologyRestClient,
    _extract_error_code,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.synology.errors import SynologyError

_PW = "super-secret-synology-admin-password-xyz"
_ACCOUNT = "homelab-monitor"
_SID = "sid-abc-123"
_SID_2 = "sid-def-456"
_BASE = "https://nas.local:5001"
_HTTP_OK = 200
_HTTP_TOO_MANY = 429
_HTTP_SERVER_ERROR = 503
_DSM_119 = 119
_DSM_400 = 400
_DSM_105 = 105
_DSM_402 = 402
_DSM_OTHER = 150


def _resp(
    *, json_value: object = None, status: int = _HTTP_OK, headers: dict[str, str] | None = None
) -> AsyncMock:
    """Build a mocked httpx.Response with the given json()/status/headers."""
    resp = AsyncMock()
    resp.status_code = status
    resp.headers = headers if headers is not None else {}
    resp.json = MagicMock(return_value=json_value)
    return resp


def _login_ok(sid: str = _SID) -> AsyncMock:
    """A successful SYNO.API.Auth login response body."""
    return _resp(json_value={"success": True, "data": {"sid": sid, "synotoken": "tok-1"}})


def _success(data: object = None, *, with_data: bool = True) -> AsyncMock:
    """A successful DSM data body (success:true) with/without a data key."""
    body: dict[str, object] = {"success": True}
    if with_data:
        body["data"] = data if data is not None else {"model": "DS3622xs+"}
    return _resp(json_value=body)


def _dsm_failure(code: int) -> AsyncMock:
    """A DSM logical-failure body (HTTP 200, success:false, error.code)."""
    return _resp(json_value={"success": False, "error": {"code": code}})


def _client(
    pw: str | None = _PW, base_url: str = _BASE, account: str = _ACCOUNT
) -> tuple[SynologyRestClient, AsyncMock]:
    """Build a client with a mocked dedicated httpx client; return (client, mock_http)."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    client = SynologyRestClient(
        base_url=base_url, http=mock_http, account=account, password_provider=lambda: pw
    )
    return client, mock_http


# ---- conformance ----


def test_client_satisfies_protocol() -> None:
    """SynologyRestClient structurally satisfies the SynologyClient Protocol."""
    client, _ = _client()
    assert isinstance(client, SynologyClient)


# ---- success: login + system_info + took ----


@pytest.mark.asyncio
async def test_system_info_logs_in_then_returns_response() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [
        _login_ok(),
        _success({"model": "DS3622xs+", "firmware_ver": "DSM 7.3.2"}),
    ]
    result = await client.system_info()
    assert isinstance(result, SynologyResponse)
    assert result.endpoint == "SYNO.Core.System/info"
    assert result.took_seconds >= 0.0
    assert isinstance(result.payload, dict)
    assert client._sid == _SID  # pyright: ignore[reportPrivateUsage]
    # First call was the login GET — assert NO session= param.
    login_call = mock_http.request.call_args_list[0]
    assert login_call.args[0] == "GET"
    login_params = login_call.kwargs["params"]
    assert login_params["api"] == "SYNO.API.Auth"
    assert login_params["method"] == "login"
    assert login_params["account"] == _ACCOUNT
    assert login_params["format"] == "sid"
    assert "session" not in login_params


@pytest.mark.asyncio
async def test_get_attaches_sid_query_param() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_login_ok(), _success()]
    await client.system_info()
    get_call = mock_http.request.call_args_list[1]
    assert get_call.args[0] == "GET"
    params = get_call.kwargs["params"]
    assert params["_sid"] == _SID
    assert params["api"] == "SYNO.Core.System"
    assert params["version"] == "3"
    assert params["method"] == "info"


@pytest.mark.asyncio
async def test_do_get_omits_sid_param_when_no_session() -> None:
    """_do_get with _sid=None must NOT include _sid in the query params (branch 369->371)."""
    client, mock_http = _client()
    # _sid is None by default on a fresh client — call _do_get directly.
    mock_http.request.return_value = _success()
    await client._do_get(  # pyright: ignore[reportPrivateUsage]
        "SYNO.Core.System", "3", "info", "SYNO.Core.System/info", None
    )
    call = mock_http.request.call_args
    params = call.kwargs["params"]
    assert "_sid" not in params
    assert params["api"] == "SYNO.Core.System"


@pytest.mark.asyncio
async def test_success_body_without_data_key_wraps_whole_body() -> None:
    """success:true with NO data key -> payload is the whole body."""
    client, mock_http = _client()
    mock_http.request.side_effect = [_login_ok(), _success(with_data=False)]
    result = await client.system_info()
    assert isinstance(result, SynologyResponse)
    assert result.payload == {"success": True}


# ---- DSM 119 re-auth ----


@pytest.mark.asyncio
async def test_dsm_119_triggers_single_reauth_then_succeeds() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [
        _login_ok(_SID),  # initial login
        _dsm_failure(_DSM_119),  # first GET -> 119
        _login_ok(_SID_2),  # re-login
        _success(),  # retry GET -> ok
    ]
    result = await client.system_info()
    assert isinstance(result, SynologyResponse)
    assert client._sid == _SID_2  # pyright: ignore[reportPrivateUsage]
    retry_call = mock_http.request.call_args_list[3]
    assert retry_call.kwargs["params"]["_sid"] == _SID_2


@pytest.mark.asyncio
async def test_dsm_119_persists_after_reauth_returns_auth_error() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [
        _login_ok(_SID),
        _dsm_failure(_DSM_119),  # first GET -> 119
        _login_ok(_SID_2),  # re-login ok
        _dsm_failure(_DSM_119),  # retry GET -> STILL 119
    ]
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "auth"
    assert result.status == _DSM_119


@pytest.mark.asyncio
async def test_dsm_119_then_reauth_fails_returns_login_error() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [
        _login_ok(_SID),
        _dsm_failure(_DSM_119),  # first GET -> 119
        _resp(json_value={"success": False}),  # re-login rejected
    ]
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "auth"


@pytest.mark.asyncio
async def test_dsm_119_reauth_then_retry_transport_error_returns_error() -> None:
    """After 119, re-auth succeeds, but the retry GET raises a transport error."""
    client, mock_http = _client()
    mock_http.request.side_effect = [
        _login_ok(_SID),
        _dsm_failure(_DSM_119),  # first GET -> 119
        _login_ok(_SID_2),  # re-auth ok
        httpx.ReadTimeout("retry timed out"),  # retry GET raises
    ]
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "timeout"


# ---- DSM error-code mappings ----


@pytest.mark.asyncio
async def test_dsm_400_maps_auth() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_login_ok(), _dsm_failure(_DSM_400)]
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "auth"
    assert result.status == _DSM_400


@pytest.mark.asyncio
async def test_dsm_105_maps_api_error() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_login_ok(), _dsm_failure(_DSM_105)]
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "api_error"
    assert result.status == _DSM_105


@pytest.mark.asyncio
async def test_dsm_402_maps_api_error() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_login_ok(), _dsm_failure(_DSM_402)]
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "api_error"
    assert result.status == _DSM_402


@pytest.mark.asyncio
async def test_dsm_other_code_maps_api_error() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_login_ok(), _dsm_failure(_DSM_OTHER)]
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "api_error"
    assert result.status == _DSM_OTHER


@pytest.mark.asyncio
async def test_dsm_malformed_error_body_code_zero_maps_api_error() -> None:
    """success:false with no error key -> _extract_error_code returns 0 -> api_error."""
    client, mock_http = _client()
    mock_http.request.side_effect = [_login_ok(), _resp(json_value={"success": False})]
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "api_error"
    assert result.status == 0


# ---- HTTP status mappings ----


@pytest.mark.asyncio
async def test_429_maps_rate_limited() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_login_ok(), _resp(status=_HTTP_TOO_MANY)]
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "rate_limited"
    assert result.status == _HTTP_TOO_MANY


@pytest.mark.asyncio
async def test_5xx_maps_http_error_not_propagated() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_login_ok(), _resp(status=_HTTP_SERVER_ERROR)]
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "http_error"
    assert result.status == _HTTP_SERVER_ERROR


@pytest.mark.asyncio
async def test_connect_error_maps_unreachable() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_login_ok(), httpx.ConnectError("refused")]
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "unreachable"


@pytest.mark.asyncio
async def test_connect_timeout_maps_unreachable() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_login_ok(), httpx.ConnectTimeout("connect timed out")]
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "unreachable"


@pytest.mark.asyncio
async def test_read_timeout_maps_timeout() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_login_ok(), httpx.ReadTimeout("read timed out")]
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "timeout"


@pytest.mark.asyncio
async def test_generic_timeout_exception_maps_timeout() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_login_ok(), httpx.TimeoutException("pool timeout")]
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "timeout"


@pytest.mark.asyncio
async def test_non_json_body_is_bad_response() -> None:
    client, mock_http = _client()
    bad = _resp()
    bad.json = MagicMock(side_effect=ValueError("no json"))
    mock_http.request.side_effect = [_login_ok(), bad]
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "bad_response"


@pytest.mark.asyncio
async def test_body_not_object_is_bad_response() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_login_ok(), _resp(json_value=["not", "an", "object"])]
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "bad_response"


@pytest.mark.asyncio
async def test_body_missing_success_is_bad_response() -> None:
    """success key absent / not a bool -> bad_response (neither True nor False branch)."""
    client, mock_http = _client()
    mock_http.request.side_effect = [_login_ok(), _resp(json_value={"data": {}})]
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "bad_response"


# ---- login failures ----


@pytest.mark.asyncio
async def test_no_password_returns_auth_without_network_call() -> None:
    client, mock_http = _client(pw=None)
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "auth"
    assert result.message == "no synology password configured"
    mock_http.request.assert_not_called()


@pytest.mark.asyncio
async def test_login_success_false_returns_auth() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _resp(json_value={"success": False})
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "auth"


@pytest.mark.asyncio
async def test_login_no_sid_returns_auth() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _resp(json_value={"success": True, "data": {}})
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "auth"


@pytest.mark.asyncio
async def test_login_data_not_object_returns_auth() -> None:
    """success:true but data is not an object -> _parse_session yields no sid -> auth."""
    client, mock_http = _client()
    mock_http.request.return_value = _resp(json_value={"success": True, "data": "nope"})
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "auth"


@pytest.mark.asyncio
async def test_login_connect_error_maps_unreachable() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = httpx.ConnectError("refused")
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "unreachable"


@pytest.mark.asyncio
async def test_login_timeout_maps_timeout() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = httpx.ReadTimeout("timed out")
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "timeout"


@pytest.mark.asyncio
async def test_login_429_maps_rate_limited() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _resp(status=_HTTP_TOO_MANY)
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "rate_limited"


@pytest.mark.asyncio
async def test_login_5xx_maps_auth() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _resp(status=_HTTP_SERVER_ERROR)
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "auth"
    assert result.status == _HTTP_SERVER_ERROR


@pytest.mark.asyncio
async def test_login_non_json_is_bad_response() -> None:
    client, mock_http = _client()
    bad = _resp()
    bad.json = MagicMock(side_effect=ValueError("no json"))
    mock_http.request.return_value = bad
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "bad_response"


@pytest.mark.asyncio
async def test_login_body_not_object_is_bad_response() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _resp(json_value=["not", "an", "object"])
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert result.reason == "bad_response"


# ---- password / sid never leak ----


@pytest.mark.asyncio
async def test_password_never_in_error_message_on_rejected_login() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _resp(json_value={"success": False})
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert _PW not in result.message


@pytest.mark.asyncio
async def test_password_never_in_error_message_on_unreachable() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = httpx.ConnectError(f"refused {_PW}")
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert _PW not in result.message


@pytest.mark.asyncio
async def test_sid_never_in_error_message() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_login_ok(_SID), _dsm_failure(_DSM_105)]
    result = await client.system_info()
    assert isinstance(result, SynologyError)
    assert _SID not in result.message


# ---- aclose ----


@pytest.mark.asyncio
async def test_aclose_logs_out_with_sid() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_login_ok(), _success()]
    await client.system_info()
    mock_http.request.reset_mock()
    mock_http.request.side_effect = None
    mock_http.request.return_value = _resp(json_value={"success": True})
    await client.aclose()
    logout_call = mock_http.request.call_args
    assert logout_call.args[0] == "GET"
    params = logout_call.kwargs["params"]
    assert params["method"] == "logout"
    assert params["_sid"] == _SID
    assert client._sid is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_aclose_no_session_is_noop() -> None:
    client, mock_http = _client()
    await client.aclose()
    mock_http.request.assert_not_called()


@pytest.mark.asyncio
async def test_aclose_suppresses_transport_error() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = [_login_ok(), _success()]
    await client.system_info()
    mock_http.request.reset_mock()
    mock_http.request.side_effect = httpx.ConnectError("nas is down")
    await client.aclose()  # must NOT raise
    assert client._sid is None  # pyright: ignore[reportPrivateUsage]


# ---- single-flight login ----


@pytest.mark.asyncio
async def test_concurrent_first_calls_login_once() -> None:
    client, mock_http = _client()
    login_count = 0

    async def fake_request(method: str, url: str, **kwargs: object) -> AsyncMock:
        nonlocal login_count
        params = kwargs.get("params")
        is_login = (
            isinstance(params, dict) and cast("dict[str, object]", params).get("method") == "login"
        )
        if is_login:
            login_count += 1
            await asyncio.sleep(0)  # yield so both coros queue on the lock
            return _login_ok()
        return _success()

    mock_http.request.side_effect = fake_request
    results = await asyncio.gather(client.system_info(), client.system_info())
    assert all(isinstance(r, SynologyResponse) for r in results)
    assert login_count == 1


@pytest.mark.asyncio
async def test_second_call_skips_login() -> None:
    """After a first successful call sets _sid, the second call does NOT re-login."""
    client, mock_http = _client()
    mock_http.request.side_effect = [_login_ok(), _success(), _success()]
    result1 = await client.system_info()
    assert isinstance(result1, SynologyResponse)
    result2 = await client.system_info()
    assert isinstance(result2, SynologyResponse)
    login_calls = [
        c
        for c in mock_http.request.call_args_list
        if isinstance(c.kwargs.get("params"), dict) and c.kwargs["params"].get("method") == "login"
    ]
    assert len(login_calls) == 1


# ---- scaffolding helpers ----


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "api", "version", "method", "call_kwargs"),
    [
        ("system_utilization", "SYNO.Core.System.Utilization", "1", "get", {}),
        ("system_health", "SYNO.Core.System.SystemHealth", "1", "get", {}),
        ("hardware_fanspeed", "SYNO.Core.Hardware.FanSpeed", "1", "get", {}),
        ("need_reboot", "SYNO.Core.Hardware.NeedReboot", "1", "get", {}),
        ("storage_load_info", "SYNO.Storage.CGI.Storage", "1", "load_info", {}),
        ("ups_get", "SYNO.Core.ExternalDevice.UPS", "1", "get", {}),
        ("upgrade_check", "SYNO.Core.Upgrade.Server", "4", "check", {}),
        ("package_list", "SYNO.Core.Package", "1", "list", {}),
        ("package_server_list", "SYNO.Core.Package.Server", "2", "list", {}),
        ("backup_task_list", "SYNO.Backup.Task", "1", "list", {}),
        ("backup_repository_list", "SYNO.Backup.Repository", "1", "list", {}),
        ("share_snapshot_list", "SYNO.Core.Share.Snapshot", "2", "list", {"name": "photo"}),
        ("security_scan_status", "SYNO.Core.SecurityScan.Status", "1", "system_get", {}),
        ("current_connection_list", "SYNO.Core.CurrentConnection", "1", "list", {}),
        ("ss_info", "SYNO.SurveillanceStation.Info", "1", "GetInfo", {}),
        ("ss_camera_list", "SYNO.SurveillanceStation.Camera", "9", "List", {}),
        (
            "ss_event_count_by_category",
            "SYNO.SurveillanceStation.Event",
            "5",
            "CountByCategory",
            {},
        ),
        ("ss_recording_list", "SYNO.SurveillanceStation.Recording", "6", "List", {}),
        ("ss_license", "SYNO.SurveillanceStation.License", "2", "Load", {}),
        ("ss_homemode", "SYNO.SurveillanceStation.HomeMode", "1", "GetInfo", {}),
        ("ss_log_list", "SYNO.SurveillanceStation.Log", "3", "List", {}),
    ],
)
async def test_scaffolding_helper_sends_correct_query(
    method_name: str,
    api: str,
    version: str,
    method: str,
    call_kwargs: dict[str, object],
) -> None:
    """Each scaffolding helper sends the correct api/version/method query params."""
    client, mock_http = _client()
    mock_http.request.side_effect = [_login_ok(), _success()]
    fn = getattr(client, method_name)
    result = await fn(**call_kwargs)
    assert isinstance(result, SynologyResponse)
    assert result.endpoint == f"{api}/{method}"
    get_call = mock_http.request.call_args_list[1]
    params = get_call.kwargs["params"]
    assert params["api"] == api
    assert params["version"] == version
    assert params["method"] == method
    for k, v in call_kwargs.items():
        assert params[k] == v


# ---- _extract_error_code ----


def test_extract_error_code_missing_returns_zero() -> None:
    """_extract_error_code returns 0 when error / code is absent or wrong type."""
    assert _extract_error_code({"success": False}) == 0
    assert _extract_error_code({"error": "not a dict"}) == 0
    assert _extract_error_code({"error": {"code": "nope"}}) == 0
    assert _extract_error_code({"error": {"code": True}}) == 0  # bool is not a code


# ---- load_synology_config ----


def test_load_synology_config_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_MONITOR_SYNOLOGY_URL", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_SYNOLOGY_ACCOUNT", raising=False)
    cfg = load_synology_config()
    assert isinstance(cfg, SynologyConfig)
    assert cfg.base_url == "https://192.168.2.4:5001"
    assert cfg.account == "homelab-monitor"


def test_load_synology_config_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_SYNOLOGY_URL", "https://10.0.0.9:5001")
    cfg = load_synology_config()
    assert cfg.base_url == "https://10.0.0.9:5001"


def test_load_synology_config_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_SYNOLOGY_URL", "https://10.0.0.9:5001/")
    cfg = load_synology_config()
    assert cfg.base_url == "https://10.0.0.9:5001"


def test_load_synology_config_account_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_SYNOLOGY_ACCOUNT", "monitor-svc")
    cfg = load_synology_config()
    assert cfg.account == "monitor-svc"


def test_load_synology_config_blank_account_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_SYNOLOGY_ACCOUNT", "   ")
    cfg = load_synology_config()
    assert cfg.account == "homelab-monitor"
