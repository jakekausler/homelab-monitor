"""Tests for UnifiRestClient + UnifiError (STAGE-007-001).

Covers: every UnifiError mapping (no-key auth, 401/403 auth, 429 rate_limited,
500 http_error, connect -> unreachable, timeout -> timeout, bad JSON ->
bad_response); the X-API-KEY header; UnifiResponse payload/took/endpoint; v1 vs
classic URL construction; eager non-fatal resolve_site_id (success + every
shape-mismatch branch); the key never leaking into a UnifiError.message;
load_unifi_config default / env override / trailing-slash strip / site_id.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from homelab_monitor.kernel.config import UnifiConfig, load_unifi_config
from homelab_monitor.kernel.plugins.io import UnifiClient
from homelab_monitor.kernel.unifi.client import UnifiResponse, UnifiRestClient
from homelab_monitor.kernel.unifi.errors import UnifiError

_KEY = "super-secret-unifi-api-key-xyz"
_HTTP_OK = 200
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_HTTP_TOO_MANY = 429
_HTTP_SERVER_ERROR = 500
_BASE = "https://192.168.2.1"


def _ok_response(*, json_value: object = None, status: int = _HTTP_OK) -> AsyncMock:
    """Build a mocked httpx.Response with the given json()/status."""
    resp = AsyncMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=json_value)
    return resp


def _client(
    key: str | None = _KEY, base_url: str = _BASE, site_id: str = "default"
) -> tuple[UnifiRestClient, AsyncMock]:
    """Build a client with a mocked dedicated httpx client; return (client, mock_http)."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    client = UnifiRestClient(
        base_url=base_url, http=mock_http, key_provider=lambda: key, site_id=site_id
    )
    return client, mock_http


# ---- conformance ----


def test_client_satisfies_protocol() -> None:
    """UnifiRestClient structurally satisfies the UnifiClient Protocol."""
    client, _ = _client()
    assert isinstance(client, UnifiClient)


# ---- success: v1 + classic URL construction + UnifiResponse ----


@pytest.mark.asyncio
async def test_v1_sites_success_returns_unifi_response() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(json_value={"data": [{"id": "default"}]})
    result = await client.v1_sites()
    assert isinstance(result, UnifiResponse)
    assert result.payload == {"data": [{"id": "default"}]}
    assert result.took_seconds >= 0.0
    assert result.endpoint == "v1/sites"
    call = mock_http.request.call_args
    assert call.args[0] == "GET"
    assert call.args[1] == "https://192.168.2.1/proxy/network/integrations/v1/sites"
    assert call.kwargs["headers"]["X-API-KEY"] == _KEY


@pytest.mark.asyncio
async def test_v1_devices_threads_site_id() -> None:
    client, mock_http = _client(site_id="abc123")
    mock_http.request.return_value = _ok_response(json_value={"data": []})
    await client.v1_devices()
    assert (
        mock_http.request.call_args.args[1]
        == "https://192.168.2.1/proxy/network/integrations/v1/sites/abc123/devices"
    )


@pytest.mark.asyncio
async def test_v1_clients_threads_site_id() -> None:
    client, mock_http = _client(site_id="abc123")
    mock_http.request.return_value = _ok_response(json_value={"data": []})
    await client.v1_clients()
    assert (
        mock_http.request.call_args.args[1]
        == "https://192.168.2.1/proxy/network/integrations/v1/sites/abc123/clients"
    )


@pytest.mark.asyncio
async def test_v1_device_builds_url() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(json_value={})
    result = await client.v1_device("dev-1")
    assert isinstance(result, UnifiResponse)
    assert result.endpoint == "v1/device"
    assert (
        mock_http.request.call_args.args[1]
        == "https://192.168.2.1/proxy/network/integrations/v1/devices/dev-1"
    )


@pytest.mark.asyncio
async def test_v1_device_stats_builds_url() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(json_value={})
    result = await client.v1_device_stats("dev-1")
    assert isinstance(result, UnifiResponse)
    assert result.endpoint == "v1/device_stats"
    assert (
        mock_http.request.call_args.args[1]
        == "https://192.168.2.1/proxy/network/integrations/v1/devices/dev-1/statistics/latest"
    )


@pytest.mark.asyncio
async def test_stat_sysinfo_builds_classic_url() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(json_value={"data": []})
    result = await client.stat_sysinfo()
    assert isinstance(result, UnifiResponse)
    assert result.endpoint == "stat/sysinfo"
    assert (
        mock_http.request.call_args.args[1]
        == "https://192.168.2.1/proxy/network/api/s/default/stat/sysinfo"
    )


@pytest.mark.asyncio
async def test_rest_alarm_classic_url_includes_query() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(json_value={"data": []})
    result = await client.rest_alarm()
    assert isinstance(result, UnifiResponse)
    assert (
        mock_http.request.call_args.args[1]
        == "https://192.168.2.1/proxy/network/api/s/default/rest/alarm?archived=false"
    )


@pytest.mark.asyncio
async def test_remaining_classic_helpers_build_urls() -> None:
    """Exercise the remaining classic helpers so their one-liners are covered."""
    cases: list[tuple[Callable[[UnifiRestClient], Awaitable[UnifiResponse | UnifiError]], str]] = [
        (lambda c: c.stat_device(), "stat/device"),
        (lambda c: c.stat_sta(), "stat/sta"),
        (lambda c: c.stat_alluser(), "stat/alluser"),
        (lambda c: c.stat_health(), "stat/health"),
        (lambda c: c.stat_stadpi(), "stat/stadpi"),
        (lambda c: c.rest_networkconf(), "rest/networkconf"),
    ]
    for call, ep in cases:
        client, mock_http = _client()
        mock_http.request.return_value = _ok_response(json_value={"data": []})
        result: UnifiResponse | UnifiError = await call(client)
        assert isinstance(result, UnifiResponse)
        assert result.endpoint == ep
        assert mock_http.request.call_args.args[1] == (
            f"https://192.168.2.1/proxy/network/api/s/default/{ep}"
        )


@pytest.mark.asyncio
async def test_took_seconds_is_float() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(json_value={"data": []})
    result = await client.stat_sysinfo()
    assert isinstance(result, UnifiResponse)
    assert isinstance(result.took_seconds, float)
    assert result.took_seconds >= 0.0


@pytest.mark.asyncio
async def test_v2_traffic_success_returns_bare_object() -> None:
    """v2_traffic returns a bare object (no {meta,data} envelope)."""
    client, mock_http = _client()
    payload = {
        "client_usage_by_app": [
            {
                "client": {"mac": "aa:bb:cc:dd:ee:ff"},
                "usage_by_app": [{"application": 7, "category": 13, "total_bytes": 1000}],
            }
        ],
        "total_usage_by_app": [],
    }
    mock_http.request.return_value = _ok_response(json_value=payload)
    result = await client.v2_traffic(1000, 2000)
    assert isinstance(result, UnifiResponse)
    assert result.payload == payload
    assert result.endpoint == "v2/traffic"
    # Verify URL includes v2 prefix, site name, and query params.
    call_url = mock_http.request.call_args.args[1]
    assert "/proxy/network/v2/api/site/default/traffic?" in call_url
    assert "start=1000" in call_url
    assert "end=2000" in call_url
    assert call_url.startswith("https://192.168.2.1")


@pytest.mark.asyncio
async def test_v2_traffic_empty_clients() -> None:
    """v2_traffic with empty client_usage_by_app returns bare object."""
    client, mock_http = _client()
    payload: dict[str, object] = {"client_usage_by_app": [], "total_usage_by_app": []}
    mock_http.request.return_value = _ok_response(json_value=payload)
    result = await client.v2_traffic(1000, 2000)
    assert isinstance(result, UnifiResponse)
    assert result.payload == payload


@pytest.mark.asyncio
async def test_v2_traffic_http_error() -> None:
    """v2_traffic 403 maps to auth UnifiError."""
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(json_value={}, status=_HTTP_FORBIDDEN)
    result = await client.v2_traffic(1000, 2000)
    assert isinstance(result, UnifiError)
    assert result.reason == "auth"


# ---- error mapping ----


@pytest.mark.asyncio
async def test_no_key_returns_auth_without_network_call() -> None:
    client, mock_http = _client(key=None)
    result = await client.v1_sites()
    assert isinstance(result, UnifiError)
    assert result.reason == "auth"
    assert result.status is None
    mock_http.request.assert_not_called()


@pytest.mark.asyncio
async def test_401_maps_auth() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(status=_HTTP_UNAUTHORIZED)
    result = await client.stat_sysinfo()
    assert isinstance(result, UnifiError)
    assert result.reason == "auth"
    assert result.status == _HTTP_UNAUTHORIZED


@pytest.mark.asyncio
async def test_403_maps_auth() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(status=_HTTP_FORBIDDEN)
    result = await client.stat_sysinfo()
    assert isinstance(result, UnifiError)
    assert result.reason == "auth"
    assert result.status == _HTTP_FORBIDDEN


@pytest.mark.asyncio
async def test_429_maps_rate_limited() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(status=_HTTP_TOO_MANY)
    result = await client.stat_sysinfo()
    assert isinstance(result, UnifiError)
    assert result.reason == "rate_limited"
    assert result.status == _HTTP_TOO_MANY


@pytest.mark.asyncio
async def test_500_maps_http_error() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(status=_HTTP_SERVER_ERROR)
    result = await client.stat_sysinfo()
    assert isinstance(result, UnifiError)
    assert result.reason == "http_error"
    assert result.status == _HTTP_SERVER_ERROR


@pytest.mark.asyncio
async def test_connect_error_maps_unreachable() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = httpx.ConnectError("refused")
    result = await client.stat_sysinfo()
    assert isinstance(result, UnifiError)
    assert result.reason == "unreachable"


@pytest.mark.asyncio
async def test_connect_timeout_maps_unreachable() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = httpx.ConnectTimeout("connect timed out")
    result = await client.stat_sysinfo()
    assert isinstance(result, UnifiError)
    assert result.reason == "unreachable"


@pytest.mark.asyncio
async def test_read_timeout_maps_timeout() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = httpx.ReadTimeout("read timed out")
    result = await client.stat_sysinfo()
    assert isinstance(result, UnifiError)
    assert result.reason == "timeout"


@pytest.mark.asyncio
async def test_generic_timeout_exception_maps_timeout() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = httpx.TimeoutException("pool timeout")
    result = await client.stat_sysinfo()
    assert isinstance(result, UnifiError)
    assert result.reason == "timeout"


@pytest.mark.asyncio
async def test_non_json_body_is_bad_response() -> None:
    client, mock_http = _client()
    resp = _ok_response()
    resp.json = MagicMock(side_effect=ValueError("no json"))
    mock_http.request.return_value = resp
    result = await client.stat_sysinfo()
    assert isinstance(result, UnifiError)
    assert result.reason == "bad_response"


# ---- key never leaks ----


@pytest.mark.asyncio
async def test_key_never_in_error_message_on_auth() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(status=_HTTP_UNAUTHORIZED)
    result = await client.stat_sysinfo()
    assert isinstance(result, UnifiError)
    assert _KEY not in result.message


@pytest.mark.asyncio
async def test_key_never_in_error_message_on_no_key() -> None:
    client, _ = _client(key=None)
    result = await client.stat_sysinfo()
    assert isinstance(result, UnifiError)
    assert _KEY not in result.message


@pytest.mark.asyncio
async def test_key_never_in_error_message_on_unreachable() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = httpx.ConnectError(f"refused {_KEY}")
    result = await client.stat_sysinfo()
    assert isinstance(result, UnifiError)
    assert _KEY not in result.message


# ---- base_url normalization ----


@pytest.mark.asyncio
async def test_base_url_trailing_slash_stripped() -> None:
    client, mock_http = _client(base_url="https://192.168.2.1/")
    mock_http.request.return_value = _ok_response(json_value={"data": []})
    await client.stat_sysinfo()
    assert (
        mock_http.request.call_args.args[1]
        == "https://192.168.2.1/proxy/network/api/s/default/stat/sysinfo"
    )


# ---- resolve_site_id ----


@pytest.mark.asyncio
async def test_resolve_site_id_success_caches_and_returns_none() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(
        json_value={"data": [{"id": "site-7"}, {"id": "other"}]}
    )
    result = await client.resolve_site_id()
    assert result is None
    assert client.v1_site_id == "site-7"
    assert client.site_name == "default"


@pytest.mark.asyncio
async def test_resolve_site_id_v1_error_passthrough_leaves_site_id() -> None:
    client, mock_http = _client()
    mock_http.request.side_effect = httpx.ConnectError("refused")
    result = await client.resolve_site_id()
    assert isinstance(result, UnifiError)
    assert result.reason == "unreachable"
    assert client.v1_site_id == "default"


@pytest.mark.asyncio
async def test_resolve_site_id_non_object_payload_is_bad_response() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(json_value=["not", "an", "object"])
    result = await client.resolve_site_id()
    assert isinstance(result, UnifiError)
    assert result.reason == "bad_response"
    assert client.v1_site_id == "default"


@pytest.mark.asyncio
async def test_resolve_site_id_data_not_list_is_bad_response() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(json_value={"data": {"not": "a list"}})
    result = await client.resolve_site_id()
    assert isinstance(result, UnifiError)
    assert result.reason == "bad_response"
    assert client.v1_site_id == "default"


@pytest.mark.asyncio
async def test_resolve_site_id_empty_data_is_bad_response() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(json_value={"data": []})
    result = await client.resolve_site_id()
    assert isinstance(result, UnifiError)
    assert result.reason == "bad_response"
    assert client.v1_site_id == "default"


@pytest.mark.asyncio
async def test_resolve_site_id_entry_not_object_is_bad_response() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(json_value={"data": ["not-an-object"]})
    result = await client.resolve_site_id()
    assert isinstance(result, UnifiError)
    assert result.reason == "bad_response"
    assert client.v1_site_id == "default"


@pytest.mark.asyncio
async def test_resolve_site_id_id_not_string_is_bad_response() -> None:
    client, mock_http = _client()
    mock_http.request.return_value = _ok_response(json_value={"data": [{"id": 123}]})
    result = await client.resolve_site_id()
    assert isinstance(result, UnifiError)
    assert result.reason == "bad_response"
    assert client.v1_site_id == "default"


@pytest.mark.asyncio
async def test_classic_url_uses_site_name_not_resolved_v1_uuid() -> None:
    # REGRESSION: classic API needs the short site NAME, not the v1 UUID (live 3b, STAGE-007-001)
    client, mock_http = _client()
    # First call: resolve_site_id — returns v1/sites payload with a UUID-like id
    mock_http.request.return_value = _ok_response(
        json_value={"data": [{"id": "88f7af54-uuid"}, {"id": "other"}]}
    )
    resolve_result: UnifiResponse | UnifiError | None = await client.resolve_site_id()
    assert resolve_result is None
    assert client.v1_site_id == "88f7af54-uuid"
    assert client.site_name == "default"
    # Second call: stat_sysinfo — classic URL must use the short site NAME, not the UUID
    mock_http.request.return_value = _ok_response(json_value={"data": []})
    stat_result: UnifiResponse | UnifiError = await client.stat_sysinfo()
    assert isinstance(stat_result, UnifiResponse)
    classic_url: str = mock_http.request.call_args.args[1]
    assert "/api/s/default/stat/sysinfo" in classic_url
    assert "88f7af54-uuid" not in classic_url


@pytest.mark.asyncio
async def test_v1_devices_uses_resolved_uuid_after_resolution() -> None:
    # Completeness: v1 site-scoped helpers adopt the resolved UUID post-resolution
    # (code review, STAGE-007-001)
    client, mock_http = _client()
    # First call: resolve_site_id — returns v1/sites payload with a UUID-like id
    mock_http.request.return_value = _ok_response(
        json_value={"data": [{"id": "88f7af54-uuid"}, {"id": "other"}]}
    )
    resolve_result: UnifiResponse | UnifiError | None = await client.resolve_site_id()
    assert resolve_result is None
    assert client.v1_site_id == "88f7af54-uuid"
    # Second call: v1_devices — must use the resolved UUID, NOT the original "default"
    mock_http.request.return_value = _ok_response(json_value={"data": []})
    devices_result: UnifiResponse | UnifiError = await client.v1_devices()
    assert isinstance(devices_result, UnifiResponse)
    v1_url: str = mock_http.request.call_args.args[1]
    assert "/sites/88f7af54-uuid/devices" in v1_url
    assert "/sites/default/devices" not in v1_url


# ---- load_unifi_config ----


def test_load_unifi_config_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_MONITOR_UNIFI_URL", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_UNIFI_SITE_ID", raising=False)
    cfg = load_unifi_config()
    assert isinstance(cfg, UnifiConfig)
    assert cfg.base_url == "https://192.168.2.1"
    assert cfg.site_id == "default"


def test_load_unifi_config_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_UNIFI_URL", "https://10.0.0.1")
    cfg = load_unifi_config()
    assert cfg.base_url == "https://10.0.0.1"


def test_load_unifi_config_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_UNIFI_URL", "https://10.0.0.1/")
    cfg = load_unifi_config()
    assert cfg.base_url == "https://10.0.0.1"


def test_load_unifi_config_site_id_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_UNIFI_SITE_ID", "custom-site")
    cfg = load_unifi_config()
    assert cfg.site_id == "custom-site"


def test_load_unifi_config_host_lan_ip_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_MONITOR_UNIFI_HOST_LAN_IP", raising=False)
    cfg = load_unifi_config()
    assert cfg.host_lan_ip == "192.168.2.148"


def test_load_unifi_config_host_lan_ip_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_UNIFI_HOST_LAN_IP", "10.0.0.5")
    cfg = load_unifi_config()
    assert cfg.host_lan_ip == "10.0.0.5"


def test_load_unifi_config_ssh_lease_enabled_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_MONITOR_UNIFI_SSH_LEASE_ENABLED", raising=False)
    cfg = load_unifi_config()
    assert cfg.ssh_lease_enabled is False


def test_load_unifi_config_ssh_lease_enabled_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_UNIFI_SSH_LEASE_ENABLED", "true")
    cfg = load_unifi_config()
    assert cfg.ssh_lease_enabled is True


def test_load_unifi_config_ssh_lease_enabled_truthy_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for value in ("1", "TRUE", "Yes"):
        monkeypatch.setenv("HOMELAB_MONITOR_UNIFI_SSH_LEASE_ENABLED", value)
        cfg = load_unifi_config()
        assert cfg.ssh_lease_enabled is True, value


def test_load_unifi_config_ssh_lease_enabled_falsey_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A non-truthy env value (set but not in the truthy set) parses to False.
    monkeypatch.setenv("HOMELAB_MONITOR_UNIFI_SSH_LEASE_ENABLED", "nope")
    cfg = load_unifi_config()
    assert cfg.ssh_lease_enabled is False


def test_load_unifi_config_ssh_lease_target_id_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset env -> the default target id 'udm'."""
    monkeypatch.delenv("HOMELAB_MONITOR_UNIFI_SSH_LEASE_TARGET_ID", raising=False)
    cfg = load_unifi_config()
    assert cfg.ssh_lease_target_id == "udm"


def test_load_unifi_config_ssh_lease_target_id_blank_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitespace-only env -> the default target id (empty branch)."""
    monkeypatch.setenv("HOMELAB_MONITOR_UNIFI_SSH_LEASE_TARGET_ID", "   ")
    cfg = load_unifi_config()
    assert cfg.ssh_lease_target_id == "udm"


def test_load_unifi_config_ssh_lease_target_id_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set env -> stripped override value."""
    monkeypatch.setenv("HOMELAB_MONITOR_UNIFI_SSH_LEASE_TARGET_ID", "  synology  ")
    cfg = load_unifi_config()
    assert cfg.ssh_lease_target_id == "synology"


_DEFAULT_OBSERVATION_RETENTION_DAYS = 90
_OVERRIDE_OBSERVATION_RETENTION_DAYS = 30


def test_load_unifi_config_observation_retention_days_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HOMELAB_MONITOR_UNIFI_OBSERVATION_RETENTION_DAYS", raising=False)
    cfg = load_unifi_config()
    assert cfg.observation_retention_days == _DEFAULT_OBSERVATION_RETENTION_DAYS


def test_load_unifi_config_observation_retention_days_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_UNIFI_OBSERVATION_RETENTION_DAYS", "30")
    cfg = load_unifi_config()
    assert cfg.observation_retention_days == _OVERRIDE_OBSERVATION_RETENTION_DAYS
