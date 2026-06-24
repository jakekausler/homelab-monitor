"""Unit tests for PiholeDnsSplitCollector (STAGE-006-015).

100% branch coverage: empty-pihole-host guard (probes direct only), both-ok,
pihole-fail/direct-ok, both-fail, response outcomes (latency emitted) vs no-response
outcomes (latency OMITTED) per path, up=1/0, error-list propagation (order + dedup),
metric-name + path-constant contract, registration.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import structlog

import homelab_monitor.plugins.collectors.integrations.pihole.dns_split as dns_split_mod
from homelab_monitor.kernel.dns import DnsProbeResult
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter, InMemoryMetricsWriter
from homelab_monitor.kernel.plugins.loader import PluginLoader
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.integrations.pihole import register_all
from homelab_monitor.plugins.collectors.integrations.pihole.dns_split import (
    M_PROBE_RESULT,
    M_SECONDS,
    M_UP,
    PATH_DIRECT,
    PATH_PIHOLE,
    PiholeDnsSplitCollector,
)

_PIHOLE_HOST = "192.168.2.149"
_DIRECT_HOST = "1.1.1.1"


def _ctx(writer: InMemoryMetricsWriter) -> CollectorContext:
    return CollectorContext(
        config=CollectorConfig(
            name="pihole_dns_split",
            interval_seconds=60,
            timeout_seconds=15,
        ),
        db=None,  # type: ignore[arg-type]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="pihole_dns_split"),
    )


def _dns(
    *,
    ok: bool,
    error: str | None,
    latency: float = 0.012,
    rcode: int = 0,
    truncated: bool = False,
) -> DnsProbeResult:
    return DnsProbeResult(
        ok=ok,
        rcode=rcode,
        truncated=truncated,
        latency_seconds=latency,
        error=error,
    )


def _patch_resolve_by_host(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pihole: DnsProbeResult,
    direct: DnsProbeResult,
) -> None:
    """Patch resolve_a to return per-path results keyed on resolver_ip."""

    async def fake_resolve(
        resolver_ip: str, qname: str, *, port: int = 53, timeout_seconds: float
    ) -> DnsProbeResult:
        return direct if resolver_ip == _DIRECT_HOST else pihole

    monkeypatch.setattr(dns_split_mod, "resolve_a", fake_resolve)


def _make_collector() -> PiholeDnsSplitCollector:
    collector = PiholeDnsSplitCollector()
    collector._pihole_host = _PIHOLE_HOST  # pyright: ignore[reportPrivateUsage]
    collector._direct_host = _DIRECT_HOST  # pyright: ignore[reportPrivateUsage]
    return collector


def _gauge_value(
    writer: InMemoryMetricsWriter,
    name: str,
    labels: dict[str, str] | None = None,
) -> float:
    labels = labels or {}
    for e in writer.recorded:  # pyright: ignore[reportPrivateUsage]
        if e.kind == "gauge" and e.name == name and e.labels == labels:
            return e.value
    raise AssertionError(f"gauge {name}{labels} not recorded")


@pytest.mark.asyncio
async def test_both_ok_emits_up_latency_outcome_per_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both paths ok -> up=1.0, latency emitted, outcome=ok for each path."""
    _patch_resolve_by_host(
        monkeypatch,
        pihole=_dns(ok=True, error=None, latency=0.010),
        direct=_dns(ok=True, error=None, latency=0.020),
    )
    writer = InMemoryMetricsWriter()
    collector = _make_collector()

    result = await collector.run(_ctx(writer))

    assert result.ok is True
    assert result.errors == []
    assert result.metrics_emitted == 6  # noqa: PLR2004
    assert (
        _gauge_value(writer, M_UP, {"path": PATH_PIHOLE}) == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    )
    assert (
        _gauge_value(writer, M_UP, {"path": PATH_DIRECT}) == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    )
    assert (
        _gauge_value(writer, M_PROBE_RESULT, {"path": PATH_PIHOLE, "outcome": "ok"})
        == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    )
    assert (
        _gauge_value(writer, M_PROBE_RESULT, {"path": PATH_DIRECT, "outcome": "ok"})
        == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    )
    assert (
        _gauge_value(writer, M_SECONDS, {"path": PATH_PIHOLE}) == pytest.approx(0.010)  # pyright: ignore[reportUnknownMemberType]
    )
    assert (
        _gauge_value(writer, M_SECONDS, {"path": PATH_DIRECT}) == pytest.approx(0.020)  # pyright: ignore[reportUnknownMemberType]
    )


@pytest.mark.asyncio
async def test_pihole_fail_direct_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pi-hole timeout (no latency) + direct ok (latency) -> result.ok False."""
    _patch_resolve_by_host(
        monkeypatch,
        pihole=_dns(ok=False, error="timeout"),
        direct=_dns(ok=True, error=None, latency=0.020),
    )
    writer = InMemoryMetricsWriter()
    collector = _make_collector()

    result = await collector.run(_ctx(writer))

    assert result.ok is False
    assert result.errors == ["timeout"]
    # pihole: up + outcome (no latency) = 2; direct: up + outcome + latency = 3
    assert result.metrics_emitted == 5  # noqa: PLR2004
    assert (
        _gauge_value(writer, M_UP, {"path": PATH_PIHOLE}) == pytest.approx(0.0)  # pyright: ignore[reportUnknownMemberType]
    )
    assert (
        _gauge_value(writer, M_UP, {"path": PATH_DIRECT}) == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    )
    assert (
        _gauge_value(writer, M_PROBE_RESULT, {"path": PATH_PIHOLE, "outcome": "timeout"})
        == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    )
    # latency emitted for direct only
    assert (
        _gauge_value(writer, M_SECONDS, {"path": PATH_DIRECT}) == pytest.approx(0.020)  # pyright: ignore[reportUnknownMemberType]
    )
    with pytest.raises(AssertionError):
        _gauge_value(writer, M_SECONDS, {"path": PATH_PIHOLE})


@pytest.mark.asyncio
async def test_both_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both paths fail (servfail = response outcome, socket_error = no-response)."""
    _patch_resolve_by_host(
        monkeypatch,
        pihole=_dns(ok=False, error="servfail", latency=0.005, rcode=2),
        direct=_dns(ok=False, error="socket_error"),
    )
    writer = InMemoryMetricsWriter()
    collector = _make_collector()

    result = await collector.run(_ctx(writer))

    assert result.ok is False
    assert result.errors == ["servfail", "socket_error"]
    # pihole servfail: up+outcome+latency=3 (response outcome); direct socket_error:
    # up+outcome=2 (no-response) => 5
    assert result.metrics_emitted == 5  # noqa: PLR2004
    assert (
        _gauge_value(writer, M_PROBE_RESULT, {"path": PATH_PIHOLE, "outcome": "servfail"})
        == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    )
    assert (
        _gauge_value(writer, M_PROBE_RESULT, {"path": PATH_DIRECT, "outcome": "socket_error"})
        == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    )
    assert (
        _gauge_value(writer, M_SECONDS, {"path": PATH_PIHOLE}) == pytest.approx(0.005)  # pyright: ignore[reportUnknownMemberType]
    )
    with pytest.raises(AssertionError):
        _gauge_value(writer, M_SECONDS, {"path": PATH_DIRECT})


@pytest.mark.asyncio
async def test_empty_pihole_host_guard_probes_direct_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty _pihole_host fails pihole closed WITHOUT calling resolve_a on it.

    The direct path is still probed. resolve_a must NEVER be invoked with an empty
    resolver_ip.
    """
    calls: list[str] = []

    async def fake_resolve(
        resolver_ip: str, qname: str, *, port: int = 53, timeout_seconds: float
    ) -> DnsProbeResult:
        calls.append(resolver_ip)
        return _dns(ok=True, error=None, latency=0.020)

    monkeypatch.setattr(dns_split_mod, "resolve_a", fake_resolve)

    writer = InMemoryMetricsWriter()
    collector = PiholeDnsSplitCollector()
    # _pihole_host left as "" (default); _direct_host default "1.1.1.1".

    result = await collector.run(_ctx(writer))

    assert calls == ["1.1.1.1"]  # resolve_a only called for the direct path
    assert result.ok is False
    assert result.errors == ["socket_error"]
    # pihole: up+outcome=2 (guard, no latency); direct ok: up+outcome+latency=3 => 5
    assert result.metrics_emitted == 5  # noqa: PLR2004
    assert (
        _gauge_value(writer, M_UP, {"path": PATH_PIHOLE}) == pytest.approx(0.0)  # pyright: ignore[reportUnknownMemberType]
    )
    assert (
        _gauge_value(writer, M_PROBE_RESULT, {"path": PATH_PIHOLE, "outcome": "socket_error"})
        == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    )
    assert (
        _gauge_value(writer, M_UP, {"path": PATH_DIRECT}) == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    )
    with pytest.raises(AssertionError):
        _gauge_value(writer, M_SECONDS, {"path": PATH_PIHOLE})


def test_metric_name_constants_match_contract() -> None:
    """Literal contract test: metric names + path constants are stable."""
    assert M_UP == "homelab_dns_resolution_up"
    assert M_SECONDS == "homelab_dns_resolution_seconds"
    assert M_PROBE_RESULT == "homelab_dns_resolution_probe_result"
    assert PATH_PIHOLE == "pihole"
    assert PATH_DIRECT == "direct"


def test_collector_registered() -> None:
    """register_all registers PiholeDnsSplitCollector in the pihole bundle."""
    loader = MagicMock(spec=PluginLoader)
    register_all(loader)

    registered_classes = [call.args[0] for call in loader.register.call_args_list]
    assert PiholeDnsSplitCollector in registered_classes
