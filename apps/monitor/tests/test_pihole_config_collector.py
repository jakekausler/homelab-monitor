"""Tests for PiholeConfigCollector (STAGE-006-018).

Covers: config() call, payload parsing with fail-closed to 0.0 on missing/wrong-type
fields, PiholeError propagation, pihole-not-configured path, collector registration.
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
from homelab_monitor.plugins.collectors.integrations.pihole.config import (
    M_API_TOOK,
    M_QUERY_LOGGING,
    PiholeConfigCollector,
)

# ---- Fake clients ----


class _FakePiholeBase:
    """Base stub implementing PiholeClient Protocol (all methods raise NotImplementedError)."""

    async def info_version(self) -> PiholeResponse | PiholeError:
        raise NotImplementedError

    async def info_ftl(self) -> PiholeResponse | PiholeError:
        raise NotImplementedError

    async def info_database(self) -> PiholeResponse | PiholeError:
        raise NotImplementedError

    async def info_messages(self) -> PiholeResponse | PiholeError:
        raise NotImplementedError

    async def info_system(self) -> PiholeResponse | PiholeError:
        raise NotImplementedError

    async def stats_summary(self) -> PiholeResponse | PiholeError:
        raise NotImplementedError

    async def stats_upstreams(self) -> PiholeResponse | PiholeError:
        raise NotImplementedError

    async def stats_query_types(self) -> PiholeResponse | PiholeError:
        raise NotImplementedError

    async def stats_top_clients(self, **_: object) -> PiholeResponse | PiholeError:
        raise NotImplementedError

    async def stats_top_domains(self, **_: object) -> PiholeResponse | PiholeError:
        raise NotImplementedError

    async def stats_recent_blocked(self) -> PiholeResponse | PiholeError:
        raise NotImplementedError

    async def dns_blocking(self) -> PiholeResponse | PiholeError:
        raise NotImplementedError

    async def config(self) -> PiholeResponse | PiholeError:
        raise NotImplementedError

    async def lists(self) -> PiholeResponse | PiholeError:
        raise NotImplementedError

    async def network_devices(self) -> PiholeResponse | PiholeError:
        raise NotImplementedError

    async def queries(self, params: dict[str, str]) -> PiholeResponse | PiholeError:
        raise NotImplementedError

    async def aclose(self) -> None:
        pass


class _FakeConfigOk(_FakePiholeBase):
    def __init__(self, payload: object, took: float = 0.000123) -> None:
        self._payload = payload
        self._took = took

    async def config(self) -> PiholeResponse | PiholeError:
        return PiholeResponse(payload=self._payload, took_seconds=self._took, endpoint="config")


class _FakeConfigError(_FakePiholeBase):
    def __init__(self, message: str = "timeout") -> None:
        self._message = message

    async def config(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="timeout", message=self._message)


# ---- Helpers ----


def _ctx(writer: InMemoryMetricsWriter, pihole: _FakePiholeBase | None) -> CollectorContext:
    """Build a CollectorContext with the given metrics writer + pihole client."""
    return CollectorContext(
        config=CollectorConfig(
            name="pihole_config",
            interval_seconds=30,
            timeout_seconds=15,
        ),
        db=None,  # type: ignore[arg-type]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="pihole_config"),
        pihole=pihole,  # type: ignore[arg-type]
    )


def _gauge_value(writer: InMemoryMetricsWriter, name: str, labels: dict[str, str]) -> float | None:
    """Extract a gauge value from the writer's recorded metrics."""
    for entry in writer.recorded:
        if entry.kind == "gauge" and entry.name == name and entry.labels == labels:
            return entry.value
    return None


# ---- Tests ----


@pytest.mark.asyncio
async def test_query_logging_enabled_true() -> None:
    """config.dns.queryLogging=True -> gauge 1.0."""
    writer = InMemoryMetricsWriter()
    pihole = _FakeConfigOk({"config": {"dns": {"queryLogging": True}}, "took": 0.000123})
    collector = PiholeConfigCollector()
    result = await collector.run(_ctx(writer, pihole))
    assert result.ok is True
    assert result.metrics_emitted == 2  # noqa: PLR2004 -- 2 metrics (api_took + query_logging)
    assert _gauge_value(writer, M_QUERY_LOGGING, {}) == 1.0
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "config"}) == pytest.approx(0.000123)  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.asyncio
async def test_query_logging_enabled_false() -> None:
    """config.dns.queryLogging=False -> gauge 0.0."""
    writer = InMemoryMetricsWriter()
    pihole = _FakeConfigOk({"config": {"dns": {"queryLogging": False}}, "took": 0.0})
    collector = PiholeConfigCollector()
    result = await collector.run(_ctx(writer, pihole))
    assert result.ok is True
    assert _gauge_value(writer, M_QUERY_LOGGING, {}) == 0.0


@pytest.mark.asyncio
async def test_payload_not_dict_fail_closed() -> None:
    """payload is list (not dict) -> gauge 0.0."""
    writer = InMemoryMetricsWriter()
    pihole = _FakeConfigOk([1, 2])
    collector = PiholeConfigCollector()
    result = await collector.run(_ctx(writer, pihole))
    assert result.ok is True
    assert result.metrics_emitted == 2  # noqa: PLR2004 -- 2 metrics (api_took + query_logging)
    assert _gauge_value(writer, M_QUERY_LOGGING, {}) == 0.0


@pytest.mark.asyncio
async def test_config_key_missing_fail_closed() -> None:
    """payload missing 'config' key -> gauge 0.0."""
    writer = InMemoryMetricsWriter()
    pihole = _FakeConfigOk({"took": 0.0})
    collector = PiholeConfigCollector()
    result = await collector.run(_ctx(writer, pihole))
    assert result.ok is True
    assert _gauge_value(writer, M_QUERY_LOGGING, {}) == 0.0


@pytest.mark.asyncio
async def test_dns_key_missing_fail_closed() -> None:
    """config missing 'dns' key -> gauge 0.0."""
    writer = InMemoryMetricsWriter()
    pihole = _FakeConfigOk({"config": {}})
    collector = PiholeConfigCollector()
    result = await collector.run(_ctx(writer, pihole))
    assert result.ok is True
    assert _gauge_value(writer, M_QUERY_LOGGING, {}) == 0.0


@pytest.mark.asyncio
async def test_querylogging_non_bool_fail_closed() -> None:
    """queryLogging is string 'yes' (not bool) -> gauge 0.0."""
    writer = InMemoryMetricsWriter()
    pihole = _FakeConfigOk({"config": {"dns": {"queryLogging": "yes"}}})
    collector = PiholeConfigCollector()
    result = await collector.run(_ctx(writer, pihole))
    assert result.ok is True
    assert _gauge_value(writer, M_QUERY_LOGGING, {}) == 0.0


@pytest.mark.asyncio
async def test_config_returns_pihole_error() -> None:
    """config() returns PiholeError -> ok=False, no metrics."""
    writer = InMemoryMetricsWriter()
    pihole = _FakeConfigError("GET /api/config: timed out")
    collector = PiholeConfigCollector()
    result = await collector.run(_ctx(writer, pihole))
    assert result.ok is False
    assert result.errors == ["GET /api/config: timed out"]
    assert result.metrics_emitted == 0
    assert writer.recorded == []


@pytest.mark.asyncio
async def test_pihole_none_not_configured() -> None:
    """pihole client is None -> ok=False, no metrics."""
    writer = InMemoryMetricsWriter()
    collector = PiholeConfigCollector()
    result = await collector.run(_ctx(writer, None))
    assert result.ok is False
    assert result.errors == ["pihole client not configured"]
    assert result.metrics_emitted == 0


def test_registered_in_bundle() -> None:
    """pihole_config is registered via register_all + PluginLoader."""
    loader = MagicMock(spec=PluginLoader)
    register_all(loader)

    registered_classes = [call.args[0] for call in loader.register.call_args_list]
    assert PiholeConfigCollector in registered_classes


def test_metric_name_constants() -> None:
    """Metric name constants match expected values."""
    assert M_API_TOOK == "homelab_pihole_api_took_seconds"
    assert M_QUERY_LOGGING == "homelab_pihole_query_logging_enabled"
