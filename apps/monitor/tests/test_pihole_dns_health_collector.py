"""Unit tests for PiholeDnsHealthCollector (STAGE-006-014).

100% branch coverage: dns_host-unconfigured guard, ok (latency emitted),
servfail/nxdomain/refused/no_answer/truncated (response outcomes -> latency emitted),
timeout/socket_error/malformed/id_mismatch (no-response -> latency OMITTED),
up=1/0, error-list propagation, metric-name contract, registration.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import structlog

import homelab_monitor.plugins.collectors.integrations.pihole.dns_health as dns_health_mod
from homelab_monitor.kernel.dns import DnsProbeResult
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter, InMemoryMetricsWriter
from homelab_monitor.kernel.plugins.loader import PluginLoader
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.integrations.pihole import register_all
from homelab_monitor.plugins.collectors.integrations.pihole.dns_health import (
    M_DNS_PROBE_RESULT,
    M_DNS_PROBE_SECONDS,
    M_UP,
    PiholeDnsHealthCollector,
)


def _ctx(writer: InMemoryMetricsWriter) -> CollectorContext:
    return CollectorContext(
        config=CollectorConfig(
            name="pihole_dns_health",
            interval_seconds=30,
            timeout_seconds=15,
        ),
        db=None,  # type: ignore[arg-type]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="pihole_dns_health"),
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


def _patch_resolve(monkeypatch: pytest.MonkeyPatch, result: DnsProbeResult) -> None:
    async def fake_resolve(
        resolver_ip: str, qname: str, *, port: int = 53, timeout_seconds: float
    ) -> DnsProbeResult:
        return result

    monkeypatch.setattr(dns_health_mod, "resolve_a", fake_resolve)


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


@pytest.mark.asyncio
async def test_dns_host_unconfigured_guard() -> None:
    """Unconfigured dns_host (default ""); should guard and fail closed."""
    writer = InMemoryMetricsWriter()
    collector = PiholeDnsHealthCollector()
    ctx = _ctx(writer)

    result = await collector.run(ctx)

    assert result.ok is False
    assert result.errors == ["dns_host not configured"]
    assert result.metrics_emitted == 2  # noqa: PLR2004
    assert _gauge_value(writer, M_UP, {}) == pytest.approx(0.0)  # pyright: ignore[reportUnknownMemberType]
    assert (
        _gauge_value(writer, M_DNS_PROBE_RESULT, {"outcome": "socket_error"}) == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    )
    assert M_DNS_PROBE_SECONDS not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_ok_emits_up_latency_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ok=True -> up=1.0, latency emitted, outcome=ok."""
    _patch_resolve(monkeypatch, _dns(ok=True, error=None, latency=0.012))

    writer = InMemoryMetricsWriter()
    collector = PiholeDnsHealthCollector()
    collector._dns_host = "1.2.3.4"  # pyright: ignore[reportPrivateUsage]
    ctx = _ctx(writer)

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == []
    assert result.metrics_emitted == 3  # noqa: PLR2004
    assert _gauge_value(writer, M_UP, {}) == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    assert (
        _gauge_value(writer, M_DNS_PROBE_RESULT, {"outcome": "ok"}) == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    )
    latency_val = _gauge_value(writer, M_DNS_PROBE_SECONDS, {})
    assert latency_val == pytest.approx(0.012)  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.asyncio
async def test_servfail(monkeypatch: pytest.MonkeyPatch) -> None:
    """servfail -> up=0.0, outcome=servfail, latency emitted (response outcome)."""
    _patch_resolve(monkeypatch, _dns(ok=False, error="servfail", rcode=2))

    writer = InMemoryMetricsWriter()
    collector = PiholeDnsHealthCollector()
    collector._dns_host = "1.2.3.4"  # pyright: ignore[reportPrivateUsage]
    ctx = _ctx(writer)

    result = await collector.run(ctx)

    assert result.ok is False
    assert result.errors == ["servfail"]
    assert result.metrics_emitted == 3  # noqa: PLR2004
    assert _gauge_value(writer, M_UP, {}) == pytest.approx(0.0)  # pyright: ignore[reportUnknownMemberType]
    assert (
        _gauge_value(writer, M_DNS_PROBE_RESULT, {"outcome": "servfail"}) == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    )
    assert M_DNS_PROBE_SECONDS in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_nxdomain(monkeypatch: pytest.MonkeyPatch) -> None:
    """nxdomain -> up=0.0, outcome=nxdomain, latency emitted."""
    _patch_resolve(monkeypatch, _dns(ok=False, error="nxdomain", rcode=3))

    writer = InMemoryMetricsWriter()
    collector = PiholeDnsHealthCollector()
    collector._dns_host = "1.2.3.4"  # pyright: ignore[reportPrivateUsage]
    ctx = _ctx(writer)

    result = await collector.run(ctx)

    assert result.ok is False
    assert result.metrics_emitted == 3  # noqa: PLR2004
    assert (
        _gauge_value(writer, M_DNS_PROBE_RESULT, {"outcome": "nxdomain"}) == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    )
    assert M_DNS_PROBE_SECONDS in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """refused -> up=0.0, outcome=refused, latency emitted."""
    _patch_resolve(monkeypatch, _dns(ok=False, error="refused", rcode=5))

    writer = InMemoryMetricsWriter()
    collector = PiholeDnsHealthCollector()
    collector._dns_host = "1.2.3.4"  # pyright: ignore[reportPrivateUsage]
    ctx = _ctx(writer)

    result = await collector.run(ctx)

    assert result.ok is False
    assert result.metrics_emitted == 3  # noqa: PLR2004
    assert (
        _gauge_value(writer, M_DNS_PROBE_RESULT, {"outcome": "refused"}) == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    )
    assert M_DNS_PROBE_SECONDS in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_no_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """no_answer -> up=0.0, outcome=no_answer, latency emitted."""
    _patch_resolve(monkeypatch, _dns(ok=False, error="no_answer", rcode=0))

    writer = InMemoryMetricsWriter()
    collector = PiholeDnsHealthCollector()
    collector._dns_host = "1.2.3.4"  # pyright: ignore[reportPrivateUsage]
    ctx = _ctx(writer)

    result = await collector.run(ctx)

    assert result.ok is False
    assert result.metrics_emitted == 3  # noqa: PLR2004
    assert (
        _gauge_value(writer, M_DNS_PROBE_RESULT, {"outcome": "no_answer"}) == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    )
    assert M_DNS_PROBE_SECONDS in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """truncated -> up=0.0, outcome=truncated, latency emitted (response)."""
    _patch_resolve(monkeypatch, _dns(ok=False, error="truncated", truncated=True))

    writer = InMemoryMetricsWriter()
    collector = PiholeDnsHealthCollector()
    collector._dns_host = "1.2.3.4"  # pyright: ignore[reportPrivateUsage]
    ctx = _ctx(writer)

    result = await collector.run(ctx)

    assert result.ok is False
    assert result.metrics_emitted == 3  # noqa: PLR2004
    assert (
        _gauge_value(writer, M_DNS_PROBE_RESULT, {"outcome": "truncated"}) == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    )
    assert M_DNS_PROBE_SECONDS in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_timeout_omits_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    """timeout -> up=0.0, outcome=timeout, latency OMITTED (no-response)."""
    _patch_resolve(monkeypatch, _dns(ok=False, error="timeout"))

    writer = InMemoryMetricsWriter()
    collector = PiholeDnsHealthCollector()
    collector._dns_host = "1.2.3.4"  # pyright: ignore[reportPrivateUsage]
    ctx = _ctx(writer)

    result = await collector.run(ctx)

    assert result.ok is False
    assert result.errors == ["timeout"]
    assert result.metrics_emitted == 2  # noqa: PLR2004
    assert (
        _gauge_value(writer, M_DNS_PROBE_RESULT, {"outcome": "timeout"}) == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    )
    assert M_DNS_PROBE_SECONDS not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_socket_error_omits_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    """socket_error -> up=0.0, outcome=socket_error, latency OMITTED."""
    _patch_resolve(monkeypatch, _dns(ok=False, error="socket_error", rcode=-1))

    writer = InMemoryMetricsWriter()
    collector = PiholeDnsHealthCollector()
    collector._dns_host = "1.2.3.4"  # pyright: ignore[reportPrivateUsage]
    ctx = _ctx(writer)

    result = await collector.run(ctx)

    assert result.ok is False
    assert result.metrics_emitted == 2  # noqa: PLR2004
    assert (
        _gauge_value(writer, M_DNS_PROBE_RESULT, {"outcome": "socket_error"}) == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    )
    assert M_DNS_PROBE_SECONDS not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_malformed_omits_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    """malformed -> up=0.0, outcome=malformed, latency OMITTED."""
    _patch_resolve(monkeypatch, _dns(ok=False, error="malformed", rcode=-1))

    writer = InMemoryMetricsWriter()
    collector = PiholeDnsHealthCollector()
    collector._dns_host = "1.2.3.4"  # pyright: ignore[reportPrivateUsage]
    ctx = _ctx(writer)

    result = await collector.run(ctx)

    assert result.ok is False
    assert result.metrics_emitted == 2  # noqa: PLR2004
    assert (
        _gauge_value(writer, M_DNS_PROBE_RESULT, {"outcome": "malformed"}) == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    )
    assert M_DNS_PROBE_SECONDS not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_id_mismatch_omits_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    """id_mismatch -> up=0.0, outcome=id_mismatch, latency OMITTED."""
    _patch_resolve(monkeypatch, _dns(ok=False, error="id_mismatch", rcode=-1))

    writer = InMemoryMetricsWriter()
    collector = PiholeDnsHealthCollector()
    collector._dns_host = "1.2.3.4"  # pyright: ignore[reportPrivateUsage]
    ctx = _ctx(writer)

    result = await collector.run(ctx)

    assert result.ok is False
    assert result.metrics_emitted == 2  # noqa: PLR2004
    assert (
        _gauge_value(writer, M_DNS_PROBE_RESULT, {"outcome": "id_mismatch"}) == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    )
    assert M_DNS_PROBE_SECONDS not in _all_metric_names(writer)


def test_metric_name_constants_match_contract() -> None:
    """Metric-name constants match the contract."""
    assert M_UP == "homelab_pihole_up"
    assert M_DNS_PROBE_SECONDS == "homelab_pihole_dns_probe_seconds"
    assert M_DNS_PROBE_RESULT == "homelab_pihole_dns_probe_result"


def test_collector_registered_via_register_all() -> None:
    """PiholeDnsHealthCollector is registered via register_all."""
    loader = MagicMock(spec=PluginLoader)
    register_all(loader)

    registered_classes = [call.args[0] for call in loader.register.call_args_list]
    assert PiholeDnsHealthCollector in registered_classes
