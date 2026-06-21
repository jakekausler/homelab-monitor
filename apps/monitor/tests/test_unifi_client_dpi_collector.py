"""Unit tests for UnifiClientDpiCollector (STAGE-007-022).

Covers: the v2 traffic endpoint (epoch-ms window, 24h lookback), the by-volume
cardinality cap (top-N by total bytes), app/category ID→name resolution with
raw-ID fallback, the honest client_records data signal, the always-on
dpi_enabled + latency + drop-gauge indicators, every record/entry/row skip
branch, the total_bytes / fallback rx+tx value logic, and every error path.
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
    M_DPI_CLIENT_RECORDS,
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


class _FakeTrafficOk(_FakeUnifiBase):
    """v2_traffic returns a fixed bare payload."""

    def __init__(self, clients: list[object], took: float = 0.012) -> None:
        self._clients = clients
        self._took = took

    async def v2_traffic(self, start_ms: int, end_ms: int) -> UnifiResponse | UnifiError:
        payload: dict[str, object] = {
            "client_usage_by_app": self._clients,
            "total_usage_by_app": [],
        }
        return UnifiResponse(payload=payload, took_seconds=self._took, endpoint="v2/traffic")


class _FakeTrafficRawPayload(_FakeUnifiBase):
    """v2_traffic returns an arbitrary (possibly non-dict) payload."""

    def __init__(self, payload: object, took: float = 0.01) -> None:
        self._payload = payload
        self._took = took

    async def v2_traffic(self, start_ms: int, end_ms: int) -> UnifiResponse | UnifiError:
        return UnifiResponse(payload=self._payload, took_seconds=self._took, endpoint="v2/traffic")


class _FakeTrafficFail(_FakeUnifiBase):
    """v2_traffic returns a UnifiError."""

    async def v2_traffic(self, start_ms: int, end_ms: int) -> UnifiResponse | UnifiError:
        return UnifiError(reason="timeout", message="traffic timed out")


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


# --- synthetic v2 traffic fixture builders -----------------------------------
def _row(
    application: int, category: int, *, total: object = None, rx: object = None, tx: object = None
) -> dict[str, object]:
    """Build one usage_by_app row; omit total/rx/tx when None is passed."""
    r: dict[str, object] = {"application": application, "category": category}
    if total is not None:
        r["total_bytes"] = total
    if rx is not None:
        r["bytes_received"] = rx
    if tx is not None:
        r["bytes_transmitted"] = tx
    return r


def _client(mac: object, usage: object) -> dict[str, object]:
    """Build one client_usage_by_app entry."""
    return {"client": {"mac": mac}, "usage_by_app": usage}


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
# Test 2: v2_traffic UnifiError -> ok=False, no emits.
# ============================================================================
@pytest.mark.asyncio
async def test_traffic_error_fails() -> None:
    """v2_traffic UnifiError -> ok=False, errors=[message], nothing emitted."""
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeTrafficFail()))
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["traffic timed out"]
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


# ============================================================================
# Test 3: empty clients ([]) -> ok=True, dpi_enabled + latency + records=0, no series.
# ============================================================================
@pytest.mark.asyncio
async def test_empty_clients_emits_indicators_no_series() -> None:
    """client_usage_by_app == [] -> ok=True: dpi_enabled 1.0 + latency + records 0.0 + drop 0.0."""
    writer = InMemoryMetricsWriter()
    clients: list[object] = []
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeTrafficOk(clients)))

    assert result.ok is True
    assert _gauge_value(writer, M_DPI_ENABLED, {}) == 1.0
    assert _gauge_value(writer, "homelab_unifi_api_took_seconds", {"endpoint": "v2/traffic"}) == (
        0.012  # noqa: PLR2004
    )
    assert _gauge_value(writer, M_DPI_CLIENT_RECORDS, {}) == 0.0
    assert _drop_value(writer, _DPI_FAMILY) == 0.0
    assert _gauges(writer, M_CLIENT_DPI_BYTES) == []
    assert [e for e in result.events if isinstance(e, SuggestionEvent)] == []


# ============================================================================
# Test 4: non-dict payload -> _parse_clients returns [] (FALSE dict-guard).
# ============================================================================
@pytest.mark.asyncio
async def test_non_dict_payload_no_series() -> None:
    """payload is a list (not a dict) -> clients=[]: ok=True, no DPI series, records 0."""
    writer = InMemoryMetricsWriter()
    payload: list[str] = ["not", "a", "dict"]
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeTrafficRawPayload(payload)))

    assert result.ok is True
    assert _gauge_value(writer, M_DPI_ENABLED, {}) == 1.0
    assert _gauge_value(writer, M_DPI_CLIENT_RECORDS, {}) == 0.0
    assert _drop_value(writer, _DPI_FAMILY) == 0.0
    assert _gauges(writer, M_CLIENT_DPI_BYTES) == []


# ============================================================================
# Test 4b: dict payload with non-list client_usage_by_app -> clients=[].
# ============================================================================
@pytest.mark.asyncio
async def test_client_usage_not_a_list_no_series() -> None:
    """payload is a dict but client_usage_by_app is not a list -> clients=[]: ok=True."""
    writer = InMemoryMetricsWriter()
    payload: dict[str, object] = {"client_usage_by_app": "not-a-list"}
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeTrafficRawPayload(payload)))

    assert result.ok is True
    assert _gauges(writer, M_CLIENT_DPI_BYTES) == []
    assert _gauge_value(writer, M_DPI_CLIENT_RECORDS, {}) == 0.0


# ============================================================================
# Test 4c: list with non-dict entry -> skipped by list comp.
# ============================================================================
@pytest.mark.asyncio
async def test_client_usage_non_dict_entry_skipped() -> None:
    """A non-dict client_usage_by_app entry is skipped; a sibling valid one survives."""
    non_dict_client: object = "not-a-dict"
    valid_client = _client("aa:aa:aa:aa:aa:01", [_row(1, 2, total=100.0)])
    clients: list[object] = [non_dict_client, valid_client]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeTrafficOk(clients)))

    assert result.ok is True
    assert _dpi_clients(writer) == {"aa:aa:aa:aa:aa:01"}
    assert _gauge_value(writer, M_DPI_CLIENT_RECORDS, {}) == 1.0


# ============================================================================
# Test 5: client entry not a dict -> skipped (client-dict-guard FALSE side).
# ============================================================================
@pytest.mark.asyncio
async def test_client_not_a_dict_skipped() -> None:
    """A client entry where client is not a dict is skipped."""
    clients: list[object] = [
        {"client": "not-a-dict", "usage_by_app": []},
        _client("aa:aa:aa:aa:aa:01", [_row(1, 2, total=100.0)]),
    ]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeTrafficOk(clients)))

    assert result.ok is True
    assert _dpi_clients(writer) == {"aa:aa:aa:aa:aa:01"}
    assert _gauge_value(writer, M_DPI_CLIENT_RECORDS, {}) == 1.0


# ============================================================================
# Test 6: client mac missing/non-str/empty -> skipped (mac-guard FALSE side).
# ============================================================================
@pytest.mark.asyncio
async def test_client_mac_missing_or_nonstr_skipped() -> None:
    """Clients with missing/non-str/empty mac are skipped; good one emits."""
    no_mac: dict[str, object] = {"client": {}, "usage_by_app": [_row(1, 2, total=10.0)]}
    non_str_mac: dict[str, object] = {
        "client": {"mac": 123},
        "usage_by_app": [_row(1, 2, total=10.0)],
    }
    empty_mac: dict[str, object] = {"client": {"mac": ""}, "usage_by_app": [_row(1, 2, total=10.0)]}
    good = _client("aa:aa:aa:aa:aa:aa", [_row(1, 2, total=100.0)])
    clients: list[object] = [no_mac, non_str_mac, empty_mac, good]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeTrafficOk(clients)))

    assert result.ok is True
    assert _dpi_clients(writer) == {"aa:aa:aa:aa:aa:aa"}
    assert _gauge_value(writer, M_DPI_CLIENT_RECORDS, {}) == 1.0


# ============================================================================
# Test 7: usage_by_app not a list -> client skipped (usage-guard FALSE side).
# ============================================================================
@pytest.mark.asyncio
async def test_usage_not_a_list_skipped() -> None:
    """A client whose usage_by_app is not a list (None / dict) is skipped."""
    usage_none: dict[str, object] = {"client": {"mac": "aa:aa:aa:aa:aa:01"}, "usage_by_app": None}
    usage_dict: dict[str, object] = {
        "client": {"mac": "aa:aa:aa:aa:aa:02"},
        "usage_by_app": {"app": 1},
    }
    clients: list[object] = [usage_none, usage_dict]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeTrafficOk(clients)))

    assert result.ok is True
    assert _gauges(writer, M_CLIENT_DPI_BYTES) == []
    assert _gauge_value(writer, M_DPI_CLIENT_RECORDS, {}) == 0.0


# ============================================================================
# Test 8: usage_by_app entry non-dict -> skipped (entry-dict-guard FALSE side).
# ============================================================================
@pytest.mark.asyncio
async def test_usage_row_non_dict_skipped() -> None:
    """A non-dict usage_by_app entry is skipped; a sibling valid row still emits."""
    usage: list[object] = ["not-a-dict", _row(1, 2, total=50.0)]
    client = _client("aa:aa:aa:aa:aa:01", usage)
    clients: list[object] = [client]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeTrafficOk(clients)))

    assert result.ok is True
    assert len(_gauges(writer, M_CLIENT_DPI_BYTES)) == 1
    assert _gauge_value(writer, M_DPI_CLIENT_RECORDS, {}) == 1.0


# ============================================================================
# Test 9: row application/category not int -> skipped (int-guard FALSE side).
# ============================================================================
@pytest.mark.asyncio
async def test_row_app_or_cat_not_int_skipped() -> None:
    """Rows with non-int application or category are skipped; a valid one emits."""
    non_int_app: dict[str, object] = {"application": "not-int", "category": 2, "total_bytes": 10.0}
    non_int_cat: dict[str, object] = {"application": 1, "category": "not-int", "total_bytes": 10.0}
    good = _row(1, 2, total=70.0)
    usage: list[object] = [non_int_app, non_int_cat, good]
    client = _client("aa:aa:aa:aa:aa:01", usage)
    clients: list[object] = [client]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeTrafficOk(clients)))

    assert result.ok is True
    assert len(_gauges(writer, M_CLIENT_DPI_BYTES)) == 1
    assert _gauge_value(writer, M_DPI_CLIENT_RECORDS, {}) == 1.0


# ============================================================================
# Test 10: row no usable bytes (all None/missing) -> skipped (bytes-guard FALSE side).
# ============================================================================
@pytest.mark.asyncio
async def test_row_no_usable_bytes_skipped() -> None:
    """Row with neither total_bytes nor rx/tx is skipped (no usable bytes)."""
    no_bytes = _row(1, 2)  # no total, rx, or tx
    good = _row(3, 4, total=10.0)
    usage: list[object] = [no_bytes, good]
    client = _client("aa:aa:aa:aa:aa:01", usage)
    clients: list[object] = [client]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeTrafficOk(clients)))

    assert result.ok is True
    assert len(_gauges(writer, M_CLIENT_DPI_BYTES)) == 1
    assert _gauge_value(writer, M_DPI_CLIENT_RECORDS, {}) == 1.0


# ============================================================================
# Test 11: total_bytes present and parseable -> value == total_bytes.
# ============================================================================
@pytest.mark.asyncio
async def test_total_bytes_used_when_present() -> None:
    """Row with total_bytes: value == total_bytes."""
    usage = [_row(1, 2, total=999.0)]
    client = _client("aa:aa:aa:aa:aa:01", usage)
    clients: list[object] = [client]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeTrafficOk(clients)))

    assert result.ok is True
    assert (
        _gauge_value(
            writer, M_CLIENT_DPI_BYTES, {"client": "aa:aa:aa:aa:aa:01", "app": "1", "cat": "2"}
        )
        == 999.0  # noqa: PLR2004
    )


# ============================================================================
# Test 11b: total_bytes absent -> fallback to rx + tx.
# ============================================================================
@pytest.mark.asyncio
async def test_falls_back_to_rx_plus_tx_when_no_total() -> None:
    """Row without total_bytes: fallback to bytes_received + bytes_transmitted."""
    # app/cat 900x are absent from the catalog -> raw-string fallback labels.
    rx_only = _row(9001, 9002, rx=42.0)  # no total, no tx
    tx_only = _row(9003, 9004, tx=99.0)  # no total, no rx
    usage: list[object] = [rx_only, tx_only]
    client = _client("aa:aa:aa:aa:aa:01", usage)
    clients: list[object] = [client]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeTrafficOk(clients)))

    assert result.ok is True
    assert (
        _gauge_value(
            writer,
            M_CLIENT_DPI_BYTES,
            {"client": "aa:aa:aa:aa:aa:01", "app": "9001", "cat": "9002"},
        )
        == 42.0  # noqa: PLR2004
    )
    assert (
        _gauge_value(
            writer,
            M_CLIENT_DPI_BYTES,
            {"client": "aa:aa:aa:aa:aa:01", "app": "9003", "cat": "9004"},
        )
        == 99.0  # noqa: PLR2004
    )


# ============================================================================
# Test 12: known app/cat IDs resolve to catalog names (HIT branch).
# ============================================================================
@pytest.mark.asyncio
async def test_known_id_resolves_to_name() -> None:
    """Known app/cat IDs resolve via catalog.

    app=193, cat=4 -> app_key(4, 193) == 262337 == 'Amazon Instant Video';
    cat 4 == 'Media streaming services' (vendored ubntwiki catalog).
    """
    # app=193, cat=4 is in the comprehensive catalog (compound key 262337).
    usage = [_row(193, 4, total=100.0)]
    client = _client("aa:aa:aa:aa:aa:01", usage)
    clients: list[object] = [client]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeTrafficOk(clients)))

    assert result.ok is True
    assert (
        _gauge_value(
            writer,
            M_CLIENT_DPI_BYTES,
            {
                "client": "aa:aa:aa:aa:aa:01",
                "app": "Amazon Instant Video",
                "cat": "Media streaming services",
            },
        )
        == 100.0  # noqa: PLR2004
    )


# ============================================================================
# Test 13: unknown app/cat IDs fallback to stringified raw ID (MISS branch).
# ============================================================================
@pytest.mark.asyncio
async def test_unknown_id_falls_back_to_raw_string() -> None:
    """Unknown app/cat IDs: fallback to raw stringified ID."""
    # app=9999, cat=8888 are not in the catalog.
    usage = [_row(9999, 8888, total=100.0)]
    client = _client("aa:aa:aa:aa:aa:01", usage)
    clients: list[object] = [client]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeTrafficOk(clients)))

    assert result.ok is True
    assert (
        _gauge_value(
            writer,
            M_CLIENT_DPI_BYTES,
            {"client": "aa:aa:aa:aa:aa:01", "app": "9999", "cat": "8888"},
        )
        == 100.0  # noqa: PLR2004
    )


# ============================================================================
# Test 14: records metric counts only clients with ≥1 observation.
# ============================================================================
@pytest.mark.asyncio
async def test_records_metric_counts_contributing_clients() -> None:
    """dpi_client_records == count of clients that contributed ≥1 observation."""
    # Client 1: has valid rows -> contributes.
    # Client 2: all rows skipped (invalid app/cat) -> does NOT contribute.
    # Client 3: has valid row -> contributes.
    valid_row = _row(1, 2, total=100.0)
    invalid_row: dict[str, object] = {"application": "not-int", "category": 2, "total_bytes": 10.0}

    c1 = _client("aa:aa:aa:aa:aa:01", [valid_row])
    c2 = _client("bb:bb:bb:bb:bb:02", [invalid_row])
    c3 = _client("cc:cc:cc:cc:cc:03", [valid_row])

    clients: list[object] = [c1, c2, c3]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeTrafficOk(clients)))

    assert result.ok is True
    assert _gauge_value(writer, M_DPI_CLIENT_RECORDS, {}) == 2.0  # noqa: PLR2004 c1 and c3


# ============================================================================
# Test 15: populated multi-client UNDER cap -> all survive, drop 0, records correct.
# ============================================================================
@pytest.mark.asyncio
async def test_populated_under_cap_all_survive() -> None:
    """Multiple clients/apps under the default cap -> all emit; drop 0."""
    # app/cat 900x are absent from the catalog -> raw-string fallback labels.
    c1 = _client("aa:aa:aa:aa:aa:01", [_row(9001, 9002, total=100.0), _row(9003, 9004, total=10.0)])
    c2 = _client("bb:bb:bb:bb:bb:01", [_row(9001, 9002, total=1.0)])
    clients: list[object] = [c1, c2]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeTrafficOk(clients)))

    assert result.ok is True
    assert (
        _gauge_value(
            writer,
            M_CLIENT_DPI_BYTES,
            {"client": "aa:aa:aa:aa:aa:01", "app": "9001", "cat": "9002"},
        )
        == 100.0  # noqa: PLR2004
    )
    assert (
        _gauge_value(
            writer,
            M_CLIENT_DPI_BYTES,
            {"client": "aa:aa:aa:aa:aa:01", "app": "9003", "cat": "9004"},
        )
        == 10.0  # noqa: PLR2004
    )
    assert (
        _gauge_value(
            writer,
            M_CLIENT_DPI_BYTES,
            {"client": "bb:bb:bb:bb:bb:01", "app": "9001", "cat": "9002"},
        )
        == 1.0
    )
    assert _drop_value(writer, _DPI_FAMILY) == 0.0
    # latency + dpi_enabled + records + 3 DPI series + drop gauge = 7.
    assert result.metrics_emitted == 7  # noqa: PLR2004
    assert _gauge_value(writer, M_DPI_CLIENT_RECORDS, {}) == 2.0  # noqa: PLR2004


# ============================================================================
# Test 16: OVER cap -> survivors==cap (biggest consumers), drop=excess, 1 event.
# ============================================================================
@pytest.mark.asyncio
async def test_over_cap_keeps_biggest_and_emits_one_suggestion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """cap=2 (via YAML override), feed 4 observations of differing totals.

    Assert: exactly cap survivors, they are the 2 BIGGEST by total bytes
    (proving BY-VOLUME, not lexical), drop gauge == 4-2 == 2, and exactly ONE
    warning SuggestionEvent.
    """
    cfg = tmp_path / "homelab-monitor.yaml"
    cfg.write_text("cardinality_caps:\n  families:\n    unifi_dpi: 2\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg))

    # Four distinct (client,app,cat) observations with totals 10/20/30/40.
    # app/cat 900x are absent from the catalog -> raw-string fallback labels.
    c = _client(
        "aa:aa:aa:aa:aa:01",
        [
            _row(9001, 9001, total=10.0),  # total 10 (smallest)
            _row(9002, 9002, total=20.0),  # total 20
            _row(9003, 9003, total=30.0),  # total 30
            _row(9004, 9004, total=40.0),  # total 40 (biggest)
        ],
    )
    clients: list[object] = [c]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeTrafficOk(clients)))

    assert result.ok is True
    survivors = _gauges(writer, M_CLIENT_DPI_BYTES)
    assert len(survivors) == 2  # noqa: PLR2004
    survivor_apps = {e.labels["app"] for e in survivors}
    # The two BIGGEST totals (40 -> app 9004, 30 -> app 9003) survive; 10/20 dropped.
    assert survivor_apps == {"9003", "9004"}
    assert _drop_value(writer, _DPI_FAMILY) == 2.0  # 4 - 2  # noqa: PLR2004

    suggestions = [e for e in result.events if isinstance(e, SuggestionEvent)]
    assert len(suggestions) == 1
    assert suggestions[0].severity == "warning"


# ============================================================================
# Test 16b: negative cap clamped to zero -> zero survivors, drop == len(obs).
# ============================================================================
@pytest.mark.asyncio
async def test_negative_cap_clamped_to_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """cap=-1 in YAML -> clamped to 0 -> zero survivors, drop gauge == 3, 1 event."""
    cfg = tmp_path / "homelab-monitor.yaml"
    cfg.write_text("cardinality_caps:\n  families:\n    unifi_dpi: -1\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg))

    c = _client(
        "aa:aa:aa:aa:aa:01",
        [
            _row(1, 1, total=10.0),
            _row(2, 2, total=20.0),
            _row(3, 3, total=30.0),
        ],
    )
    clients: list[object] = [c]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeTrafficOk(clients)))

    assert result.ok is True
    assert _gauges(writer, M_CLIENT_DPI_BYTES) == []
    assert _drop_value(writer, _DPI_FAMILY) == 3.0  # noqa: PLR2004
    suggestions = [e for e in result.events if isinstance(e, SuggestionEvent)]
    assert len(suggestions) == 1
    assert suggestions[0].severity == "warning"


# ============================================================================
# Test 17: by-volume ordering (the load-bearing correctness assertion).
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

    # app/cat 900x are absent from the catalog -> raw-string fallback labels.
    c = _client(
        "aa:aa:aa:aa:aa:01",
        [
            _row(9001, 9001, total=5.0),  # total 5  -> lexically smallest, LOWEST volume
            _row(9002, 9002, total=500.0),  # total 500
            _row(9003, 9003, total=300.0),  # total 300
        ],
    )
    clients: list[object] = [c]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeTrafficOk(clients)))

    assert result.ok is True
    survivor_apps = {e.labels["app"] for e in _gauges(writer, M_CLIENT_DPI_BYTES)}
    # The two highest-volume tuples (500 -> app 9002, 300 -> app 9003) survive; the
    # lexically-smallest tuple (app 9001, total 5) is dropped -> proves by-volume.
    assert survivor_apps == {"9002", "9003"}
    assert "9001" not in survivor_apps
    assert _drop_value(writer, _DPI_FAMILY) == 1.0  # 3 - 2


# ============================================================================
# Test 18: latency value passthrough + metrics_emitted equals recorded gauges.
# ============================================================================
@pytest.mark.asyncio
async def test_metrics_emitted_matches_recorded() -> None:
    """metrics_emitted equals the count of recorded gauge writes."""
    c = _client("aa:aa:aa:aa:aa:01", [_row(1, 2, total=10.0)])
    clients: list[object] = [c]
    writer = InMemoryMetricsWriter()
    result = await UnifiClientDpiCollector().run(_ctx(writer, _FakeTrafficOk(clients, took=0.077)))

    recorded = [e for e in writer.recorded if e.kind == "gauge"]  # pyright: ignore[reportPrivateUsage]
    assert result.metrics_emitted == len(recorded)
    assert (
        _gauge_value(writer, "homelab_unifi_api_took_seconds", {"endpoint": "v2/traffic"}) == 0.077  # noqa: PLR2004
    )
