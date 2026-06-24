"""Unit tests for the synology_security collector (STAGE-008-013, fixture-based).

100% branch coverage of security.py. Field names + payload shapes are LIVE-VERIFIED
(captured JSON: items is a DICT keyed by category, lastScanTime a bare epoch string,
connection has top-level total). Exercises the CO-EQUAL combine (ok=False ONLY when BOTH
fetches fail), the always-emit security_status (seeded 2.0) / security_safe (seeded 0.0)
baselines, the emit-on-success active_connections seed-0 break, and every conditional
guard's BOTH sides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from homelab_monitor.kernel.plugins.context import CollectorContext

from homelab_monitor.kernel.plugins.io import MemoryRetainingMetricsWriter
from homelab_monitor.kernel.synology.client import SynologyResponse
from homelab_monitor.kernel.synology.errors import SynologyError
from homelab_monitor.plugins.collectors.integrations.synology.security import (
    M_ACTIVE_CONNECTIONS,
    M_SECURITY_FINDINGS,
    M_SECURITY_FINDINGS_TOTAL,
    M_SECURITY_LAST_SCAN_AGE_SECONDS,
    M_SECURITY_LAST_SCAN_TIMESTAMP,
    M_SECURITY_SAFE,
    M_SECURITY_STATUS,
    SynologySecurityCollector,
)

_API_TOOK = "homelab_synology_api_took_seconds"
_DROP = "homelab_metric_family_dropped_series"

_EXPECTED_INTERVAL = 3600.0
_EXPECTED_TIMEOUT = 30.0

# 7 cap-routed families emitted by _emit.
_FAMILY_COUNT = 7
# Two co-equal fetches: security + connection.
_EXPECTED_API_TOOK_COUNT = 2

# Findings grid dimensions.
_GRID_CELLS = 25  # 5 categories x 5 severities
_TOTALS_COUNT = 5  # one per severity

# Status numeric values.
_STATUS_SAFE = 0.0
_STATUS_WARNING = 1.0
_STATUS_RISK = 2.0
_STATUS_DANGER = 3.0
_STATUS_BASELINE = 2.0

_CONN_TOTAL_5 = 5.0
_LIVE_SCAN_EPOCH = "1782090303"


def _resp(payload: object, endpoint: str) -> SynologyResponse:
    return SynologyResponse(payload=payload, took_seconds=0.5, endpoint=endpoint)


def _sec_resp(payload: object) -> SynologyResponse:
    return _resp(payload, "SYNO.Core.SecurityScan.Status/system_get")


def _conn_resp(payload: object) -> SynologyResponse:
    return _resp(payload, "SYNO.Core.CurrentConnection/list")


def _zero_fail() -> dict[str, object]:
    return {"danger": 0, "info": 0, "outOfDate": 0, "risk": 0, "warning": 0}


def _live_security_payload() -> dict[str, object]:
    """The verified live shape: sysStatus risk, userInfo.fail.risk=1, others 0."""
    return {
        "items": {
            "malware": {"category": "malware", "fail": _zero_fail()},
            "network": {"category": "network", "fail": _zero_fail()},
            "systemCheck": {"category": "systemCheck", "fail": _zero_fail()},
            "update": {"category": "update", "fail": _zero_fail()},
            "userInfo": {
                "category": "userInfo",
                "fail": {"danger": 0, "info": 0, "outOfDate": 0, "risk": 1, "warning": 0},
            },
        },
        "lastScanTime": _LIVE_SCAN_EPOCH,
        "success": True,
        "sysProgress": 100,
        "sysStatus": "risk",
    }


def _live_connection_payload() -> dict[str, object]:
    return {"items": [], "systime": "Wed Jun 24 06:44:08 2026\n", "total": 5}


class _FakeSynology:
    """Stand-in for ctx.synology with 2 independently programmable methods."""

    def __init__(self, security: object = None, connection: object = None) -> None:
        self._security = security if security is not None else _sec_resp(_live_security_payload())
        self._connection = (
            connection if connection is not None else _conn_resp(_live_connection_payload())
        )

    async def security_scan_status(self) -> object:
        return self._security

    async def current_connection_list(self) -> object:
        return self._connection


@dataclass
class _Ctx:
    """Typed stand-in for CollectorContext used in security tests."""

    vm: MemoryRetainingMetricsWriter = field(default_factory=MemoryRetainingMetricsWriter)
    synology: object = None


def _ctx(writer: MemoryRetainingMetricsWriter, synology: object) -> _Ctx:
    return _Ctx(vm=writer, synology=synology)


def _gauges_named(
    writer: MemoryRetainingMetricsWriter, name: str
) -> list[tuple[str, float, dict[str, str]]]:
    return [g for g in writer.gauges if g[0] == name]


# === Test cases: 0-16 (100% branch coverage) ===


def test_security_classvars() -> None:
    """Verify collector class variables."""
    assert SynologySecurityCollector.name == "synology_security"
    assert SynologySecurityCollector.interval == timedelta(seconds=3600)
    assert SynologySecurityCollector.interval.total_seconds() == _EXPECTED_INTERVAL
    assert SynologySecurityCollector.timeout == timedelta(seconds=30)
    assert SynologySecurityCollector.timeout.total_seconds() == _EXPECTED_TIMEOUT
    assert SynologySecurityCollector.concurrency_group == "synology"


# Row 1: Full live shape (success path, items-is-dict, findings grid + totals, etc.)
@pytest.mark.asyncio
async def test_security_full_live_shape() -> None:
    """Full live security + connection payloads, all families emit."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology()))
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_SECURITY_STATUS) == [(M_SECURITY_STATUS, _STATUS_RISK, {})]
    assert _gauges_named(writer, M_SECURITY_SAFE) == [(M_SECURITY_SAFE, 0.0, {})]
    assert len(_gauges_named(writer, M_SECURITY_FINDINGS)) == _GRID_CELLS
    by_cell = {
        (g[2]["category"], g[2]["severity"]): g[1]
        for g in _gauges_named(writer, M_SECURITY_FINDINGS)
    }
    assert by_cell[("userInfo", "risk")] == 1.0
    assert by_cell[("malware", "danger")] == 0.0
    assert len(_gauges_named(writer, M_SECURITY_FINDINGS_TOTAL)) == _TOTALS_COUNT
    by_sev = {g[2]["severity"]: g[1] for g in _gauges_named(writer, M_SECURITY_FINDINGS_TOTAL)}
    assert by_sev["risk"] == 1.0
    assert by_sev["danger"] == 0.0
    assert len(_gauges_named(writer, M_SECURITY_LAST_SCAN_TIMESTAMP)) == 1
    assert _gauges_named(writer, M_SECURITY_LAST_SCAN_TIMESTAMP)[0][1] == float(_LIVE_SCAN_EPOCH)
    assert len(_gauges_named(writer, M_SECURITY_LAST_SCAN_AGE_SECONDS)) == 1
    assert _gauges_named(writer, M_SECURITY_LAST_SCAN_AGE_SECONDS)[0][1] >= 0.0
    assert _gauges_named(writer, M_ACTIVE_CONNECTIONS) == [
        (M_ACTIVE_CONNECTIONS, _CONN_TOTAL_5, {})
    ]


# Row 2: sysStatus="safe", findings all zero (status mapped, safe=1.0 branch)
@pytest.mark.asyncio
async def test_security_status_safe() -> None:
    """sysStatus=safe -> status=0.0, safe=1.0."""
    payload = _live_security_payload()
    payload["sysStatus"] = "safe"
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology(security=_sec_resp(payload))))
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_SECURITY_STATUS) == [(M_SECURITY_STATUS, _STATUS_SAFE, {})]
    assert _gauges_named(writer, M_SECURITY_SAFE) == [(M_SECURITY_SAFE, 1.0, {})]


# Row 3a: sysStatus="warning"
@pytest.mark.asyncio
async def test_security_status_warning() -> None:
    """sysStatus=warning -> status=1.0, safe=0.0."""
    payload = _live_security_payload()
    payload["sysStatus"] = "warning"
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology(security=_sec_resp(payload))))
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_SECURITY_STATUS) == [(M_SECURITY_STATUS, _STATUS_WARNING, {})]
    assert _gauges_named(writer, M_SECURITY_SAFE) == [(M_SECURITY_SAFE, 0.0, {})]


# Row 3b: sysStatus="danger"
@pytest.mark.asyncio
async def test_security_status_danger() -> None:
    """sysStatus=danger -> status=3.0, safe=0.0."""
    payload = _live_security_payload()
    payload["sysStatus"] = "danger"
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology(security=_sec_resp(payload))))
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_SECURITY_STATUS) == [(M_SECURITY_STATUS, _STATUS_DANGER, {})]
    assert _gauges_named(writer, M_SECURITY_SAFE) == [(M_SECURITY_SAFE, 0.0, {})]


# Row 4: sysStatus unknown string -> clamp to 3.0
@pytest.mark.asyncio
async def test_security_status_unknown_string() -> None:
    """sysStatus=bogus -> clamped to 3.0, safe=0.0."""
    payload = _live_security_payload()
    payload["sysStatus"] = "bogus"
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology(security=_sec_resp(payload))))
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_SECURITY_STATUS) == [(M_SECURITY_STATUS, _STATUS_DANGER, {})]
    assert _gauges_named(writer, M_SECURITY_SAFE) == [(M_SECURITY_SAFE, 0.0, {})]


# Row 5: sysStatus absent -> seeded baseline survives
@pytest.mark.asyncio
async def test_security_status_absent() -> None:
    """sysStatus absent -> status stays seeded 2.0, safe stays 0.0."""
    payload = _live_security_payload()
    del payload["sysStatus"]
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology(security=_sec_resp(payload))))
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_SECURITY_STATUS) == [(M_SECURITY_STATUS, _STATUS_BASELINE, {})]
    assert _gauges_named(writer, M_SECURITY_SAFE) == [(M_SECURITY_SAFE, 0.0, {})]
    # Findings grid still emitted (items present).
    assert len(_gauges_named(writer, M_SECURITY_FINDINGS)) == _GRID_CELLS


# Row 5b: sysStatus present but non-str (int) -> isinstance guard False, seeded baseline survives
@pytest.mark.asyncio
async def test_security_status_present_non_str() -> None:
    """sysStatus present but non-str (int 123) -> status stays seeded 2.0, safe stays 0.0."""
    payload = _live_security_payload()
    payload["sysStatus"] = 123
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology(security=_sec_resp(payload))))
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_SECURITY_STATUS) == [(M_SECURITY_STATUS, _STATUS_BASELINE, {})]
    assert _gauges_named(writer, M_SECURITY_SAFE) == [(M_SECURITY_SAFE, 0.0, {})]
    # Findings grid still emitted (items present).
    assert len(_gauges_named(writer, M_SECURITY_FINDINGS)) == _GRID_CELLS


# Row 6: One category's fail dict missing a severity
@pytest.mark.asyncio
async def test_security_findings_missing_severity_key() -> None:
    """Missing severity key -> emits 0.0 for that cell."""
    payload = _live_security_payload()
    items_cast = cast("dict[str, dict[str, object]]", payload["items"])
    items_cast["userInfo"]["fail"] = {"danger": 0, "risk": 1}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology(security=_sec_resp(payload))))
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    by_cell = {
        (g[2]["category"], g[2]["severity"]): g[1]
        for g in _gauges_named(writer, M_SECURITY_FINDINGS)
    }
    assert by_cell[("userInfo", "info")] == 0.0
    assert by_cell[("userInfo", "warning")] == 0.0
    assert by_cell[("userInfo", "outOfDate")] == 0.0
    assert by_cell[("userInfo", "risk")] == 1.0
    assert len(_gauges_named(writer, M_SECURITY_FINDINGS)) == _GRID_CELLS


# Row 7: Category entirely absent from items dict
@pytest.mark.asyncio
async def test_security_category_absent() -> None:
    """Missing category -> all 5 cells for that category emit 0.0, grid still 25."""
    payload = _live_security_payload()
    del cast("dict[str, object]", payload["items"])["update"]
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology(security=_sec_resp(payload))))
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    by_cell = {
        (g[2]["category"], g[2]["severity"]): g[1]
        for g in _gauges_named(writer, M_SECURITY_FINDINGS)
    }
    for sev in ["danger", "warning", "risk", "outOfDate", "info"]:
        assert by_cell[("update", sev)] == 0.0
    assert len(_gauges_named(writer, M_SECURITY_FINDINGS)) == _GRID_CELLS


# Row 8a: Scan time present (epoch string)
@pytest.mark.asyncio
async def test_security_scan_time_present_string() -> None:
    """lastScanTime present (epoch string) -> timestamp + age emitted."""
    payload = _live_security_payload()
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology(security=_sec_resp(payload))))
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert len(_gauges_named(writer, M_SECURITY_LAST_SCAN_TIMESTAMP)) == 1
    assert _gauges_named(writer, M_SECURITY_LAST_SCAN_TIMESTAMP)[0][1] == float(_LIVE_SCAN_EPOCH)
    assert len(_gauges_named(writer, M_SECURITY_LAST_SCAN_AGE_SECONDS)) == 1
    assert _gauges_named(writer, M_SECURITY_LAST_SCAN_AGE_SECONDS)[0][1] >= 0.0


# Row 8b: Scan time absent
@pytest.mark.asyncio
async def test_security_scan_time_absent() -> None:
    """lastScanTime absent -> timestamp + age families empty."""
    payload = _live_security_payload()
    del payload["lastScanTime"]
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology(security=_sec_resp(payload))))
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_SECURITY_LAST_SCAN_TIMESTAMP) == []
    assert _gauges_named(writer, M_SECURITY_LAST_SCAN_AGE_SECONDS) == []
    # Status/safe still emitted.
    assert len(_gauges_named(writer, M_SECURITY_STATUS)) == 1
    assert len(_gauges_named(writer, M_SECURITY_SAFE)) == 1


# Row 8c: Scan time garbage (unparseable)
@pytest.mark.asyncio
async def test_security_scan_time_garbage() -> None:
    """lastScanTime garbage -> timestamp + age families empty."""
    payload = _live_security_payload()
    payload["lastScanTime"] = "not-a-time"
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology(security=_sec_resp(payload))))
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_SECURITY_LAST_SCAN_TIMESTAMP) == []
    assert _gauges_named(writer, M_SECURITY_LAST_SCAN_AGE_SECONDS) == []


# Row 8d: Scan time as datetime string (strptime path)
_DATETIME_STR_SCAN_TIME = "2026-06-24 06:44:08"
_DATETIME_STR_EXPECTED_EPOCH = 1782283448.0


@pytest.mark.asyncio
async def test_security_scan_time_datetime_string() -> None:
    """lastScanTime as '%Y-%m-%d %H:%M:%S' string -> timestamp + age emitted via strptime."""
    payload = _live_security_payload()
    payload["lastScanTime"] = _DATETIME_STR_SCAN_TIME
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology(security=_sec_resp(payload))))
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert len(_gauges_named(writer, M_SECURITY_LAST_SCAN_TIMESTAMP)) == 1
    actual_epoch = _gauges_named(writer, M_SECURITY_LAST_SCAN_TIMESTAMP)[0][1]
    assert actual_epoch == _DATETIME_STR_EXPECTED_EPOCH
    assert len(_gauges_named(writer, M_SECURITY_LAST_SCAN_AGE_SECONDS)) == 1
    assert _gauges_named(writer, M_SECURITY_LAST_SCAN_AGE_SECONDS)[0][1] >= 0.0


# Row 9: Items is not a dict
@pytest.mark.asyncio
async def test_security_items_not_a_dict() -> None:
    """items is a list (or non-dict) -> findings grid/totals absent, status/safe/timestamp still."""
    payload = _live_security_payload()
    payload["items"] = []
    payload["sysStatus"] = "risk"
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology(security=_sec_resp(payload))))
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_SECURITY_FINDINGS) == []
    assert _gauges_named(writer, M_SECURITY_FINDINGS_TOTAL) == []
    assert _gauges_named(writer, M_SECURITY_STATUS) == [(M_SECURITY_STATUS, _STATUS_RISK, {})]
    assert _gauges_named(writer, M_SECURITY_SAFE) == [(M_SECURITY_SAFE, 0.0, {})]
    # Timestamp still emitted (default payload has a valid lastScanTime).
    assert len(_gauges_named(writer, M_SECURITY_LAST_SCAN_TIMESTAMP)) == 1


# Row 10: Security fetch fails, connection ok
@pytest.mark.asyncio
async def test_security_fetch_fails_connection_ok() -> None:
    """Security fetch error -> seeded baselines, no findings, connection still emits."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                security=SynologyError(reason="timeout", message="sec timed out"),
                connection=_conn_resp(_live_connection_payload()),
            ),
        ),
    )
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == ["sec timed out"]
    assert _gauges_named(writer, M_SECURITY_STATUS) == [(M_SECURITY_STATUS, _STATUS_BASELINE, {})]
    assert _gauges_named(writer, M_SECURITY_SAFE) == [(M_SECURITY_SAFE, 0.0, {})]
    assert _gauges_named(writer, M_SECURITY_FINDINGS) == []
    assert _gauges_named(writer, M_SECURITY_FINDINGS_TOTAL) == []
    assert _gauges_named(writer, M_SECURITY_LAST_SCAN_TIMESTAMP) == []
    assert _gauges_named(writer, M_SECURITY_LAST_SCAN_AGE_SECONDS) == []
    assert _gauges_named(writer, M_ACTIVE_CONNECTIONS) == [
        (M_ACTIVE_CONNECTIONS, _CONN_TOTAL_5, {})
    ]


# Row 11: Connection fetch fails, security ok
@pytest.mark.asyncio
async def test_security_connection_fails_security_ok() -> None:
    """Connection fetch error -> active_connections absent, security families still emitted."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                security=_sec_resp(_live_security_payload()),
                connection=SynologyError(reason="timeout", message="conn timed out"),
            ),
        ),
    )
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == ["conn timed out"]
    assert _gauges_named(writer, M_ACTIVE_CONNECTIONS) == []
    assert _gauges_named(writer, M_SECURITY_STATUS) == [(M_SECURITY_STATUS, _STATUS_RISK, {})]
    assert len(_gauges_named(writer, M_SECURITY_FINDINGS)) == _GRID_CELLS


# Row 12a: Connection total absent
@pytest.mark.asyncio
async def test_security_connection_total_absent() -> None:
    """Connection payload missing total -> active_connections absent."""
    payload: dict[str, object] = {"items": [], "systime": "x"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, _FakeSynology(connection=_conn_resp(payload))),
    )
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_ACTIVE_CONNECTIONS) == []


# Row 12b: Connection total non-numeric
@pytest.mark.asyncio
async def test_security_connection_total_non_numeric() -> None:
    """Connection payload total is non-numeric -> active_connections absent."""
    payload: dict[str, object] = {"items": [], "total": "foo"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, _FakeSynology(connection=_conn_resp(payload))),
    )
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_ACTIVE_CONNECTIONS) == []


# Row 13: Both fetches fail
@pytest.mark.asyncio
async def test_security_both_fetches_fail() -> None:
    """Both security + connection fail -> ok=False, seeded scalars emit, families absent."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                security=SynologyError(reason="timeout", message="sec timed out"),
                connection=SynologyError(reason="timeout", message="conn timed out"),
            ),
        ),
    )
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is False
    assert result.errors == ["sec timed out", "conn timed out"]
    assert _gauges_named(writer, M_SECURITY_STATUS) == [(M_SECURITY_STATUS, _STATUS_BASELINE, {})]
    assert _gauges_named(writer, M_SECURITY_SAFE) == [(M_SECURITY_SAFE, 0.0, {})]
    assert _gauges_named(writer, M_SECURITY_FINDINGS) == []
    assert _gauges_named(writer, M_ACTIVE_CONNECTIONS) == []
    assert len(_gauges_named(writer, _DROP)) == _FAMILY_COUNT


# Row 14: Unconfigured client
@pytest.mark.asyncio
async def test_security_unconfigured_client() -> None:
    """ctx.synology is None -> ok=False, no metrics emitted."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, None))
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is False
    assert result.errors == ["synology client not configured"]
    assert result.metrics_emitted == 0
    assert writer.gauges == []


# Row 15a: Security payload not a dict
@pytest.mark.asyncio
async def test_security_payload_not_a_dict() -> None:
    """Security payload not a dict -> seeded baselines, no findings, connection still emits."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                security=_sec_resp("nope"),
                connection=_conn_resp(_live_connection_payload()),
            ),
        ),
    )
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_SECURITY_STATUS) == [(M_SECURITY_STATUS, _STATUS_BASELINE, {})]
    assert _gauges_named(writer, M_SECURITY_SAFE) == [(M_SECURITY_SAFE, 0.0, {})]
    assert _gauges_named(writer, M_SECURITY_FINDINGS) == []
    assert _gauges_named(writer, M_SECURITY_LAST_SCAN_TIMESTAMP) == []
    assert _gauges_named(writer, M_ACTIVE_CONNECTIONS) == [
        (M_ACTIVE_CONNECTIONS, _CONN_TOTAL_5, {})
    ]


# Row 15b: Connection payload not a dict
@pytest.mark.asyncio
async def test_security_connection_payload_not_a_dict() -> None:
    """Connection payload not a dict -> active_connections absent, security families ok."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                security=_sec_resp(_live_security_payload()),
                connection=_conn_resp("nope"),
            ),
        ),
    )
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_ACTIVE_CONNECTIONS) == []
    assert _gauges_named(writer, M_SECURITY_STATUS) == [(M_SECURITY_STATUS, _STATUS_RISK, {})]
    assert len(_gauges_named(writer, M_SECURITY_FINDINGS)) == _GRID_CELLS


# Row 16: Metrics emitted accounting
@pytest.mark.asyncio
async def test_security_metrics_emitted_accounting() -> None:
    """Accounting check: api_took + drop + all family series = total emitted."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology()))
    collector = SynologySecurityCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert len(_gauges_named(writer, _API_TOOK)) == _EXPECTED_API_TOOK_COUNT
    assert len(_gauges_named(writer, _DROP)) == _FAMILY_COUNT
    assert result.metrics_emitted == len(writer.gauges)
