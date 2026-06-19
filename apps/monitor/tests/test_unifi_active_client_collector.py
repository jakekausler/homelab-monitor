"""DB-backed unit tests for UnifiActiveClientCollector (STAGE-007-007).

Mirrors test_unifi_identity.py (real migrated SqliteRepository via the ``repo``
fixture + UnifiClientRepo queries) and test_unifi_wan_collector.py (fake UnifiClient
+ InMemoryMetricsWriter + gauge/counter assertion helpers). The context factory
injects the REAL migrated repo as ``ctx.db`` so the collector's write transaction
hits a real SQLite DB.
"""

from __future__ import annotations

import pytest
import structlog

from homelab_monitor.kernel.db.repositories.unifi_clients_repository import UnifiClientRepo
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
    UnifiClient,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.kernel.unifi.client import UnifiResponse
from homelab_monitor.kernel.unifi.errors import UnifiError
from homelab_monitor.plugins.collectors.integrations.unifi.active_client import (
    UnifiActiveClientCollector,
)

_HOST_IP = "192.168.2.148"


# --- assertion helpers (mirror the wan test) -------------------------------------


def _gauges(writer: InMemoryMetricsWriter, name: str) -> list[tuple[float, dict[str, str]]]:
    """Return (value, labels) for all recorded GAUGE entries with the given name."""
    return [(e.value, e.labels) for e in writer.recorded if e.name == name and e.kind == "gauge"]


def _gauge_value(
    writer: InMemoryMetricsWriter,
    name: str,
    label_subset: dict[str, str],
) -> float | None:
    """Return the value of the first GAUGE matching name + all label_subset entries."""
    for e in writer.recorded:
        if (
            e.kind == "gauge"
            and e.name == name
            and all(e.labels.get(k) == v for k, v in label_subset.items())
        ):
            return e.value
    return None


def _counter_value(
    writer: InMemoryMetricsWriter,
    name: str,
    label_subset: dict[str, str],
) -> float | None:
    """Return the value of the first COUNTER matching name + all label_subset entries."""
    for e in writer.recorded:
        if (
            e.kind == "counter"
            and e.name == name
            and all(e.labels.get(k) == v for k, v in label_subset.items())
        ):
            return e.value
    return None


# --- fake unifi clients (mirror _FakeUnifiBase in the wan test) ------------------


class _FakeUnifiBase:
    """Base fake UnifiClient: every method returns a stub UnifiError."""

    site_name: str = "default"
    v1_site_id: str = "fake-uuid"

    async def v1_sites(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def v1_devices(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def v1_device(self, device_id: str) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def v1_device_stats(self, device_id: str) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def v1_clients(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def stat_device(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def stat_sta(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def stat_alluser(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def stat_health(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def stat_dpi(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def stat_stadpi(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def rest_networkconf(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def rest_alarm(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def stat_sysinfo(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def resolve_site_id(self) -> UnifiError | None:
        return None


class _FakeStaAllOk(_FakeUnifiBase):
    """Both endpoints succeed with injected payloads."""

    def __init__(self, sta_payload: object, alluser_payload: object) -> None:
        self._sta = sta_payload
        self._alluser = alluser_payload

    async def stat_sta(self) -> UnifiResponse | UnifiError:
        return UnifiResponse(payload=self._sta, took_seconds=0.11, endpoint="stat/sta")

    async def stat_alluser(self) -> UnifiResponse | UnifiError:
        return UnifiResponse(payload=self._alluser, took_seconds=0.22, endpoint="stat/alluser")


class _FakeStaFail(_FakeUnifiBase):
    """stat_sta returns UnifiError (hard fail)."""

    async def stat_sta(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="timeout", message="sta timed out")


class _FakeAlluserFail(_FakeUnifiBase):
    """stat_sta succeeds; stat_alluser returns UnifiError (degrade)."""

    def __init__(self, sta_payload: object) -> None:
        self._sta = sta_payload

    async def stat_sta(self) -> UnifiResponse | UnifiError:
        return UnifiResponse(payload=self._sta, took_seconds=0.11, endpoint="stat/sta")

    async def stat_alluser(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="timeout", message="alluser timed out")


# --- context factory: REAL db + fake unifi + in-memory writer --------------------


def _ctx(
    repo: SqliteRepository,
    writer: InMemoryMetricsWriter,
    unifi: UnifiClient | None,
) -> CollectorContext:
    """CollectorContext with a REAL migrated db, fake unifi, in-memory writer."""
    return CollectorContext(
        config=CollectorConfig(name="unifi_active_client", interval_seconds=60, timeout_seconds=30),
        db=repo,
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="unifi_active_client"),
        unifi=unifi,
    )


# --- synthetic fixtures modeling the live shapes --------------------------------
#
# Epoch ints for first_seen/last_seen. Values are arbitrary but stable.

_FS = 1_700_000_000
_LS = 1_700_003_600


def _sta_payload() -> dict[str, object]:
    """stat/sta payload: 1 wired + 2 wireless (2 essid, 2 radio) + host + bad-mac.

    Distinct real macs in sta: wired, w1, w2, host. The bad-mac record is skipped
    by both the helper and the rollups/extraction.
    """
    wired: dict[str, object] = {
        "mac": "aa:00:00:00:00:01",
        "ip": "192.168.2.50",
        "hostname": "wired-host",
        "name": "Wired Host",
        "oui": "Intel",
        "network": "LAN",
        "is_wired": True,
        "sw_mac": "sw:00",
        "sw_port": 7,
        "use_fixedip": False,
        "fixed_ip": None,
        "first_seen": _FS,
        "last_seen": _LS,
    }
    wireless1: dict[str, object] = {
        "mac": "aa:00:00:00:00:02",
        "ip": "192.168.2.51",
        "hostname": "phone",
        "network": "IoT",
        "is_wired": False,
        "essid": "HomeWiFi",
        "radio": "ng",
        "ap_mac": "ap:00:00:00:00:01",
        "use_fixedip": False,
        "fixed_ip": None,
        "first_seen": _FS,
        "last_seen": _LS,
    }
    wireless2: dict[str, object] = {
        "mac": "aa:00:00:00:00:03",
        "ip": "192.168.2.52",
        "hostname": "laptop",
        "network": "LAN",
        "is_wired": False,
        "essid": "GuestWiFi",
        "radio": "na",
        "ap_mac": "ap:00:00:00:00:02",
        "use_fixedip": False,
        "fixed_ip": None,
        "first_seen": _FS,
        "last_seen": _LS,
    }
    host: dict[str, object] = {
        "mac": "aa:00:00:00:00:99",
        "ip": _HOST_IP,
        "hostname": "monitor",
        "network": "Default",
        "is_wired": True,
        "sw_mac": "sw:00",
        "sw_port": 1,
        "use_fixedip": False,
        "fixed_ip": None,
        "first_seen": _FS,
        "last_seen": _LS,
    }
    bad_mac: dict[str, object] = {
        "mac": 12345,  # non-str -> skipped by helper AND extraction
        "ip": "192.168.2.250",
        "is_wired": True,
        "first_seen": _FS,
        "last_seen": _LS,
    }
    data: list[object] = [wired, wireless1, wireless2, host, bad_mac]
    payload: dict[str, object] = {"meta": {"rc": "ok"}, "data": data}
    return payload


def _alluser_payload() -> dict[str, object]:
    """stat/alluser payload: 1 offline-only client + 1 mac already in sta.

    offline mac aa:00:00:00:00:10 is NOT in sta -> offline. The dup mac
    aa:00:00:00:00:01 IS in sta -> must NOT downgrade (helper skips already-seen).
    """
    offline: dict[str, object] = {
        "mac": "aa:00:00:00:00:10",
        "last_ip": "192.168.2.60",
        "hostname": "offline-device",
        "is_wired": True,
        "use_fixedip": False,
        "fixed_ip": None,
        "first_seen": _FS,
        "last_seen": _LS,
    }
    dup: dict[str, object] = {
        "mac": "aa:00:00:00:00:01",
        "last_ip": "192.168.2.50",
        "hostname": "wired-host",
        "is_wired": True,
        "use_fixedip": False,
        "fixed_ip": None,
        "first_seen": _FS,
        "last_seen": _LS,
    }
    data: list[object] = [offline, dup]
    payload: dict[str, object] = {"meta": {"rc": "ok"}, "data": data}
    return payload


# --- test cases ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_upsert_online_and_offline(repo: SqliteRepository) -> None:
    """Online clients upserted online=True; offline-only upserted online=False."""
    writer = InMemoryMetricsWriter()
    fake = _FakeStaAllOk(_sta_payload(), _alluser_payload())
    result = await UnifiActiveClientCollector().run(_ctx(repo, writer, fake))
    assert result.ok is True

    client_repo = UnifiClientRepo(repo)
    online = await client_repo.get_client("aa:00:00:00:00:02")
    assert online is not None
    assert online.online is True
    offline = await client_repo.get_client("aa:00:00:00:00:10")
    assert offline is not None
    assert offline.online is False

    assert _gauge_value(writer, "homelab_unifi_identity_clients_upserted", {}) == 5.0  # noqa: PLR2004


@pytest.mark.asyncio
async def test_observations_appended(repo: SqliteRepository) -> None:
    """The observations_appended self-metric reflects online records with an ip."""
    writer = InMemoryMetricsWriter()
    fake = _FakeStaAllOk(_sta_payload(), _alluser_payload())
    await UnifiActiveClientCollector().run(_ctx(repo, writer, fake))
    assert _gauge_value(writer, "homelab_unifi_identity_observations_appended", {}) == 4.0  # noqa: PLR2004


@pytest.mark.asyncio
async def test_host_reconciled(repo: SqliteRepository) -> None:
    """The host record (ip == host_lan_ip) is reconciled exactly once."""
    # Seed the host:<ip> sentinel row (lifespan does this in prod via
    # ensure_host_row); promote_to_host_conn returns False without it.
    client_repo = UnifiClientRepo(repo)
    await client_repo.ensure_host_row(_HOST_IP)

    writer = InMemoryMetricsWriter()
    fake = _FakeStaAllOk(_sta_payload(), _alluser_payload())
    await UnifiActiveClientCollector().run(_ctx(repo, writer, fake))
    assert _gauge_value(writer, "homelab_unifi_identity_hosts_reconciled", {}) == 1.0

    # The host real-mac row exists; the helper's promote_to_host_conn ran.
    host_row = await client_repo.get_client("aa:00:00:00:00:99")
    assert host_row is not None
    assert host_row.is_host is True


@pytest.mark.asyncio
async def test_new_client_signal_fresh_then_unchanged(repo: SqliteRepository) -> None:
    """First run: all macs new. Second run (same DB): zero new."""
    writer1 = InMemoryMetricsWriter()
    fake = _FakeStaAllOk(_sta_payload(), _alluser_payload())
    collector = UnifiActiveClientCollector()

    await collector.run(_ctx(repo, writer1, fake))
    assert _counter_value(writer1, "homelab_unifi_new_client_total", {}) == 5.0  # noqa: PLR2004
    assert _gauge_value(writer1, "homelab_unifi_new_client", {"mac": "aa:00:00:00:00:02"}) == 1.0

    writer2 = InMemoryMetricsWriter()
    await collector.run(_ctx(repo, writer2, fake))
    assert _counter_value(writer2, "homelab_unifi_new_client_total", {}) == 0.0
    # No per-mac new_client gauges on the second run.
    assert _gauges(writer2, "homelab_unifi_new_client") == []


@pytest.mark.asyncio
async def test_rollups(repo: SqliteRepository) -> None:
    """All roster rollups computed from the live stat/sta parse."""
    writer = InMemoryMetricsWriter()
    fake = _FakeStaAllOk(_sta_payload(), _alluser_payload())
    await UnifiActiveClientCollector().run(_ctx(repo, writer, fake))

    assert _gauge_value(writer, "homelab_unifi_active_client_count", {}) == 4.0  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_known_client_count", {}) == 5.0  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_offline_client_count", {}) == 1.0

    assert _gauge_value(writer, "homelab_unifi_ssid_client_count", {"ssid": "HomeWiFi"}) == 1.0
    assert _gauge_value(writer, "homelab_unifi_ssid_client_count", {"ssid": "GuestWiFi"}) == 1.0

    assert _gauge_value(writer, "homelab_unifi_client_count_by_network", {"network": "LAN"}) == 2.0  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_client_count_by_network", {"network": "IoT"}) == 1.0
    assert (
        _gauge_value(writer, "homelab_unifi_client_count_by_network", {"network": "Default"}) == 1.0
    )

    assert (
        _gauge_value(writer, "homelab_unifi_client_count_by_ap", {"ap_mac": "ap:00:00:00:00:01"})
        == 1.0
    )
    assert (
        _gauge_value(writer, "homelab_unifi_client_count_by_ap", {"ap_mac": "ap:00:00:00:00:02"})
        == 1.0
    )

    assert _gauge_value(writer, "homelab_unifi_client_count_by_band", {"band": "2.4ghz"}) == 1.0
    assert _gauge_value(writer, "homelab_unifi_client_count_by_band", {"band": "5ghz"}) == 1.0

    assert _gauge_value(writer, "homelab_unifi_client_count_by_link", {"link": "wired"}) == 2.0  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_client_count_by_link", {"link": "wireless"}) == 2.0  # noqa: PLR2004


@pytest.mark.asyncio
async def test_alluser_degrade(repo: SqliteRepository) -> None:
    """D2: stat_alluser fails -> ok=True, degraded gauge=1, sta still upserted."""
    writer = InMemoryMetricsWriter()
    fake = _FakeAlluserFail(_sta_payload())
    result = await UnifiActiveClientCollector().run(_ctx(repo, writer, fake))
    assert result.ok is True
    assert "alluser timed out" in result.errors
    assert _gauge_value(writer, "homelab_unifi_alluser_degraded", {}) == 1.0

    # sta clients still upserted (4 online from sta: wired/w1/w2/host).
    assert _gauge_value(writer, "homelab_unifi_identity_clients_upserted", {}) == 4.0  # noqa: PLR2004
    client_repo = UnifiClientRepo(repo)
    assert await client_repo.get_client("aa:00:00:00:00:02") is not None
    # Only the sta latency emitted (no alluser latency on degrade).
    assert _gauge_value(writer, "homelab_unifi_api_took_seconds", {"endpoint": "stat/sta"}) == 0.11  # noqa: PLR2004
    assert _gauges(writer, "homelab_unifi_api_took_seconds") == [(0.11, {"endpoint": "stat/sta"})]


@pytest.mark.asyncio
async def test_sta_failure_hard_fails_no_upsert(repo: SqliteRepository) -> None:
    """stat_sta UnifiError -> ok=False, no upsert (registry unchanged)."""
    writer = InMemoryMetricsWriter()
    fake = _FakeStaFail()
    result = await UnifiActiveClientCollector().run(_ctx(repo, writer, fake))
    assert result.ok is False
    assert result.errors == ["sta timed out"]
    assert result.metrics_emitted == 0

    client_repo = UnifiClientRepo(repo)
    assert await client_repo.list_clients() == []


@pytest.mark.asyncio
async def test_none_client() -> None:
    """ctx.unifi is None -> ok=False with the canonical error."""
    writer = InMemoryMetricsWriter()
    # db is unused on this path; pass a stub repo is unnecessary -- use None ignore.
    result = await UnifiActiveClientCollector().run(
        _ctx(None, writer, None)  # pyright: ignore[reportArgumentType]
    )
    assert result.ok is False
    assert result.errors == ["unifi client not configured"]


@pytest.mark.asyncio
async def test_malformed_sta_payload_no_upsert(repo: SqliteRepository) -> None:
    """sta 200 with a non-dict payload -> ok=True, no upsert, zero rollups."""
    writer = InMemoryMetricsWriter()
    bad_payload: list[object] = []  # not a dict -> _parse_records returns []
    fake = _FakeStaAllOk(bad_payload, bad_payload)
    result = await UnifiActiveClientCollector().run(_ctx(repo, writer, fake))
    assert result.ok is True

    client_repo = UnifiClientRepo(repo)
    assert await client_repo.list_clients() == []
    assert _gauge_value(writer, "homelab_unifi_active_client_count", {}) == 0.0
    assert _gauge_value(writer, "homelab_unifi_known_client_count", {}) == 0.0
    assert _counter_value(writer, "homelab_unifi_new_client_total", {}) == 0.0


@pytest.mark.asyncio
async def test_malformed_data_not_list(repo: SqliteRepository) -> None:
    """sta payload is a dict but data is not a list -> [] records, ok=True."""
    writer = InMemoryMetricsWriter()
    bad_data: dict[str, object] = {"meta": {"rc": "ok"}, "data": "nope"}
    good_alluser: dict[str, object] = {"meta": {"rc": "ok"}, "data": []}
    fake = _FakeStaAllOk(bad_data, good_alluser)
    result = await UnifiActiveClientCollector().run(_ctx(repo, writer, fake))
    assert result.ok is True
    assert _gauge_value(writer, "homelab_unifi_active_client_count", {}) == 0.0


@pytest.mark.asyncio
async def test_skipped_record_metric(repo: SqliteRepository) -> None:
    """The bad-mac record increments the skipped self-metric."""
    writer = InMemoryMetricsWriter()
    fake = _FakeStaAllOk(_sta_payload(), _alluser_payload())
    await UnifiActiveClientCollector().run(_ctx(repo, writer, fake))
    assert _gauge_value(writer, "homelab_unifi_identity_skipped", {}) == 1.0


@pytest.mark.asyncio
async def test_both_endpoints_emit_latency(repo: SqliteRepository) -> None:
    """Both endpoints emit api_took_seconds when both succeed."""
    writer = InMemoryMetricsWriter()
    fake = _FakeStaAllOk(_sta_payload(), _alluser_payload())
    await UnifiActiveClientCollector().run(_ctx(repo, writer, fake))
    assert _gauge_value(writer, "homelab_unifi_api_took_seconds", {"endpoint": "stat/sta"}) == 0.11  # noqa: PLR2004
    assert (
        _gauge_value(writer, "homelab_unifi_api_took_seconds", {"endpoint": "stat/alluser"}) == 0.22  # noqa: PLR2004
    )


@pytest.mark.asyncio
async def test_metrics_emitted_matches_recorded_writes(repo: SqliteRepository) -> None:
    """result.metrics_emitted equals the number of gauge + counter writes recorded."""
    writer = InMemoryMetricsWriter()
    fake = _FakeStaAllOk(_sta_payload(), _alluser_payload())
    result = await UnifiActiveClientCollector().run(_ctx(repo, writer, fake))
    recorded_writes = sum(1 for e in writer.recorded if e.kind in ("gauge", "counter"))
    assert result.metrics_emitted == recorded_writes


@pytest.mark.asyncio
async def test_sentinel_mac_excluded_from_new(repo: SqliteRepository) -> None:
    """A pre-existing host:<ip> sentinel row is excluded from prior/current macs."""
    # Pre-seed a sentinel row via the repo's ensure_host_row method.
    client_repo = UnifiClientRepo(repo)
    await client_repo.ensure_host_row(_HOST_IP)

    writer = InMemoryMetricsWriter()
    fake = _FakeStaAllOk(_sta_payload(), _alluser_payload())
    await UnifiActiveClientCollector().run(_ctx(repo, writer, fake))

    # The sentinel never appears as a new_client gauge (it's excluded).
    assert _gauge_value(writer, "homelab_unifi_new_client", {"mac": f"host:{_HOST_IP}"}) is None
    # The real 5 macs are still counted as new (since we seeded the sentinel separately).
    assert _counter_value(writer, "homelab_unifi_new_client_total", {}) == 5.0  # noqa: PLR2004


@pytest.mark.asyncio
async def test_rollup_skip_branches_for_sparse_records(repo: SqliteRepository) -> None:
    """Cover the FALSE/skip rollup branches: a wireless record missing essid/ap_mac/
    radio, an alluser record with a non-str mac (skipped in _build_record_by_mac),
    and a wired record missing the network field."""
    bare_wireless: dict[str, object] = {
        "mac": "aa:00:00:00:00:20",
        "ip": "192.168.2.70",
        "hostname": "bare-wireless",
        "network": "IoT",
        "is_wired": False,
        # NO essid / ap_mac / radio -> exercises the FALSE direction of each.
        "use_fixedip": False,
        "fixed_ip": None,
        "first_seen": _FS,
        "last_seen": _LS,
    }
    no_network_wired: dict[str, object] = {
        "mac": "aa:00:00:00:00:21",
        "ip": "192.168.2.71",
        "hostname": "no-network-wired",
        "is_wired": True,
        # NO network field -> exercises the network is None branch.
        "use_fixedip": False,
        "fixed_ip": None,
        "first_seen": _FS,
        "last_seen": _LS,
    }
    sta_data: list[object] = [bare_wireless, no_network_wired]
    sta_payload: dict[str, object] = {"meta": {"rc": "ok"}, "data": sta_data}

    bad_mac_alluser: dict[str, object] = {
        "mac": 999,
        "last_ip": "192.168.2.72",
        "is_wired": True,
    }
    alluser_data: list[object] = [bad_mac_alluser]
    alluser_payload: dict[str, object] = {"meta": {"rc": "ok"}, "data": alluser_data}

    writer = InMemoryMetricsWriter()
    fake = _FakeStaAllOk(sta_payload, alluser_payload)
    result = await UnifiActiveClientCollector().run(_ctx(repo, writer, fake))
    assert result.ok is True

    assert _gauge_value(writer, "homelab_unifi_client_count_by_link", {"link": "wireless"}) == 1.0
    assert _gauge_value(writer, "homelab_unifi_client_count_by_link", {"link": "wired"}) == 1.0
    assert _gauges(writer, "homelab_unifi_ssid_client_count") == []
    assert _gauges(writer, "homelab_unifi_client_count_by_ap") == []
    assert _gauges(writer, "homelab_unifi_client_count_by_band") == []
