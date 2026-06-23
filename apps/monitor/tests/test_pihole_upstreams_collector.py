"""Unit tests for PiholeUpstreamsCollector (STAGE-006-006).

Covers 100% branch coverage across:
- happy path (full payload with 3 upstreams: 2 pseudo + 1 real)
- ctx.pihole is None
- stats_upstreams() returns PiholeError
- payload not a dict
- upstreams key missing
- upstreams key not a list
- entry in list not a dict (skipped)
- entry missing ip key (skipped)
- entry ip not a str (skipped)
- entry missing port key (skipped)
- entry port not an int (skipped)
- entry port is bool (subclass of int, must reject)
- real upstream label format (port != -1 → "{ip}#{port}")
- pseudo-upstream label format (port == -1 → bare ip)
- entry count non-numeric (skipped)
- metric-name constants literal match
- omitted metrics never emitted (statistics, total_queries, forwarded)
"""

from __future__ import annotations

import pytest
import structlog

from homelab_monitor.kernel.pihole.client import PiholeResponse
from homelab_monitor.kernel.pihole.errors import PiholeError
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter, InMemoryMetricsWriter
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.integrations.pihole.upstreams import (
    M_API_TOOK,
    M_UPSTREAM_QUERIES,
    PiholeUpstreamsCollector,
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


class _FakeUpstreamsOk(_FakePiholeBase):
    """stats_upstreams returns a configurable PiholeResponse."""

    def __init__(self, payload: object, took: float = 0.000112) -> None:
        self._payload = payload
        self._took = took

    async def stats_upstreams(self) -> PiholeResponse | PiholeError:
        return PiholeResponse(
            payload=self._payload, took_seconds=self._took, endpoint="stats/upstreams"
        )


class _FakeUpstreamsError(_FakePiholeBase):
    """stats_upstreams returns a PiholeError."""

    def __init__(self, message: str = "timeout") -> None:
        self._message = message

    async def stats_upstreams(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="timeout", message=self._message)


# ---------------------------------------------------------------------------
# ctx builder
# ---------------------------------------------------------------------------


def _ctx(writer: InMemoryMetricsWriter, pihole: object | None) -> CollectorContext:
    """Build a CollectorContext wired to the given writer and pihole client."""
    return CollectorContext(
        config=CollectorConfig(
            name="pihole_upstreams",
            interval_seconds=30,
            timeout_seconds=15,
        ),
        db=None,  # type: ignore[arg-type]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="pihole_upstreams"),
        pihole=pihole,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
# Full payload fixture (from live Pi-hole v6.6.2)
# ---------------------------------------------------------------------------

_FULL_PAYLOAD: dict[str, object] = {
    "upstreams": [
        {
            "ip": "blocklist",
            "name": "blocklist",
            "port": -1,
            "count": 48852,
            "statistics": {"response": 0.0, "variance": 0.0},
        },
        {
            "ip": "cache",
            "name": "cache",
            "port": -1,
            "count": 36276,
            "statistics": {"response": 0.0, "variance": 0.0},
        },
        {
            "ip": "127.0.0.1",
            "name": "localhost",
            "port": 5335,
            "count": 6893,
            "statistics": {"response": 0.034, "variance": 0.001},
        },
    ],
    "total_queries": 92925,
    "forwarded_queries": 6887,
    "took": 0.000112,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_full_payload() -> None:
    """Full payload: all expected metrics emitted, ok=True, correct counts."""
    writer = InMemoryMetricsWriter()
    collector = PiholeUpstreamsCollector()
    ctx = _ctx(writer, _FakeUpstreamsOk(_FULL_PAYLOAD))

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == []
    assert result.metrics_emitted == 4  # noqa: PLR2004

    # api_took
    api_took = _gauge_value(writer, M_API_TOOK, {"endpoint": "stats/upstreams"})
    assert api_took == pytest.approx(0.000112)  # pyright: ignore[reportUnknownMemberType]

    # pseudo-upstream: blocklist (port=-1 → label="blocklist")
    assert _gauge_value(writer, M_UPSTREAM_QUERIES, {"upstream": "blocklist"}) == 48852.0  # noqa: PLR2004

    # pseudo-upstream: cache (port=-1 → label="cache")
    assert _gauge_value(writer, M_UPSTREAM_QUERIES, {"upstream": "cache"}) == 36276.0  # noqa: PLR2004

    # real upstream: 127.0.0.1#5335 (port != -1 → label="127.0.0.1#5335")
    assert _gauge_value(writer, M_UPSTREAM_QUERIES, {"upstream": "127.0.0.1#5335"}) == 6893.0  # noqa: PLR2004


@pytest.mark.asyncio
async def test_ctx_pihole_none() -> None:
    """ctx.pihole is None → ok=False, error message, 0 emits."""
    writer = InMemoryMetricsWriter()
    collector = PiholeUpstreamsCollector()
    result = await collector.run(_ctx(writer, None))
    assert result.ok is False
    assert result.errors == ["pihole client not configured"]
    assert result.metrics_emitted == 0
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_stats_upstreams_returns_pihole_error() -> None:
    """stats_upstreams() returns PiholeError → ok=False, errors carries message, 0 emits."""
    writer = InMemoryMetricsWriter()
    collector = PiholeUpstreamsCollector()
    ctx = _ctx(writer, _FakeUpstreamsError("GET /api/stats/upstreams: timed out"))
    result = await collector.run(ctx)
    assert result.ok is False
    assert result.errors == ["GET /api/stats/upstreams: timed out"]
    assert result.metrics_emitted == 0
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_payload_not_a_dict() -> None:
    """payload is a list (not a dict) → ok=False, errors=["unexpected payload shape"]."""
    writer = InMemoryMetricsWriter()
    collector = PiholeUpstreamsCollector()
    ctx = _ctx(writer, _FakeUpstreamsOk(["not", "a", "dict"]))
    result = await collector.run(ctx)
    assert result.ok is False
    assert result.errors == ["unexpected payload shape"]
    assert result.metrics_emitted == 0


@pytest.mark.asyncio
async def test_upstreams_key_missing() -> None:
    """upstreams key missing → api_took emitted, rest skipped; ok=True (partial)."""
    payload: dict[str, object] = {"total_queries": 100, "took": 0.001}
    writer = InMemoryMetricsWriter()
    collector = PiholeUpstreamsCollector()
    ctx = _ctx(writer, _FakeUpstreamsOk(payload))
    result = await collector.run(ctx)
    assert result.ok is True
    assert result.errors == []
    assert result.metrics_emitted == 1  # api_took only
    assert M_UPSTREAM_QUERIES not in _all_metric_names(writer)
    assert M_API_TOOK in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_upstreams_not_a_list() -> None:
    """upstreams value is a dict (not a list) → api_took emitted; ok=True (partial)."""
    payload: dict[str, object] = {"upstreams": {"ip": "bad"}, "took": 0.001}
    writer = InMemoryMetricsWriter()
    collector = PiholeUpstreamsCollector()
    ctx = _ctx(writer, _FakeUpstreamsOk(payload))
    result = await collector.run(ctx)
    assert result.ok is True
    assert result.metrics_emitted == 1  # api_took only
    assert M_UPSTREAM_QUERIES not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_entry_not_a_dict_skipped() -> None:
    """List contains non-dict entry (string) — skipped; valid entries still emit."""
    payload: dict[str, object] = {
        "upstreams": [
            "not-a-dict",
            {"ip": "cache", "name": "cache", "port": -1, "count": 100},
        ],
        "took": 0.001,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeUpstreamsCollector()
    ctx = _ctx(writer, _FakeUpstreamsOk(payload))
    result = await collector.run(ctx)
    assert result.ok is True
    # only api_took + 1 valid upstream = 2
    assert result.metrics_emitted == 2  # noqa: PLR2004
    assert _gauge_value(writer, M_UPSTREAM_QUERIES, {"upstream": "cache"}) == 100.0  # noqa: PLR2004


@pytest.mark.asyncio
async def test_entry_missing_ip_skipped() -> None:
    """Entry with no ip key → skipped; other entries emitted."""
    payload: dict[str, object] = {
        "upstreams": [
            {"name": "cache", "port": -1, "count": 100},  # no ip
            {"ip": "blocklist", "name": "blocklist", "port": -1, "count": 200},
        ],
        "took": 0.001,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeUpstreamsCollector()
    ctx = _ctx(writer, _FakeUpstreamsOk(payload))
    result = await collector.run(ctx)
    assert result.ok is True
    # only api_took + blocklist = 2
    assert result.metrics_emitted == 2  # noqa: PLR2004
    assert _gauge_value(writer, M_UPSTREAM_QUERIES, {"upstream": "blocklist"}) == 200.0  # noqa: PLR2004


@pytest.mark.asyncio
async def test_entry_ip_not_a_str_skipped() -> None:
    """Entry with ip as int → skipped."""
    payload: dict[str, object] = {
        "upstreams": [
            {"ip": 12345, "port": -1, "count": 100},  # ip is int, not str
        ],
        "took": 0.001,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeUpstreamsCollector()
    ctx = _ctx(writer, _FakeUpstreamsOk(payload))
    result = await collector.run(ctx)
    assert result.ok is True
    assert result.metrics_emitted == 1  # api_took only


@pytest.mark.asyncio
async def test_entry_missing_port_skipped() -> None:
    """Entry with no port key → skipped."""
    payload: dict[str, object] = {
        "upstreams": [
            {"ip": "cache", "name": "cache", "count": 100},  # no port
        ],
        "took": 0.001,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeUpstreamsCollector()
    ctx = _ctx(writer, _FakeUpstreamsOk(payload))
    result = await collector.run(ctx)
    assert result.ok is True
    assert result.metrics_emitted == 1  # api_took only


@pytest.mark.asyncio
async def test_entry_port_not_an_int_skipped() -> None:
    """Entry with port as string → skipped."""
    payload: dict[str, object] = {
        "upstreams": [
            {"ip": "cache", "port": "not-an-int", "count": 100},
        ],
        "took": 0.001,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeUpstreamsCollector()
    ctx = _ctx(writer, _FakeUpstreamsOk(payload))
    result = await collector.run(ctx)
    assert result.ok is True
    assert result.metrics_emitted == 1  # api_took only


@pytest.mark.asyncio
async def test_entry_port_is_bool_skipped() -> None:
    """Entry with port as bool (bool is subclass of int) → must be rejected."""
    payload: dict[str, object] = {
        "upstreams": [
            {"ip": "cache", "port": True, "count": 100},
        ],
        "took": 0.001,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeUpstreamsCollector()
    ctx = _ctx(writer, _FakeUpstreamsOk(payload))
    result = await collector.run(ctx)
    assert result.ok is True
    assert result.metrics_emitted == 1  # api_took only


@pytest.mark.asyncio
async def test_real_upstream_label_format() -> None:
    """Port != -1 → label is {ip}#{port}; must NOT emit without port suffix."""
    payload: dict[str, object] = {
        "upstreams": [
            {"ip": "127.0.0.1", "name": "localhost", "port": 5335, "count": 6893},
        ],
        "took": 0.001,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeUpstreamsCollector()
    ctx = _ctx(writer, _FakeUpstreamsOk(payload))
    result = await collector.run(ctx)
    assert result.ok is True
    assert _gauge_value(writer, M_UPSTREAM_QUERIES, {"upstream": "127.0.0.1#5335"}) == 6893.0  # noqa: PLR2004
    # Must NOT emit with label "127.0.0.1" (no port suffix)
    assert _gauge_value(writer, M_UPSTREAM_QUERIES, {"upstream": "127.0.0.1"}) is None


@pytest.mark.asyncio
async def test_pseudo_upstream_label_format() -> None:
    """Port == -1 → label is bare ip with no #-1 suffix."""
    payload: dict[str, object] = {
        "upstreams": [
            {"ip": "cache", "port": -1, "count": 36276},
        ],
        "took": 0.001,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeUpstreamsCollector()
    ctx = _ctx(writer, _FakeUpstreamsOk(payload))
    await collector.run(ctx)
    assert _gauge_value(writer, M_UPSTREAM_QUERIES, {"upstream": "cache"}) == 36276.0  # noqa: PLR2004
    # Must NOT emit with label "cache#-1"
    assert _gauge_value(writer, M_UPSTREAM_QUERIES, {"upstream": "cache#-1"}) is None


@pytest.mark.asyncio
async def test_entry_non_numeric_count_skipped() -> None:
    """Entry with non-numeric count → that entry's metric skipped; others still emit."""
    payload: dict[str, object] = {
        "upstreams": [
            {"ip": "cache", "port": -1, "count": "bad"},  # non-numeric count
            {"ip": "blocklist", "port": -1, "count": 200},
        ],
        "took": 0.001,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeUpstreamsCollector()
    ctx = _ctx(writer, _FakeUpstreamsOk(payload))
    result = await collector.run(ctx)
    assert result.ok is True
    # api_took + blocklist = 2
    assert result.metrics_emitted == 2  # noqa: PLR2004
    assert _gauge_value(writer, M_UPSTREAM_QUERIES, {"upstream": "cache"}) is None
    assert _gauge_value(writer, M_UPSTREAM_QUERIES, {"upstream": "blocklist"}) == 200.0  # noqa: PLR2004


def test_metric_name_constants_match_contract() -> None:
    """Metric-name constants must equal the literal card-contract names."""
    assert M_UPSTREAM_QUERIES == "homelab_pihole_upstream_queries"
    assert M_API_TOOK == "homelab_pihole_api_took_seconds"


@pytest.mark.asyncio
async def test_omitted_metrics_never_emitted() -> None:
    """statistics, total_queries, forwarded_queries, kind, is_pseudo never emitted."""
    writer = InMemoryMetricsWriter()
    collector = PiholeUpstreamsCollector()
    ctx = _ctx(writer, _FakeUpstreamsOk(_FULL_PAYLOAD))
    await collector.run(ctx)
    names = _all_metric_names(writer)
    assert not any("statistics" in n for n in names)
    assert not any("total_queries" in n for n in names)
    assert not any("forwarded" in n for n in names)
    assert not any("kind" in n for n in names)
    assert not any("is_pseudo" in n for n in names)
