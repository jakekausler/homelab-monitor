"""Tests for UnifiWanCollector -- stat/health WAN/speedtest/failover parsing."""

from __future__ import annotations

import structlog

from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
    UnifiClient,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.kernel.unifi.client import UnifiResponse
from homelab_monitor.kernel.unifi.errors import UnifiError
from homelab_monitor.plugins.collectors.integrations.unifi.wan import UnifiWanCollector

# ---------------------------------------------------------------------------
# Fixture payloads -- synthetic stat/health responses (data is a LIST)
# ---------------------------------------------------------------------------

# Fixture 1: healthy single-active-WAN, NO speedtest run (lastrun == 0).
_WWW_HEALTHY_NO_SPEEDTEST: dict[str, object] = {
    "subsystem": "www",
    "status": "ok",
    "latency": 10,
    "drops": 3,
    "rx_bytes-r": 137878,
    "tx_bytes-r": 180274,
    "xput_down": 0.0,
    "xput_up": 0.0,
    "speedtest_ping": 0,
    "speedtest_lastrun": 0,
    "speedtest_status": "Idle",
}
_WAN_TWO_WANS_SECONDARY_DOWN: dict[str, object] = {
    "subsystem": "wan",
    "uptime_stats": {
        "WAN": {"uptime": 591614, "availability": 100.0},
        "WAN2": {"downtime": 1007304},
    },
}
_PAYLOAD_HEALTHY_NO_SPEEDTEST: dict[str, object] = {
    "meta": {"rc": "ok"},
    "data": [_WWW_HEALTHY_NO_SPEEDTEST, _WAN_TWO_WANS_SECONDARY_DOWN],
}

# Fixture 2: completed speedtest (lastrun > 0).
_WWW_COMPLETED_SPEEDTEST: dict[str, object] = {
    "subsystem": "www",
    "status": "ok",
    "latency": 10,
    "drops": 3,
    "rx_bytes-r": 137878,
    "tx_bytes-r": 180274,
    "xput_down": 512.3,
    "xput_up": 22.1,
    "speedtest_ping": 12,
    "speedtest_lastrun": 1718000000,
    "speedtest_status": "Idle",
}
_PAYLOAD_COMPLETED_SPEEDTEST: dict[str, object] = {
    "meta": {"rc": "ok"},
    "data": [_WWW_COMPLETED_SPEEDTEST, _WAN_TWO_WANS_SECONDARY_DOWN],
}

# Fixture 3: single-WAN (no failover peer).
_WAN_SINGLE: dict[str, object] = {
    "subsystem": "wan",
    "uptime_stats": {
        "WAN": {"uptime": 1000, "availability": 100.0},
    },
}
_PAYLOAD_SINGLE_WAN: dict[str, object] = {
    "meta": {"rc": "ok"},
    "data": [_WWW_HEALTHY_NO_SPEEDTEST, _WAN_SINGLE],
}

# Fixture 4: active failover (primary down + secondary carrying traffic).
_WWW_PRIMARY_WARNING: dict[str, object] = {
    "subsystem": "www",
    "status": "warning",
    "latency": 10,
    "drops": 3,
    "rx_bytes-r": 137878,
    "tx_bytes-r": 180274,
    "speedtest_lastrun": 0,
}
_WAN_ACTIVE_FAILOVER: dict[str, object] = {
    "subsystem": "wan",
    "uptime_stats": {
        "WAN": {"downtime": 50},
        "WAN2": {"uptime": 200, "availability": 100.0},
    },
}
_PAYLOAD_ACTIVE_FAILOVER: dict[str, object] = {
    "meta": {"rc": "ok"},
    "data": [_WWW_PRIMARY_WARNING, _WAN_ACTIVE_FAILOVER],
}

# Fixture 5a: data missing the www entry (only wan).
_PAYLOAD_NO_WWW: dict[str, object] = {
    "meta": {"rc": "ok"},
    "data": [_WAN_TWO_WANS_SECONDARY_DOWN],
}

# Fixture 5b: data missing the wan entry (only www).
_PAYLOAD_NO_WAN: dict[str, object] = {
    "meta": {"rc": "ok"},
    "data": [_WWW_HEALTHY_NO_SPEEDTEST],
}

# Fixture 6: malformed -- non-dict entry, entry with no subsystem, status non-str,
# uptime_stats not a dict.
_WWW_BAD_STATUS: dict[str, object] = {
    "subsystem": "www",
    "status": 123,  # non-string status -> wan_up skipped
}
_WAN_BAD_UPTIME_STATS: dict[str, object] = {
    "subsystem": "wan",
    "uptime_stats": "not-a-dict",  # -> failover metrics skipped
}
_PAYLOAD_MALFORMED: dict[str, object] = {
    "meta": {"rc": "ok"},
    "data": [
        "not-a-dict",
        {"no_subsystem": True},
        _WWW_BAD_STATUS,
        _WAN_BAD_UPTIME_STATS,
    ],
}

# ---------------------------------------------------------------------------
# Fake Unifi clients (conform to the UnifiClient Protocol; override stat_health)
# ---------------------------------------------------------------------------


class _FakeUnifiBase:
    """Base class for fake Unifi clients with shared stub methods."""

    site_name: str = "default"
    v1_site_id: str = "fake-uuid"

    async def stat_device(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

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


class _FakeUnifiHealthOk(_FakeUnifiBase):
    """Returns a caller-supplied health payload via stat_health()."""

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload_response = payload

    async def stat_health(self) -> UnifiResponse | UnifiError:
        return UnifiResponse(
            payload=self.payload_response,
            took_seconds=0.03,
            endpoint="stat/health",
        )


class _FakeUnifiHealthFail(_FakeUnifiBase):
    """Returns a UnifiError from stat_health()."""

    async def stat_health(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="unreachable", message="GET stat/health: connection failed")


# ---------------------------------------------------------------------------
# Context factory + gauge assertion helpers
# ---------------------------------------------------------------------------


def _ctx(
    writer: InMemoryMetricsWriter,
    unifi: UnifiClient | None,
) -> CollectorContext:
    """Minimal CollectorContext -- only vm + unifi are used by run()."""
    return CollectorContext(
        config=CollectorConfig(name="unifi_wan", interval_seconds=30, timeout_seconds=15),
        db=None,  # pyright: ignore[reportArgumentType]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="unifi_wan"),  # pyright: ignore[reportArgumentType]
        unifi=unifi,
    )


def _gauges(writer: InMemoryMetricsWriter, name: str) -> list[tuple[float, dict[str, str]]]:
    """Return (value, labels) for all recorded entries with the given name."""
    return [(e.value, e.labels) for e in writer.recorded if e.name == name]


def _gauge_value(
    writer: InMemoryMetricsWriter,
    name: str,
    label_subset: dict[str, str],
) -> float | None:
    """Return the value of the first gauge matching name + all label_subset entries."""
    for e in writer.recorded:
        if e.name == name and all(e.labels.get(k) == v for k, v in label_subset.items()):
            return e.value
    return None


# ---------------------------------------------------------------------------
# Tests: ClassVars
# ---------------------------------------------------------------------------


def test_classvars() -> None:
    """ClassVars match the locked spec values."""
    assert UnifiWanCollector.name == "unifi_wan"
    assert UnifiWanCollector.interval.total_seconds() == 30.0  # noqa: PLR2004
    assert UnifiWanCollector.timeout.total_seconds() == 15.0  # noqa: PLR2004
    assert UnifiWanCollector.concurrency_group == "unifi"


# ---------------------------------------------------------------------------
# Test 1: healthy single-active-WAN, no speedtest run
# ---------------------------------------------------------------------------


async def test_healthy_no_speedtest() -> None:
    """www healthy, lastrun=0.

    wan/latency/drops/xput emitted; lastrun emitted; no speedtest results.
    """
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiHealthOk(_PAYLOAD_HEALTHY_NO_SPEEDTEST)
    result = await UnifiWanCollector().run(_ctx(writer, fake))
    assert result.ok is True

    assert _gauge_value(writer, "homelab_unifi_wan_up", {}) == 1.0
    assert _gauge_value(writer, "homelab_unifi_wan_latency_seconds", {}) == 0.01  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_wan_drops", {}) == 3.0  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_wan_xput_down_bytes_per_sec", {}) == 137878.0  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_wan_xput_up_bytes_per_sec", {}) == 180274.0  # noqa: PLR2004

    # lastrun=0 IS emitted; speedtest results are NOT.
    assert _gauge_value(writer, "homelab_unifi_speedtest_lastrun", {}) == 0.0
    assert _gauges(writer, "homelab_unifi_speedtest_download_mbps") == []
    assert _gauges(writer, "homelab_unifi_speedtest_upload_mbps") == []
    assert _gauges(writer, "homelab_unifi_speedtest_ping_seconds") == []

    # Failover: 2 WAN keys -> capable=1.0; secondary down + primary up -> active=0.0.
    assert _gauge_value(writer, "homelab_unifi_wan_failover_capable", {}) == 1.0
    assert _gauge_value(writer, "homelab_unifi_wan_failover_active", {}) == 0.0
    assert _gauge_value(writer, "homelab_unifi_wan_uptime_seconds", {}) == 591614.0  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Test 2: completed speedtest
# ---------------------------------------------------------------------------


async def test_completed_speedtest() -> None:
    """lastrun>0: download/upload/ping emitted (ping ms->s); lastrun emitted."""
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiHealthOk(_PAYLOAD_COMPLETED_SPEEDTEST)
    result = await UnifiWanCollector().run(_ctx(writer, fake))
    assert result.ok is True

    assert _gauge_value(writer, "homelab_unifi_speedtest_lastrun", {}) == 1718000000.0  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_speedtest_download_mbps", {}) == 512.3  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_speedtest_upload_mbps", {}) == 22.1  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_speedtest_ping_seconds", {}) == 0.012  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Test 3: single-WAN (no failover peer)
# ---------------------------------------------------------------------------


async def test_single_wan_not_failover_capable() -> None:
    """One WAN key -> capable=0.0, active=0.0."""
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiHealthOk(_PAYLOAD_SINGLE_WAN)
    result = await UnifiWanCollector().run(_ctx(writer, fake))
    assert result.ok is True

    assert _gauge_value(writer, "homelab_unifi_wan_failover_capable", {}) == 0.0
    assert _gauge_value(writer, "homelab_unifi_wan_failover_active", {}) == 0.0


# ---------------------------------------------------------------------------
# Test 4: active failover
# ---------------------------------------------------------------------------


async def test_active_failover() -> None:
    """Primary down (status=warning) + secondary carrying traffic -> active=1.0."""
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiHealthOk(_PAYLOAD_ACTIVE_FAILOVER)
    result = await UnifiWanCollector().run(_ctx(writer, fake))
    assert result.ok is True

    assert _gauge_value(writer, "homelab_unifi_wan_up", {}) == 0.0
    assert _gauge_value(writer, "homelab_unifi_wan_failover_capable", {}) == 1.0
    assert _gauge_value(writer, "homelab_unifi_wan_failover_active", {}) == 1.0


# ---------------------------------------------------------------------------
# Test 5: graceful degrade -- missing www or wan entry
# ---------------------------------------------------------------------------


async def test_missing_www_entry() -> None:
    """data has only the wan entry -> no www metrics; failover metrics still emitted; ok=True."""
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiHealthOk(_PAYLOAD_NO_WWW)
    result = await UnifiWanCollector().run(_ctx(writer, fake))
    assert result.ok is True

    assert _gauges(writer, "homelab_unifi_wan_up") == []
    assert _gauges(writer, "homelab_unifi_wan_latency_seconds") == []
    # Failover still emitted (www absent -> primary treated as down via empty dict).
    assert _gauge_value(writer, "homelab_unifi_wan_failover_capable", {}) == 1.0
    assert _gauge_value(writer, "homelab_unifi_wan_failover_active", {}) == 0.0


async def test_missing_wan_entry() -> None:
    """data has only the www entry -> www metrics emitted; no failover metrics; ok=True."""
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiHealthOk(_PAYLOAD_NO_WAN)
    result = await UnifiWanCollector().run(_ctx(writer, fake))
    assert result.ok is True

    assert _gauge_value(writer, "homelab_unifi_wan_up", {}) == 1.0
    assert _gauges(writer, "homelab_unifi_wan_failover_capable") == []
    assert _gauges(writer, "homelab_unifi_wan_failover_active") == []


# ---------------------------------------------------------------------------
# Test 6: malformed payload -- no crash, ok=True, no spurious metrics
# ---------------------------------------------------------------------------


async def test_malformed_entries() -> None:
    """Non-dict entry, missing subsystem, non-str status, non-dict uptime_stats -> no crash."""
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiHealthOk(_PAYLOAD_MALFORMED)
    result = await UnifiWanCollector().run(_ctx(writer, fake))
    assert result.ok is True

    # status non-str -> wan_up skipped.
    assert _gauges(writer, "homelab_unifi_wan_up") == []
    # uptime_stats not a dict -> no failover metrics.
    assert _gauges(writer, "homelab_unifi_wan_failover_capable") == []
    assert _gauges(writer, "homelab_unifi_wan_failover_active") == []
    # lastrun absent -> not emitted.
    assert _gauges(writer, "homelab_unifi_speedtest_lastrun") == []


# ---------------------------------------------------------------------------
# Test 7: API latency emitted
# ---------------------------------------------------------------------------


async def test_api_latency_emitted() -> None:
    """homelab_unifi_api_took_seconds{endpoint=stat/health} == 0.03 on success."""
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiHealthOk(_PAYLOAD_HEALTHY_NO_SPEEDTEST)
    await UnifiWanCollector().run(_ctx(writer, fake))

    lat = _gauge_value(writer, "homelab_unifi_api_took_seconds", {"endpoint": "stat/health"})
    assert lat == 0.03  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Test 8: error paths
# ---------------------------------------------------------------------------


async def test_unifi_error_returns_failed() -> None:
    """UnifiError from stat_health -> failed run.

    ok=False, error in errors, metrics_emitted=0, no latency gauge.
    """
    writer = InMemoryMetricsWriter()
    result = await UnifiWanCollector().run(_ctx(writer, _FakeUnifiHealthFail()))
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert "GET stat/health: connection failed" in result.errors[0]
    assert _gauges(writer, "homelab_unifi_api_took_seconds") == []


async def test_unifi_client_none() -> None:
    """ctx.unifi=None -> ok=False, errors=['unifi client not configured'], no crash."""
    writer = InMemoryMetricsWriter()
    result = await UnifiWanCollector().run(_ctx(writer, None))
    assert result.ok is False
    assert result.errors == ["unifi client not configured"]
    assert result.metrics_emitted == 0


# ---------------------------------------------------------------------------
# Test: payload-not-dict and data-not-list early returns
# ---------------------------------------------------------------------------


async def test_payload_not_dict() -> None:
    """payload not a dict -> ok=True, only the latency gauge emitted."""
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiHealthOk(payload="not-a-dict")  # type: ignore[arg-type]
    result = await UnifiWanCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert _gauges(writer, "homelab_unifi_wan_up") == []
    # Only the api_took_seconds latency gauge was emitted.
    assert result.metrics_emitted == 1


async def test_data_not_list() -> None:
    """payload['data'] not a list -> ok=True, only the latency gauge emitted."""
    writer = InMemoryMetricsWriter()
    payload: dict[str, object] = {"meta": {"rc": "ok"}, "data": {"subsystem": "www"}}
    fake = _FakeUnifiHealthOk(payload)
    result = await UnifiWanCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert _gauges(writer, "homelab_unifi_wan_up") == []
    assert result.metrics_emitted == 1


# ---------------------------------------------------------------------------
# Test 9: metrics_emitted count consistency
# ---------------------------------------------------------------------------


async def test_metrics_emitted_count() -> None:
    """result.metrics_emitted equals the number of write_gauge calls recorded."""
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiHealthOk(_PAYLOAD_HEALTHY_NO_SPEEDTEST)
    result = await UnifiWanCollector().run(_ctx(writer, fake))
    assert result.ok is True
    gauge_count = len(writer.recorded)
    assert result.metrics_emitted == gauge_count


# ---------------------------------------------------------------------------
# Extra: non-dict peer value + absent WAN sub-dict (branch closure)
# ---------------------------------------------------------------------------

_WAN_NONDICT_PEER_NO_PRIMARY: dict[str, object] = {
    "subsystem": "wan",
    "uptime_stats": {
        "WAN2": "not-a-dict",
        "WAN3": {"downtime": 5},
    },
}
_PAYLOAD_NONDICT_PEER: dict[str, object] = {
    "meta": {"rc": "ok"},
    "data": [_WWW_HEALTHY_NO_SPEEDTEST, _WAN_NONDICT_PEER_NO_PRIMARY],
}


async def test_nondict_peer_and_absent_primary() -> None:
    """Non-dict peer value is skipped; absent WAN sub-dict.

    No wan_uptime; capable=1.0, active=0.0.
    """
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiHealthOk(_PAYLOAD_NONDICT_PEER)
    result = await UnifiWanCollector().run(_ctx(writer, fake))
    assert result.ok is True

    # Two keys (WAN2, WAN3) -> capable=1.0.
    assert _gauge_value(writer, "homelab_unifi_wan_failover_capable", {}) == 1.0
    # WAN2 is non-dict (skipped); WAN3 down + primary up -> active=0.0.
    assert _gauge_value(writer, "homelab_unifi_wan_failover_active", {}) == 0.0
    # No "WAN" sub-dict present -> wan_uptime_seconds not emitted.
    assert _gauges(writer, "homelab_unifi_wan_uptime_seconds") == []


# ---------------------------------------------------------------------------
# Extra: completed speedtest with absent ping field (branch closure)
# ---------------------------------------------------------------------------

_WWW_SPEEDTEST_NO_PING: dict[str, object] = {
    "subsystem": "www",
    "status": "ok",
    "speedtest_lastrun": 1718000000,
    "xput_down": 512.3,
    "xput_up": 22.1,
    # speedtest_ping intentionally absent -> as_float(None) is None
}
_PAYLOAD_SPEEDTEST_NO_PING: dict[str, object] = {
    "meta": {"rc": "ok"},
    "data": [_WWW_SPEEDTEST_NO_PING],
}


async def test_completed_speedtest_absent_ping() -> None:
    """lastrun>0 but speedtest_ping absent: download/upload emitted; ping gauge NOT emitted."""
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiHealthOk(_PAYLOAD_SPEEDTEST_NO_PING)
    result = await UnifiWanCollector().run(_ctx(writer, fake))
    assert result.ok is True

    assert _gauge_value(writer, "homelab_unifi_speedtest_lastrun", {}) == 1718000000.0  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_speedtest_download_mbps", {}) == 512.3  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_speedtest_upload_mbps", {}) == 22.1  # noqa: PLR2004
    # speedtest_ping absent -> ping gauge NOT emitted (covers wan.py:142->exit FALSE).
    assert _gauges(writer, "homelab_unifi_speedtest_ping_seconds") == []
