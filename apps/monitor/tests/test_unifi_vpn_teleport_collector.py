"""Unit tests for UnifiVpnTeleportCollector (STAGE-007-014).

Covers: null client (not_configured), all 6 UnifiError reasons, non-dict
payload (bad_response), dict with bad rc (bad_response), rc ok + non-list data
(device_not_found), rc ok + empty list (device_not_found), rc ok + devices with
no teleport_version (not_initialized, incl. non-dict entries + empty-string tv),
rc ok + teleport_version found (ok/up=1 + version emitted),
_find_teleport_version skip branches (int tv, empty-string tv, non-dict entry).
"""

from __future__ import annotations

import pytest
import structlog

from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
    MetricEntry,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.kernel.unifi.client import UnifiResponse
from homelab_monitor.kernel.unifi.errors import UnifiError
from homelab_monitor.plugins.collectors.integrations.unifi.vpn_teleport import (
    _M_API_TOOK,  # pyright: ignore[reportPrivateUsage]
    _M_REASON,  # pyright: ignore[reportPrivateUsage]
    _M_UP,  # pyright: ignore[reportPrivateUsage]
    _M_VERSION,  # pyright: ignore[reportPrivateUsage]
    UnifiVpnTeleportCollector,
)

_DEVICE_ENDPOINT = "stat/device"


# --- assertion helpers -------------------------------------------------------
def _gauges(writer: InMemoryMetricsWriter, name: str) -> list[MetricEntry]:
    """Return all recorded gauge entries with the given metric name."""
    return [
        e
        for e in writer.recorded  # pyright: ignore[reportPrivateUsage]
        if e.kind == "gauge" and e.name == name
    ]


def _gauge_value(writer: InMemoryMetricsWriter, name: str, labels: dict[str, str]) -> float | None:
    """Return the value of the gauge matching name + exact labels, or None."""
    for e in writer.recorded:  # pyright: ignore[reportPrivateUsage]
        if e.kind == "gauge" and e.name == name and e.labels == labels:
            return e.value
    return None


# --- fake clients ------------------------------------------------------------
class _FakeUnifiBase:
    """Base fake UnifiClient: every method returns a stub UnifiError."""

    site_name: str = "default"
    v1_site_id: str = "fake-uuid"

    async def v1_sites(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def v1_devices(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def v1_device(self, device_id: str) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def v1_device_stats(self, device_id: str) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def v1_clients(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def stat_device(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def stat_sta(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def stat_alluser(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def stat_health(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def stat_dpi(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def stat_stadpi(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def rest_networkconf(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def rest_alarm(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def stat_sysinfo(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def resolve_site_id(self) -> UnifiError | None:
        return None


class _FakeDeviceOk(_FakeUnifiBase):
    """stat_device returns a UnifiResponse wrapping the given payload."""

    def __init__(self, payload: object, took: float = 0.05) -> None:
        self._payload = payload
        self._took = took

    async def stat_device(self) -> UnifiResponse | UnifiError:
        return UnifiResponse(
            payload=self._payload,
            took_seconds=self._took,
            endpoint=_DEVICE_ENDPOINT,
        )


class _FakeDeviceFail(_FakeUnifiBase):
    """stat_device returns a configured UnifiError."""

    def __init__(self, error: UnifiError) -> None:
        self._error = error

    async def stat_device(self) -> UnifiResponse | UnifiError:
        return self._error


def _ctx(writer: InMemoryMetricsWriter, unifi: object | None) -> CollectorContext:
    """Build a CollectorContext.

    CollectorConfig has extra='forbid' -- do NOT pass concurrency_group
    (it is a ClassVar, not a config field, and will raise pydantic
    ValidationError).
    """
    return CollectorContext(
        config=CollectorConfig(
            name="unifi_vpn_teleport",
            interval_seconds=60,
            timeout_seconds=15,
        ),
        db=None,  # type: ignore[arg-type]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="unifi_vpn_teleport"),
        unifi=unifi,  # type: ignore[arg-type]
    )


# =============================================================================
# Test 1: null client -> up=0, reason=not_configured, no api_took, ok=False
# =============================================================================
@pytest.mark.asyncio
async def test_none_client_emits_up_zero_not_configured() -> None:
    """ctx.unifi is None -> up=0, reason=not_configured, no api_took."""
    writer = InMemoryMetricsWriter()
    result = await UnifiVpnTeleportCollector().run(_ctx(writer, None))

    assert result.ok is False
    assert result.metrics_emitted == 2  # noqa: PLR2004
    assert result.errors == ["unifi client not configured"]
    assert _gauge_value(writer, _M_UP, {}) == 0.0
    assert _gauge_value(writer, _M_REASON, {"reason": "not_configured"}) == 1.0
    assert _gauges(writer, _M_API_TOOK) == []
    assert _gauges(writer, _M_VERSION) == []


# =============================================================================
# Tests 2-7: UnifiError for each reason -> up=0, reason=<reason>, no api_took
# =============================================================================
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    ["unreachable", "timeout", "auth", "rate_limited", "http_error", "bad_response"],
)
async def test_unifi_error_reason(reason: str) -> None:
    """UnifiError with any reason -> up=0, reason labeled, no api_took."""
    error = UnifiError(
        reason=reason,  # type: ignore[arg-type]
        message=f"stat/device {reason}",
    )
    writer = InMemoryMetricsWriter()
    result = await UnifiVpnTeleportCollector().run(_ctx(writer, _FakeDeviceFail(error)))

    assert result.ok is False
    assert result.metrics_emitted == 2  # noqa: PLR2004
    assert result.errors == [f"stat/device {reason}"]
    assert _gauge_value(writer, _M_UP, {}) == 0.0
    assert _gauge_value(writer, _M_REASON, {"reason": reason}) == 1.0
    assert _gauges(writer, _M_API_TOOK) == []


# =============================================================================
# Test 8: non-dict payload -> bad_response, api_took emitted
# =============================================================================
@pytest.mark.asyncio
async def test_non_dict_payload_bad_response() -> None:
    """payload is a list (not a dict) -> bad_response, api_took emitted."""
    payload: list[str] = ["x"]
    writer = InMemoryMetricsWriter()
    result = await UnifiVpnTeleportCollector().run(_ctx(writer, _FakeDeviceOk(payload, took=0.03)))

    assert result.ok is False
    assert result.metrics_emitted == 3  # noqa: PLR2004
    assert result.errors == ["unifi stat/device payload not a dict"]
    assert (
        _gauge_value(writer, _M_API_TOOK, {"endpoint": _DEVICE_ENDPOINT}) == 0.03  # noqa: PLR2004
    )
    assert _gauge_value(writer, _M_UP, {}) == 0.0
    assert _gauge_value(writer, _M_REASON, {"reason": "bad_response"}) == 1.0


# =============================================================================
# Test 9: dict payload, meta.rc != "ok" -> bad_response, api_took emitted
# NOTE: rc check runs BEFORE data extraction; data=[] here but rc fails first.
# =============================================================================
@pytest.mark.asyncio
async def test_response_bad_rc_emits_bad_response() -> None:
    """meta.rc != 'ok' -> bad_response (rc checked before data),
    api_took emitted."""
    payload: dict[str, object] = {"meta": {"rc": "error"}, "data": []}
    writer = InMemoryMetricsWriter()
    result = await UnifiVpnTeleportCollector().run(_ctx(writer, _FakeDeviceOk(payload, took=0.02)))

    assert result.ok is False
    assert result.metrics_emitted == 3  # noqa: PLR2004
    assert result.errors == ["unifi stat/device meta.rc not ok"]
    assert (
        _gauge_value(writer, _M_API_TOOK, {"endpoint": _DEVICE_ENDPOINT}) == 0.02  # noqa: PLR2004
    )
    assert _gauge_value(writer, _M_UP, {}) == 0.0
    assert _gauge_value(writer, _M_REASON, {"reason": "bad_response"}) == 1.0


# =============================================================================
# Test 10: rc ok, data not a list -> device_not_found
# =============================================================================
@pytest.mark.asyncio
async def test_rc_ok_data_not_a_list_device_not_found() -> None:
    """rc ok + data is a string (not list) -> device_not_found."""
    payload: dict[str, object] = {"meta": {"rc": "ok"}, "data": "nope"}
    writer = InMemoryMetricsWriter()
    result = await UnifiVpnTeleportCollector().run(_ctx(writer, _FakeDeviceOk(payload)))

    assert result.ok is False
    assert result.metrics_emitted == 3  # noqa: PLR2004
    assert result.errors == ["unifi stat/device returned no devices"]
    assert _gauge_value(writer, _M_API_TOOK, {"endpoint": _DEVICE_ENDPOINT}) is not None
    assert _gauge_value(writer, _M_UP, {}) == 0.0
    assert _gauge_value(writer, _M_REASON, {"reason": "device_not_found"}) == 1.0


# =============================================================================
# Test 11: rc ok, data == [] -> device_not_found
# =============================================================================
@pytest.mark.asyncio
async def test_rc_ok_empty_data_list_device_not_found() -> None:
    """rc ok + data == [] -> device_not_found."""
    payload: dict[str, object] = {"meta": {"rc": "ok"}, "data": []}
    writer = InMemoryMetricsWriter()
    result = await UnifiVpnTeleportCollector().run(_ctx(writer, _FakeDeviceOk(payload)))

    assert result.ok is False
    assert result.metrics_emitted == 3  # noqa: PLR2004
    assert result.errors == ["unifi stat/device returned no devices"]
    assert _gauge_value(writer, _M_API_TOOK, {"endpoint": _DEVICE_ENDPOINT}) is not None
    assert _gauge_value(writer, _M_UP, {}) == 0.0
    assert _gauge_value(writer, _M_REASON, {"reason": "device_not_found"}) == 1.0


# =============================================================================
# Test 12: rc ok, devices present but none has teleport_version -> not_initialized
# Includes a non-dict entry (42) to cover the isinstance(entry,dict) skip branch
# in _find_teleport_version, and a regular dict with no tv field.
# =============================================================================
@pytest.mark.asyncio
async def test_rc_ok_no_teleport_version_not_initialized() -> None:
    """Devices present but no teleport_version -> not_initialized.

    data=[42, {"name":"sw1"}]: the int entry exercises the non-dict skip;
    the dict entry exercises the absent-tv skip.
    """
    payload: dict[str, object] = {
        "meta": {"rc": "ok"},
        "data": [42, {"name": "sw1"}],
    }
    writer = InMemoryMetricsWriter()
    result = await UnifiVpnTeleportCollector().run(_ctx(writer, _FakeDeviceOk(payload)))

    assert result.ok is False
    assert result.metrics_emitted == 3  # noqa: PLR2004
    assert result.errors == ["unifi gateway reports no teleport_version"]
    assert _gauge_value(writer, _M_API_TOOK, {"endpoint": _DEVICE_ENDPOINT}) is not None
    assert _gauge_value(writer, _M_UP, {}) == 0.0
    assert _gauge_value(writer, _M_REASON, {"reason": "not_initialized"}) == 1.0
    assert _gauges(writer, _M_VERSION) == []


# =============================================================================
# Test 13: rc ok, one device has teleport_version -> ok, up=1, version emitted
# =============================================================================
@pytest.mark.asyncio
async def test_rc_ok_teleport_version_found() -> None:
    """A device with non-empty teleport_version -> up=1, reason=ok,
    version emitted."""
    payload: dict[str, object] = {
        "meta": {"rc": "ok"},
        "data": [{"name": "sw1"}, {"teleport_version": "1"}],
    }
    writer = InMemoryMetricsWriter()
    result = await UnifiVpnTeleportCollector().run(_ctx(writer, _FakeDeviceOk(payload, took=0.05)))

    assert result.ok is True
    assert result.metrics_emitted == 4  # noqa: PLR2004
    assert result.errors == []
    assert (
        _gauge_value(writer, _M_API_TOOK, {"endpoint": _DEVICE_ENDPOINT}) == 0.05  # noqa: PLR2004
    )
    assert _gauge_value(writer, _M_UP, {}) == 1.0
    assert _gauge_value(writer, _M_REASON, {"reason": "ok"}) == 1.0
    assert _gauge_value(writer, _M_VERSION, {"version": "1"}) == 1.0


# =============================================================================
# Test 14: _find_teleport_version skip branches -- int tv, empty-string tv, then
# a valid string "2". Verifies the isinstance(tv,str) false branch AND the
# empty-string false branch AND continue-to-next behavior.
# =============================================================================
@pytest.mark.asyncio
async def test_find_teleport_version_skips_int_and_empty_string() -> None:
    """int tv and empty-string tv are skipped; the third entry's "2" is used."""
    payload: dict[str, object] = {
        "meta": {"rc": "ok"},
        "data": [
            {"teleport_version": 1},  # int -> skip (isinstance(tv, str) is False)
            {"teleport_version": ""},  # empty string -> skip (not tv)
            {"teleport_version": "2"},  # first valid -> returned
        ],
    }
    writer = InMemoryMetricsWriter()
    result = await UnifiVpnTeleportCollector().run(_ctx(writer, _FakeDeviceOk(payload)))

    assert result.ok is True
    assert result.metrics_emitted == 4  # noqa: PLR2004
    assert _gauge_value(writer, _M_VERSION, {"version": "2"}) == 1.0
    assert _gauge_value(writer, _M_UP, {}) == 1.0
    assert _gauge_value(writer, _M_REASON, {"reason": "ok"}) == 1.0
