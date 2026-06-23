"""Unit tests for PiholeClientsCollector (STAGE-006-012). 100% branch."""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from homelab_monitor.kernel.metrics.cardinality import M_FAMILY_DROPPED_SERIES
from homelab_monitor.kernel.pihole.client import PiholeResponse
from homelab_monitor.kernel.pihole.errors import PiholeError
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter, InMemoryMetricsWriter
from homelab_monitor.kernel.plugins.loader import PluginLoader
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.integrations.pihole import register_all
from homelab_monitor.plugins.collectors.integrations.pihole.top_clients import (
    M_API_TOOK,
    M_CLIENT_BLOCKED,
    M_CLIENT_QUERIES,
    M_TOP_BLOCKED_DOMAIN,
    M_TOP_PERMITTED_DOMAIN,
    PiholeClientsCollector,
)

# Sentinel meaning "make this endpoint raise a PiholeError instead of OK".
_ERR = object()


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

    async def stats_top_clients(
        self, *, blocked: bool = False, count: int | None = None
    ) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def stats_top_domains(
        self, *, blocked: bool = False, count: int | None = None
    ) -> PiholeResponse | PiholeError:
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


class _FakeClients(_FakePiholeBase):
    """Configurable fake: each of the 5 endpoints returns OK payload or PiholeError.

    Pass a payload object to return OK; pass the ``_ERR`` sentinel to return a
    PiholeError. The blocked variants are selected by the ``blocked`` kwarg.
    """

    def __init__(  # noqa: PLR0913
        self,
        *,
        clients_payload: object = _ERR,
        devices_payload: object = _ERR,
        blocked_clients_payload: object = _ERR,
        domains_payload: object = _ERR,
        blocked_domains_payload: object = _ERR,
        took: float = 0.001,
    ) -> None:
        self._clients = clients_payload
        self._devices = devices_payload
        self._blocked_clients = blocked_clients_payload
        self._domains = domains_payload
        self._blocked_domains = blocked_domains_payload
        self._took = took

    async def stats_top_clients(
        self, *, blocked: bool = False, count: int | None = None
    ) -> PiholeResponse | PiholeError:
        if blocked:
            if self._blocked_clients is _ERR:
                return PiholeError(reason="timeout", message="blocked clients failed")
            return PiholeResponse(
                payload=self._blocked_clients,
                took_seconds=self._took,
                endpoint="stats/top_clients_blocked",
            )
        if self._clients is _ERR:
            return PiholeError(reason="timeout", message="clients failed")
        return PiholeResponse(
            payload=self._clients, took_seconds=self._took, endpoint="stats/top_clients"
        )

    async def network_devices(self) -> PiholeResponse | PiholeError:
        if self._devices is _ERR:
            return PiholeError(reason="timeout", message="devices failed")
        return PiholeResponse(
            payload=self._devices, took_seconds=self._took, endpoint="network/devices"
        )

    async def stats_top_domains(
        self, *, blocked: bool = False, count: int | None = None
    ) -> PiholeResponse | PiholeError:
        if blocked:
            if self._blocked_domains is _ERR:
                return PiholeError(reason="timeout", message="blocked domains failed")
            return PiholeResponse(
                payload=self._blocked_domains,
                took_seconds=self._took,
                endpoint="stats/top_domains_blocked",
            )
        if self._domains is _ERR:
            return PiholeError(reason="timeout", message="domains failed")
        return PiholeResponse(
            payload=self._domains, took_seconds=self._took, endpoint="stats/top_domains"
        )


def _ctx(writer: InMemoryMetricsWriter, pihole: object | None) -> CollectorContext:
    """Build a CollectorContext wired to the given writer and pihole client."""
    return CollectorContext(
        config=CollectorConfig(
            name="pihole_clients",
            interval_seconds=60,
            timeout_seconds=15,
        ),
        db=None,  # type: ignore[arg-type]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="pihole_clients"),
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


def _count(writer: InMemoryMetricsWriter, name: str) -> int:
    return sum(1 for e in writer.recorded if e.name == name)  # pyright: ignore[reportPrivateUsage]


def _drop_value(writer: InMemoryMetricsWriter, family: str) -> float | None:
    for e in writer.recorded:  # pyright: ignore[reportPrivateUsage]
        if (
            e.kind == "gauge"
            and e.name == M_FAMILY_DROPPED_SERIES
            and e.labels == {"family": family}
        ):
            return e.value
    return None


def _entries_for(writer: InMemoryMetricsWriter, name: str) -> list[dict[str, str]]:
    return [
        e.labels  # pyright: ignore[reportPrivateUsage]
        for e in writer.recorded  # pyright: ignore[reportPrivateUsage]
        if e.name == name
    ]


def test_metric_name_constants_match_contract() -> None:
    """Public metric name constants match the contract."""
    assert M_API_TOOK == "homelab_pihole_api_took_seconds"
    assert M_CLIENT_QUERIES == "homelab_pihole_client_queries"
    assert M_CLIENT_BLOCKED == "homelab_pihole_client_blocked"
    assert M_TOP_BLOCKED_DOMAIN == "homelab_pihole_top_blocked_domain"
    assert M_TOP_PERMITTED_DOMAIN == "homelab_pihole_top_permitted_domain"
    assert M_FAMILY_DROPPED_SERIES == "homelab_metric_family_dropped_series"


@pytest.mark.asyncio
async def test_ctx_pihole_none() -> None:
    """ctx.pihole is None -> ok=False, error message, 0 metrics."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, None)
    result = await PiholeClientsCollector().run(ctx)
    assert result.ok is False
    assert result.errors == ["pihole client not configured"]
    assert result.metrics_emitted == 0
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_happy_path_all_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """All 5 endpoints OK with attributed loopback + LAN clients + domains."""
    monkeypatch.setenv("HOMELAB_MONITOR_PIHOLE_HOST_LAN_IP", "192.168.2.148")

    clients_payload = {
        "clients": [
            {"name": "pi.hole", "ip": "127.0.0.1", "count": 100},
            {"name": "", "ip": "192.168.2.10", "count": 50},
            {"name": "", "ip": "192.168.2.11", "count": 25},
        ],
        "total_queries": 175,
        "blocked_queries": 10,
        "took": 0.001,
    }
    devices_payload: dict[str, object] = {
        "devices": [
            {
                "id": 1,
                "hwaddr": "aa:bb:cc:dd:ee:ff",
                "macVendor": "Acme",
                "ips": [{"ip": "192.168.2.10", "name": None}],
            }
        ],
        "took": 0.001,
    }
    blocked_clients_payload = {
        "clients": [
            {"name": "", "ip": "192.168.2.10", "count": 7},
            {"name": "pi.hole", "ip": "127.0.0.1", "count": 3},
        ],
        "total_queries": 175,
        "blocked_queries": 10,
        "took": 0.001,
    }
    domains_payload = {
        "domains": [
            {"domain": "ads.example", "count": 40},
            {"domain": "cdn.example", "count": 10},
        ],
        "total_queries": 175,
        "blocked_queries": 10,
        "took": 0.001,
    }
    blocked_domains_payload = {
        "domains": [{"domain": "tracker.example", "count": 20}],
        "total_queries": 175,
        "blocked_queries": 10,
        "took": 0.001,
    }
    fake = _FakeClients(
        clients_payload=clients_payload,
        devices_payload=devices_payload,
        blocked_clients_payload=blocked_clients_payload,
        domains_payload=domains_payload,
        blocked_domains_payload=blocked_domains_payload,
    )
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))

    assert result.ok is True
    assert result.errors == []
    assert result.events == []

    # api_took for all 5 endpoints.
    for endpoint in (
        "stats/top_clients",
        "network/devices",
        "stats/top_clients_blocked",
        "stats/top_domains",
        "stats/top_domains_blocked",
    ):
        assert _gauge_value(writer, M_API_TOOK, {"endpoint": endpoint}) is not None

    # Loopback client (resolver_self): host_lan_ip label present, no client_mac.
    loopback_labels = {
        "client_ip": "127.0.0.1",
        "client_name": "pi.hole",
        "client_kind": "resolver_self",
        "host_lan_ip": "192.168.2.148",
        "client_mac": "",
    }
    assert _gauge_value(writer, M_CLIENT_QUERIES, loopback_labels) == 100.0  # noqa: PLR2004
    assert _gauge_value(writer, M_CLIENT_BLOCKED, loopback_labels) == 3.0  # noqa: PLR2004

    # LAN client WITH a MAC: client_mac label present, NO host_lan_ip label.
    lan_mac_labels = {
        "client_ip": "192.168.2.10",
        "client_name": "",
        "client_kind": "lan",
        "client_mac": "aa:bb:cc:dd:ee:ff",
        "host_lan_ip": "",
    }
    assert _gauge_value(writer, M_CLIENT_QUERIES, lan_mac_labels) == 50.0  # noqa: PLR2004
    assert _gauge_value(writer, M_CLIENT_BLOCKED, lan_mac_labels) == 7.0  # noqa: PLR2004

    # LAN client WITHOUT a MAC: no client_mac label, no host_lan_ip label.
    lan_nomac_labels = {
        "client_ip": "192.168.2.11",
        "client_name": "",
        "client_kind": "lan",
        "host_lan_ip": "",
        "client_mac": "",
    }
    assert _gauge_value(writer, M_CLIENT_QUERIES, lan_nomac_labels) == 25.0  # noqa: PLR2004
    assert _gauge_value(writer, M_CLIENT_BLOCKED, lan_nomac_labels) == 0.0

    # Domains.
    assert _gauge_value(writer, M_TOP_PERMITTED_DOMAIN, {"domain": "ads.example"}) == 40.0  # noqa: PLR2004
    assert _gauge_value(writer, M_TOP_PERMITTED_DOMAIN, {"domain": "cdn.example"}) == 10.0  # noqa: PLR2004
    assert _gauge_value(writer, M_TOP_BLOCKED_DOMAIN, {"domain": "tracker.example"}) == 20.0  # noqa: PLR2004

    # All 4 drop gauges present, value 0 (nothing dropped under default cap 50).
    assert _drop_value(writer, M_CLIENT_QUERIES) == 0.0
    assert _drop_value(writer, M_CLIENT_BLOCKED) == 0.0
    assert _drop_value(writer, M_TOP_PERMITTED_DOMAIN) == 0.0
    assert _drop_value(writer, M_TOP_BLOCKED_DOMAIN) == 0.0

    # Exact emit count:
    #   5 api_took
    # + 3 clients * 2 (queries+blocked) = 6
    # + 2 client drop gauges
    # + 2 permitted domains + 1 permitted drop = 3
    # + 1 blocked domain + 1 blocked drop = 2
    # = 18
    assert result.metrics_emitted == 18  # noqa: PLR2004

    # All emitted homelab_pihole_client_queries and homelab_pihole_client_blocked
    # series must have exactly the 5-key label set.
    EXPECTED_CLIENT_LABEL_KEYS = {
        "client_ip",
        "client_name",
        "client_kind",
        "host_lan_ip",
        "client_mac",
    }
    for metric_name in (M_CLIENT_QUERIES, M_CLIENT_BLOCKED):
        series_labels = _entries_for(writer, metric_name)
        assert series_labels, f"expected at least one {metric_name} series"
        for labels in series_labels:
            assert set(labels.keys()) == EXPECTED_CLIENT_LABEL_KEYS


@pytest.mark.asyncio
async def test_loopback_unattributed_when_host_lan_ip_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty host_lan_ip -> loopback client is 'unattributed', no host_lan_ip label."""
    monkeypatch.setenv("HOMELAB_MONITOR_PIHOLE_HOST_LAN_IP", "")
    clients_payload = {
        "clients": [{"name": "pi.hole", "ip": "127.0.0.1", "count": 9}],
        "took": 0.001,
    }
    fake = _FakeClients(clients_payload=clients_payload)
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))

    assert result.ok is True
    labels = {
        "client_ip": "127.0.0.1",
        "client_name": "pi.hole",
        "client_kind": "unattributed",
        "host_lan_ip": "",
        "client_mac": "",
    }
    assert _gauge_value(writer, M_CLIENT_QUERIES, labels) == 9.0  # noqa: PLR2004
    # host_lan_ip label present with value "".
    for label_set in _entries_for(writer, M_CLIENT_QUERIES):
        assert label_set["host_lan_ip"] == ""


@pytest.mark.asyncio
async def test_client_cap_drops(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """cap=2, feed 3 LAN clients -> 2 survivors, drop gauge 1.0 for both families."""
    cfg = tmp_path / "homelab-monitor.yaml"
    cfg.write_text(
        "cardinality_caps:\n  families:\n    pihole_client_queries: 2\n    pihole_top_domains: 2\n"
    )
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg))

    clients_payload = {
        "clients": [
            {"name": "", "ip": "192.168.2.10", "count": 5},
            {"name": "", "ip": "192.168.2.11", "count": 4},
            {"name": "", "ip": "192.168.2.12", "count": 3},
        ],
        "took": 0.001,
    }
    fake = _FakeClients(clients_payload=clients_payload)
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))

    assert result.ok is True
    assert _count(writer, M_CLIENT_QUERIES) == 2  # noqa: PLR2004
    assert _drop_value(writer, M_CLIENT_QUERIES) == 1.0
    assert _drop_value(writer, M_CLIENT_BLOCKED) == 1.0


@pytest.mark.asyncio
async def test_domain_cap_drops(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """cap=2, feed 3 permitted domains -> 2 survivors, drop gauge 1.0."""
    cfg = tmp_path / "homelab-monitor.yaml"
    cfg.write_text("cardinality_caps:\n  families:\n    pihole_top_domains: 2\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg))
    domains_payload = {
        "domains": [
            {"domain": "a.example", "count": 9},
            {"domain": "b.example", "count": 8},
            {"domain": "c.example", "count": 7},
        ],
        "took": 0.001,
    }
    fake = _FakeClients(domains_payload=domains_payload)
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert _count(writer, M_TOP_PERMITTED_DOMAIN) == 2  # noqa: PLR2004
    assert _drop_value(writer, M_TOP_PERMITTED_DOMAIN) == 1.0


@pytest.mark.asyncio
async def test_top_clients_error_others_ok() -> None:
    """top_clients errors -> clients block skipped, no client drop gauges, ok=True."""
    fake = _FakeClients(
        clients_payload=_ERR,
        domains_payload={"domains": [{"domain": "x.example", "count": 1}], "took": 0.001},
    )
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert "clients failed" in result.errors
    # Client block skipped entirely: no client metrics, NO client drop gauges.
    assert _count(writer, M_CLIENT_QUERIES) == 0
    assert _drop_value(writer, M_CLIENT_QUERIES) is None
    assert _drop_value(writer, M_CLIENT_BLOCKED) is None


@pytest.mark.asyncio
async def test_devices_error_no_mac_labels() -> None:
    """Only network/devices errors -> ok=True, client_mac labels absent."""
    clients_payload = {
        "clients": [{"name": "", "ip": "192.168.2.10", "count": 5}],
        "took": 0.001,
    }
    fake = _FakeClients(clients_payload=clients_payload, devices_payload=_ERR)
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert "devices failed" in result.errors
    for label_set in _entries_for(writer, M_CLIENT_QUERIES):
        assert label_set["client_mac"] == ""


@pytest.mark.asyncio
async def test_blocked_clients_error() -> None:
    """top_clients?blocked errors -> blocked_by_ip empty, client_blocked values 0."""
    clients_payload = {
        "clients": [{"name": "", "ip": "192.168.2.10", "count": 5}],
        "took": 0.001,
    }
    fake = _FakeClients(clients_payload=clients_payload, blocked_clients_payload=_ERR)
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert "blocked clients failed" in result.errors
    labels = {
        "client_ip": "192.168.2.10",
        "client_name": "",
        "client_kind": "lan",
        "host_lan_ip": "",
        "client_mac": "",
    }
    assert _gauge_value(writer, M_CLIENT_BLOCKED, labels) == 0.0


@pytest.mark.asyncio
async def test_domains_error() -> None:
    """top_domains errors -> no permitted-domain metrics/drop gauge, ok still True."""
    fake = _FakeClients(
        domains_payload=_ERR,
        blocked_domains_payload={
            "domains": [{"domain": "t.example", "count": 2}],
            "took": 0.001,
        },
    )
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert "domains failed" in result.errors
    assert _count(writer, M_TOP_PERMITTED_DOMAIN) == 0
    assert _drop_value(writer, M_TOP_PERMITTED_DOMAIN) is None
    assert _drop_value(writer, M_TOP_BLOCKED_DOMAIN) == 0.0


@pytest.mark.asyncio
async def test_blocked_domains_error() -> None:
    """top_domains?blocked errors -> no blocked-domain metrics/drop, ok still True."""
    fake = _FakeClients(
        domains_payload={"domains": [{"domain": "a.example", "count": 1}], "took": 0.001},
        blocked_domains_payload=_ERR,
    )
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert "blocked domains failed" in result.errors
    assert _count(writer, M_TOP_BLOCKED_DOMAIN) == 0
    assert _drop_value(writer, M_TOP_BLOCKED_DOMAIN) is None


@pytest.mark.asyncio
async def test_all_endpoints_error() -> None:
    """All 5 endpoints error -> ok=False, all errors appended, 0 metrics."""
    fake = _FakeClients()  # all default _ERR
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]
    assert "clients failed" in result.errors
    assert "devices failed" in result.errors
    assert "blocked clients failed" in result.errors
    assert "domains failed" in result.errors
    assert "blocked domains failed" in result.errors


@pytest.mark.asyncio
async def test_clients_payload_not_dict() -> None:
    """top_clients non-dict payload -> clients_ok True but empty -> drop gauges 0."""
    fake = _FakeClients(clients_payload=[1, 2, 3])
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "stats/top_clients"}) is not None
    assert _count(writer, M_CLIENT_QUERIES) == 0
    # clients_ok True with empty parse -> both drop gauges emit 0.
    assert _drop_value(writer, M_CLIENT_QUERIES) == 0.0
    assert _drop_value(writer, M_CLIENT_BLOCKED) == 0.0


@pytest.mark.asyncio
async def test_devices_payload_not_dict() -> None:
    """network/devices non-dict payload -> ip_mac empty, no crash, no mac labels."""
    clients_payload = {
        "clients": [{"name": "", "ip": "192.168.2.10", "count": 5}],
        "took": 0.001,
    }
    fake = _FakeClients(clients_payload=clients_payload, devices_payload="nope")
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "network/devices"}) is not None
    for label_set in _entries_for(writer, M_CLIENT_QUERIES):
        assert label_set["client_mac"] == ""


@pytest.mark.asyncio
async def test_blocked_clients_payload_not_dict() -> None:
    """top_clients?blocked non-dict payload -> blocked_by_ip empty, blocked values 0."""
    clients_payload = {
        "clients": [{"name": "", "ip": "192.168.2.10", "count": 5}],
        "took": 0.001,
    }
    fake = _FakeClients(clients_payload=clients_payload, blocked_clients_payload=42)
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is True
    labels = {
        "client_ip": "192.168.2.10",
        "client_name": "",
        "client_kind": "lan",
        "host_lan_ip": "",
        "client_mac": "",
    }
    assert _gauge_value(writer, M_CLIENT_BLOCKED, labels) == 0.0


@pytest.mark.asyncio
async def test_domains_payload_not_dict() -> None:
    """top_domains non-dict payload -> domains_ok True, no permitted series/drop."""
    fake = _FakeClients(domains_payload=3.14)
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "stats/top_domains"}) is not None
    assert _count(writer, M_TOP_PERMITTED_DOMAIN) == 0
    assert _drop_value(writer, M_TOP_PERMITTED_DOMAIN) is None


@pytest.mark.asyncio
async def test_blocked_domains_payload_not_dict() -> None:
    """top_domains?blocked non-dict payload -> no blocked series/drop, ok True."""
    fake = _FakeClients(blocked_domains_payload=None)
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "stats/top_domains_blocked"}) is not None
    assert _count(writer, M_TOP_BLOCKED_DOMAIN) == 0
    assert _drop_value(writer, M_TOP_BLOCKED_DOMAIN) is None


@pytest.mark.asyncio
async def test_clients_key_missing() -> None:
    """top_clients dict without 'clients' key -> empty -> client drop gauges 0."""
    fake = _FakeClients(clients_payload={"took": 0.001})
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert _count(writer, M_CLIENT_QUERIES) == 0
    assert _drop_value(writer, M_CLIENT_QUERIES) == 0.0
    assert _drop_value(writer, M_CLIENT_BLOCKED) == 0.0


@pytest.mark.asyncio
async def test_clients_key_not_a_list() -> None:
    """top_clients 'clients' not a list -> empty parse -> drop gauges 0."""
    fake = _FakeClients(clients_payload={"clients": "oops", "took": 0.001})
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert _drop_value(writer, M_CLIENT_QUERIES) == 0.0


@pytest.mark.asyncio
async def test_blocked_clients_key_not_a_list() -> None:
    """top_clients?blocked 'clients' not a list -> blocked_by_ip empty."""
    clients_payload = {"clients": [{"ip": "192.168.2.10", "count": 5}], "took": 0.001}
    fake = _FakeClients(
        clients_payload=clients_payload,
        blocked_clients_payload={"clients": 7, "took": 0.001},
    )
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is True
    labels = {
        "client_ip": "192.168.2.10",
        "client_name": "",
        "client_kind": "lan",
        "host_lan_ip": "",
        "client_mac": "",
    }
    assert _gauge_value(writer, M_CLIENT_BLOCKED, labels) == 0.0


@pytest.mark.asyncio
async def test_domains_key_missing() -> None:
    """top_domains dict without 'domains' key -> 0 survivors, drop gauge 0."""
    fake = _FakeClients(domains_payload={"took": 0.001})
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert _count(writer, M_TOP_PERMITTED_DOMAIN) == 0
    assert _drop_value(writer, M_TOP_PERMITTED_DOMAIN) == 0.0


@pytest.mark.asyncio
async def test_domains_key_not_a_list() -> None:
    """top_domains 'domains' not a list -> 0 survivors, drop gauge 0."""
    fake = _FakeClients(domains_payload={"domains": 5, "took": 0.001})
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert _drop_value(writer, M_TOP_PERMITTED_DOMAIN) == 0.0


@pytest.mark.asyncio
async def test_malformed_client_entries_skipped() -> None:
    """Non-dict entry, missing ip, non-str ip, empty ip, non-numeric count handled."""
    clients_payload = {
        "clients": [
            "not-a-dict",
            {"name": "", "count": 5},  # missing ip
            {"name": "", "ip": 123, "count": 5},  # non-str ip
            {"name": "", "ip": "", "count": 5},  # empty ip
            {"name": 99, "ip": "192.168.2.20", "count": "nope"},  # non-str name, bad count
        ],
        "took": 0.001,
    }
    fake = _FakeClients(clients_payload=clients_payload)
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is True
    # Only the last entry survives: name coerced to "", count -> 0.0.
    labels = {
        "client_ip": "192.168.2.20",
        "client_name": "",
        "client_kind": "lan",
        "host_lan_ip": "",
        "client_mac": "",
    }
    assert _gauge_value(writer, M_CLIENT_QUERIES, labels) == 0.0
    assert _count(writer, M_CLIENT_QUERIES) == 1


@pytest.mark.asyncio
async def test_malformed_blocked_client_entries_skipped() -> None:
    """Blocked map skips non-dict / missing-ip / non-str-ip / empty-ip entries."""
    clients_payload = {"clients": [{"ip": "192.168.2.20", "count": 5}], "took": 0.001}
    blocked_payload = {
        "clients": [
            "x",
            {"count": 1},  # missing ip
            {"ip": 5, "count": 1},  # non-str ip
            {"ip": "", "count": 1},  # empty ip
            {"ip": "192.168.2.20", "count": 3},
        ],
        "took": 0.001,
    }
    fake = _FakeClients(clients_payload=clients_payload, blocked_clients_payload=blocked_payload)
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is True
    labels = {
        "client_ip": "192.168.2.20",
        "client_name": "",
        "client_kind": "lan",
        "host_lan_ip": "",
        "client_mac": "",
    }
    assert _gauge_value(writer, M_CLIENT_BLOCKED, labels) == 3.0  # noqa: PLR2004


@pytest.mark.asyncio
async def test_malformed_domain_entries_skipped() -> None:
    """Domain parse skips non-dict / missing-domain / non-str / empty domain."""
    domains_payload = {
        "domains": [
            "x",
            {"count": 1},  # missing domain
            {"domain": 5, "count": 1},  # non-str domain
            {"domain": "", "count": 1},  # empty domain
            {"domain": "ok.example", "count": "bad"},  # bad count -> 0.0
        ],
        "took": 0.001,
    }
    fake = _FakeClients(domains_payload=domains_payload)
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert _gauge_value(writer, M_TOP_PERMITTED_DOMAIN, {"domain": "ok.example"}) == 0.0
    assert _count(writer, M_TOP_PERMITTED_DOMAIN) == 1


@pytest.mark.asyncio
async def test_flatten_devices_all_branches() -> None:
    """Devices flatten covers: non-dict device, bad hwaddr, non-list ips, bad ip,
    multi-IP same MAC, duplicate IP first-wins."""
    clients_payload = {
        "clients": [
            {"ip": "10.0.0.1", "count": 1},
            {"ip": "10.0.0.2", "count": 1},
            {"ip": "10.0.0.9", "count": 1},
        ],
        "took": 0.001,
    }
    devices_payload: dict[str, object] = {
        "devices": [
            "not-a-dict",  # non-dict device skipped
            {"hwaddr": None, "ips": []},  # non-str hwaddr skipped
            {"hwaddr": "", "ips": []},  # empty hwaddr skipped
            {"hwaddr": "aa:11", "ips": "oops"},  # non-list ips skipped
            {
                "hwaddr": "bb:22",
                "ips": [
                    "x",  # non-dict ip_entry skipped
                    {"name": "n"},  # missing ip skipped
                    {"ip": 5},  # non-str ip skipped
                    {"ip": ""},  # empty ip skipped
                    {"ip": "10.0.0.1"},  # first IP for this MAC
                    {"ip": "10.0.0.2"},  # multi-IP same MAC
                ],
            },
            {"hwaddr": "cc:33", "ips": [{"ip": "10.0.0.1"}]},  # duplicate IP, first-wins (bb:22)
        ],
        "took": 0.001,
    }
    fake = _FakeClients(clients_payload=clients_payload, devices_payload=devices_payload)
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is True
    # 10.0.0.1 -> bb:22 (first wins over cc:33), 10.0.0.2 -> bb:22, 10.0.0.9 -> no MAC.
    labels_1 = {
        "client_ip": "10.0.0.1",
        "client_name": "",
        "client_kind": "lan",
        "client_mac": "bb:22",
        "host_lan_ip": "",
    }
    assert _gauge_value(writer, M_CLIENT_QUERIES, labels_1) == 1.0
    labels_2 = {
        "client_ip": "10.0.0.2",
        "client_name": "",
        "client_kind": "lan",
        "client_mac": "bb:22",
        "host_lan_ip": "",
    }
    assert _gauge_value(writer, M_CLIENT_QUERIES, labels_2) == 1.0
    labels_9 = {
        "client_ip": "10.0.0.9",
        "client_name": "",
        "client_kind": "lan",
        "host_lan_ip": "",
        "client_mac": "",
    }
    assert _gauge_value(writer, M_CLIENT_QUERIES, labels_9) == 1.0
    assert labels_9["client_mac"] == ""  # sanity (empty mac for 10.0.0.9)


@pytest.mark.asyncio
async def test_devices_key_missing_or_not_list() -> None:
    """devices payload without 'devices' key or non-list -> ip_mac empty."""
    clients_payload = {"clients": [{"ip": "10.0.0.1", "count": 1}], "took": 0.001}
    fake = _FakeClients(clients_payload=clients_payload, devices_payload={"took": 0.001})
    writer = InMemoryMetricsWriter()
    result = await PiholeClientsCollector().run(_ctx(writer, fake))
    assert result.ok is True
    for label_set in _entries_for(writer, M_CLIENT_QUERIES):
        assert label_set["client_mac"] == ""

    fake2 = _FakeClients(
        clients_payload=clients_payload, devices_payload={"devices": "x", "took": 0.001}
    )
    writer2 = InMemoryMetricsWriter()
    result2 = await PiholeClientsCollector().run(_ctx(writer2, fake2))
    assert result2.ok is True
    for label_set in _entries_for(writer2, M_CLIENT_QUERIES):
        assert label_set["client_mac"] == ""


def test_collector_registered_via_register_all() -> None:
    """PiholeClientsCollector is registered through the public bundle entrypoint."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    names = {record.config.name for record in loaded}
    assert "pihole_clients" in names
