"""Tests for HaUpCollector — probe -> homelab_ha_up gauge (1.0/0.0)."""

from __future__ import annotations

import structlog

from homelab_monitor.kernel.ha.client import (
    HaConfigResult,
    HaErrorLogResult,
    HaServiceResult,
    HaState,
)
from homelab_monitor.kernel.ha.errors import HaError
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    HomeAssistantClient,
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_up import (
    HaUpCollector,
)


class _FakeHaUp:
    """HA client double whose get_config() always succeeds (HA up)."""

    async def get_config(self) -> HaConfigResult | HaError:
        return HaConfigResult(version="2026.6.0", time_zone="America/New_York")

    async def get_states(self) -> list[HaState] | HaError:
        return []

    async def get_error_log(self) -> HaErrorLogResult | HaError:
        return HaErrorLogResult(text="")

    async def call_service(
        self, domain: str, service: str, data: dict[str, object] | None = None
    ) -> HaServiceResult | HaError:
        return HaServiceResult(changed_states=[])


class _FakeHaDown:
    """HA client double whose get_config() returns an HaError (HA down)."""

    async def get_config(self) -> HaConfigResult | HaError:
        return HaError(reason="unreachable", message="GET /api/config: connection failed")

    async def get_states(self) -> list[HaState] | HaError:
        return HaError(reason="unreachable", message="down")

    async def get_error_log(self) -> HaErrorLogResult | HaError:
        return HaError(reason="unreachable", message="down")

    async def call_service(
        self, domain: str, service: str, data: dict[str, object] | None = None
    ) -> HaServiceResult | HaError:
        return HaError(reason="unreachable", message="down")


def _ctx(writer: InMemoryMetricsWriter, ha: HomeAssistantClient | None) -> CollectorContext:
    """Minimal CollectorContext — only ha + vm are real (the only fields run() reads)."""
    return CollectorContext(
        config=CollectorConfig(name="ha_up", interval_seconds=30, timeout_seconds=10),
        db=None,  # pyright: ignore[reportArgumentType]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="ha_up"),  # pyright: ignore[reportArgumentType]
        ha=ha,
    )


def test_ha_up_classvars() -> None:
    """ClassVars match the locked cadence + concurrency group."""
    assert HaUpCollector.name == "ha_up"
    assert HaUpCollector.interval.total_seconds() == 30.0  # noqa: PLR2004
    assert HaUpCollector.timeout.total_seconds() == 10.0  # noqa: PLR2004
    assert HaUpCollector.concurrency_group == "homeassistant"


async def test_ha_up_emits_one_when_reachable() -> None:
    """get_config() success -> homelab_ha_up == 1.0, ok=True, metrics_emitted=1."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaUp())
    result = await HaUpCollector().run(ctx)
    assert result.ok is True
    assert result.metrics_emitted == 1
    gauges = [e for e in writer.recorded if e.name == "homelab_ha_up"]
    assert len(gauges) == 1
    assert gauges[0].value == 1.0
    assert gauges[0].labels == {}


async def test_ha_up_emits_zero_when_haerror() -> None:
    """get_config() -> HaError -> homelab_ha_up == 0.0, but run still ok=True."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaDown())
    result = await HaUpCollector().run(ctx)
    assert result.ok is True
    assert result.metrics_emitted == 1
    gauges = [e for e in writer.recorded if e.name == "homelab_ha_up"]
    assert len(gauges) == 1
    assert gauges[0].value == 0.0


async def test_ha_up_emits_zero_when_ha_none() -> None:
    """ctx.ha is None -> homelab_ha_up == 0.0 (defensive), run ok=True."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, None)
    result = await HaUpCollector().run(ctx)
    assert result.ok is True
    assert result.metrics_emitted == 1
    gauges = [e for e in writer.recorded if e.name == "homelab_ha_up"]
    assert len(gauges) == 1
    assert gauges[0].value == 0.0
