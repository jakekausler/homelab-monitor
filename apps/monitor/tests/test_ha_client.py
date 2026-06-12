"""Tests for HomeAssistantRestClient + HaError (STAGE-005-001).

Covers: config/states/error_log/call_service parsing; the full HaError mapping
(no-token auth, 401/403 auth, 5xx http_error, connect -> unreachable, timeout
-> timeout, bad JSON / wrong shape -> bad_response); token never leaks into an
HaError.message; load_ha_config default / env override / trailing-slash strip.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from homelab_monitor.kernel.config import HaConfig, load_ha_config
from homelab_monitor.kernel.ha.client import (
    HaConfigResult,
    HaErrorLogResult,
    HaServiceResult,
    HaState,
    HomeAssistantRestClient,
)
from homelab_monitor.kernel.ha.errors import HaError
from homelab_monitor.kernel.plugins.io import HomeAssistantClient

_TOKEN = "super-secret-ha-token-xyz"
_HTTP_OK = 200
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_HTTP_SERVER_ERROR = 500
_HTTP_BAD_GATEWAY = 502
_EXPECTED_STATE_COUNT = 2


def _ok_response(*, json_value: object = None, text: str = "", status: int = _HTTP_OK) -> AsyncMock:
    """Build a mocked httpx.Response with the given json()/text/status."""
    resp = AsyncMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=json_value)
    resp.text = text
    return resp


def _client(
    token: str | None = _TOKEN, base_url: str = "http://ha.local:8123"
) -> tuple[HomeAssistantRestClient, AsyncMock]:
    """Build a client with a mocked shared httpx client; return (client, mock_http)."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    client = HomeAssistantRestClient(
        base_url=base_url, http=mock_http, token_provider=lambda: token
    )
    return client, mock_http


# ---- conformance ----


def test_client_satisfies_protocol() -> None:
    """HomeAssistantRestClient structurally satisfies the HomeAssistantClient Protocol."""
    client, _ = _client()
    assert isinstance(client, HomeAssistantClient)


# ---- get_config ----


@pytest.mark.asyncio
async def test_get_config_parses_version_and_timezone() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(
        json_value={"version": "2026.6.1", "time_zone": "America/New_York"}
    )
    result = await client.get_config()
    assert isinstance(result, HaConfigResult)
    assert result.version == "2026.6.1"
    assert result.time_zone == "America/New_York"


@pytest.mark.asyncio
async def test_get_config_missing_fields_default_empty() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(json_value={})
    result = await client.get_config()
    assert isinstance(result, HaConfigResult)
    assert result.version == ""
    assert result.time_zone == ""


@pytest.mark.asyncio
async def test_get_config_non_object_body_is_bad_response() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(json_value=["not", "an", "object"])
    result = await client.get_config()
    assert isinstance(result, HaError)
    assert result.reason == "bad_response"


@pytest.mark.asyncio
async def test_get_config_builds_authenticated_url() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(json_value={"version": "1", "time_zone": "UTC"})
    await client.get_config()
    call = mock_http.request.call_args
    assert call.args[0] == "GET"
    assert call.args[1] == "http://ha.local:8123/api/config"
    assert call.kwargs["headers"]["Authorization"] == f"Bearer {_TOKEN}"


# ---- get_states ----


@pytest.mark.asyncio
async def test_get_states_parses_state_objects() -> None:
    client, mock_http = _client()
    _states: list[object] = [
        {
            "entity_id": "sensor.temp",
            "state": "21.5",
            "attributes": {"unit_of_measurement": "C", "battery": 80},
            "last_changed": "2026-06-10T12:00:00+00:00",
            "last_updated": "2026-06-10T12:00:01+00:00",
        },
        {
            "entity_id": "light.kitchen",
            "state": "on",
            "attributes": {},
            "last_changed": "2026-06-10T11:00:00+00:00",
            "last_updated": "2026-06-10T11:00:00+00:00",
        },
    ]
    mock_http.request.return_value = _ok_response(json_value=_states)
    result = await client.get_states()
    assert isinstance(result, list)
    assert len(result) == _EXPECTED_STATE_COUNT
    first = result[0]
    assert isinstance(first, HaState)
    assert first.entity_id == "sensor.temp"
    assert first.state == "21.5"
    assert first.attributes["battery"] == 80  # noqa: PLR2004
    assert first.last_changed == "2026-06-10T12:00:00+00:00"


@pytest.mark.asyncio
async def test_get_states_missing_fields_default() -> None:
    client, mock_http = _client()
    _empty: list[object] = [{}]
    mock_http.request.return_value = _ok_response(json_value=_empty)
    result = await client.get_states()
    assert isinstance(result, list)
    assert result[0].entity_id == ""
    assert result[0].state == ""
    assert result[0].attributes == {}
    assert result[0].last_changed == ""
    assert result[0].last_updated == ""


@pytest.mark.asyncio
async def test_get_states_non_list_body_is_bad_response() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(json_value={"not": "a list"})
    result = await client.get_states()
    assert isinstance(result, HaError)
    assert result.reason == "bad_response"


@pytest.mark.asyncio
async def test_get_states_entry_not_object_is_bad_response() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(json_value=["not-an-object"])
    result = await client.get_states()
    assert isinstance(result, HaError)
    assert result.reason == "bad_response"


# ---- get_error_log ----


@pytest.mark.asyncio
async def test_get_error_log_returns_text() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(text="2026-06-10 ERROR something\n")
    result = await client.get_error_log()
    assert isinstance(result, HaErrorLogResult)
    assert "ERROR something" in result.text


@pytest.mark.asyncio
async def test_get_error_log_500_is_http_error() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(status=_HTTP_SERVER_ERROR)
    result = await client.get_error_log()
    assert isinstance(result, HaError)
    assert result.reason == "http_error"
    assert result.status == _HTTP_SERVER_ERROR


@pytest.mark.asyncio
async def test_get_error_log_connect_error_maps_unreachable() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = httpx.ConnectError("refused")
    result = await client.get_error_log()
    assert isinstance(result, HaError)
    assert result.reason == "unreachable"


# ---- call_service ----


@pytest.mark.asyncio
async def test_call_service_posts_with_body_and_parses_changed_states() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(
        json_value=[{"entity_id": "light.kitchen", "state": "on"}]
    )
    result = await client.call_service("light", "turn_on", {"entity_id": "light.kitchen"})
    assert isinstance(result, HaServiceResult)
    assert result.changed_states[0]["entity_id"] == "light.kitchen"
    call = mock_http.request.call_args
    assert call.args[0] == "POST"
    assert call.args[1] == "http://ha.local:8123/api/services/light/turn_on"
    assert call.kwargs["json"] == {"entity_id": "light.kitchen"}


@pytest.mark.asyncio
async def test_call_service_none_data_posts_empty_object() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(json_value=[])
    result = await client.call_service("homeassistant", "check_config")
    assert isinstance(result, HaServiceResult)
    assert result.changed_states == []
    assert mock_http.request.call_args.kwargs["json"] == {}


@pytest.mark.asyncio
async def test_call_service_drops_non_dict_entries() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(json_value=[{"entity_id": "a"}, "garbage", 5])
    result = await client.call_service("light", "turn_on")
    assert isinstance(result, HaServiceResult)
    assert result.changed_states == [{"entity_id": "a"}]


@pytest.mark.asyncio
async def test_call_service_non_list_body_is_bad_response() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(json_value={"not": "a list"})
    result = await client.call_service("light", "turn_on")
    assert isinstance(result, HaError)
    assert result.reason == "bad_response"


@pytest.mark.asyncio
async def test_call_service_non_json_body_is_bad_response() -> None:
    client, mock_http = _client()
    resp = _ok_response()
    resp.json = MagicMock(side_effect=ValueError("no json"))
    mock_http.request.return_value = resp
    result = await client.call_service("light", "turn_on")
    assert isinstance(result, HaError)
    assert result.reason == "bad_response"


@pytest.mark.asyncio
async def test_call_service_connect_error_maps_unreachable() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = httpx.ConnectError("refused")
    result = await client.call_service("notify", "mobile_app_x", {"message": "hi"})
    assert isinstance(result, HaError)
    assert result.reason == "unreachable"


@pytest.mark.asyncio
async def test_call_service_500_is_http_error() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(status=_HTTP_SERVER_ERROR)
    result = await client.call_service("notify", "mobile_app_x", {"message": "hi"})
    assert isinstance(result, HaError)
    assert result.reason == "http_error"
    assert result.status == _HTTP_SERVER_ERROR


# ---- error mapping ----


@pytest.mark.asyncio
async def test_no_token_returns_auth_without_network_call() -> None:
    client, mock_http = _client(token=None)
    result = await client.get_config()
    assert isinstance(result, HaError)
    assert result.reason == "auth"
    assert result.message == "no token configured"
    mock_http.request.assert_not_called()


@pytest.mark.asyncio
async def test_401_maps_auth() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(status=_HTTP_UNAUTHORIZED)
    result = await client.get_config()
    assert isinstance(result, HaError)
    assert result.reason == "auth"
    assert result.status == _HTTP_UNAUTHORIZED


@pytest.mark.asyncio
async def test_403_maps_auth() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(status=_HTTP_FORBIDDEN)
    result = await client.get_states()
    assert isinstance(result, HaError)
    assert result.reason == "auth"
    assert result.status == _HTTP_FORBIDDEN


@pytest.mark.asyncio
async def test_502_maps_http_error() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(status=_HTTP_BAD_GATEWAY)
    result = await client.get_config()
    assert isinstance(result, HaError)
    assert result.reason == "http_error"
    assert result.status == _HTTP_BAD_GATEWAY


@pytest.mark.asyncio
async def test_connect_error_maps_unreachable() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = httpx.ConnectError("refused")
    result = await client.get_config()
    assert isinstance(result, HaError)
    assert result.reason == "unreachable"


@pytest.mark.asyncio
async def test_connect_timeout_maps_unreachable() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = httpx.ConnectTimeout("connect timed out")
    result = await client.get_config()
    assert isinstance(result, HaError)
    assert result.reason == "unreachable"


@pytest.mark.asyncio
async def test_read_timeout_maps_timeout() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = httpx.ReadTimeout("read timed out")
    result = await client.get_config()
    assert isinstance(result, HaError)
    assert result.reason == "timeout"


@pytest.mark.asyncio
async def test_generic_timeout_exception_maps_timeout() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = httpx.TimeoutException("pool timeout")
    result = await client.get_config()
    assert isinstance(result, HaError)
    assert result.reason == "timeout"


@pytest.mark.asyncio
async def test_get_json_non_json_body_is_bad_response() -> None:
    client, mock_http = _client()
    resp = _ok_response()
    resp.json = MagicMock(side_effect=ValueError("no json"))
    mock_http.request.return_value = resp
    result = await client.get_config()
    assert isinstance(result, HaError)
    assert result.reason == "bad_response"


# ---- token never leaks ----


@pytest.mark.asyncio
async def test_token_never_in_error_message_on_auth() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(status=_HTTP_UNAUTHORIZED)
    result = await client.get_config()
    assert isinstance(result, HaError)
    assert _TOKEN not in result.message


@pytest.mark.asyncio
async def test_token_never_in_error_message_on_unreachable() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = httpx.ConnectError(
        f"refused {_TOKEN}"
    )  # token in transport msg
    result = await client.get_config()
    assert isinstance(result, HaError)
    # Our mapped message is built from method+path only, never the transport exception text.
    assert _TOKEN not in result.message


# ---- base_url normalization ----


@pytest.mark.asyncio
async def test_base_url_trailing_slash_stripped() -> None:
    client, mock_http = _client(base_url="http://ha.local:8123/")
    mock_http.request.return_value = _ok_response(json_value={"version": "1", "time_zone": "UTC"})
    await client.get_config()
    assert mock_http.request.call_args.args[1] == "http://ha.local:8123/api/config"


# ---- load_ha_config ----


def test_load_ha_config_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_MONITOR_HA_URL", raising=False)
    cfg = load_ha_config()
    assert isinstance(cfg, HaConfig)
    assert cfg.base_url == "http://192.168.2.148:8123"


def test_load_ha_config_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_HA_URL", "http://10.0.0.5:8123")
    cfg = load_ha_config()
    assert cfg.base_url == "http://10.0.0.5:8123"


def test_load_ha_config_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_HA_URL", "http://10.0.0.5:8123/")
    cfg = load_ha_config()
    assert cfg.base_url == "http://10.0.0.5:8123"


def test_load_ha_config_notify_service_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_MONITOR_HA_NOTIFY_SERVICE", raising=False)
    cfg = load_ha_config()
    assert cfg.notify_service == ""


def test_load_ha_config_notify_service_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_HA_NOTIFY_SERVICE", "mobile_app_pixel")
    cfg = load_ha_config()
    assert cfg.notify_service == "mobile_app_pixel"
