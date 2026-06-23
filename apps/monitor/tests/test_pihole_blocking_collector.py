"""Unit tests for PiholeBlockingCollector (STAGE-006-008).

Covers 100% branch coverage across:
- happy path enabled, timer null → blocking_enabled==1.0, timer NOT emitted
- disabled WITH timer → blocking_enabled==0.0, timer==300.0
- disabled indefinitely (timer=null) → enabled==0.0, timer NOT emitted
- blocking="failed" → enabled==0.0 (fail-closed)
- blocking="weird" (unrecognized string) → enabled==0.0
- blocking non-string (e.g. int 123 or absent) → enabled==0.0
- ctx.pihole is None → ok=False, 0 emits
- dns_blocking() returns PiholeError → ok=False, 0 emits
- payload not a dict → ok=False, errors=["unexpected payload shape"], metrics_emitted==1
- timer present but non-numeric string → NOT emitted (as_float None branch)
- timer is bool → NOT emitted (as_float excludes bool)
- metric-name constants literal match
- registration via register_all + PluginLoader
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import structlog

from homelab_monitor.kernel.pihole.client import PiholeResponse
from homelab_monitor.kernel.pihole.errors import PiholeError
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter, InMemoryMetricsWriter
from homelab_monitor.kernel.plugins.loader import PluginLoader
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.integrations.pihole import register_all
from homelab_monitor.plugins.collectors.integrations.pihole.blocking import (
    M_API_TOOK,
    M_BLOCKING_ENABLED,
    M_BLOCKING_TIMER,
    PiholeBlockingCollector,
)


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


class _FakeBlockingOk(_FakePiholeBase):
    """dns_blocking returns a configurable PiholeResponse."""

    def __init__(self, payload: object, took: float = 0.000088) -> None:
        self._payload = payload
        self._took = took

    async def dns_blocking(self) -> PiholeResponse | PiholeError:
        return PiholeResponse(
            payload=self._payload, took_seconds=self._took, endpoint="dns/blocking"
        )


class _FakeBlockingError(_FakePiholeBase):
    """dns_blocking returns a PiholeError."""

    def __init__(self, message: str = "timeout") -> None:
        self._message = message

    async def dns_blocking(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="timeout", message=self._message)


def _ctx(writer: InMemoryMetricsWriter, pihole: object | None) -> CollectorContext:
    """Build a CollectorContext wired to the given writer and pihole client."""
    return CollectorContext(
        config=CollectorConfig(
            name="pihole_blocking",
            interval_seconds=30,
            timeout_seconds=15,
        ),
        db=None,  # type: ignore[arg-type]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="pihole_blocking"),
        pihole=pihole,  # type: ignore[arg-type]
    )


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


@pytest.mark.asyncio
async def test_happy_path_enabled_no_timer() -> None:
    """blocking="enabled", timer=null → enabled==1.0, api_took emitted, timer NOT emitted."""
    payload: dict[str, object] = {"blocking": "enabled", "timer": None, "took": 0.000088}
    writer = InMemoryMetricsWriter()
    collector = PiholeBlockingCollector()
    ctx = _ctx(writer, _FakeBlockingOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == []
    assert result.metrics_emitted == 2  # noqa: PLR2004

    api_took = _gauge_value(writer, M_API_TOOK, {"endpoint": "dns/blocking"})
    assert api_took == pytest.approx(0.000088)  # pyright: ignore[reportUnknownMemberType]

    enabled_val = _gauge_value(writer, M_BLOCKING_ENABLED, {})
    assert enabled_val == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]

    assert M_BLOCKING_TIMER not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_disabled_with_timer() -> None:
    """blocking="disabled", timer=300 → enabled==0.0, timer==300.0."""
    payload: dict[str, object] = {"blocking": "disabled", "timer": 300, "took": 0.000088}
    writer = InMemoryMetricsWriter()
    collector = PiholeBlockingCollector()
    ctx = _ctx(writer, _FakeBlockingOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.metrics_emitted == 3  # noqa: PLR2004

    enabled_val = _gauge_value(writer, M_BLOCKING_ENABLED, {})
    assert enabled_val == pytest.approx(0.0)  # pyright: ignore[reportUnknownMemberType]

    timer_val = _gauge_value(writer, M_BLOCKING_TIMER, {})
    assert timer_val == pytest.approx(300.0)  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.asyncio
async def test_timer_zero_is_emitted() -> None:
    """blocking="disabled", timer=0 → enabled==0.0, timer==0.0 (timer IS emitted)."""
    payload: dict[str, object] = {"blocking": "disabled", "timer": 0, "took": 0.000088}
    writer = InMemoryMetricsWriter()
    collector = PiholeBlockingCollector()
    ctx = _ctx(writer, _FakeBlockingOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.metrics_emitted == 3  # noqa: PLR2004

    enabled_val = _gauge_value(writer, M_BLOCKING_ENABLED, {})
    assert enabled_val == pytest.approx(0.0)  # pyright: ignore[reportUnknownMemberType]

    assert M_BLOCKING_TIMER in _all_metric_names(writer)

    timer_val = _gauge_value(writer, M_BLOCKING_TIMER, {})
    assert timer_val == 0.0


@pytest.mark.asyncio
async def test_disabled_no_timer() -> None:
    """blocking="disabled", timer=null → enabled==0.0, timer NOT emitted."""
    payload: dict[str, object] = {"blocking": "disabled", "timer": None, "took": 0.000088}
    writer = InMemoryMetricsWriter()
    collector = PiholeBlockingCollector()
    ctx = _ctx(writer, _FakeBlockingOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.metrics_emitted == 2  # noqa: PLR2004

    enabled_val = _gauge_value(writer, M_BLOCKING_ENABLED, {})
    assert enabled_val == pytest.approx(0.0)  # pyright: ignore[reportUnknownMemberType]
    assert M_BLOCKING_TIMER not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_blocking_failed_state() -> None:
    """blocking="failed" → enabled==0.0 (fail-closed)."""
    payload: dict[str, object] = {"blocking": "failed", "timer": None, "took": 0.000088}
    writer = InMemoryMetricsWriter()
    collector = PiholeBlockingCollector()
    ctx = _ctx(writer, _FakeBlockingOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    enabled_val = _gauge_value(writer, M_BLOCKING_ENABLED, {})
    assert enabled_val == pytest.approx(0.0)  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.asyncio
async def test_blocking_unrecognized_string() -> None:
    """blocking="weird" → enabled==0.0."""
    payload: dict[str, object] = {"blocking": "weird", "timer": None, "took": 0.000088}
    writer = InMemoryMetricsWriter()
    collector = PiholeBlockingCollector()
    ctx = _ctx(writer, _FakeBlockingOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    enabled_val = _gauge_value(writer, M_BLOCKING_ENABLED, {})
    assert enabled_val == pytest.approx(0.0)  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.asyncio
async def test_blocking_non_string() -> None:
    """blocking=123 (not a str) → enabled==0.0 (isinstance str branch is False)."""
    payload: dict[str, object] = {"blocking": 123, "timer": None, "took": 0.000088}
    writer = InMemoryMetricsWriter()
    collector = PiholeBlockingCollector()
    ctx = _ctx(writer, _FakeBlockingOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    enabled_val = _gauge_value(writer, M_BLOCKING_ENABLED, {})
    assert enabled_val == pytest.approx(0.0)  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.asyncio
async def test_blocking_key_absent() -> None:
    """blocking key missing entirely → enabled==0.0."""
    payload: dict[str, object] = {"timer": None, "took": 0.000088}
    writer = InMemoryMetricsWriter()
    collector = PiholeBlockingCollector()
    ctx = _ctx(writer, _FakeBlockingOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    enabled_val = _gauge_value(writer, M_BLOCKING_ENABLED, {})
    assert enabled_val == pytest.approx(0.0)  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.asyncio
async def test_ctx_pihole_none() -> None:
    """ctx.pihole is None → ok=False, error message, 0 emits."""
    writer = InMemoryMetricsWriter()
    collector = PiholeBlockingCollector()
    result = await collector.run(_ctx(writer, None))
    assert result.ok is False
    assert result.errors == ["pihole client not configured"]
    assert result.metrics_emitted == 0
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_dns_blocking_returns_pihole_error() -> None:
    """dns_blocking() returns PiholeError → ok=False, errors carries message, 0 emits."""
    writer = InMemoryMetricsWriter()
    collector = PiholeBlockingCollector()
    ctx = _ctx(writer, _FakeBlockingError("GET /api/dns/blocking: timed out"))
    result = await collector.run(ctx)
    assert result.ok is False
    assert result.errors == ["GET /api/dns/blocking: timed out"]
    assert result.metrics_emitted == 0
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_payload_not_a_dict() -> None:
    """payload is a list (not a dict) → ok=False, errors=["unexpected
    payload shape"], metrics_emitted==1."""
    writer = InMemoryMetricsWriter()
    collector = PiholeBlockingCollector()
    ctx = _ctx(writer, _FakeBlockingOk(["not", "a", "dict"]))
    result = await collector.run(ctx)
    assert result.ok is False
    assert result.errors == ["unexpected payload shape"]
    assert result.metrics_emitted == 1  # api_took already emitted


@pytest.mark.asyncio
async def test_timer_non_numeric_string_not_emitted() -> None:
    """timer="soon" → as_float returns None → timer NOT emitted."""
    payload: dict[str, object] = {"blocking": "disabled", "timer": "soon", "took": 0.000088}
    writer = InMemoryMetricsWriter()
    collector = PiholeBlockingCollector()
    ctx = _ctx(writer, _FakeBlockingOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.metrics_emitted == 2  # noqa: PLR2004
    assert M_BLOCKING_TIMER not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_timer_bool_not_emitted() -> None:
    """timer=True → as_float excludes bool → timer NOT emitted."""
    payload: dict[str, object] = {"blocking": "disabled", "timer": True, "took": 0.000088}
    writer = InMemoryMetricsWriter()
    collector = PiholeBlockingCollector()
    ctx = _ctx(writer, _FakeBlockingOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    assert M_BLOCKING_TIMER not in _all_metric_names(writer)


def test_metric_name_constants_match_contract() -> None:
    """Metric-name constants must equal the literal contract names."""
    assert M_API_TOOK == "homelab_pihole_api_took_seconds"
    assert M_BLOCKING_ENABLED == "homelab_pihole_blocking_enabled"
    assert M_BLOCKING_TIMER == "homelab_pihole_blocking_timer_seconds"


@pytest.mark.asyncio
async def test_registration() -> None:
    """PiholeBlockingCollector is registered via register_all + PluginLoader."""
    loader = MagicMock(spec=PluginLoader)
    register_all(loader)

    registered_classes = [call.args[0] for call in loader.register.call_args_list]
    assert PiholeBlockingCollector in registered_classes
