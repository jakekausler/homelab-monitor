"""Unit tests for UnboundStatsCollector (STAGE-006-013). 100% branch."""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from homelab_monitor.kernel.docker.socket_client import (
    DockerSocketConnectionError,
    ExecResult,
)
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter, InMemoryMetricsWriter
from homelab_monitor.kernel.plugins.loader import PluginLoader
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.integrations.pihole import register_all
from homelab_monitor.plugins.collectors.integrations.pihole.unbound_stats import (
    M_ANSWER_BOGUS,
    M_ANSWER_RCODE,
    M_ANSWER_SECURE,
    M_API_TOOK,
    M_CACHE_HIT_RATIO,
    M_CACHE_HITS,
    M_CACHE_MISSES,
    M_EXTENDED_ENABLED,
    M_PREFETCH,
    M_QUERIES,
    M_QUERY_TYPE,
    M_RECURSION_TIME,
    M_REQUESTLIST_CURRENT,
    M_REQUESTLIST_EXCEEDED,
    UnboundStatsCollector,
)

_FIXTURES = Path(__file__).parent / "fixtures"


def _read_fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


class _FakeBackend:
    """Minimal ExecCapture double: returns a preset ExecResult or raises an exc."""

    def __init__(self, *, result: ExecResult | None = None, exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc
        self.calls: list[dict[str, object]] = []

    async def exec_capture(
        self, *, container_id: str, cmd: list[str], timeout_seconds: float
    ) -> ExecResult:
        self.calls.append(
            {
                "container_id": container_id,
                "cmd": cmd,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result


def _ctx(writer: InMemoryMetricsWriter) -> CollectorContext:
    return CollectorContext(
        config=CollectorConfig(
            name="unbound_stats",
            interval_seconds=60,
            timeout_seconds=15,
        ),
        db=None,  # type: ignore[arg-type]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="unbound_stats"),
        pihole=None,  # type: ignore[arg-type]
    )


def _gauge_value(
    writer: InMemoryMetricsWriter,
    name: str,
    labels: dict[str, str] | None = None,
) -> float | None:
    labels = labels or {}
    for e in writer.recorded:  # pyright: ignore[reportPrivateUsage]
        if e.kind == "gauge" and e.name == name and e.labels == labels:
            return e.value
    return None


def _count(writer: InMemoryMetricsWriter, name: str) -> int:
    return sum(1 for e in writer.recorded if e.name == name)  # pyright: ignore[reportPrivateUsage]


def _ok_result(stdout: str) -> ExecResult:
    return ExecResult(exit_code=0, stdout=stdout, stderr="")


def _collector_from_stdout(stdout: str) -> UnboundStatsCollector:
    """Build a collector whose fetch will parse the given stdout."""
    backend = _FakeBackend(result=_ok_result(stdout))
    return UnboundStatsCollector(socket_client=backend)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Contract: metric-name constants
# ---------------------------------------------------------------------------


def test_metric_name_constants_match_contract() -> None:
    assert M_QUERIES == "homelab_unbound_queries_total"
    assert M_CACHE_HITS == "homelab_unbound_cache_hits_total"
    assert M_CACHE_MISSES == "homelab_unbound_cache_misses_total"
    assert M_CACHE_HIT_RATIO == "homelab_unbound_cache_hit_ratio"
    assert M_PREFETCH == "homelab_unbound_prefetch_total"
    assert M_RECURSION_TIME == "homelab_unbound_recursion_time_seconds"
    assert M_REQUESTLIST_CURRENT == "homelab_unbound_requestlist_current"
    assert M_REQUESTLIST_EXCEEDED == "homelab_unbound_requestlist_exceeded_total"
    assert M_EXTENDED_ENABLED == "homelab_pihole_unbound_extended_stats_enabled"
    assert M_API_TOOK == "homelab_pihole_api_took_seconds"
    assert M_QUERY_TYPE == "homelab_unbound_query_type"
    assert M_ANSWER_RCODE == "homelab_unbound_answer_rcode"
    assert M_ANSWER_SECURE == "homelab_unbound_answer_secure_total"
    assert M_ANSWER_BOGUS == "homelab_unbound_answer_bogus_total"


# ---------------------------------------------------------------------------
# Guard / error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_socket_client_none_returns_unconfigured() -> None:
    writer = InMemoryMetricsWriter()
    collector = UnboundStatsCollector(socket_client=None)
    result = await collector.run(_ctx(writer))
    assert result.ok is False
    assert result.errors == ["client_unconfigured"]
    assert result.metrics_emitted == 0
    assert len(writer.recorded) == 0  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_fetch_error_emits_api_took_only() -> None:
    """Backend raises -> fetch returns UnboundError; api_took still emitted."""
    writer = InMemoryMetricsWriter()
    backend = _FakeBackend(exc=DockerSocketConnectionError("boom"))
    collector = UnboundStatsCollector(socket_client=backend)  # type: ignore[arg-type]
    result = await collector.run(_ctx(writer))
    assert result.ok is False
    assert result.metrics_emitted == 1
    assert len(result.errors) == 1
    assert "container_unreachable" in result.errors[0] or ("socket_error" in result.errors[0])
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "unbound/stats_noreset"}) is not None


@pytest.mark.asyncio
async def test_fetch_control_error_via_nonzero_exit() -> None:
    """Non-zero exit -> control_error; api_took emitted, ok=False."""
    writer = InMemoryMetricsWriter()
    backend = _FakeBackend(result=ExecResult(exit_code=1, stdout="", stderr="not running"))
    collector = UnboundStatsCollector(socket_client=backend)  # type: ignore[arg-type]
    result = await collector.run(_ctx(writer))
    assert result.ok is False
    assert result.metrics_emitted == 1
    assert len(result.errors) == 1


# ---------------------------------------------------------------------------
# Happy path: extended
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_extended() -> None:
    writer = InMemoryMetricsWriter()
    collector = _collector_from_stdout(_read_fixture("unbound_stats_extended.txt"))
    result = await collector.run(_ctx(writer))

    assert result.ok is True
    assert result.errors == []

    # Default set.
    assert _gauge_value(writer, M_QUERIES, {}) == 108637.0  # noqa: PLR2004
    assert _gauge_value(writer, M_CACHE_HITS, {}) == 4924.0  # noqa: PLR2004
    assert _gauge_value(writer, M_CACHE_MISSES, {}) == 103713.0  # noqa: PLR2004
    assert _gauge_value(writer, M_PREFETCH, {}) == 2575.0  # noqa: PLR2004
    assert _gauge_value(writer, M_REQUESTLIST_CURRENT, {}) == 0.0
    assert _gauge_value(writer, M_REQUESTLIST_EXCEEDED, {}) == 0.0

    avg = _gauge_value(writer, M_RECURSION_TIME, {"quantile": "avg"})
    assert avg == pytest.approx(0.049841)  # pyright: ignore[reportUnknownMemberType]
    median = _gauge_value(writer, M_RECURSION_TIME, {"quantile": "median"})
    assert median == pytest.approx(0.0328449)  # pyright: ignore[reportUnknownMemberType]

    # Extended-enabled flag.
    assert _gauge_value(writer, M_EXTENDED_ENABLED, {}) == 1.0

    # Cache hit ratio (derived).
    ratio = _gauge_value(writer, M_CACHE_HIT_RATIO, {})
    assert ratio == pytest.approx(4924 / (4924 + 103713))  # pyright: ignore[reportUnknownMemberType]

    # Histogram quantiles present, ordered p50 <= p95 <= p99.
    p50 = _gauge_value(writer, M_RECURSION_TIME, {"quantile": "0.5"})
    p95 = _gauge_value(writer, M_RECURSION_TIME, {"quantile": "0.95"})
    p99 = _gauge_value(writer, M_RECURSION_TIME, {"quantile": "0.99"})
    assert p50 is not None
    assert p95 is not None
    assert p99 is not None
    assert p50 <= p95 <= p99
    assert 0.0 <= p50 <= 1.0

    # Query types (10 present).
    assert _gauge_value(writer, M_QUERY_TYPE, {"type": "A"}) == 53914.0  # noqa: PLR2004
    assert _gauge_value(writer, M_QUERY_TYPE, {"type": "AAAA"}) == 23342.0  # noqa: PLR2004
    assert _count(writer, M_QUERY_TYPE) == 10  # noqa: PLR2004

    # Rcodes (7 present incl. lowercase nodata).
    assert _gauge_value(writer, M_ANSWER_RCODE, {"rcode": "NOERROR"}) == 106823.0  # noqa: PLR2004
    assert _gauge_value(writer, M_ANSWER_RCODE, {"rcode": "nodata"}) == 25060.0  # noqa: PLR2004
    assert _count(writer, M_ANSWER_RCODE) == 7  # noqa: PLR2004

    # Secure / bogus.
    assert _gauge_value(writer, M_ANSWER_SECURE, {}) == 15052.0  # noqa: PLR2004
    assert _gauge_value(writer, M_ANSWER_BOGUS, {}) == 0.0

    # api_took.
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "unbound/stats_noreset"}) is not None

    # Exact emit count:
    #   api_took(1) + extended_enabled(1)
    #   + default set: queries, cachehits, cachemiss, prefetch,
    #     requestlist.current, requestlist.exceeded (6)
    #   + recursion avg + median (2)
    #   + cache_hit_ratio (1)
    #   + histogram quantiles (3)
    #   + query types (10) + rcodes (7)
    #   + secure + bogus (2)
    #   = 1+1+6+2+1+3+10+7+2 = 33
    assert result.metrics_emitted == 33  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Happy path: default (degrade)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_default_degrades() -> None:
    writer = InMemoryMetricsWriter()
    collector = _collector_from_stdout(_read_fixture("unbound_stats_default.txt"))
    result = await collector.run(_ctx(writer))

    assert result.ok is True
    assert _gauge_value(writer, M_QUERIES, {}) == 108637.0  # noqa: PLR2004
    assert _gauge_value(writer, M_EXTENDED_ENABLED, {}) == 0.0

    # No extended families.
    assert _count(writer, M_QUERY_TYPE) == 0
    assert _count(writer, M_ANSWER_RCODE) == 0
    assert _gauge_value(writer, M_ANSWER_SECURE, {}) is None
    assert _gauge_value(writer, M_ANSWER_BOGUS, {}) is None
    assert _gauge_value(writer, M_RECURSION_TIME, {"quantile": "0.5"}) is None

    # avg/median ARE in the default fixture (they're not extended-only).
    assert _gauge_value(writer, M_RECURSION_TIME, {"quantile": "avg"}) == pytest.approx(0.049841)  # pyright: ignore[reportUnknownMemberType]

    # api_took present.
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "unbound/stats_noreset"}) is not None

    # Exact emit count:
    #   api_took(1) + extended_enabled(1) + default set(6)
    #   + recursion avg + median(2) + cache_hit_ratio(1) = 11
    assert result.metrics_emitted == 11  # noqa: PLR2004


# ---------------------------------------------------------------------------
# _read fallback + missing key (through run)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_thread0_fallback_and_missing_key() -> None:
    """Only thread0.* present -> still emits; a missing key is skipped."""
    # thread0 only for queries; cachehits/cachemiss present (so extended stays
    # off — no histogram/query.type keys); prefetch ABSENT (skipped).
    stdout = "\n".join(
        [
            "thread0.num.queries=10",
            "thread0.num.cachehits=4",
            "thread0.num.cachemiss=6",
            "thread0.requestlist.current.all=0",
            "thread0.requestlist.exceeded=0",
        ]
    )
    writer = InMemoryMetricsWriter()
    collector = _collector_from_stdout(stdout)
    result = await collector.run(_ctx(writer))

    assert result.ok is True
    assert _gauge_value(writer, M_QUERIES, {}) == 10.0  # noqa: PLR2004
    assert _gauge_value(writer, M_CACHE_HITS, {}) == 4.0  # noqa: PLR2004
    # prefetch absent -> not emitted.
    assert _gauge_value(writer, M_PREFETCH, {}) is None
    # ratio = 4/10.
    ratio = _gauge_value(writer, M_CACHE_HIT_RATIO, {})
    assert ratio == pytest.approx(0.4)  # pyright: ignore[reportUnknownMemberType]
    assert _gauge_value(writer, M_EXTENDED_ENABLED, {}) == 0.0


@pytest.mark.asyncio
async def test_total_prefix_preferred_over_thread0() -> None:
    """When both total.* and thread0.* exist, total.* wins."""
    stdout = "\n".join(
        [
            "thread0.num.queries=1",
            "total.num.queries=99",
        ]
    )
    writer = InMemoryMetricsWriter()
    collector = _collector_from_stdout(stdout)
    result = await collector.run(_ctx(writer))
    assert result.ok is True
    assert _gauge_value(writer, M_QUERIES, {}) == 99.0  # noqa: PLR2004


@pytest.mark.asyncio
async def test_cache_hit_ratio_denom_zero_skipped() -> None:
    """hits=0, misses=0 -> denom 0 -> ratio NOT emitted."""
    stdout = "\n".join(
        [
            "total.num.cachehits=0",
            "total.num.cachemiss=0",
        ]
    )
    writer = InMemoryMetricsWriter()
    collector = _collector_from_stdout(stdout)
    result = await collector.run(_ctx(writer))
    assert result.ok is True
    assert _gauge_value(writer, M_CACHE_HIT_RATIO, {}) is None


@pytest.mark.asyncio
async def test_cache_hit_ratio_missing_hits_skipped() -> None:
    """Only misses present -> ratio NOT emitted (hits None branch)."""
    stdout = "total.num.cachemiss=5"
    writer = InMemoryMetricsWriter()
    collector = _collector_from_stdout(stdout)
    result = await collector.run(_ctx(writer))
    assert result.ok is True
    assert _gauge_value(writer, M_CACHE_HIT_RATIO, {}) is None


@pytest.mark.asyncio
async def test_cache_hit_ratio_missing_misses_skipped() -> None:
    """Only hits present -> ratio NOT emitted (misses None branch)."""
    stdout = "total.num.cachehits=5"
    writer = InMemoryMetricsWriter()
    collector = _collector_from_stdout(stdout)
    result = await collector.run(_ctx(writer))
    assert result.ok is True
    assert _gauge_value(writer, M_CACHE_HIT_RATIO, {}) is None


# ---------------------------------------------------------------------------
# _histogram_quantiles edges (through run, extended_enabled True)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extended_no_histogram_no_quantiles() -> None:
    """Extended (query.type present) but no histogram -> no quantile series."""
    stdout = "\n".join(
        [
            "num.query.type.A=5",
        ]
    )
    writer = InMemoryMetricsWriter()
    collector = _collector_from_stdout(stdout)
    result = await collector.run(_ctx(writer))
    assert result.ok is True
    assert _gauge_value(writer, M_EXTENDED_ENABLED, {}) == 1.0
    assert _gauge_value(writer, M_RECURSION_TIME, {"quantile": "0.5"}) is None
    # query type emitted; rcode/secure/bogus absent (their None/empty branches).
    assert _gauge_value(writer, M_QUERY_TYPE, {"type": "A"}) == 5.0  # noqa: PLR2004
    assert _count(writer, M_ANSWER_RCODE) == 0
    assert _gauge_value(writer, M_ANSWER_SECURE, {}) is None
    assert _gauge_value(writer, M_ANSWER_BOGUS, {}) is None


@pytest.mark.asyncio
async def test_extended_malformed_histogram_key_skipped() -> None:
    """A malformed histogram key is skipped; no buckets -> no quantiles."""
    stdout = "\n".join(
        [
            "num.query.type.A=5",
            "histogram.bad=12",
        ]
    )
    writer = InMemoryMetricsWriter()
    collector = _collector_from_stdout(stdout)
    result = await collector.run(_ctx(writer))
    assert result.ok is True
    assert _gauge_value(writer, M_RECURSION_TIME, {"quantile": "0.5"}) is None


@pytest.mark.asyncio
async def test_extended_histogram_float_parse_error_skipped() -> None:
    """A histogram key with one '.to.' but a non-float bound hits the ValueError-skip path."""
    stdout = "\n".join(
        [
            "num.query.type.A=5",
            "histogram.notafloat.to.000000.016384=10",
        ]
    )
    writer = InMemoryMetricsWriter()
    collector = _collector_from_stdout(stdout)
    result = await collector.run(_ctx(writer))
    assert result.ok is True
    # The `histogram.` key makes extended_enabled True...
    assert _gauge_value(writer, M_EXTENDED_ENABLED, {}) == 1.0
    # ...but the only bucket fails float-parse -> no buckets -> no quantiles emitted.
    assert _gauge_value(writer, M_RECURSION_TIME, {"quantile": "0.5"}) is None


@pytest.mark.asyncio
async def test_extended_histogram_zero_total_no_quantiles() -> None:
    """All bucket counts 0 -> total<=0 -> no quantiles."""
    stdout = "\n".join(
        [
            "num.query.type.A=5",
            "histogram.000000.000000.to.000000.001000=0",
            "histogram.000000.001000.to.000000.002000=0",
        ]
    )
    writer = InMemoryMetricsWriter()
    collector = _collector_from_stdout(stdout)
    result = await collector.run(_ctx(writer))
    assert result.ok is True
    assert _gauge_value(writer, M_RECURSION_TIME, {"quantile": "0.5"}) is None


@pytest.mark.asyncio
async def test_extended_single_bucket_quantiles() -> None:
    """Single non-zero bucket -> all quantiles land inside [lo, hi]."""
    stdout = "\n".join(
        [
            "num.query.type.A=5",
            "histogram.000000.010000.to.000000.020000=100",
        ]
    )
    writer = InMemoryMetricsWriter()
    collector = _collector_from_stdout(stdout)
    result = await collector.run(_ctx(writer))
    assert result.ok is True
    p50 = _gauge_value(writer, M_RECURSION_TIME, {"quantile": "0.5"})
    p99 = _gauge_value(writer, M_RECURSION_TIME, {"quantile": "0.99"})
    assert p50 is not None
    assert p99 is not None
    assert 0.01 <= p50 <= 0.02  # noqa: PLR2004
    assert 0.01 <= p99 <= 0.02  # noqa: PLR2004
    assert p50 <= p99


@pytest.mark.asyncio
async def test_extended_multi_bucket_with_zero_count_bucket() -> None:
    """A zero-count bucket between non-zero ones is skipped in the walk."""
    stdout = "\n".join(
        [
            "num.query.type.A=5",
            "histogram.000000.000000.to.000000.010000=10",
            "histogram.000000.010000.to.000000.020000=0",
            "histogram.000000.020000.to.000000.030000=90",
        ]
    )
    writer = InMemoryMetricsWriter()
    collector = _collector_from_stdout(stdout)
    result = await collector.run(_ctx(writer))
    assert result.ok is True
    p50 = _gauge_value(writer, M_RECURSION_TIME, {"quantile": "0.5"})
    p99 = _gauge_value(writer, M_RECURSION_TIME, {"quantile": "0.99"})
    assert p50 is not None
    assert p99 is not None
    # total=100, rank(p50)=50 -> lands in the 0.020-0.030 bucket.
    assert 0.02 <= p50 <= 0.03  # noqa: PLR2004
    assert p50 <= p99


@pytest.mark.asyncio
async def test_extended_secure_present_bogus_absent() -> None:
    """secure present, bogus absent -> exercises the bogus is-None branch."""
    stdout = "\n".join(
        [
            "num.query.type.A=5",
            "num.answer.secure=12",
        ]
    )
    writer = InMemoryMetricsWriter()
    collector = _collector_from_stdout(stdout)
    result = await collector.run(_ctx(writer))
    assert result.ok is True
    assert _gauge_value(writer, M_ANSWER_SECURE, {}) == 12.0  # noqa: PLR2004
    assert _gauge_value(writer, M_ANSWER_BOGUS, {}) is None


# ---------------------------------------------------------------------------
# Bundle registration
# ---------------------------------------------------------------------------


def test_collector_registered_in_bundle() -> None:
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    names = {lc.collector.name for lc in loaded}
    assert "unbound_stats" in names
