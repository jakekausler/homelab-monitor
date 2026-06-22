"""Unit tests for UnifiClientStatsCollector (STAGE-007-008).

Covers the six {mac}-keyed capped families (signal / tx-rate / rx-rate / uptime /
tx-bytes / rx-bytes), the four bounded experience rollups, the cardinality cap +
cross-family survivor identity (Decision A2), wired graceful degrade, the rollup
threshold boundaries (incl. the div-by-zero guard), and every error path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
    MetricEntry,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig, SuggestionEvent
from homelab_monitor.kernel.unifi.client import UnifiResponse
from homelab_monitor.kernel.unifi.errors import UnifiError
from homelab_monitor.plugins.collectors.integrations.unifi.client_stats import (
    M_AP_CLIENT_COUNT,
    M_CLIENT_INFO,
    M_HIGH_RETRIES,
    M_POOR_SATISFACTION,
    M_POOR_SIGNAL,
    M_RX_BYTES,
    M_RX_RATE_BPS,
    M_SIGNAL_DBM,
    M_TX_BYTES,
    M_TX_RATE_BPS,
    M_UPTIME,
    UnifiClientStatsCollector,
)

_DROP_METRIC = "homelab_metric_family_dropped_series"


# --- assertion helpers (mirror test_unifi_active_client_collector.py) --------
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


def _drop_value(writer: InMemoryMetricsWriter, family: str) -> float | None:
    """Return the value of the dropped-series gauge for a given family."""
    for e in _gauges(writer, _DROP_METRIC):
        if e.labels.get("family") == family:
            return e.value
    return None


def _survivor_macs(writer: InMemoryMetricsWriter, family: str) -> set[str]:
    """Return the set of {mac} label values that survived in a capped family."""
    return {e.labels["mac"] for e in _gauges(writer, family)}


# --- fake clients (conform to the UnifiClient Protocol via the base) ---------
# Copy the _FakeUnifiBase class VERBATIM from
# apps/monitor/tests/test_unifi_active_client_collector.py (it stubs ALL 12
# UnifiClient Protocol methods returning UnifiError). Then subclass it here to
# override ONLY stat_sta().


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


class _FakeStaOk(_FakeUnifiBase):
    """stat_sta returns a fixed classic payload."""

    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records

    async def stat_sta(self) -> UnifiResponse | UnifiError:
        payload: dict[str, object] = {"meta": {"rc": "ok"}, "data": self._records}
        return UnifiResponse(payload=payload, took_seconds=0.012, endpoint="stat/sta")


class _FakeStaMalformed(_FakeUnifiBase):
    """stat_sta returns a 200 with a non-list ``data`` (malformed body)."""

    async def stat_sta(self) -> UnifiResponse | UnifiError:
        payload: dict[str, object] = {"meta": {"rc": "ok"}, "data": "not-a-list"}
        return UnifiResponse(payload=payload, took_seconds=0.01, endpoint="stat/sta")


class _FakeStaFail(_FakeUnifiBase):
    """stat_sta returns a UnifiError."""

    async def stat_sta(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="timeout", message="sta timed out")


def _ctx(writer: InMemoryMetricsWriter, unifi: object | None) -> CollectorContext:
    """Build a CollectorContext. Copy the CollectorConfig kwargs + every other
    field VERBATIM from the active_client test's _ctx(); only ``vm`` and ``unifi``
    differ here. db/http/ssh/secrets are unused by this collector (set as the
    active_client test sets them; None where the Protocol allows)."""
    return CollectorContext(
        config=CollectorConfig(
            name="unifi_client_stats",
            interval_seconds=60,
            timeout_seconds=15,
        ),
        db=None,  # type: ignore[arg-type]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="unifi_client_stats"),
        unifi=unifi,  # type: ignore[arg-type]
    )


# --- synthetic stat/sta fixtures ---------------------------------------------
def _wireless_ok(  # noqa: PLR0913
    mac: str,
    *,
    ap_mac: str,
    signal: float = -55.0,
    satisfaction: float = 95.0,
    tx_retries: float = 1.0,
    wifi_tx_attempts: float = 1000.0,
) -> dict[str, object]:
    """A healthy wireless client record (good signal/satisfaction/retries)."""
    rec: dict[str, object] = {
        "mac": mac,
        "is_wired": False,
        "ap_mac": ap_mac,
        "essid": "Home",
        "signal": signal,
        "tx_rate": 866000.0,  # KBPS
        "rx_rate": 650000.0,  # KBPS
        "satisfaction": satisfaction,
        "tx_retries": tx_retries,
        "wifi_tx_attempts": wifi_tx_attempts,
        "uptime": 3600.0,
        "tx_bytes": 1_000_000.0,
        "rx_bytes": 2_000_000.0,
    }
    return rec


def _wired_ok(mac: str) -> dict[str, object]:
    """A wired client: no signal/rate/retries; wired-* byte keys; satisfaction=100."""
    rec: dict[str, object] = {
        "mac": mac,
        "is_wired": True,
        "satisfaction": 100.0,
        "uptime": 7200.0,
        "wired-tx_bytes": 5_000_000.0,
        "wired-rx_bytes": 6_000_000.0,
    }
    return rec


# ============================================================================
# Test 1: per-client capped families emit with {mac} labels + correct values.
# ============================================================================
@pytest.mark.asyncio
async def test_per_client_families_values() -> None:
    """signal/tx_rate(*1000)/rx_rate(*1000)/uptime/tx_bytes/rx_bytes per mac."""
    wifi: dict[str, object] = _wireless_ok("aa:aa:aa:aa:aa:aa", ap_mac="ap1")
    wired: dict[str, object] = _wired_ok("bb:bb:bb:bb:bb:bb")
    records: list[dict[str, object]] = [wifi, wired]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientStatsCollector().run(_ctx(writer, _FakeStaOk(records)))

    assert result.ok is True
    wifi_labels: dict[str, str] = {"mac": "aa:aa:aa:aa:aa:aa"}
    assert _gauge_value(writer, M_SIGNAL_DBM, wifi_labels) == -55.0  # noqa: PLR2004
    assert _gauge_value(writer, M_TX_RATE_BPS, wifi_labels) == 866000.0 * 1000.0
    assert _gauge_value(writer, M_RX_RATE_BPS, wifi_labels) == 650000.0 * 1000.0
    assert _gauge_value(writer, M_UPTIME, wifi_labels) == 3600.0  # noqa: PLR2004
    assert _gauge_value(writer, M_TX_BYTES, wifi_labels) == 1_000_000.0  # noqa: PLR2004
    assert _gauge_value(writer, M_RX_BYTES, wifi_labels) == 2_000_000.0  # noqa: PLR2004

    wired_labels: dict[str, str] = {"mac": "bb:bb:bb:bb:bb:bb"}
    # wired: uptime + wired-* bytes present; NO signal/rate series.
    assert _gauge_value(writer, M_UPTIME, wired_labels) == 7200.0  # noqa: PLR2004
    assert _gauge_value(writer, M_TX_BYTES, wired_labels) == 5_000_000.0  # noqa: PLR2004
    assert _gauge_value(writer, M_RX_BYTES, wired_labels) == 6_000_000.0  # noqa: PLR2004
    assert _gauge_value(writer, M_SIGNAL_DBM, wired_labels) is None
    assert _gauge_value(writer, M_TX_RATE_BPS, wired_labels) is None
    assert _gauge_value(writer, M_RX_RATE_BPS, wired_labels) is None


# ============================================================================
# Test 2: CAP enforcement (survivors==cap, drop gauge, one SuggestionEvent).
# ============================================================================
@pytest.mark.asyncio
async def test_over_cap_drops_and_one_suggestion_per_family(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """cap=3, feed 7 wireless clients -> 3 survivors per family, 4 dropped, 1 suggestion/family."""
    cfg = tmp_path / "homelab-monitor.yaml"
    cfg.write_text("cardinality_caps:\n  families:\n    unifi_client_stats: 3\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg))

    records: list[dict[str, object]] = [
        _wireless_ok(f"aa:aa:aa:aa:aa:{i:02d}", ap_mac="ap1") for i in range(7)
    ]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientStatsCollector().run(_ctx(writer, _FakeStaOk(records)))

    assert result.ok is True
    # Each capped family that received >cap observations -> exactly cap survivors.
    for fam in (
        M_SIGNAL_DBM,
        M_TX_RATE_BPS,
        M_RX_RATE_BPS,
        M_UPTIME,
        M_TX_BYTES,
        M_RX_BYTES,
        M_CLIENT_INFO,
    ):
        assert len(_gauges(writer, fam)) == 3  # noqa: PLR2004
        assert _drop_value(writer, fam) == 4.0  # 7 - 3  # noqa: PLR2004

    # One SuggestionEvent per over-cap family (7 families each over cap here).
    suggestions = [e for e in result.events if isinstance(e, SuggestionEvent)]
    assert len(suggestions) == 7  # noqa: PLR2004
    assert all(s.severity == "warning" for s in suggestions)


# ============================================================================
# Test 3: CROSS-FAMILY SURVIVOR IDENTITY (Decision A2 -- load-bearing).
# ============================================================================
@pytest.mark.asyncio
async def test_cross_family_survivor_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Under >cap, the surviving {mac} set is IDENTICAL across families."""
    cfg = tmp_path / "homelab-monitor.yaml"
    cfg.write_text("cardinality_caps:\n  families:\n    unifi_client_stats: 3\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg))

    records: list[dict[str, object]] = [
        _wireless_ok(f"aa:aa:aa:aa:aa:{i:02d}", ap_mac="ap1") for i in range(7)
    ]
    writer = InMemoryMetricsWriter()
    await UnifiClientStatsCollector().run(_ctx(writer, _FakeStaOk(records)))

    signal_macs = _survivor_macs(writer, M_SIGNAL_DBM)
    uptime_macs = _survivor_macs(writer, M_UPTIME)
    rx_rate_macs = _survivor_macs(writer, M_RX_RATE_BPS)
    tx_bytes_macs = _survivor_macs(writer, M_TX_BYTES)
    assert len(signal_macs) == 3  # noqa: PLR2004
    assert signal_macs == uptime_macs
    assert signal_macs == rx_rate_macs
    assert signal_macs == tx_bytes_macs


# ============================================================================
# Test 3b: CROSS-FAMILY SURVIVOR SUBSET under mixed roster (Decision A2).
# ============================================================================
@pytest.mark.asyncio
async def test_cross_family_survivor_subset_mixed_roster(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mixed roster under cap: wireless-family survivors are a SUBSET of all-client survivors.

    The 3 wireless-only families (signal/tx_rate/rx_rate) draw from wireless macs;
    the 3 all-client families (uptime/tx_bytes/rx_bytes) draw from wireless+wired.
    Under a mixed roster the wireless survivor set is a subset of the all-client
    survivor set, and wired macs never appear in a wireless-only family.
    """
    cfg = tmp_path / "homelab-monitor.yaml"
    cfg.write_text("cardinality_caps:\n  families:\n    unifi_client_stats: 2\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg))

    records: list[dict[str, object]] = [
        _wireless_ok("aa:aa:aa:aa:aa:01", ap_mac="ap1"),
        _wireless_ok("aa:aa:aa:aa:aa:02", ap_mac="ap1"),
        _wireless_ok("aa:aa:aa:aa:aa:03", ap_mac="ap1"),
        _wired_ok("bb:bb:bb:bb:bb:01"),
        _wired_ok("bb:bb:bb:bb:bb:02"),
    ]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientStatsCollector().run(_ctx(writer, _FakeStaOk(records)))
    assert result.ok is True

    signal_macs = _survivor_macs(writer, M_SIGNAL_DBM)
    uptime_macs = _survivor_macs(writer, M_UPTIME)
    tx_rate_macs = _survivor_macs(writer, M_TX_RATE_BPS)
    rx_rate_macs = _survivor_macs(writer, M_RX_RATE_BPS)
    tx_bytes_macs = _survivor_macs(writer, M_TX_BYTES)
    rx_bytes_macs = _survivor_macs(writer, M_RX_BYTES)
    wired_macs: set[str] = {"bb:bb:bb:bb:bb:01", "bb:bb:bb:bb:bb:02"}

    # wireless-only families capped to 2 wireless macs; all-client families capped to 2 macs total.
    assert len(signal_macs) == 2  # noqa: PLR2004
    assert len(tx_rate_macs) == 2  # noqa: PLR2004
    assert len(rx_rate_macs) == 2  # noqa: PLR2004
    assert len(uptime_macs) == 2  # noqa: PLR2004
    assert len(tx_bytes_macs) == 2  # noqa: PLR2004
    assert len(rx_bytes_macs) == 2  # noqa: PLR2004

    # The wireless macs (aa:...) sort before the wired macs (bb:...), so the
    # wireless macs win the cap slots and the wireless survivors are a subset of
    # the all-client survivors here. (This subset is roster-sort-dependent; the
    # len==cap and isdisjoint-from-wired assertions below are unconditional.)
    assert signal_macs <= uptime_macs
    assert tx_rate_macs <= uptime_macs
    assert rx_rate_macs <= uptime_macs

    # wired macs never appear in wireless-only families.
    assert signal_macs.isdisjoint(wired_macs)
    assert tx_rate_macs.isdisjoint(wired_macs)
    assert rx_rate_macs.isdisjoint(wired_macs)


# ============================================================================
# Test 4: under-cap -> drop gauge present with value 0.0, no SuggestionEvent.
# ============================================================================
@pytest.mark.asyncio
async def test_under_cap_zero_drop_no_suggestion() -> None:
    """Two clients, default cap 200 -> drop gauge 0.0, no suggestions."""
    records: list[dict[str, object]] = [
        _wireless_ok("aa:aa:aa:aa:aa:01", ap_mac="ap1"),
        _wireless_ok("aa:aa:aa:aa:aa:02", ap_mac="ap1"),
    ]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientStatsCollector().run(_ctx(writer, _FakeStaOk(records)))

    assert _drop_value(writer, M_SIGNAL_DBM) == 0.0
    assert _drop_value(writer, M_UPTIME) == 0.0
    assert [e for e in result.events if isinstance(e, SuggestionEvent)] == []


# ============================================================================
# Test 5: rollup counts (poor signal / poor satisfaction / high retries).
# ============================================================================
@pytest.mark.asyncio
async def test_rollup_counts() -> None:
    """Poor-signal, poor-satisfaction, high-retries counts + div-by-zero guard."""
    good: dict[str, object] = _wireless_ok("aa:aa:aa:aa:aa:01", ap_mac="ap1")
    poor_sig: dict[str, object] = _wireless_ok(
        "aa:aa:aa:aa:aa:02", ap_mac="ap1", signal=-80.0
    )  # < -70
    poor_sat: dict[str, object] = _wireless_ok(
        "aa:aa:aa:aa:aa:03", ap_mac="ap1", satisfaction=40.0
    )  # < 50
    high_retry: dict[str, object] = _wireless_ok(
        "aa:aa:aa:aa:aa:04", ap_mac="ap1", tx_retries=200.0, wifi_tx_attempts=1000.0
    )  # 20% > 10%
    zero_attempts: dict[str, object] = _wireless_ok(
        "aa:aa:aa:aa:aa:05", ap_mac="ap1", tx_retries=5.0, wifi_tx_attempts=0.0
    )  # div-by-zero guard -> NOT counted
    wired: dict[str, object] = _wired_ok("bb:bb:bb:bb:bb:bb")  # satisfaction=100
    records: list[dict[str, object]] = [
        good,
        poor_sig,
        poor_sat,
        high_retry,
        zero_attempts,
        wired,
    ]
    writer = InMemoryMetricsWriter()
    await UnifiClientStatsCollector().run(_ctx(writer, _FakeStaOk(records)))

    assert _gauge_value(writer, M_POOR_SIGNAL, {"threshold": "-70"}) == 1.0
    assert _gauge_value(writer, M_POOR_SATISFACTION, {"threshold": "50"}) == 1.0
    assert _gauge_value(writer, M_HIGH_RETRIES, {"threshold": "10"}) == 1.0


# ============================================================================
# Test 6: per-AP client counts (wireless only; wired excluded).
# ============================================================================
@pytest.mark.asyncio
async def test_ap_client_count() -> None:
    """Per-AP wireless client counts; wired client not counted."""
    records: list[dict[str, object]] = [
        _wireless_ok("aa:aa:aa:aa:aa:01", ap_mac="ap1"),
        _wireless_ok("aa:aa:aa:aa:aa:02", ap_mac="ap1"),
        _wireless_ok("aa:aa:aa:aa:aa:03", ap_mac="ap2"),
        _wired_ok("bb:bb:bb:bb:bb:bb"),
    ]
    writer = InMemoryMetricsWriter()
    await UnifiClientStatsCollector().run(_ctx(writer, _FakeStaOk(records)))

    assert _gauge_value(writer, M_AP_CLIENT_COUNT, {"ap_mac": "ap1"}) == 2.0  # noqa: PLR2004
    assert _gauge_value(writer, M_AP_CLIENT_COUNT, {"ap_mac": "ap2"}) == 1.0


# ============================================================================
# Test 7: wired graceful degrade (covered partly in Test 1; explicit here).
# ============================================================================
@pytest.mark.asyncio
async def test_wired_graceful_degrade() -> None:
    """Wired client: uptime + wired-* bytes; NO signal/rate series for its mac."""
    records: list[dict[str, object]] = [_wired_ok("bb:bb:bb:bb:bb:bb")]
    writer = InMemoryMetricsWriter()
    await UnifiClientStatsCollector().run(_ctx(writer, _FakeStaOk(records)))

    labels: dict[str, str] = {"mac": "bb:bb:bb:bb:bb:bb"}
    assert _gauge_value(writer, M_UPTIME, labels) == 7200.0  # noqa: PLR2004
    assert _gauge_value(writer, M_TX_BYTES, labels) == 5_000_000.0  # noqa: PLR2004
    assert _gauge_value(writer, M_SIGNAL_DBM, labels) is None
    assert _gauge_value(writer, M_TX_RATE_BPS, labels) is None
    assert _gauge_value(writer, M_RX_RATE_BPS, labels) is None


# ============================================================================
# Test 8: API latency emitted.
# ============================================================================
@pytest.mark.asyncio
async def test_api_latency_emitted() -> None:
    """homelab_unifi_api_took_seconds{endpoint=stat/sta} == took_seconds."""
    records: list[dict[str, object]] = [_wireless_ok("aa:aa:aa:aa:aa:01", ap_mac="ap1")]
    writer = InMemoryMetricsWriter()
    await UnifiClientStatsCollector().run(_ctx(writer, _FakeStaOk(records)))

    assert (
        _gauge_value(writer, "homelab_unifi_api_took_seconds", {"endpoint": "stat/sta"}) == 0.012  # noqa: PLR2004
    )


# ============================================================================
# Test 9a: None client -> ok=False, no emits.
# ============================================================================
@pytest.mark.asyncio
async def test_none_client_fails() -> None:
    """ctx.unifi is None -> ok=False, metrics_emitted==0."""
    writer = InMemoryMetricsWriter()
    result = await UnifiClientStatsCollector().run(_ctx(writer, None))
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["unifi client not configured"]
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


# ============================================================================
# Test 9b: stat_sta UnifiError -> ok=False, no emits.
# ============================================================================
@pytest.mark.asyncio
async def test_sta_error_fails() -> None:
    """stat_sta UnifiError -> ok=False, errors=[message], nothing emitted."""
    writer = InMemoryMetricsWriter()
    result = await UnifiClientStatsCollector().run(_ctx(writer, _FakeStaFail()))
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["sta timed out"]
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


# ============================================================================
# Test 9c: malformed payload -> ok=True, 6 drop gauges at 0 + rollups at 0 + latency.
# ============================================================================
@pytest.mark.asyncio
async def test_malformed_payload_emits_drops_and_zero_rollups() -> None:
    """data not a list -> records=[]: 6 drop gauges 0.0 + rollups 0.0 + latency."""
    writer = InMemoryMetricsWriter()
    result = await UnifiClientStatsCollector().run(_ctx(writer, _FakeStaMalformed()))

    assert result.ok is True
    for fam in (
        M_SIGNAL_DBM,
        M_TX_RATE_BPS,
        M_RX_RATE_BPS,
        M_UPTIME,
        M_TX_BYTES,
        M_RX_BYTES,
        M_CLIENT_INFO,
    ):
        assert _drop_value(writer, fam) == 0.0
        assert _gauges(writer, fam) == []  # no survivor series
    assert _gauge_value(writer, M_POOR_SIGNAL, {"threshold": "-70"}) == 0.0
    assert _gauge_value(writer, M_POOR_SATISFACTION, {"threshold": "50"}) == 0.0
    assert _gauge_value(writer, M_HIGH_RETRIES, {"threshold": "10"}) == 0.0
    assert _gauges(writer, M_AP_CLIENT_COUNT) == []  # no AP series
    assert (
        _gauge_value(writer, "homelab_unifi_api_took_seconds", {"endpoint": "stat/sta"}) == 0.01  # noqa: PLR2004
    )


# ============================================================================
# Test 10: skipped record (missing/non-str mac) -> not in any family/rollup.
# ============================================================================
@pytest.mark.asyncio
async def test_missing_mac_record_skipped() -> None:
    """A record with a non-str mac contributes nothing and does not crash."""
    bad: dict[str, object] = {
        "mac": 12345,  # non-str -> skipped
        "is_wired": False,
        "ap_mac": "ap1",
        "signal": -80.0,
        "satisfaction": 10.0,
    }
    good: dict[str, object] = _wireless_ok("aa:aa:aa:aa:aa:01", ap_mac="ap1")
    records: list[dict[str, object]] = [bad, good]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientStatsCollector().run(_ctx(writer, _FakeStaOk(records)))

    assert result.ok is True
    # bad record's poor signal/satisfaction must NOT be counted (it was skipped).
    assert _gauge_value(writer, M_POOR_SIGNAL, {"threshold": "-70"}) == 0.0
    assert _gauge_value(writer, M_POOR_SATISFACTION, {"threshold": "50"}) == 0.0
    # only the good client survives in the signal family.
    assert _survivor_macs(writer, M_SIGNAL_DBM) == {"aa:aa:aa:aa:aa:01"}


# ============================================================================
# Test 11: metrics_emitted == total recorded writes.
# ============================================================================
@pytest.mark.asyncio
async def test_metrics_emitted_matches_recorded() -> None:
    """metrics_emitted equals the count of recorded gauge writes."""
    records: list[dict[str, object]] = [
        _wireless_ok("aa:aa:aa:aa:aa:01", ap_mac="ap1"),
        _wired_ok("bb:bb:bb:bb:bb:bb"),
    ]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientStatsCollector().run(_ctx(writer, _FakeStaOk(records)))
    recorded = [e for e in writer.recorded if e.kind == "gauge"]  # pyright: ignore[reportPrivateUsage]
    assert result.metrics_emitted == len(recorded)


class _FakeStaNonDict(_FakeUnifiBase):
    """stat_sta returns a 200 whose payload is NOT a dict (a bare list)."""

    async def stat_sta(self) -> UnifiResponse | UnifiError:
        payload: list[str] = ["not", "a", "dict"]
        return UnifiResponse(payload=payload, took_seconds=0.02, endpoint="stat/sta")


# ============================================================================
# Test 12: non-dict payload -> _parse_records returns [] (line 96 FALSE path).
# ============================================================================
@pytest.mark.asyncio
async def test_non_dict_payload_emits_drops_and_zero_rollups() -> None:
    """payload is a list (not a dict) -> records=[]: 6 drop gauges 0.0 + rollups 0.0."""
    writer = InMemoryMetricsWriter()
    result = await UnifiClientStatsCollector().run(_ctx(writer, _FakeStaNonDict()))

    assert result.ok is True
    for fam in (
        M_SIGNAL_DBM,
        M_TX_RATE_BPS,
        M_RX_RATE_BPS,
        M_UPTIME,
        M_TX_BYTES,
        M_RX_BYTES,
        M_CLIENT_INFO,
    ):
        assert _drop_value(writer, fam) == 0.0
        assert _gauges(writer, fam) == []  # no survivor series
    assert _gauge_value(writer, M_POOR_SIGNAL, {"threshold": "-70"}) == 0.0
    assert _gauge_value(writer, M_POOR_SATISFACTION, {"threshold": "50"}) == 0.0
    assert _gauge_value(writer, M_HIGH_RETRIES, {"threshold": "10"}) == 0.0
    assert _gauges(writer, M_AP_CLIENT_COUNT) == []  # no AP series


# ============================================================================
# Test 13: sparse wireless record (all optional fields absent) -> every
# observation/rollup skip branch takes its FALSE side.
#   - 184->186 signal None      -> no M_SIGNAL_DBM series for this mac
#   - 187->189 tx_rate None     -> no M_TX_RATE_BPS series
#   - 190->193 rx_rate None     -> no M_RX_RATE_BPS series
#   - 194->197 uptime None      -> no M_UPTIME series
#   - 200->202 tx_bytes None    -> no M_TX_BYTES series
#   - 203->exit rx_bytes None   -> no M_RX_BYTES series
#   - 170->exit ap_mac None     -> no AP client-count series
# ============================================================================
@pytest.mark.asyncio
async def test_sparse_wireless_record_skips_all_optional_families() -> None:
    """A wireless record with only mac+is_wired skips every optional family/AP count."""
    sparse: dict[str, object] = {"mac": "cc:cc:cc:cc:cc:cc", "is_wired": False}
    records: list[dict[str, object]] = [sparse]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientStatsCollector().run(_ctx(writer, _FakeStaOk(records)))

    assert result.ok is True
    labels: dict[str, str] = {"mac": "cc:cc:cc:cc:cc:cc"}
    # Every optional capped family is skipped for this mac (None field -> FALSE branch).
    assert _gauge_value(writer, M_SIGNAL_DBM, labels) is None
    assert _gauge_value(writer, M_TX_RATE_BPS, labels) is None
    assert _gauge_value(writer, M_RX_RATE_BPS, labels) is None
    assert _gauge_value(writer, M_UPTIME, labels) is None
    assert _gauge_value(writer, M_TX_BYTES, labels) is None
    assert _gauge_value(writer, M_RX_BYTES, labels) is None
    # No ap_mac -> no per-AP count series at all.
    assert _gauges(writer, M_AP_CLIENT_COUNT) == []
    # No optional fields -> all rollup counts stay 0.
    assert _gauge_value(writer, M_POOR_SIGNAL, {"threshold": "-70"}) == 0.0
    assert _gauge_value(writer, M_POOR_SATISFACTION, {"threshold": "50"}) == 0.0
    assert _gauge_value(writer, M_HIGH_RETRIES, {"threshold": "10"}) == 0.0


# ============================================================================
# Test 14: client_info{mac,name} — name-fallback branches (name / hostname / mac).
# ============================================================================
@pytest.mark.asyncio
async def test_client_info_name_fallback_all_branches() -> None:
    """client_info emits value 1.0 per mac; name = name -> hostname -> mac.

    Covers every branch of _best_name:
      * with_name:     rec['name'] present            -> name label == name
      * hostname_only: name absent, hostname present   -> name label == hostname
      * mac_only:      neither name nor hostname        -> name label == mac
    """
    with_name = _wireless_ok("aa:aa:aa:aa:aa:01", ap_mac="ap1")
    with_name["name"] = "Living Room TV"

    hostname_only = _wireless_ok("aa:aa:aa:aa:aa:02", ap_mac="ap1")
    hostname_only["name"] = ""  # empty string falls through to hostname
    hostname_only["hostname"] = "laptop-1"

    mac_only = _wireless_ok("aa:aa:aa:aa:aa:03", ap_mac="ap1")
    # neither 'name' nor 'hostname' present

    records: list[dict[str, object]] = [with_name, hostname_only, mac_only]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientStatsCollector().run(_ctx(writer, _FakeStaOk(records)))
    assert result.ok is True

    # Branch A: explicit name.
    assert (
        _gauge_value(writer, M_CLIENT_INFO, {"mac": "aa:aa:aa:aa:aa:01", "name": "Living Room TV"})
        == 1.0
    )
    # Branch B: hostname fallback.
    assert (
        _gauge_value(writer, M_CLIENT_INFO, {"mac": "aa:aa:aa:aa:aa:02", "name": "laptop-1"}) == 1.0
    )
    # Branch C: mac fallback (name label == mac).
    assert (
        _gauge_value(
            writer,
            M_CLIENT_INFO,
            {"mac": "aa:aa:aa:aa:aa:03", "name": "aa:aa:aa:aa:aa:03"},
        )
        == 1.0
    )
    # Exactly three client_info series (one per client).
    assert len(_gauges(writer, M_CLIENT_INFO)) == 3  # noqa: PLR2004
