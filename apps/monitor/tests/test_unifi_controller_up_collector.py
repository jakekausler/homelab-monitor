"""Unit tests for UnifiControllerUpCollector (STAGE-007-013).

Covers: null client (not_configured), all 6 UnifiError reasons, non-dict
payload (bad_response), UnifiResponse with meta.rc != "ok" (bad_response),
dict payload missing meta (bad_response), rc ok + empty/non-list/no-dict data
(empty_data), rc ok + non-empty data (ok/up=1).
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
from homelab_monitor.plugins.collectors.integrations.unifi.controller_up import (
    _M_API_TOOK,  # pyright: ignore[reportPrivateUsage]
    _M_UP,  # pyright: ignore[reportPrivateUsage]
    _M_UP_REASON,  # pyright: ignore[reportPrivateUsage]
    UnifiControllerUpCollector,
)

_SYSINFO_ENDPOINT = "stat/sysinfo"


# --- assertion helpers (mirror test_unifi_alarms_collector.py) ---------------
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


# --- fake clients (mirror _FakeUnifiBase from test_unifi_alarms_collector.py) -
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

    async def stat_stadpi(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def rest_networkconf(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def rest_alarm(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def stat_sysinfo(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def v2_traffic(self, start_ms: int, end_ms: int) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def resolve_site_id(self) -> UnifiError | None:
        return None


class _FakeSysinfoOk(_FakeUnifiBase):
    """stat_sysinfo returns a UnifiResponse wrapping the given payload."""

    def __init__(self, payload: object, took: float = 0.042) -> None:
        self._payload = payload
        self._took = took

    async def stat_sysinfo(self) -> UnifiResponse | UnifiError:
        return UnifiResponse(
            payload=self._payload,
            took_seconds=self._took,
            endpoint=_SYSINFO_ENDPOINT,
        )


class _FakeSysinfoFail(_FakeUnifiBase):
    """stat_sysinfo returns a configured UnifiError."""

    def __init__(self, error: UnifiError) -> None:
        self._error = error

    async def stat_sysinfo(self) -> UnifiResponse | UnifiError:
        return self._error


def _ctx(writer: InMemoryMetricsWriter, unifi: object | None) -> CollectorContext:
    """Build a CollectorContext. Mirrors the alarms test _ctx().

    CollectorConfig has extra='forbid' -- do NOT pass concurrency_group
    (it is a ClassVar, not a config field, and will raise pydantic
    ValidationError).
    """
    return CollectorContext(
        config=CollectorConfig(
            name="unifi_controller_up",
            interval_seconds=30,
            timeout_seconds=10,
        ),
        db=None,  # type: ignore[arg-type]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="unifi_controller_up"),
        unifi=unifi,  # type: ignore[arg-type]
    )


# =============================================================================
# Test 1: null client -> up=0, up_reason not_configured, no api_took, ok=False
# =============================================================================
@pytest.mark.asyncio
async def test_none_client_emits_up_zero_not_configured() -> None:
    """ctx.unifi is None -> up=0, up_reason reason=not_configured, no api_took."""
    writer = InMemoryMetricsWriter()
    result = await UnifiControllerUpCollector().run(_ctx(writer, None))

    assert result.ok is False
    assert result.metrics_emitted == 2  # noqa: PLR2004
    assert result.errors == ["unifi client not configured"]
    assert _gauge_value(writer, _M_UP, {}) == 0.0
    assert _gauge_value(writer, _M_UP_REASON, {"reason": "not_configured"}) == 1.0
    assert _gauges(writer, _M_API_TOOK) == []


# =============================================================================
# Tests 2-7: UnifiError for each reason -> up=0, up_reason=<reason>, no api_took
# =============================================================================
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    ["unreachable", "timeout", "auth", "rate_limited", "http_error", "bad_response"],
)
async def test_unifi_error_reason(reason: str) -> None:
    """UnifiError with any reason -> up=0, up_reason labels reason, no api_took."""
    error = UnifiError(
        reason=reason,  # type: ignore[arg-type]
        message=f"sysinfo {reason}",
    )
    writer = InMemoryMetricsWriter()
    result = await UnifiControllerUpCollector().run(_ctx(writer, _FakeSysinfoFail(error)))

    assert result.ok is False
    assert result.metrics_emitted == 2  # noqa: PLR2004
    assert result.errors == [f"sysinfo {reason}"]
    assert _gauge_value(writer, _M_UP, {}) == 0.0
    assert _gauge_value(writer, _M_UP_REASON, {"reason": reason}) == 1.0
    assert _gauges(writer, _M_API_TOOK) == []


# =============================================================================
# Test 8: UnifiResponse with meta.rc != "ok" -> bad_response, api_took emitted
# =============================================================================
@pytest.mark.asyncio
async def test_response_bad_rc_emits_bad_response() -> None:
    """meta.rc != 'ok' -> bad_response, up=0, api_took emitted, ok=False."""
    payload: dict[str, object] = {"meta": {"rc": "error"}, "data": []}
    writer = InMemoryMetricsWriter()
    result = await UnifiControllerUpCollector().run(
        _ctx(writer, _FakeSysinfoOk(payload, took=0.021))
    )

    assert result.ok is False
    assert result.metrics_emitted == 3  # noqa: PLR2004
    assert result.errors == ["unifi sysinfo meta.rc not ok"]
    assert (
        _gauge_value(writer, _M_API_TOOK, {"endpoint": _SYSINFO_ENDPOINT}) == 0.021  # noqa: PLR2004
    )
    assert _gauge_value(writer, _M_UP, {}) == 0.0
    assert _gauge_value(writer, _M_UP_REASON, {"reason": "bad_response"}) == 1.0


# =============================================================================
# Test 9: rc ok + empty data -> empty_data, api_took emitted, ok=False
# =============================================================================
@pytest.mark.asyncio
async def test_response_rc_ok_empty_data() -> None:
    """meta.rc == 'ok' + data == [] -> empty_data, up=0, api_took emitted."""
    payload: dict[str, object] = {"meta": {"rc": "ok"}, "data": []}
    writer = InMemoryMetricsWriter()
    result = await UnifiControllerUpCollector().run(_ctx(writer, _FakeSysinfoOk(payload)))

    assert result.ok is False
    assert result.metrics_emitted == 3  # noqa: PLR2004
    assert result.errors == ["unifi sysinfo returned no data"]
    assert _gauge_value(writer, _M_API_TOOK, {"endpoint": _SYSINFO_ENDPOINT}) is not None
    assert _gauge_value(writer, _M_UP, {}) == 0.0
    assert _gauge_value(writer, _M_UP_REASON, {"reason": "empty_data"}) == 1.0


# =============================================================================
# Test 10: rc ok + non-empty data -> ok=True, up=1, reason=ok, api_took emitted
# =============================================================================
@pytest.mark.asyncio
async def test_response_rc_ok_with_data_up_one() -> None:
    """rc ok + non-empty data -> up=1, reason=ok, api_took=took, ok=True."""
    payload: dict[str, object] = {
        "meta": {"rc": "ok"},
        "data": [{"version": "10.4.57", "uptime": 1071507}],
    }
    writer = InMemoryMetricsWriter()
    result = await UnifiControllerUpCollector().run(
        _ctx(writer, _FakeSysinfoOk(payload, took=0.042))
    )

    assert result.ok is True
    assert result.metrics_emitted == 3  # noqa: PLR2004
    assert result.errors == []
    assert (
        _gauge_value(writer, _M_API_TOOK, {"endpoint": _SYSINFO_ENDPOINT}) == 0.042  # noqa: PLR2004
    )
    assert _gauge_value(writer, _M_UP, {}) == 1.0
    assert _gauge_value(writer, _M_UP_REASON, {"reason": "ok"}) == 1.0


# =============================================================================
# Test 11: non-dict payload -> rc_ok=False -> bad_response, api_took emitted
# =============================================================================
@pytest.mark.asyncio
async def test_non_dict_payload_bad_response() -> None:
    """payload is a list (not a dict) -> meta can't be read -> bad_response."""
    payload: list[str] = ["not", "a", "dict"]
    writer = InMemoryMetricsWriter()
    result = await UnifiControllerUpCollector().run(_ctx(writer, _FakeSysinfoOk(payload)))

    assert result.ok is False
    assert result.metrics_emitted == 3  # noqa: PLR2004
    assert result.errors == ["unifi sysinfo payload not a dict"]
    assert _gauge_value(writer, _M_API_TOOK, {"endpoint": _SYSINFO_ENDPOINT}) is not None
    assert _gauge_value(writer, _M_UP, {}) == 0.0
    assert _gauge_value(writer, _M_UP_REASON, {"reason": "bad_response"}) == 1.0


# =============================================================================
# Test 12: dict payload, meta missing -> bad_response
# =============================================================================
@pytest.mark.asyncio
async def test_dict_payload_no_meta_bad_response() -> None:
    """dict payload with no 'meta' key -> rc_ok=False -> bad_response."""
    payload: dict[str, object] = {"data": [{"version": "10.4.57"}]}
    writer = InMemoryMetricsWriter()
    result = await UnifiControllerUpCollector().run(_ctx(writer, _FakeSysinfoOk(payload)))

    assert result.ok is False
    assert _gauge_value(writer, _M_UP_REASON, {"reason": "bad_response"}) == 1.0


# =============================================================================
# Test 13: rc ok + non-list data -> _record_count 0 -> empty_data
# =============================================================================
@pytest.mark.asyncio
async def test_rc_ok_data_not_a_list_empty_data() -> None:
    """meta.rc == 'ok' + data not a list -> _record_count 0 -> empty_data."""
    payload: dict[str, object] = {"meta": {"rc": "ok"}, "data": "nope"}
    writer = InMemoryMetricsWriter()
    result = await UnifiControllerUpCollector().run(_ctx(writer, _FakeSysinfoOk(payload)))

    assert result.ok is False
    assert result.metrics_emitted == 3  # noqa: PLR2004
    assert result.errors == ["unifi sysinfo returned no data"]
    assert _gauge_value(writer, _M_UP, {}) == 0.0
    assert _gauge_value(writer, _M_UP_REASON, {"reason": "empty_data"}) == 1.0


# =============================================================================
# Test 14: rc ok + data list with NO dict entries -> _record_count 0 ->
# empty_data (covers the _record_count isinstance(r, dict) filter branch)
# =============================================================================
@pytest.mark.asyncio
async def test_rc_ok_data_list_no_dict_entries_empty_data() -> None:
    """meta.rc == 'ok' + data is a list of non-dicts -> count 0 -> empty_data."""
    payload: dict[str, object] = {"meta": {"rc": "ok"}, "data": [1, "x", None]}
    writer = InMemoryMetricsWriter()
    result = await UnifiControllerUpCollector().run(_ctx(writer, _FakeSysinfoOk(payload)))

    assert result.ok is False
    assert result.metrics_emitted == 3  # noqa: PLR2004
    assert result.errors == ["unifi sysinfo returned no data"]
    assert _gauge_value(writer, _M_UP, {}) == 0.0
    assert _gauge_value(writer, _M_UP_REASON, {"reason": "empty_data"}) == 1.0
