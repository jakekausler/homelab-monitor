"""Unit tests for PiholeStatsSummaryCollector (STAGE-006-005).

Covers 100% branch coverage across:
- happy path (full payload, all sub-objects and enum families present)
- ctx.pihole is None
- stats_summary() returns PiholeError
- payload not a dict
- queries sub-object missing
- queries sub-object not a dict
- clients sub-object missing
- clients sub-object not a dict
- enum sub-object (types/status/replies) missing or not a dict
- non-numeric scalar (queries.total is a string "bad")
- non-numeric enum value
- unique_clients never emitted
- gravity metrics never emitted
- api_took_seconds emitted
"""

from __future__ import annotations

import pytest
import structlog

from homelab_monitor.kernel.pihole.client import PiholeResponse
from homelab_monitor.kernel.pihole.errors import PiholeError
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter, InMemoryMetricsWriter, MetricEntry
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.integrations.pihole.stats_summary import (
    M_ACTIVE_CLIENTS,
    M_API_TOOK,
    M_PERCENT_BLOCKED,
    M_QUERIES_BLOCKED,
    M_QUERIES_CACHED,
    M_QUERIES_FORWARDED,
    M_QUERIES_TOTAL,
    M_QUERY_BY_REPLY,
    M_QUERY_BY_STATUS,
    M_QUERY_BY_TYPE,
    M_QUERY_FREQUENCY,
    M_TOTAL_CLIENTS,
    M_UNIQUE_DOMAINS,
    PiholeStatsSummaryCollector,
)

# ---------------------------------------------------------------------------
# Fake Pi-hole client
# ---------------------------------------------------------------------------


class _FakePiholeBase:
    """Base fake PiholeClient: every method returns a stub PiholeError."""

    async def info_version(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def info_ftl(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def info_database(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def info_messages(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def info_system(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def stats_summary(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def stats_upstreams(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def stats_query_types(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def stats_top_clients(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def stats_top_domains(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def stats_recent_blocked(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def dns_blocking(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def lists(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def network_devices(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def queries(self, params: dict[str, str]) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def aclose(self) -> None:
        pass


class _FakeSummaryOk(_FakePiholeBase):
    """stats_summary returns a configurable PiholeResponse."""

    def __init__(self, payload: object, took: float = 0.042) -> None:
        self._payload = payload
        self._took = took

    async def stats_summary(self) -> PiholeResponse | PiholeError:
        return PiholeResponse(
            payload=self._payload, took_seconds=self._took, endpoint="stats/summary"
        )


class _FakeSummaryError(_FakePiholeBase):
    """stats_summary returns a PiholeError."""

    def __init__(self, message: str = "timeout") -> None:
        self._message = message

    async def stats_summary(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="timeout", message=self._message)


# ---------------------------------------------------------------------------
# ctx builder
# ---------------------------------------------------------------------------


def _ctx(writer: InMemoryMetricsWriter, pihole: object | None) -> CollectorContext:
    """Build a CollectorContext wired to the given writer and pihole client."""
    return CollectorContext(
        config=CollectorConfig(
            name="pihole_stats_summary",
            interval_seconds=30,
            timeout_seconds=15,
        ),
        db=None,  # type: ignore[arg-type]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="pihole_stats_summary"),
        pihole=pihole,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gauges(writer: InMemoryMetricsWriter, name: str) -> list[MetricEntry]:
    return [e for e in writer.recorded if e.kind == "gauge" and e.name == name]  # pyright: ignore[reportPrivateUsage]


def _gauge_value(
    writer: InMemoryMetricsWriter, name: str, labels: dict[str, str] | None = None
) -> float | None:
    labels = labels or {}
    for e in writer.recorded:  # pyright: ignore[reportPrivateUsage]
        if e.kind == "gauge" and e.name == name and e.labels == labels:
            return e.value
    return None


def _all_metric_names(writer: InMemoryMetricsWriter) -> set[str]:
    return {e.name for e in writer.recorded}  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Full payload fixture (mirrors live Pi-hole v6.6.2 shape)
# ---------------------------------------------------------------------------

_FULL_PAYLOAD: dict[str, object] = {
    "queries": {
        "total": 12345,
        "blocked": 1234,
        "percent_blocked": 9.996,
        "unique_domains": 4567,
        "forwarded": 8000,
        "cached": 3111,
        "frequency": 123.4,
        "types": {
            "A": 8000,
            "AAAA": 2000,
            "ANY": 0,
            "SRV": 5,
            "SOA": 1,
            "PTR": 200,
            "TXT": 50,
            "NAPTR": 0,
            "MX": 3,
            "DS": 0,
            "RRSIG": 0,
            "DNSKEY": 0,
            "NS": 10,
            "SVCB": 0,
            "HTTPS": 75,
            "OTHER": 1,
        },
        "status": {
            "UNKNOWN": 0,
            "GRAVITY": 900,
            "FORWARDED": 8000,
            "CACHE": 3000,
            "REGEX": 100,
            "DENYLIST": 50,
            "EXTERNAL_BLOCKED_IP": 10,
            "EXTERNAL_BLOCKED_NULL": 5,
            "EXTERNAL_BLOCKED_NXRA": 3,
            "GRAVITY_CNAME": 80,
            "REGEX_CNAME": 20,
            "DENYLIST_CNAME": 10,
            "RETRIED": 2,
            "RETRIED_DNSSEC": 1,
            "IN_PROGRESS": 0,
            "DBBUSY": 0,
            "SPECIAL_DOMAIN": 15,
            "CACHE_STALE": 5,
            "EXTERNAL_BLOCKED_EDE15": 0,
        },
        "replies": {
            "UNKNOWN": 0,
            "NODATA": 100,
            "NXDOMAIN": 50,
            "CNAME": 300,
            "IP": 9000,
            "DOMAIN": 20,
            "RRNAME": 5,
            "SERVFAIL": 2,
            "REFUSED": 3,
            "NOTIMP": 0,
            "OTHER": 10,
            "DNSSEC": 200,
            "NONE": 0,
            "BLOB": 0,
            # Note: live shape has 15 entries; UNKNOWN + 14 others above = 15
            # Add the 15th:
            "SVCB": 1,
        },
    },
    "clients": {
        "active": 42,
        "total": 150,
    },
    "gravity": {
        "domains_being_blocked": 900000,
        "last_update": 1718000000,
    },
    "took": 0.042,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_full_payload() -> None:
    """Full payload: all expected metrics emitted, ok=True, correct counts."""
    writer = InMemoryMetricsWriter()
    collector = PiholeStatsSummaryCollector()
    ctx = _ctx(writer, _FakeSummaryOk(_FULL_PAYLOAD))

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == []
    assert result.events == []
    assert result.duration_seconds >= 0.0

    # api_took
    took = _gauge_value(writer, M_API_TOOK, {"endpoint": "stats/summary"})
    assert took == 0.042  # noqa: PLR2004

    # queries scalars
    assert _gauge_value(writer, M_QUERIES_TOTAL, {}) == 12345.0  # noqa: PLR2004
    assert _gauge_value(writer, M_QUERIES_BLOCKED, {}) == 1234.0  # noqa: PLR2004
    assert _gauge_value(writer, M_QUERIES_FORWARDED, {}) == 8000.0  # noqa: PLR2004
    assert _gauge_value(writer, M_QUERIES_CACHED, {}) == 3111.0  # noqa: PLR2004
    assert _gauge_value(writer, M_PERCENT_BLOCKED, {}) == pytest.approx(9.996)  # pyright: ignore[reportUnknownMemberType]
    assert _gauge_value(writer, M_QUERY_FREQUENCY, {}) == pytest.approx(123.4)  # pyright: ignore[reportUnknownMemberType]
    assert _gauge_value(writer, M_UNIQUE_DOMAINS, {}) == 4567.0  # noqa: PLR2004

    # clients scalars
    assert _gauge_value(writer, M_ACTIVE_CLIENTS, {}) == 42.0  # noqa: PLR2004
    assert _gauge_value(writer, M_TOTAL_CLIENTS, {}) == 150.0  # noqa: PLR2004

    # enum families present
    type_entries = _gauges(writer, M_QUERY_BY_TYPE)
    assert len(type_entries) == 16  # noqa: PLR2004
    assert any(e.labels == {"type": "A"} and e.value == 8000.0 for e in type_entries)  # noqa: PLR2004

    status_entries = _gauges(writer, M_QUERY_BY_STATUS)
    assert len(status_entries) == 19  # noqa: PLR2004

    reply_entries = _gauges(writer, M_QUERY_BY_REPLY)
    assert len(reply_entries) == 15  # noqa: PLR2004

    # metrics_emitted: 1 (api_took) + 7 (queries scalars) + 2 (clients) + 16+19+15 (enums)
    assert result.metrics_emitted == 1 + 7 + 2 + len(type_entries) + len(status_entries) + len(
        reply_entries
    )


@pytest.mark.asyncio
async def test_ctx_pihole_none() -> None:
    """ctx.pihole is None → ok=False, error message, 0 emits."""
    writer = InMemoryMetricsWriter()
    collector = PiholeStatsSummaryCollector()
    ctx = _ctx(writer, None)

    result = await collector.run(ctx)

    assert result.ok is False
    assert result.errors == ["pihole client not configured"]
    assert result.metrics_emitted == 0
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_stats_summary_returns_pihole_error() -> None:
    """stats_summary() returns PiholeError → ok=False, errors carries message, 0 emits."""
    writer = InMemoryMetricsWriter()
    collector = PiholeStatsSummaryCollector()
    ctx = _ctx(writer, _FakeSummaryError("GET /api/stats/summary: timed out"))

    result = await collector.run(ctx)

    assert result.ok is False
    assert result.errors == ["GET /api/stats/summary: timed out"]
    assert result.metrics_emitted == 0
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_payload_not_a_dict() -> None:
    """payload is a list (not a dict) → ok=False, errors=["unexpected payload shape"]."""
    writer = InMemoryMetricsWriter()
    collector = PiholeStatsSummaryCollector()
    ctx = _ctx(writer, _FakeSummaryOk(["not", "a", "dict"]))

    result = await collector.run(ctx)

    assert result.ok is False
    assert result.errors == ["unexpected payload shape"]
    assert result.metrics_emitted == 0


@pytest.mark.asyncio
async def test_queries_sub_object_missing() -> None:
    """queries key absent → query metrics skipped; clients and api_took still emitted."""
    payload: dict[str, object] = {
        "clients": {"active": 10, "total": 20},
        "took": 0.01,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeStatsSummaryCollector()
    ctx = _ctx(writer, _FakeSummaryOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    names = _all_metric_names(writer)
    assert M_QUERIES_TOTAL not in names
    assert M_QUERY_BY_TYPE not in names
    assert M_ACTIVE_CLIENTS in names
    assert M_API_TOOK in names


@pytest.mark.asyncio
async def test_queries_sub_object_not_a_dict() -> None:
    """queries key present but value is a string → treated same as missing."""
    payload: dict[str, object] = {
        "queries": "not-a-dict",
        "clients": {"active": 5, "total": 5},
        "took": 0.01,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeStatsSummaryCollector()
    ctx = _ctx(writer, _FakeSummaryOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    names = _all_metric_names(writer)
    assert M_QUERIES_TOTAL not in names
    assert M_ACTIVE_CLIENTS in names


@pytest.mark.asyncio
async def test_clients_sub_object_missing() -> None:
    """clients key absent → client metrics skipped; queries still emitted."""
    payload: dict[str, object] = {
        "queries": {
            "total": 100,
            "blocked": 10,
            "percent_blocked": 10.0,
            "unique_domains": 50,
            "forwarded": 70,
            "cached": 20,
            "frequency": 1.0,
        },
        "took": 0.01,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeStatsSummaryCollector()
    ctx = _ctx(writer, _FakeSummaryOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    names = _all_metric_names(writer)
    assert M_QUERIES_TOTAL in names
    assert M_ACTIVE_CLIENTS not in names
    assert M_TOTAL_CLIENTS not in names


@pytest.mark.asyncio
async def test_clients_sub_object_not_a_dict() -> None:
    """clients key present but value is a list → client metrics skipped."""
    payload: dict[str, object] = {
        "queries": {
            "total": 100,
            "blocked": 10,
            "percent_blocked": 10.0,
            "unique_domains": 50,
            "forwarded": 70,
            "cached": 20,
            "frequency": 1.0,
        },
        "clients": [1, 2, 3],
        "took": 0.01,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeStatsSummaryCollector()
    ctx = _ctx(writer, _FakeSummaryOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    assert M_ACTIVE_CLIENTS not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_enum_sub_object_missing() -> None:
    """types / status / replies absent in queries → those families not emitted."""
    payload: dict[str, object] = {
        "queries": {
            "total": 100,
            "blocked": 10,
            "percent_blocked": 10.0,
            "unique_domains": 50,
            "forwarded": 70,
            "cached": 20,
            "frequency": 1.0,
        },
        # no "types", "status", "replies" keys
        "took": 0.01,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeStatsSummaryCollector()
    ctx = _ctx(writer, _FakeSummaryOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    names = _all_metric_names(writer)
    assert M_QUERY_BY_TYPE not in names
    assert M_QUERY_BY_STATUS not in names
    assert M_QUERY_BY_REPLY not in names
    assert M_QUERIES_TOTAL in names


@pytest.mark.asyncio
async def test_enum_sub_object_not_a_dict() -> None:
    """types is a list (not a dict) → that family skipped; others may still emit."""
    payload: dict[str, object] = {
        "queries": {
            "total": 100,
            "blocked": 10,
            "percent_blocked": 10.0,
            "unique_domains": 50,
            "forwarded": 70,
            "cached": 20,
            "frequency": 1.0,
            "types": ["not", "a", "dict"],  # wrong type
            "status": {"GRAVITY": 50},
            "replies": {},
        },
        "took": 0.01,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeStatsSummaryCollector()
    ctx = _ctx(writer, _FakeSummaryOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    names = _all_metric_names(writer)
    assert M_QUERY_BY_TYPE not in names
    assert M_QUERY_BY_STATUS in names  # still emitted


@pytest.mark.asyncio
async def test_non_numeric_scalar_skipped() -> None:
    """queries.total is a non-numeric string → that metric skipped, others emitted."""
    payload: dict[str, object] = {
        "queries": {
            "total": "bad",
            "blocked": 10,
            "percent_blocked": 10.0,
            "unique_domains": 50,
            "forwarded": 70,
            "cached": 20,
            "frequency": 1.0,
        },
        "took": 0.01,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeStatsSummaryCollector()
    ctx = _ctx(writer, _FakeSummaryOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    assert M_QUERIES_TOTAL not in _all_metric_names(writer)
    assert M_QUERIES_BLOCKED in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_non_numeric_enum_value_skipped() -> None:
    """An enum entry with a non-numeric value is skipped; others in the family emitted."""
    payload: dict[str, object] = {
        "queries": {
            "total": 100,
            "blocked": 10,
            "percent_blocked": 10.0,
            "unique_domains": 50,
            "forwarded": 70,
            "cached": 20,
            "frequency": 1.0,
            "types": {"A": 100, "AAAA": "bad_value", "PTR": 5},
        },
        "took": 0.01,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeStatsSummaryCollector()
    ctx = _ctx(writer, _FakeSummaryOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    type_entries = _gauges(writer, M_QUERY_BY_TYPE)
    labels = {e.labels["type"] for e in type_entries}
    assert "A" in labels
    assert "AAAA" not in labels  # skipped: non-numeric
    assert "PTR" in labels


@pytest.mark.asyncio
async def test_unique_clients_never_emitted() -> None:
    """homelab_pihole_unique_clients is NEVER in the emitted set (retracted metric)."""
    writer = InMemoryMetricsWriter()
    collector = PiholeStatsSummaryCollector()
    ctx = _ctx(writer, _FakeSummaryOk(_FULL_PAYLOAD))
    await collector.run(ctx)
    assert "homelab_pihole_unique_clients" not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_gravity_metrics_never_emitted() -> None:
    """gravity metrics are NEVER emitted (owned by STAGE-006-007)."""
    writer = InMemoryMetricsWriter()
    collector = PiholeStatsSummaryCollector()
    ctx = _ctx(writer, _FakeSummaryOk(_FULL_PAYLOAD))
    await collector.run(ctx)
    names = _all_metric_names(writer)
    assert not any("gravity" in n for n in names)


@pytest.mark.asyncio
async def test_api_took_seconds_emitted() -> None:
    """homelab_pihole_api_took_seconds is emitted with endpoint label on success."""
    writer = InMemoryMetricsWriter()
    collector = PiholeStatsSummaryCollector()
    ctx = _ctx(writer, _FakeSummaryOk({"queries": {}, "clients": {}}, took=0.099))
    result = await collector.run(ctx)
    assert result.ok is True
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "stats/summary"}) == pytest.approx(0.099)  # pyright: ignore[reportUnknownMemberType]


def test_metric_name_constants_match_contract() -> None:
    """Metric-name constants must equal the literal card-contract names.

    Guards against silent drift: the other tests assert via the constants
    (self-referential), so a regressed constant value would still pass them.
    These literal-string assertions pin the names the alert rules (016/017)
    and Grafana (026) depend on.
    """
    assert M_QUERIES_TOTAL == "homelab_pihole_queries_total"
    assert M_QUERIES_BLOCKED == "homelab_pihole_blocked_total"
    assert M_QUERIES_FORWARDED == "homelab_pihole_forwarded_total"
    assert M_QUERIES_CACHED == "homelab_pihole_cached_total"
    assert M_PERCENT_BLOCKED == "homelab_pihole_percent_blocked"
    assert M_QUERY_FREQUENCY == "homelab_pihole_query_frequency"
    assert M_UNIQUE_DOMAINS == "homelab_pihole_unique_domains"
    assert M_ACTIVE_CLIENTS == "homelab_pihole_active_clients"
    assert M_TOTAL_CLIENTS == "homelab_pihole_total_clients"
    assert M_QUERY_BY_TYPE == "homelab_pihole_query_by_type"
    assert M_QUERY_BY_STATUS == "homelab_pihole_query_by_status"
    assert M_QUERY_BY_REPLY == "homelab_pihole_query_by_reply"
    assert M_API_TOOK == "homelab_pihole_api_took_seconds"
