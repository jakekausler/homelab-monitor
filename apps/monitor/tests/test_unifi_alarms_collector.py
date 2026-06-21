"""Unit tests for UnifiAlarmsCollector (STAGE-007-010).

Covers the always-present total count (threat_count == 0.0 on empty), the
per-type breakdown via the key -> subsystem -> unknown fallback chain, the
defensive _id dedup, every record-skip branch (missing / empty / non-string
_id, duplicate _id), the malformed-payload paths (non-dict payload, non-list
data, non-dict entry), and both error paths (None client, UnifiError).
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
from homelab_monitor.plugins.collectors.integrations.unifi.alarms import (
    M_THREAT,
    M_THREAT_COUNT,
    UnifiAlarmsCollector,
    _parse_records,  # pyright: ignore[reportPrivateUsage]
    _threat_type,  # pyright: ignore[reportPrivateUsage]
)

_ALARM_ENDPOINT = "rest/alarm?archived=false"


# --- assertion helpers (mirror test_unifi_client_dpi_collector.py) ----------
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


def _threat_types(writer: InMemoryMetricsWriter) -> set[str]:
    """Return the set of {type} label values emitted in the threat family."""
    return {e.labels["type"] for e in _gauges(writer, M_THREAT)}


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


class _FakeAlarmOk(_FakeUnifiBase):
    """rest_alarm returns a fixed classic payload wrapping the given records."""

    def __init__(self, records: list[object], took: float = 0.012) -> None:
        self._records = records
        self._took = took

    async def rest_alarm(self) -> UnifiResponse | UnifiError:
        payload: dict[str, object] = {"meta": {"rc": "ok"}, "data": self._records}
        return UnifiResponse(payload=payload, took_seconds=self._took, endpoint=_ALARM_ENDPOINT)


class _FakeAlarmRawPayload(_FakeUnifiBase):
    """rest_alarm returns an arbitrary (possibly non-dict) payload."""

    def __init__(self, payload: object, took: float = 0.01) -> None:
        self._payload = payload
        self._took = took

    async def rest_alarm(self) -> UnifiResponse | UnifiError:
        return UnifiResponse(
            payload=self._payload, took_seconds=self._took, endpoint=_ALARM_ENDPOINT
        )


class _FakeAlarmFail(_FakeUnifiBase):
    """rest_alarm returns a UnifiError."""

    async def rest_alarm(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="timeout", message="alarm timed out")


def _ctx(writer: InMemoryMetricsWriter, unifi: object | None) -> CollectorContext:
    """Build a CollectorContext. Mirrors the client_dpi test's _ctx(); db/http/
    ssh/secrets are unused by this collector. Note: CollectorConfig has
    extra='forbid' -- do NOT pass concurrency_group (it is a ClassVar)."""
    return CollectorContext(
        config=CollectorConfig(
            name="unifi_threat",
            interval_seconds=60,
            timeout_seconds=15,
        ),
        db=None,  # type: ignore[arg-type]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="unifi_threat"),
        unifi=unifi,  # type: ignore[arg-type]
    )


# --- synthetic rest/alarm fixture builder -------------------------------------
def _alarm(rid: object, *, key: object = None, subsystem: object = None) -> dict[str, object]:
    """Build one alarm record; include _id/key/subsystem only when provided.

    When ``rid`` is None the _id key is omitted entirely (to exercise the
    missing-_id branch). Pass an explicit value (str / "" / int) for the others.
    """
    rec: dict[str, object] = {}
    if rid is not None:
        rec["_id"] = rid
    if key is not None:
        rec["key"] = key
    if subsystem is not None:
        rec["subsystem"] = subsystem
    return rec


# ============================================================================
# _threat_type unit tests (all three fallback branches in isolation).
# ============================================================================
def test_threat_type_uses_key() -> None:
    """A valid string key wins -> type == key."""
    rec: dict[str, object] = {"key": "EVT_IPS_IpsAlert", "subsystem": "ips"}
    assert _threat_type(rec) == "EVT_IPS_IpsAlert"


def test_threat_type_falls_back_to_subsystem() -> None:
    """No usable key (missing / empty / non-str) -> falls back to subsystem."""
    missing: dict[str, object] = {"subsystem": "ips"}
    empty_key: dict[str, object] = {"key": "", "subsystem": "ids"}
    non_str_key: dict[str, object] = {"key": 123, "subsystem": "honeypot"}
    assert _threat_type(missing) == "ips"
    assert _threat_type(empty_key) == "ids"
    assert _threat_type(non_str_key) == "honeypot"


def test_threat_type_unknown_when_neither() -> None:
    """Neither key nor subsystem usable -> "unknown"."""
    empty: dict[str, object] = {}
    empty_both: dict[str, object] = {"key": "", "subsystem": ""}
    assert _threat_type(empty) == "unknown"
    assert _threat_type(empty_both) == "unknown"


# ============================================================================
# _parse_records unit tests (malformed-payload branches in isolation).
# ============================================================================
def test_parse_records_non_dict_payload() -> None:
    """Non-dict payload -> []."""
    assert _parse_records(["not", "a", "dict"]) == []


def test_parse_records_data_not_a_list() -> None:
    """Dict payload whose data is not a list -> []."""
    payload: dict[str, object] = {"meta": {}, "data": None}
    assert _parse_records(payload) == []


def test_parse_records_skips_non_dict_entries() -> None:
    """Non-dict entries in data are skipped; dict entries are kept."""
    good: dict[str, object] = {"_id": "a"}
    payload: dict[str, object] = {"data": ["string-entry", 42, good]}
    assert _parse_records(payload) == [good]


# ============================================================================
# Test 1: None client -> ok=False, no emits.
# ============================================================================
@pytest.mark.asyncio
async def test_none_client_fails() -> None:
    """ctx.unifi is None -> ok=False, metrics_emitted==0, nothing emitted."""
    writer = InMemoryMetricsWriter()
    result = await UnifiAlarmsCollector().run(_ctx(writer, None))
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["unifi client not configured"]
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


# ============================================================================
# Test 2: rest_alarm UnifiError -> ok=False, no emits.
# ============================================================================
@pytest.mark.asyncio
async def test_alarm_error_fails() -> None:
    """rest_alarm UnifiError -> ok=False, errors=[message], nothing emitted."""
    writer = InMemoryMetricsWriter()
    result = await UnifiAlarmsCollector().run(_ctx(writer, _FakeAlarmFail()))
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["alarm timed out"]
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


# ============================================================================
# Test 3: empty data ([]) -> ok=True, latency + threat_count=0.0, no per-type.
# ============================================================================
@pytest.mark.asyncio
async def test_empty_data_emits_count_zero_no_types() -> None:
    """data == [] -> ok=True: latency + threat_count 0.0, no per-type series."""
    writer = InMemoryMetricsWriter()
    records: list[object] = []
    result = await UnifiAlarmsCollector().run(_ctx(writer, _FakeAlarmOk(records)))

    assert result.ok is True
    assert (
        _gauge_value(writer, "homelab_unifi_api_took_seconds", {"endpoint": _ALARM_ENDPOINT})
        == 0.012  # noqa: PLR2004
    )
    assert _gauge_value(writer, M_THREAT_COUNT, {}) == 0.0
    assert _gauges(writer, M_THREAT) == []
    # latency + count only.
    assert result.metrics_emitted == 2  # noqa: PLR2004


# ============================================================================
# Test 4: non-dict payload -> _parse_records [] (FALSE dict-guard).
# ============================================================================
@pytest.mark.asyncio
async def test_non_dict_payload_count_zero() -> None:
    """payload is a list (not a dict) -> records=[]: ok=True, count 0.0, latency."""
    writer = InMemoryMetricsWriter()
    payload: list[str] = ["not", "a", "dict"]
    result = await UnifiAlarmsCollector().run(_ctx(writer, _FakeAlarmRawPayload(payload)))

    assert result.ok is True
    assert _gauge_value(writer, M_THREAT_COUNT, {}) == 0.0
    assert (
        _gauge_value(writer, "homelab_unifi_api_took_seconds", {"endpoint": _ALARM_ENDPOINT})
        == 0.01  # noqa: PLR2004
    )
    assert _gauges(writer, M_THREAT) == []


# ============================================================================
# Test 5: dict payload with non-list data -> [] (FALSE data-list guard).
# ============================================================================
@pytest.mark.asyncio
async def test_data_not_a_list_count_zero() -> None:
    """payload is a dict whose data is not a list -> records=[]: ok=True, count 0.0."""
    writer = InMemoryMetricsWriter()
    payload: dict[str, object] = {"meta": {"rc": "ok"}, "data": None}
    result = await UnifiAlarmsCollector().run(_ctx(writer, _FakeAlarmRawPayload(payload)))

    assert result.ok is True
    assert _gauge_value(writer, M_THREAT_COUNT, {}) == 0.0
    assert _gauges(writer, M_THREAT) == []


# ============================================================================
# Test 6: non-dict entry in data -> skipped by _parse_records.
# ============================================================================
@pytest.mark.asyncio
async def test_non_dict_entry_skipped() -> None:
    """A string entry alongside a valid record -> string skipped, record counted."""
    good = _alarm("id-1", key="EVT_IPS_IpsAlert")
    records: list[object] = ["not-a-dict", good]
    writer = InMemoryMetricsWriter()
    result = await UnifiAlarmsCollector().run(_ctx(writer, _FakeAlarmOk(records)))

    assert result.ok is True
    assert _gauge_value(writer, M_THREAT_COUNT, {}) == 1.0
    assert _gauge_value(writer, M_THREAT, {"type": "EVT_IPS_IpsAlert"}) == 1.0


# ============================================================================
# Test 7: missing / empty / non-string _id -> skipped (id-guard FALSE side).
# ============================================================================
@pytest.mark.asyncio
async def test_unusable_id_records_skipped() -> None:
    """Records with missing, empty-string, or non-string _id contribute nothing."""
    no_id = _alarm(None, key="EVT_IPS_IpsAlert")  # _id omitted entirely
    empty_id = _alarm("", key="EVT_IPS_IpsAlert")  # _id == ""
    non_str_id = _alarm(123, key="EVT_IPS_IpsAlert")  # _id is an int
    good = _alarm("id-1", key="EVT_IPS_IpsAlert")
    records: list[object] = [no_id, empty_id, non_str_id, good]
    writer = InMemoryMetricsWriter()
    result = await UnifiAlarmsCollector().run(_ctx(writer, _FakeAlarmOk(records)))

    assert result.ok is True
    # Only the one record with a usable _id counts.
    assert _gauge_value(writer, M_THREAT_COUNT, {}) == 1.0
    assert _gauge_value(writer, M_THREAT, {"type": "EVT_IPS_IpsAlert"}) == 1.0


# ============================================================================
# Test 8: duplicate _id -> counted once (rid in seen_ids branch).
# ============================================================================
@pytest.mark.asyncio
async def test_duplicate_id_counted_once() -> None:
    """Two records sharing the same _id are counted as one distinct alarm."""
    dup_a = _alarm("dup-1", key="EVT_IPS_IpsAlert")
    dup_b = _alarm("dup-1", key="EVT_IPS_IpsAlert")  # same _id
    distinct = _alarm("id-2", key="EVT_IPS_IpsAlert")
    records: list[object] = [dup_a, dup_b, distinct]
    writer = InMemoryMetricsWriter()
    result = await UnifiAlarmsCollector().run(_ctx(writer, _FakeAlarmOk(records)))

    assert result.ok is True
    # Two DISTINCT ids -> count 2, both of the same type.
    assert _gauge_value(writer, M_THREAT_COUNT, {}) == 2.0  # noqa: PLR2004
    assert _gauge_value(writer, M_THREAT, {"type": "EVT_IPS_IpsAlert"}) == 2.0  # noqa: PLR2004


# ============================================================================
# Test 9: type fallback chain across distinct records (key / subsystem / unknown).
# ============================================================================
@pytest.mark.asyncio
async def test_type_fallback_chain_emits_per_type() -> None:
    """key -> subsystem -> unknown each produce a distinct per-type series."""
    by_key = _alarm("id-1", key="EVT_IPS_IpsAlert")
    by_subsystem = _alarm("id-2", subsystem="ids")  # no key
    by_unknown = _alarm("id-3")  # neither key nor subsystem
    records: list[object] = [by_key, by_subsystem, by_unknown]
    writer = InMemoryMetricsWriter()
    result = await UnifiAlarmsCollector().run(_ctx(writer, _FakeAlarmOk(records)))

    assert result.ok is True
    assert _gauge_value(writer, M_THREAT_COUNT, {}) == 3.0  # noqa: PLR2004
    assert _threat_types(writer) == {"EVT_IPS_IpsAlert", "ids", "unknown"}
    assert _gauge_value(writer, M_THREAT, {"type": "EVT_IPS_IpsAlert"}) == 1.0
    assert _gauge_value(writer, M_THREAT, {"type": "ids"}) == 1.0
    assert _gauge_value(writer, M_THREAT, {"type": "unknown"}) == 1.0


# ============================================================================
# Test 10: populated multi-type payload -> correct total + per-type counts.
# ============================================================================
@pytest.mark.asyncio
async def test_populated_multi_type_counts() -> None:
    """2 alarms of one key + 1 of another -> total 3, per-type 2 and 1."""
    a1 = _alarm("id-1", key="EVT_IPS_IpsAlert")
    a2 = _alarm("id-2", key="EVT_IPS_IpsAlert")
    a3 = _alarm("id-3", key="EVT_IDS_X")
    records: list[object] = [a1, a2, a3]
    writer = InMemoryMetricsWriter()
    result = await UnifiAlarmsCollector().run(_ctx(writer, _FakeAlarmOk(records)))

    assert result.ok is True
    assert _gauge_value(writer, M_THREAT_COUNT, {}) == 3.0  # noqa: PLR2004
    assert _gauge_value(writer, M_THREAT, {"type": "EVT_IPS_IpsAlert"}) == 2.0  # noqa: PLR2004
    assert _gauge_value(writer, M_THREAT, {"type": "EVT_IDS_X"}) == 1.0
    assert _threat_types(writer) == {"EVT_IPS_IpsAlert", "EVT_IDS_X"}


# ============================================================================
# Test 11: metrics_emitted equals the count of recorded gauge writes.
# ============================================================================
@pytest.mark.asyncio
async def test_metrics_emitted_matches_recorded() -> None:
    """metrics_emitted == latency + count + one per present type."""
    a1 = _alarm("id-1", key="EVT_IPS_IpsAlert")
    a2 = _alarm("id-2", key="EVT_IDS_X")
    records: list[object] = [a1, a2]
    writer = InMemoryMetricsWriter()
    result = await UnifiAlarmsCollector().run(_ctx(writer, _FakeAlarmOk(records, took=0.077)))

    recorded = [e for e in writer.recorded if e.kind == "gauge"]  # pyright: ignore[reportPrivateUsage]
    assert result.metrics_emitted == len(recorded)
    # latency + count + 2 types == 4.
    assert result.metrics_emitted == 4  # noqa: PLR2004
    assert (
        _gauge_value(writer, "homelab_unifi_api_took_seconds", {"endpoint": _ALARM_ENDPOINT})
        == 0.077  # noqa: PLR2004
    )
