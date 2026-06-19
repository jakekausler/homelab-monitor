"""Unit tests for UnifiClientDpiCollector (STAGE-007-009).

Covers the by-volume cardinality cap (top-N by combined bytes -- the deliberate
deviation from client_stats' lexical CappedEmitter), the always-on dpi_enabled +
latency + drop-gauge indicators, the graceful-empty paths (None data, [{}]
sentinel, non-dict payload), every record/entry skip branch, the rx/tx combine
logic, and every error path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from homelab_monitor.kernel.metrics.cardinality import M_FAMILY_DROPPED_SERIES
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
    MetricEntry,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig, SuggestionEvent
from homelab_monitor.kernel.unifi.client import UnifiResponse
from homelab_monitor.kernel.unifi.errors import UnifiError
from homelab_monitor.plugins.collectors.integrations.unifi.client_dpi import (
    M_CLIENT_DPI_BYTES,
    M_DPI_ENABLED,
    UnifiClientDpiCollector,
)

_DPI_FAMILY = "homelab_unifi_client_dpi_bytes"


# --- assertion helpers (mirror test_unifi_client_stats_collector.py) ----------
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
    for e in _gauges(writer, M_FAMILY_DROPPED_SERIES):
        if e.labels.get("family") == family:
            return e.value
    return None


def _dpi_clients(writer: InMemoryMetricsWriter) -> set[str]:
    """Return the set of {client} label values that survived in the DPI family."""
    return {e.labels["client"] for e in _gauges(writer, M_CLIENT_DPI_BYTES)}


# --- fake clients (conform to the UnifiClient Protocol via the base) ----------
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


class _FakeStadpiOk(_FakeUnifiBase):
    """stat_stadpi returns a fixed classic payload."""

    def __init__(self, records: list[object], took: float = 0.012) -> None:
        self._records = records
        self._took = took

    async def stat_stadpi(self) -> UnifiResponse | UnifiError:
        payload: dict[str, object] = {"meta": {"rc": "ok"}, "data": self._records}
        return UnifiResponse(payload=payload, took_seconds=self._took, endpoint="stat/stadpi")


class _FakeStadpiRawPayload(_FakeUnifiBase):
    """stat_stadpi returns an arbitrary (possibly non-dict) payload."""

    def __init__(self, payload: object, took: float = 0.01) -> None:
        self._payload = payload
        self._took = took

    async def stat_stadpi(self) -> UnifiResponse | UnifiError:
        return UnifiResponse(payload=self._payload, took_seconds=self._took, endpoint="stat/stadpi")


class _FakeStadpiFail(_FakeUnifiBase):
    """stat_stadpi returns a UnifiError."""

    async def stat_stadpi(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="timeout", message="stadpi timed out")


def _ctx(writer: InMemoryMetricsWriter, unifi: object | None) -> CollectorContext:
    """Build a CollectorContext. Mirrors the client_stats test's _ctx(); db/http/
    ssh/secrets are unused by this collector. Note: CollectorConfig has
    extra='forbid' -- do NOT pass concurrency_group (it is a ClassVar)."""
    return CollectorContext(
        config=CollectorConfig(
            name="unifi_client_dpi",
            interval_seconds=300,
            timeout_seconds=15,
        ),
        db=None,  # type: ignore[arg-type]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="unifi_client_dpi"),
        unifi=unifi,  # type: ignore[arg-type]
    )


# --- synthetic stat/stadpi fixture builders -----------------------------------
def _entry(app: int, cat: int, *, rx: object = None, tx: object = None) -> dict[str, object]:
    """Build one by_app entry; omit rx/tx when None is passed."""
    e: dict[str, object] = {"app": app, "cat": cat}
    if rx is not None:
        e["rx_bytes"] = rx
    if tx is not None:
        e["tx_bytes"] = tx
    return e


def _record(mac: str, by_app: list[dict[str, object]]) -> dict[str, object]:
    """Build one per-client DPI record."""
    rec: dict[str, object] = {"mac": mac, "by_app": by_app}
    return rec


# ============================================================================
# Test 1: None client -> ok=False, no emits.
# ============================================================================
@pytest.mark.asyncio
async def test_none_client_fails() -> None:
    """ctx.unifi is None -> ok=False, metrics_emitted==0, nothing emitted."""
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, None))
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["unifi client not configured"]
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


# ============================================================================
# Test 2: stat_stadpi UnifiError -> ok=False, no emits.
# ============================================================================
@pytest.mark.asyncio
async def test_stadpi_error_fails() -> None:
    """stat_stadpi UnifiError -> ok=False, errors=[message], nothing emitted."""
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeStadpiFail()))
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["stadpi timed out"]
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


# ============================================================================
# Test 3: empty data ([]) -> ok=True, dpi_enabled + latency + drop=0, no series.
# ============================================================================
@pytest.mark.asyncio
async def test_empty_data_emits_indicators_no_series() -> None:
    """data == [] -> ok=True: dpi_enabled 1.0 + latency + drop gauge 0.0, no DPI series."""
    writer = InMemoryMetricsWriter()
    records: list[object] = []
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeStadpiOk(records)))

    assert result.ok is True
    assert _gauge_value(writer, M_DPI_ENABLED, {}) == 1.0
    assert _gauge_value(writer, "homelab_unifi_api_took_seconds", {"endpoint": "stat/stadpi"}) == (
        0.012  # noqa: PLR2004
    )
    assert _drop_value(writer, _DPI_FAMILY) == 0.0
    assert _gauges(writer, M_CLIENT_DPI_BYTES) == []
    assert [e for e in result.events if isinstance(e, SuggestionEvent)] == []


# ============================================================================
# Test 4: [{}] empty-object sentinel -> same as empty (no mac/by_app).
# ============================================================================
@pytest.mark.asyncio
async def test_empty_object_sentinel_no_series() -> None:
    """data == [{}] -> ok=True, no DPI series, drop gauge 0.0 (record has no mac)."""
    writer = InMemoryMetricsWriter()
    records: list[object] = [{}]
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeStadpiOk(records)))

    assert result.ok is True
    assert _gauge_value(writer, M_DPI_ENABLED, {}) == 1.0
    assert _drop_value(writer, _DPI_FAMILY) == 0.0
    assert _gauges(writer, M_CLIENT_DPI_BYTES) == []


# ============================================================================
# Test 5: non-dict payload -> _parse_records returns [] (FALSE dict-guard).
# ============================================================================
@pytest.mark.asyncio
async def test_non_dict_payload_no_series() -> None:
    """payload is a list (not a dict) -> records=[]: ok=True, no DPI series, drop 0.0."""
    writer = InMemoryMetricsWriter()
    payload: list[str] = ["not", "a", "dict"]
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeStadpiRawPayload(payload)))

    assert result.ok is True
    assert _gauge_value(writer, M_DPI_ENABLED, {}) == 1.0
    assert _drop_value(writer, _DPI_FAMILY) == 0.0
    assert _gauges(writer, M_CLIENT_DPI_BYTES) == []


# ============================================================================
# Test 5b: dict payload with non-list data -> records=[] (FALSE data-list guard).
# ============================================================================
@pytest.mark.asyncio
async def test_data_not_a_list_no_series() -> None:
    """payload is a dict whose data is not a list -> records=[]: ok=True, no series."""
    writer = InMemoryMetricsWriter()
    payload: dict[str, object] = {"meta": {"rc": "ok"}, "data": "not-a-list"}
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeStadpiRawPayload(payload)))

    assert result.ok is True
    assert _gauges(writer, M_CLIENT_DPI_BYTES) == []
    assert _drop_value(writer, _DPI_FAMILY) == 0.0


# ============================================================================
# Test 6: record missing mac -> skipped (mac-guard FALSE side).
# ============================================================================
@pytest.mark.asyncio
async def test_record_missing_mac_skipped() -> None:
    """A record with no mac (and one non-str mac) contributes nothing; good one survives."""
    no_mac: dict[str, object] = {"by_app": [_entry(1, 2, rx=10.0, tx=20.0)]}
    non_str_mac: dict[str, object] = {"mac": 123, "by_app": [_entry(1, 2, rx=5.0, tx=5.0)]}
    good = _record("aa:aa:aa:aa:aa:aa", [_entry(1, 2, rx=100.0, tx=200.0)])
    records: list[object] = [no_mac, non_str_mac, good]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeStadpiOk(records)))

    assert result.ok is True
    assert _dpi_clients(writer) == {"aa:aa:aa:aa:aa:aa"}
    assert (
        _gauge_value(
            writer, M_CLIENT_DPI_BYTES, {"client": "aa:aa:aa:aa:aa:aa", "app": "1", "cat": "2"}
        )
        == 300.0  # noqa: PLR2004
    )


# ============================================================================
# Test 7: by_app not a list -> record skipped (by_app-guard FALSE side).
# ============================================================================
@pytest.mark.asyncio
async def test_by_app_not_a_list_skipped() -> None:
    """A record whose by_app is not a list (None / dict) is skipped."""
    by_app_none: dict[str, object] = {"mac": "aa:aa:aa:aa:aa:01", "by_app": None}
    by_app_dict: dict[str, object] = {"mac": "aa:aa:aa:aa:aa:02", "by_app": {"app": 1}}
    records: list[object] = [by_app_none, by_app_dict]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeStadpiOk(records)))

    assert result.ok is True
    assert _gauges(writer, M_CLIENT_DPI_BYTES) == []
    assert _drop_value(writer, _DPI_FAMILY) == 0.0


# ============================================================================
# Test 8: by_app entry non-dict -> skipped (entry-dict-guard FALSE side).
# ============================================================================
@pytest.mark.asyncio
async def test_by_app_entry_non_dict_skipped() -> None:
    """A non-dict by_app entry is skipped; a sibling valid entry still emits."""
    by_app: list[object] = ["not-a-dict", _entry(1, 2, rx=50.0, tx=50.0)]
    rec: dict[str, object] = {"mac": "aa:aa:aa:aa:aa:01", "by_app": by_app}
    records: list[object] = [rec]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeStadpiOk(records)))

    assert result.ok is True
    assert len(_gauges(writer, M_CLIENT_DPI_BYTES)) == 1
    assert (
        _gauge_value(
            writer, M_CLIENT_DPI_BYTES, {"client": "aa:aa:aa:aa:aa:01", "app": "1", "cat": "2"}
        )
        == 100.0  # noqa: PLR2004
    )


# ============================================================================
# Test 9: entry missing app or cat -> skipped (app/cat-present guard FALSE side).
# ============================================================================
@pytest.mark.asyncio
async def test_entry_missing_app_or_cat_skipped() -> None:
    """Entries missing app (and missing cat) are skipped; a complete one emits."""
    missing_app: dict[str, object] = {"cat": 2, "rx_bytes": 10.0, "tx_bytes": 10.0}
    missing_cat: dict[str, object] = {"app": 1, "rx_bytes": 10.0, "tx_bytes": 10.0}
    good = _entry(1, 2, rx=70.0, tx=30.0)
    by_app: list[object] = [missing_app, missing_cat, good]
    rec: dict[str, object] = {"mac": "aa:aa:aa:aa:aa:01", "by_app": by_app}
    records: list[object] = [rec]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeStadpiOk(records)))

    assert result.ok is True
    assert len(_gauges(writer, M_CLIENT_DPI_BYTES)) == 1
    assert (
        _gauge_value(
            writer, M_CLIENT_DPI_BYTES, {"client": "aa:aa:aa:aa:aa:01", "app": "1", "cat": "2"}
        )
        == 100.0  # noqa: PLR2004
    )


# ============================================================================
# Test 10: entry with both rx and tx None/missing -> skipped (both-None guard).
# ============================================================================
@pytest.mark.asyncio
async def test_entry_both_bytes_none_skipped() -> None:
    """Entry with neither rx_bytes nor tx_bytes is skipped (no usable bytes)."""
    no_bytes = _entry(1, 2)  # neither rx nor tx
    good = _entry(3, 4, rx=10.0, tx=10.0)
    by_app: list[object] = [no_bytes, good]
    rec: dict[str, object] = {"mac": "aa:aa:aa:aa:aa:01", "by_app": by_app}
    records: list[object] = [rec]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeStadpiOk(records)))

    assert result.ok is True
    assert len(_gauges(writer, M_CLIENT_DPI_BYTES)) == 1
    assert (
        _gauge_value(
            writer, M_CLIENT_DPI_BYTES, {"client": "aa:aa:aa:aa:aa:01", "app": "3", "cat": "4"}
        )
        == 20.0  # noqa: PLR2004
    )


# ============================================================================
# Test 11: rx-only and tx-only -> total uses the (x or 0.0) branch.
# ============================================================================
@pytest.mark.asyncio
async def test_rx_only_and_tx_only_combine() -> None:
    """rx-only entry total == rx; tx-only entry total == tx (the (x or 0.0) branches)."""
    rx_only = _entry(1, 2, rx=42.0)  # tx None
    tx_only = _entry(3, 4, tx=99.0)  # rx None
    by_app: list[object] = [rx_only, tx_only]
    rec: dict[str, object] = {"mac": "aa:aa:aa:aa:aa:01", "by_app": by_app}
    records: list[object] = [rec]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeStadpiOk(records)))

    assert result.ok is True
    assert (
        _gauge_value(
            writer, M_CLIENT_DPI_BYTES, {"client": "aa:aa:aa:aa:aa:01", "app": "1", "cat": "2"}
        )
        == 42.0  # noqa: PLR2004
    )
    assert (
        _gauge_value(
            writer, M_CLIENT_DPI_BYTES, {"client": "aa:aa:aa:aa:aa:01", "app": "3", "cat": "4"}
        )
        == 99.0  # noqa: PLR2004
    )


# ============================================================================
# Test 12: populated multi-client multi-app UNDER cap -> all survive, drop 0.
# ============================================================================
@pytest.mark.asyncio
async def test_populated_under_cap_all_survive() -> None:
    """Multiple clients/apps under the default cap -> all emit; drop 0; count correct."""
    rec_a = _record(
        "aa:aa:aa:aa:aa:01",
        [_entry(1, 2, rx=100.0, tx=200.0), _entry(3, 4, rx=10.0, tx=5.0)],
    )
    rec_b = _record("bb:bb:bb:bb:bb:01", [_entry(1, 2, rx=1.0, tx=2.0)])
    records: list[object] = [rec_a, rec_b]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeStadpiOk(records)))

    assert result.ok is True
    assert (
        _gauge_value(
            writer, M_CLIENT_DPI_BYTES, {"client": "aa:aa:aa:aa:aa:01", "app": "1", "cat": "2"}
        )
        == 300.0  # noqa: PLR2004
    )
    assert (
        _gauge_value(
            writer, M_CLIENT_DPI_BYTES, {"client": "aa:aa:aa:aa:aa:01", "app": "3", "cat": "4"}
        )
        == 15.0  # noqa: PLR2004
    )
    assert (
        _gauge_value(
            writer, M_CLIENT_DPI_BYTES, {"client": "bb:bb:bb:bb:bb:01", "app": "1", "cat": "2"}
        )
        == 3.0  # noqa: PLR2004
    )
    assert _drop_value(writer, _DPI_FAMILY) == 0.0
    # 3 DPI series + latency + dpi_enabled + drop gauge = 6.
    assert result.metrics_emitted == 6  # noqa: PLR2004


# ============================================================================
# Test 13: OVER cap -> survivors==cap (biggest consumers), drop=excess, 1 event.
# ============================================================================
@pytest.mark.asyncio
async def test_over_cap_keeps_biggest_and_emits_one_suggestion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """cap=2 (via YAML override), feed 4 observations of differing totals.

    Assert: exactly cap survivors, they are the 2 BIGGEST by combined bytes
    (proving BY-VOLUME, not lexical), drop gauge == 4-2 == 2, and exactly ONE
    warning SuggestionEvent.
    """
    cfg = tmp_path / "homelab-monitor.yaml"
    cfg.write_text("cardinality_caps:\n  families:\n    unifi_dpi: 2\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg))

    # Four distinct (client,app,cat) observations with totals 10/20/30/40.
    rec = _record(
        "aa:aa:aa:aa:aa:01",
        [
            _entry(1, 1, rx=10.0),  # total 10 (smallest)
            _entry(2, 2, rx=20.0),  # total 20
            _entry(3, 3, rx=30.0),  # total 30
            _entry(4, 4, rx=40.0),  # total 40 (biggest)
        ],
    )
    records: list[object] = [rec]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeStadpiOk(records)))

    assert result.ok is True
    survivors = _gauges(writer, M_CLIENT_DPI_BYTES)
    assert len(survivors) == 2  # noqa: PLR2004
    survivor_apps = {e.labels["app"] for e in survivors}
    # The two BIGGEST totals (40 -> app 4, 30 -> app 3) survive; 10/20 dropped.
    assert survivor_apps == {"3", "4"}
    assert _drop_value(writer, _DPI_FAMILY) == 2.0  # 4 - 2  # noqa: PLR2004

    suggestions = [e for e in result.events if isinstance(e, SuggestionEvent)]
    assert len(suggestions) == 1
    assert suggestions[0].severity == "warning"


# ============================================================================
# Test 13b: negative cap clamped to zero -> zero survivors, drop == len(obs).
# ============================================================================
@pytest.mark.asyncio
async def test_negative_cap_clamped_to_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """cap=-1 in YAML -> clamped to 0 -> zero survivors, drop gauge == 3, 1 event."""
    cfg = tmp_path / "homelab-monitor.yaml"
    cfg.write_text("cardinality_caps:\n  families:\n    unifi_dpi: -1\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg))

    rec = _record(
        "aa:aa:aa:aa:aa:01",
        [
            _entry(1, 1, rx=10.0),
            _entry(2, 2, rx=20.0),
            _entry(3, 3, rx=30.0),
        ],
    )
    records: list[object] = [rec]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeStadpiOk(records)))

    assert result.ok is True
    assert _gauges(writer, M_CLIENT_DPI_BYTES) == []
    assert _drop_value(writer, _DPI_FAMILY) == 3.0  # noqa: PLR2004
    suggestions = [e for e in result.events if isinstance(e, SuggestionEvent)]
    assert len(suggestions) == 1
    assert suggestions[0].severity == "warning"


# ============================================================================
# Test 14: by-volume ordering (the load-bearing correctness assertion).
# ============================================================================
@pytest.mark.asyncio
async def test_by_volume_ordering_keeps_highest_totals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """cap=2 with 3 observations of differing totals -> the 2 HIGHEST survive.

    This proves the cap is BY VOLUME, not lexical: the lexically-smallest tuple
    (app=1) has the LARGEST total and MUST survive, while a lexically-smaller but
    lower-volume tuple is dropped.
    """
    cfg = tmp_path / "homelab-monitor.yaml"
    cfg.write_text("cardinality_caps:\n  families:\n    unifi_dpi: 2\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg))

    rec = _record(
        "aa:aa:aa:aa:aa:01",
        [
            _entry(1, 1, rx=5.0),  # total 5  -> lexically smallest, LOWEST volume
            _entry(2, 2, rx=500.0),  # total 500
            _entry(3, 3, rx=300.0),  # total 300
        ],
    )
    records: list[object] = [rec]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeStadpiOk(records)))

    assert result.ok is True
    survivor_apps = {e.labels["app"] for e in _gauges(writer, M_CLIENT_DPI_BYTES)}
    # The two highest-volume tuples (500 -> app 2, 300 -> app 3) survive; the
    # lexically-smallest tuple (app 1, total 5) is dropped -> proves by-volume.
    assert survivor_apps == {"2", "3"}
    assert "1" not in survivor_apps
    assert _drop_value(writer, _DPI_FAMILY) == 1.0  # 3 - 2


# ============================================================================
# Test 15: latency value passthrough + metrics_emitted equals recorded gauges.
# ============================================================================
@pytest.mark.asyncio
async def test_metrics_emitted_matches_recorded() -> None:
    """metrics_emitted equals the count of recorded gauge writes."""
    rec = _record("aa:aa:aa:aa:aa:01", [_entry(1, 2, rx=10.0, tx=20.0)])
    records: list[object] = [rec]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeStadpiOk(records, took=0.077)))

    recorded = [e for e in writer.recorded if e.kind == "gauge"]  # pyright: ignore[reportPrivateUsage]
    assert result.metrics_emitted == len(recorded)
    assert (
        _gauge_value(writer, "homelab_unifi_api_took_seconds", {"endpoint": "stat/stadpi"}) == 0.077  # noqa: PLR2004
    )
