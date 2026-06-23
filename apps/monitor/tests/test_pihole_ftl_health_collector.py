"""Unit tests for PiholeFtlHealthCollector (STAGE-006-009).

100% branch coverage across the two-endpoint resilience matrix,
dnsmasq nested-object narrowing, and emit_numeric skip paths.
"""

from __future__ import annotations

import pytest
import structlog

from homelab_monitor.kernel.pihole.client import PiholeResponse
from homelab_monitor.kernel.pihole.errors import PiholeError
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.integrations.pihole import register_all
from homelab_monitor.plugins.collectors.integrations.pihole.ftl_health import (
    M_API_TOOK,
    M_DB_QUERIES,
    M_DB_SIZE,
    M_DNSMASQ_CACHE_EVICTIONS,
    M_DNSMASQ_CACHE_INSERTIONS,
    M_FTL_CPU,
    M_FTL_MEMORY,
    M_FTL_UPTIME,
    M_PRIVACY_LEVEL,
    PiholeFtlHealthCollector,
)

# ---------------------------------------------------------------------------
# Fake Pi-hole client base (copy _FakePiholeBase from gravity test verbatim)
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


_ERR = object()


class _FakeFtlHealth(_FakePiholeBase):
    """info_ftl + info_database each independently return PiholeResponse or PiholeError.

    Pass a payload object to return OK, or the ``_ERR`` sentinel to return a PiholeError.
    """

    def __init__(  # noqa: PLR0913
        self,
        *,
        ftl_payload: object = _ERR,
        ftl_err_msg: str = "ftl failed",
        ftl_took: float = 0.0015,
        db_payload: object = _ERR,
        db_err_msg: str = "db failed",
        db_took: float = 0.0008,
    ) -> None:
        self._ftl_payload = ftl_payload
        self._ftl_err_msg = ftl_err_msg
        self._ftl_took = ftl_took
        self._db_payload = db_payload
        self._db_err_msg = db_err_msg
        self._db_took = db_took

    async def info_ftl(self) -> PiholeResponse | PiholeError:
        if self._ftl_payload is _ERR:
            return PiholeError(reason="timeout", message=self._ftl_err_msg)
        return PiholeResponse(
            payload=self._ftl_payload, took_seconds=self._ftl_took, endpoint="info/ftl"
        )

    async def info_database(self) -> PiholeResponse | PiholeError:
        if self._db_payload is _ERR:
            return PiholeError(reason="timeout", message=self._db_err_msg)
        return PiholeResponse(
            payload=self._db_payload, took_seconds=self._db_took, endpoint="info/database"
        )


# ---------------------------------------------------------------------------
# ctx builder (copy from gravity test verbatim, update name)
# ---------------------------------------------------------------------------


def _ctx(writer: InMemoryMetricsWriter, pihole: object | None) -> CollectorContext:
    """Build a CollectorContext wired to the given writer and pihole client."""
    return CollectorContext(
        config=CollectorConfig(
            name="pihole_ftl_health",
            interval_seconds=60,
            timeout_seconds=15,
        ),
        db=None,  # type: ignore[arg-type]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="pihole_ftl_health"),
        pihole=pihole,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Helpers (copy verbatim from gravity test)
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
# Live-shape fixtures (Pi-hole v6 /api/info/ftl response structure)
# ---------------------------------------------------------------------------

_FTL_PAYLOAD: dict[str, object] = {
    "ftl": {
        "uptime": 86400.0,
        "%cpu": 0.3,
        "%mem": 0.5,
        "privacy_level": 0,
        "dnsmasq": {
            "dns_cache_inserted": 1234,
            "dns_cache_live_freed": 56,
        },
    },
}

_DB_PAYLOAD: dict[str, object] = {
    "size": 104857600,  # 100 MiB
    "queries": 9999999,  # intentionally different from queries_disk
    "queries_disk": 5000000,  # this is the one that should be emitted
}


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_both_endpoints() -> None:
    """Both endpoints OK -> ok=True, all 10 metrics emitted."""
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlHealthCollector()
    fake = _FakeFtlHealth(ftl_payload=_FTL_PAYLOAD, db_payload=_DB_PAYLOAD)
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == []

    # api_took emitted twice, once per endpoint
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "info/ftl"}) is not None
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "info/database"}) is not None

    # FTL metrics
    assert _gauge_value(writer, M_FTL_UPTIME, {}) == 86400.0  # noqa: PLR2004
    assert _gauge_value(writer, M_FTL_CPU, {}) == 0.3  # noqa: PLR2004
    assert _gauge_value(writer, M_FTL_MEMORY, {}) == 0.5  # noqa: PLR2004
    assert _gauge_value(writer, M_PRIVACY_LEVEL, {}) == 0.0
    assert _gauge_value(writer, M_DNSMASQ_CACHE_INSERTIONS, {}) == 1234.0  # noqa: PLR2004
    assert _gauge_value(writer, M_DNSMASQ_CACHE_EVICTIONS, {}) == 56.0  # noqa: PLR2004

    # DB metrics
    assert _gauge_value(writer, M_DB_SIZE, {}) == 104857600.0  # noqa: PLR2004
    assert _gauge_value(writer, M_DB_QUERIES, {}) == 5000000.0  # noqa: PLR2004

    # 2 api_took + 4 ftl-flat + 2 dnsmasq + 2 db = 10
    assert result.metrics_emitted == 10  # noqa: PLR2004


@pytest.mark.asyncio
async def test_ctx_pihole_none() -> None:
    """ctx.pihole is None -> ok=False, error message, 0 metrics."""
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlHealthCollector()
    ctx = _ctx(writer, None)

    result = await collector.run(ctx)

    assert result.ok is False
    assert result.errors == ["pihole client not configured"]
    assert result.metrics_emitted == 0
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_both_endpoints_error() -> None:
    """Both endpoints error -> ok=False, both error messages, 0 metrics."""
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlHealthCollector()
    fake = _FakeFtlHealth(
        ftl_payload=_ERR,
        ftl_err_msg="ftl boom",
        db_payload=_ERR,
        db_err_msg="db boom",
    )
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is False
    assert result.errors == ["ftl boom", "db boom"]
    assert result.metrics_emitted == 0
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_ftl_error_db_ok() -> None:
    """ftl errors, db OK -> ok=True (db_ok), ftl error appended, no ftl metrics."""
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlHealthCollector()
    fake = _FakeFtlHealth(ftl_payload=_ERR, ftl_err_msg="ftl boom", db_payload=_DB_PAYLOAD)
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == ["ftl boom"]
    # ftl metrics absent
    assert M_FTL_UPTIME not in _all_metric_names(writer)
    assert M_DNSMASQ_CACHE_INSERTIONS not in _all_metric_names(writer)
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "info/ftl"}) is None
    # db metrics present
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "info/database"}) is not None
    assert _gauge_value(writer, M_DB_SIZE, {}) is not None
    assert _gauge_value(writer, M_DB_QUERIES, {}) is not None


@pytest.mark.asyncio
async def test_ftl_ok_db_error() -> None:
    """ftl OK, db errors -> ok=True (ftl_ok), db error appended, no db metrics."""
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlHealthCollector()
    fake = _FakeFtlHealth(ftl_payload=_FTL_PAYLOAD, db_payload=_ERR, db_err_msg="db boom")
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == ["db boom"]
    # ftl metrics present
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "info/ftl"}) is not None
    assert _gauge_value(writer, M_FTL_UPTIME, {}) is not None
    # db metrics absent
    assert M_DB_SIZE not in _all_metric_names(writer)
    assert M_DB_QUERIES not in _all_metric_names(writer)
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "info/database"}) is None


@pytest.mark.asyncio
async def test_ftl_payload_not_a_dict() -> None:
    """ftl payload not a dict -> ftl_ok=False, shape error appended; api_took still emitted.

    db still processed normally if OK.
    """
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlHealthCollector()
    fake = _FakeFtlHealth(ftl_payload=[], db_payload=_DB_PAYLOAD)
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    # ftl_ok=False but db_ok=True -> ok=True
    assert result.ok is True
    assert "unexpected payload shape (info/ftl)" in result.errors
    # api_took for ftl WAS emitted before shape check
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "info/ftl"}) is not None
    # no ftl content metrics
    assert M_FTL_UPTIME not in _all_metric_names(writer)
    # db metrics present
    assert _gauge_value(writer, M_DB_SIZE, {}) is not None


@pytest.mark.asyncio
async def test_db_payload_not_a_dict() -> None:
    """db payload not a dict -> db_ok=False, shape error appended; api_took still emitted.

    ftl still processed normally if OK.
    """
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlHealthCollector()
    fake = _FakeFtlHealth(ftl_payload=_FTL_PAYLOAD, db_payload="nope")
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    # db_ok=False but ftl_ok=True -> ok=True
    assert result.ok is True
    assert "unexpected payload shape (info/database)" in result.errors
    # api_took for db WAS emitted before shape check
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "info/database"}) is not None
    # no db content metrics
    assert M_DB_SIZE not in _all_metric_names(writer)
    # ftl metrics present
    assert _gauge_value(writer, M_FTL_UPTIME, {}) is not None


@pytest.mark.asyncio
async def test_dnsmasq_missing_skips_cache_metrics() -> None:
    """dnsmasq key missing -> cache metrics NOT emitted; other ftl metrics still present."""
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlHealthCollector()
    ftl_no_dnsmasq: dict[str, object] = {
        "ftl": {
            "uptime": 3600.0,
            "%cpu": 0.1,
            "%mem": 0.2,
            "privacy_level": 1,
            # no "dnsmasq" key
        },
    }
    fake = _FakeFtlHealth(ftl_payload=ftl_no_dnsmasq, db_payload=_ERR)
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauge_value(writer, M_FTL_UPTIME, {}) == 3600.0  # noqa: PLR2004
    assert M_DNSMASQ_CACHE_INSERTIONS not in _all_metric_names(writer)
    assert M_DNSMASQ_CACHE_EVICTIONS not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_dnsmasq_non_dict_skips_cache_metrics() -> None:
    """dnsmasq present but not a dict -> cache metrics NOT emitted.

    Covers isinstance False branch.
    """
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlHealthCollector()
    ftl_bad_dnsmasq: dict[str, object] = {
        "ftl": {
            "uptime": 7200.0,
            "%cpu": 0.2,
            "%mem": 0.4,
            "privacy_level": 0,
            "dnsmasq": "x",  # non-dict
        },
    }
    fake = _FakeFtlHealth(ftl_payload=ftl_bad_dnsmasq, db_payload=_ERR)
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauge_value(writer, M_FTL_UPTIME, {}) == 7200.0  # noqa: PLR2004
    assert M_DNSMASQ_CACHE_INSERTIONS not in _all_metric_names(writer)
    assert M_DNSMASQ_CACHE_EVICTIONS not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_ftl_numeric_field_non_numeric_skipped() -> None:
    """Non-numeric ftl field -> that metric NOT emitted (covers emit_numeric None path)."""
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlHealthCollector()
    ftl_bad_uptime: dict[str, object] = {
        "ftl": {
            "uptime": "up",  # non-numeric -> emit_numeric skips
            "%cpu": 0.1,
            "%mem": 0.2,
            "privacy_level": 0,
        },
    }
    fake = _FakeFtlHealth(ftl_payload=ftl_bad_uptime, db_payload=_ERR)
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert M_FTL_UPTIME not in _all_metric_names(writer)
    # other fields still emitted
    assert _gauge_value(writer, M_FTL_CPU, {}) is not None


@pytest.mark.asyncio
async def test_ftl_payload_missing_ftl_key() -> None:
    """ftl payload has no "ftl" key -> no ftl metrics emitted, result.ok=True, no error.

    Regression test: the real Pi-hole v6 shape has ftl scalars nested under a "ftl" key.
    If that key is missing, _emit_ftl returns early and silently (sub-skip pattern).
    """
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlHealthCollector()
    ftl_no_key: dict[str, object] = {
        "took": 0.1,  # endpoint succeeded, but no "ftl" key
    }
    fake = _FakeFtlHealth(ftl_payload=ftl_no_key, db_payload=_DB_PAYLOAD)
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == []

    # All 4 ftl scalar metrics NOT emitted
    assert M_FTL_UPTIME not in _all_metric_names(writer)
    assert M_FTL_CPU not in _all_metric_names(writer)
    assert M_FTL_MEMORY not in _all_metric_names(writer)
    assert M_PRIVACY_LEVEL not in _all_metric_names(writer)

    # dnsmasq cache metrics NOT emitted (no ftl object -> no dnsmasq)
    assert M_DNSMASQ_CACHE_INSERTIONS not in _all_metric_names(writer)
    assert M_DNSMASQ_CACHE_EVICTIONS not in _all_metric_names(writer)

    # api_took for ftl WAS emitted (endpoint call succeeded)
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "info/ftl"}) is not None

    # db metrics ARE emitted (db endpoint OK)
    assert _gauge_value(writer, M_DB_SIZE, {}) is not None
    assert _gauge_value(writer, M_DB_QUERIES, {}) is not None
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "info/database"}) is not None


@pytest.mark.asyncio
async def test_db_queries_reads_queries_disk_not_queries() -> None:
    """M_DB_QUERIES == queries_disk value, NOT queries (proves key selection)."""
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlHealthCollector()
    # Deliberately different values for queries vs queries_disk
    db_both_keys: dict[str, object] = {
        "size": 1024,
        "queries": 9999,  # should NOT be emitted
        "queries_disk": 5555,  # should be emitted
    }
    fake = _FakeFtlHealth(ftl_payload=_ERR, db_payload=db_both_keys)
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    val = _gauge_value(writer, M_DB_QUERIES, {})
    assert val == 5555.0  # noqa: PLR2004  -- proves queries_disk was read, not queries
    assert val != 9999.0  # noqa: PLR2004


def test_metric_name_constants_match_contract() -> None:
    """All nine public M_* constants match their literal strings."""
    assert M_API_TOOK == "homelab_pihole_api_took_seconds"
    assert M_FTL_UPTIME == "homelab_pihole_ftl_uptime_seconds"
    assert M_FTL_CPU == "homelab_pihole_ftl_cpu_percent"
    assert M_FTL_MEMORY == "homelab_pihole_ftl_memory_percent"
    assert M_PRIVACY_LEVEL == "homelab_pihole_privacy_level"
    assert M_DNSMASQ_CACHE_INSERTIONS == "homelab_pihole_dnsmasq_cache_insertions"
    assert M_DNSMASQ_CACHE_EVICTIONS == "homelab_pihole_dnsmasq_cache_evictions"
    assert M_DB_SIZE == "homelab_pihole_db_size_bytes"
    assert M_DB_QUERIES == "homelab_pihole_db_queries_total"


@pytest.mark.asyncio
async def test_registration() -> None:
    """PiholeFtlHealthCollector is registered by register_all."""
    registered: list[str] = []

    class _FakeLoader:
        def register(self, cls: type, config: object) -> None:  # type: ignore[override]
            registered.append(cls.name)  # type: ignore[attr-defined]

    register_all(_FakeLoader())  # type: ignore[arg-type]
    assert "pihole_ftl_health" in registered


@pytest.mark.asyncio
async def test_scope_out_no_host_or_gravity_metrics() -> None:
    """Happy path emits ONLY the 9 expected metric names (+ api_took).

    Specifically:
    - No host cpu/mem metrics ("homelab_pihole_host_*")
    - No gravity_domains metric (gravity.py owns it)
    - Exact set of 9 base names (api_took counts as 1 name with 2 label variants)
    """
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlHealthCollector()
    fake = _FakeFtlHealth(ftl_payload=_FTL_PAYLOAD, db_payload=_DB_PAYLOAD)
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    names = _all_metric_names(writer)

    expected = {
        M_API_TOOK,
        M_FTL_UPTIME,
        M_FTL_CPU,
        M_FTL_MEMORY,
        M_PRIVACY_LEVEL,
        M_DNSMASQ_CACHE_INSERTIONS,
        M_DNSMASQ_CACHE_EVICTIONS,
        M_DB_SIZE,
        M_DB_QUERIES,
    }
    assert names == expected

    # Explicit scope-out assertions
    assert "homelab_pihole_gravity_domains" not in names
    assert not any(n.startswith("homelab_pihole_host_") for n in names)
