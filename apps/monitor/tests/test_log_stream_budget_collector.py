"""Tests for :class:`LogStreamBudgetCollector`."""

from __future__ import annotations

import re

import httpx
import pytest
import structlog
from pytest_httpx import HTTPXMock

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    MemoryRetainingMetricsWriter,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.builtin.log_stream_budget import (
    LogStreamBudgetCollector,
    LogStreamState,
)

_VL_URL = "http://vl-test:9428"
_STATS_URL = f"{_VL_URL}/select/logsql/stats_query"
_STATS_RE = re.compile(r"http://vl-test:9428/select/logsql/stats_query.*")


def _vector_body(rows: list[dict[str, object]]) -> dict[str, object]:
    """Build a Prometheus instant-vector body in VL stats_query shape."""
    return {"status": "success", "data": {"resultType": "vector", "result": rows}}


def _row(name: str, host: str, service: str, value: str) -> dict[str, object]:
    """One instant-vector row: metric.__name__/host/service + [ts, value]."""
    return {
        "metric": {"__name__": name, "host": host, "service": service},
        "value": [1_717_000_000, value],
    }


def _ctx(
    writer: MemoryRetainingMetricsWriter,
    cfg: CollectorConfig,
    repo: SqliteRepository,
    http_client: httpx.AsyncClient,
) -> CollectorContext:
    return CollectorContext(
        config=cfg,
        db=repo,
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=http_client,
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="log_stream_budget"),  # pyright: ignore[reportArgumentType]
        ha=None,
    )


@pytest.mark.asyncio
async def test_run_emits_metrics_and_updates_state(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """Mock VL stats -> per-stream gauges + state map populated."""
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json=_vector_body(
            [
                _row("lines", "alice", "nginx", "10"),
                _row("bytes_today", "alice", "nginx", "100"),
                _row("lines", "alice", "ssh", "5"),
                _row("bytes_today", "alice", "ssh", "50"),
            ]
        ),
    )
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json=_vector_body(
            [
                _row("lines", "alice", "nginx", "1500"),
                _row("lines", "alice", "ssh", "300"),
            ]
        ),
    )
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=client)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))

    assert result.ok
    assert result.metrics_emitted == 6  # 2 streams * 3 metrics  # noqa: PLR2004
    bytes_entries = {
        (e.labels["host"], e.labels["service"]): e.value
        for e in writer.snapshot()
        if e.name == "homelab_log_stream_bytes_today"
    }
    assert bytes_entries[("alice", "nginx")] == 100.0  # noqa: PLR2004
    assert bytes_entries[("alice", "ssh")] == 50.0  # noqa: PLR2004
    rate_entries = {
        (e.labels["host"], e.labels["service"]): e.value
        for e in writer.snapshot()
        if e.name == "homelab_log_stream_lines_per_sec"
    }
    assert rate_entries[("alice", "nginx")] == 5.0  # noqa: PLR2004
    budget_entries = {
        (e.labels["host"], e.labels["service"]): e.value
        for e in writer.snapshot()
        if e.name == "homelab_log_stream_bytes_budget"
    }
    assert ("alice", "nginx") in budget_entries
    assert ("alice", "ssh") in budget_entries
    # Both streams get the same configured cap
    assert budget_entries[("alice", "nginx")] == budget_entries[("alice", "ssh")]
    assert ("alice", "nginx") in state
    assert state[("alice", "nginx")].bytes_today == 100  # noqa: PLR2004


@pytest.mark.asyncio
async def test_run_handles_transport_error(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """VL transport error -> ok=False with vl_transport: error."""
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=client)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))
    assert result.ok is False
    assert any(e.startswith("vl_transport:") for e in result.errors)
    assert state == {}


@pytest.mark.asyncio
async def test_run_handles_non_200(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """VL non-200 -> ok=False with vl_status."""
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        status_code=503,
        text="busy",
    )
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=client)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))
    assert result.ok is False
    assert any(e.startswith("vl_status:") for e in result.errors)


@pytest.mark.asyncio
async def test_run_handles_malformed_json(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """VL non-JSON body -> ok=False with vl_json."""
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        text="not-json",
    )
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=client)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))
    assert result.ok is False
    assert any(e.startswith("vl_json:") for e in result.errors)


@pytest.mark.asyncio
async def test_run_skips_items_with_missing_host_or_service(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """Items missing host or service are silently skipped."""
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json=_vector_body(
            [
                _row("bytes_today", "", "x", "1"),
                _row("bytes_today", "y", "", "1"),
                _row("bytes_today", "ok", "ok", "1"),
            ]
        ),
    )
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json=_vector_body([_row("lines", "ok", "ok", "30")]),
    )
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=client)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))
    assert result.ok
    assert len(state) == 1
    assert ("ok", "ok") in state


@pytest.mark.asyncio
async def test_run_skips_unparseable_values(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """Items with non-numeric bytes/lines are skipped."""
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json=_vector_body(
            [
                _row("bytes_today", "h", "s", "not-int"),
                _row("bytes_today", "h2", "s2", "1"),
            ]
        ),
    )
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json=_vector_body([_row("lines", "h2", "s2", "300")]),
    )
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=client)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))
    assert result.ok
    assert len(state) == 1
    assert ("h2", "s2") in state


@pytest.mark.asyncio
async def test_collect_no_http_client_returns_error_result(
    repo: SqliteRepository,
) -> None:
    """When both collector._http_client and ctx.http are None, result is ok=False."""
    state: LogStreamState = {}
    collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=None)
    writer = MemoryRetainingMetricsWriter()
    cfg = CollectorConfig(name="log_stream_budget")
    # Build a context with http=None to exercise the http_client_unavailable guard
    ctx = CollectorContext(
        config=cfg,
        db=repo,
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="log_stream_budget"),  # pyright: ignore[reportArgumentType]
        ha=None,
    )
    result = await collector.run(ctx)
    assert result.ok is False
    assert "http_client_unavailable" in result.errors


@pytest.mark.asyncio
async def test_collect_skips_non_dict_stream_item(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """Non-dict items in the result list are skipped; valid dict items are processed."""
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json={
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    "bad-non-dict-string",
                    _row("bytes_today", "h", "s", "10"),
                ],
            },
        },
    )
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json=_vector_body([_row("lines", "h", "s", "300")]),
    )
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=client)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))
    assert result.ok
    assert len(state) == 1
    assert ("h", "s") in state


@pytest.mark.asyncio
async def test_run_emits_budget_gauge_with_configured_value(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """homelab_log_stream_bytes_budget gauge reflects the configured per-stream cap."""
    custom_budget = 1_000_000  # 1 MiB — distinct from the 500 MiB default
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json=_vector_body([_row("bytes_today", "h1", "svc1", "123")]),
    )
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json=_vector_body([_row("lines", "h1", "svc1", "600")]),
    )
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(
            state=state,
            vl_url=_VL_URL,
            http_client=client,
            budget_bytes_per_day=custom_budget,
        )
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))

    assert result.ok
    assert result.metrics_emitted == 3  # 1 stream * 3 metrics  # noqa: PLR2004
    budget_entries = {
        (e.labels["host"], e.labels["service"]): e.value
        for e in writer.snapshot()
        if e.name == "homelab_log_stream_bytes_budget"
    }
    assert budget_entries[("h1", "svc1")] == float(custom_budget)


@pytest.mark.asyncio
async def test_run_handles_call2_status_error(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """Call-2 (5m rate) HTTP status error -> ok=False, no partial emit, state untouched."""
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json=_vector_body([_row("bytes_today", "h", "s", "10")]),
    )
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        status_code=503,
        text="down",
    )
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=client)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))
    assert result.ok is False
    assert any(e.startswith("vl_status:") for e in result.errors)
    # Call-1 returned valid bytes_today data; assert NO gauges were emitted
    # (the no-partial-emit guarantee: emit loop runs only after BOTH calls succeed).
    assert writer.snapshot() == []
    assert state == {}


@pytest.mark.asyncio
async def test_run_handles_call2_transport_error(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """Call-2 (5m rate) genuine transport error -> ok=False, no partial emit, state={}."""
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json=_vector_body([_row("bytes_today", "h", "s", "10")]),
    )
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=client)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))
    assert result.ok is False
    assert any(e.startswith("vl_transport:") for e in result.errors)
    assert writer.snapshot() == []
    assert state == {}


@pytest.mark.asyncio
async def test_run_status_not_success_emits_nothing(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """VL status != success -> empty rows, ok=True, no metrics, empty state."""
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json={"status": "error", "errorType": "bad", "data": {}},
    )
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json={"status": "error", "errorType": "bad", "data": {}},
    )
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=client)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))
    assert result.ok
    assert result.metrics_emitted == 0
    assert state == {}


@pytest.mark.asyncio
async def test_run_skips_non_finite_value(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """A NaN/inf bytes value is skipped by the isfinite guard."""
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json=_vector_body(
            [
                _row("bytes_today", "h", "s", "NaN"),
                _row("bytes_today", "h2", "s2", "5"),
            ]
        ),
    )
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json=_vector_body([_row("lines", "h2", "s2", "300")]),
    )
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=client)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))
    assert result.ok
    assert len(state) == 1
    assert ("h2", "s2") in state


@pytest.mark.asyncio
async def test_run_skips_rows_with_non_list_value_field(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """Rows with non-list value field are skipped (e.g., value is a dict)."""
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json=_vector_body(
            [
                {
                    "metric": {"__name__": "bytes_today", "host": "h1", "service": "s1"},
                    "value": {"not": "list"},  # Malformed value
                },
                _row("bytes_today", "h2", "s2", "10"),
            ]
        ),
    )
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json=_vector_body([_row("lines", "h2", "s2", "300")]),
    )
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=client)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))
    assert result.ok
    assert len(state) == 1
    assert ("h2", "s2") in state


@pytest.mark.asyncio
async def test_run_skips_rows_with_short_value_array(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """Rows with value array length < 2 are skipped."""
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json=_vector_body(
            [
                {
                    "metric": {"__name__": "bytes_today", "host": "h1", "service": "s1"},
                    "value": [1_717_000_000],  # Only 1 element, needs [ts, value]
                },
                _row("bytes_today", "h2", "s2", "10"),
            ]
        ),
    )
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json=_vector_body([_row("lines", "h2", "s2", "300")]),
    )
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=client)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))
    assert result.ok
    assert len(state) == 1
    assert ("h2", "s2") in state


@pytest.mark.asyncio
async def test_run_skips_non_lines_metrics_in_rate_response(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """Rate response with non-'lines' metric names are skipped (e.g., 'other_metric')."""
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json=_vector_body([_row("bytes_today", "h", "s", "100")]),
    )
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json=_vector_body(
            [
                _row("other_metric", "h", "s", "42"),  # Not "lines" — should be skipped
                _row("lines", "h", "s", "600"),  # This one is used
            ]
        ),
    )
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=client)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))
    assert result.ok
    assert result.metrics_emitted == 3  # 1 stream * 3 metrics  # noqa: PLR2004
    rate_entries = {
        (e.labels["host"], e.labels["service"]): e.value
        for e in writer.snapshot()
        if e.name == "homelab_log_stream_lines_per_sec"
    }
    # The rate should be 600 / 300 (5-min window) = 2.0
    assert rate_entries[("h", "s")] == 2.0  # noqa: PLR2004


@pytest.mark.asyncio
async def test_run_skips_malformed_rows_in_rate_response(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """Rate response with malformed value data (non-list) is skipped, good rows processed."""
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json=_vector_body([_row("bytes_today", "h", "s", "100")]),
    )
    httpx_mock.add_response(
        url=_STATS_RE,
        method="GET",
        json=_vector_body(
            [
                {
                    "metric": {"__name__": "lines", "host": "h", "service": "s"},
                    "value": "not-a-list",  # Malformed value
                },
                _row("lines", "h", "s", "600"),
            ]
        ),
    )
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=client)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))
    assert result.ok
    assert result.metrics_emitted == 3  # 1 stream * 3 metrics  # noqa: PLR2004
    rate_entries = {
        (e.labels["host"], e.labels["service"]): e.value
        for e in writer.snapshot()
        if e.name == "homelab_log_stream_lines_per_sec"
    }
    assert rate_entries[("h", "s")] == 2.0  # noqa: PLR2004
