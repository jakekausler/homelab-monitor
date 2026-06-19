"""Tests for UnifiSshLeaseCollector (STAGE-007-012)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
import structlog

from homelab_monitor.kernel.db.repositories.unifi_clients_repository import UnifiClientRepo
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter, InMemoryMetricsWriter
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.kernel.ssh.errors import HostKeyMismatch, SshTimeout, SshTransportError
from homelab_monitor.kernel.ssh.result import SshCommandResult
from homelab_monitor.plugins.collectors.integrations.unifi.ssh_lease import (
    UnifiSshLeaseCollector,
    _parse_leases,  # pyright: ignore[reportPrivateUsage]
)

_ENV_GATE = "HOMELAB_MONITOR_UNIFI_SSH_LEASE_ENABLED"


class _FakeConn:
    """A fake SshConnection whose run() returns a canned result."""

    def __init__(self, result: SshCommandResult) -> None:
        self._result = result

    async def run(self, command: str = "") -> SshCommandResult:
        return self._result


class _FakeSshFactory:
    """Fake SshClientFactory: open() yields a _FakeConn or raises a transport error."""

    def __init__(
        self,
        *,
        result: SshCommandResult | None = None,
        raise_exc: SshTransportError | None = None,
    ) -> None:
        self._result = result
        self._raise = raise_exc
        self.open_called = False

    @asynccontextmanager
    async def open(self, target_id: str) -> AsyncGenerator[_FakeConn]:
        self.open_called = True
        if self._raise is not None:
            raise self._raise
        assert self._result is not None
        yield _FakeConn(self._result)


def _ctx(
    repo: SqliteRepository,
    writer: InMemoryMetricsWriter,
    factory: object,
) -> CollectorContext:
    """CollectorContext with a real migrated db + fake ssh + in-memory writer."""
    return CollectorContext(
        config=CollectorConfig(name="unifi_ssh_lease", interval_seconds=300, timeout_seconds=15),
        db=repo,
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=factory,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="unifi_ssh_lease"),
    )


def _gauge_value(
    writer: InMemoryMetricsWriter,
    name: str,
    label_subset: dict[str, str],
) -> float | None:
    """Return the value of the first gauge matching name + all label_subset entries."""
    for e in writer.recorded:
        if (
            e.kind == "gauge"
            and e.name == name
            and all(e.labels.get(k) == v for k, v in label_subset.items())
        ):
            return e.value
    return None


_PROBE_LABELS = {"target": "udm", "probe": "unifi_dhcp_lease"}


# --------------------------------------------------------------------------------
# Gate off
# --------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gate_off_is_inert(repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate off -> ok=True, 0 metrics, SSH never opened, no DB write."""
    monkeypatch.delenv(_ENV_GATE, raising=False)
    writer = InMemoryMetricsWriter()
    factory = _FakeSshFactory(result=SshCommandResult(stdout="", stderr="", exit_status=0))
    result = await UnifiSshLeaseCollector().run(_ctx(repo, writer, factory))
    assert result.ok is True
    assert result.metrics_emitted == 0
    assert factory.open_called is False
    assert writer.recorded == []


# --------------------------------------------------------------------------------
# Transport errors
# --------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_host_key_mismatch(repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    """HostKeyMismatch -> ok=False, up=0 + host_key_mismatch=1, no lease_count."""
    monkeypatch.setenv(_ENV_GATE, "true")
    writer = InMemoryMetricsWriter()
    factory = _FakeSshFactory(raise_exc=HostKeyMismatch("udm", "key changed"))
    result = await UnifiSshLeaseCollector().run(_ctx(repo, writer, factory))
    assert result.ok is False
    assert _gauge_value(writer, "homelab_ssh_probe_up", _PROBE_LABELS) == 0.0
    assert _gauge_value(writer, "homelab_ssh_probe_host_key_mismatch", _PROBE_LABELS) == 1.0
    assert _gauge_value(writer, "homelab_unifi_dhcp_lease_count", {}) is None
    gauges = [e for e in writer.recorded if e.kind == "gauge"]  # pyright: ignore[reportPrivateUsage]
    assert result.metrics_emitted == len(gauges)


@pytest.mark.asyncio
async def test_generic_transport_error(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-mismatch SshTransportError -> ok=False, up=0 + host_key_mismatch=0."""
    monkeypatch.setenv(_ENV_GATE, "true")
    writer = InMemoryMetricsWriter()
    factory = _FakeSshFactory(raise_exc=SshTimeout("udm", "timed out"))
    result = await UnifiSshLeaseCollector().run(_ctx(repo, writer, factory))
    assert result.ok is False
    assert _gauge_value(writer, "homelab_ssh_probe_up", _PROBE_LABELS) == 0.0
    assert _gauge_value(writer, "homelab_ssh_probe_host_key_mismatch", _PROBE_LABELS) == 0.0
    assert _gauge_value(writer, "homelab_unifi_dhcp_lease_count", {}) is None
    gauges = [e for e in writer.recorded if e.kind == "gauge"]  # pyright: ignore[reportPrivateUsage]
    assert result.metrics_emitted == len(gauges)


# --------------------------------------------------------------------------------
# Non-zero exit
# --------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_nonzero_exit(repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    """exit_status != 0 -> ok=False, up=0 + mismatch=0 + duration, no lease_count."""
    monkeypatch.setenv(_ENV_GATE, "true")
    writer = InMemoryMetricsWriter()
    factory = _FakeSshFactory(
        result=SshCommandResult(stdout="", stderr="no such file", exit_status=1)
    )
    result = await UnifiSshLeaseCollector().run(_ctx(repo, writer, factory))
    assert result.ok is False
    assert _gauge_value(writer, "homelab_ssh_probe_up", _PROBE_LABELS) == 0.0
    assert _gauge_value(writer, "homelab_ssh_probe_host_key_mismatch", _PROBE_LABELS) == 0.0
    assert _gauge_value(writer, "homelab_ssh_probe_duration_seconds", _PROBE_LABELS) is not None
    assert _gauge_value(writer, "homelab_unifi_dhcp_lease_count", {}) is None
    gauges = [e for e in writer.recorded if e.kind == "gauge"]  # pyright: ignore[reportPrivateUsage]
    assert result.metrics_emitted == len(gauges)


@pytest.mark.asyncio
async def test_failure_after_success_emits_climbing_age(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure on the SAME instance after a prior success emits a climbing age."""
    monkeypatch.setenv(_ENV_GATE, "true")
    collector = UnifiSshLeaseCollector()

    # First run: success (populates _last_success_monotonic).
    writer_success = InMemoryMetricsWriter()
    factory_success = _FakeSshFactory(result=SshCommandResult(stdout="", stderr="", exit_status=0))
    result_success = await collector.run(_ctx(repo, writer_success, factory_success))
    assert result_success.ok is True

    # Second run on SAME instance: HostKeyMismatch failure.
    writer_fail = InMemoryMetricsWriter()
    factory_fail = _FakeSshFactory(raise_exc=HostKeyMismatch("udm", "key changed"))
    result_fail = await collector.run(_ctx(repo, writer_fail, factory_fail))

    assert result_fail.ok is False
    # Climbing age MUST now be emitted (prior success exists).
    age = _gauge_value(writer_fail, "homelab_ssh_last_success_age_seconds", _PROBE_LABELS)
    assert age is not None
    assert age >= 0.0
    # metrics_emitted must match gauge count (3 gauges: up + mismatch + age).
    gauges = [e for e in writer_fail.recorded if e.kind == "gauge"]  # pyright: ignore[reportPrivateUsage]
    assert result_fail.metrics_emitted == len(gauges)


# --------------------------------------------------------------------------------
# Success paths
# --------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_success_empty_stdout(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty lease file -> ok=True, lease_count=0, up=1 + duration + age=0."""
    monkeypatch.setenv(_ENV_GATE, "true")
    writer = InMemoryMetricsWriter()
    factory = _FakeSshFactory(result=SshCommandResult(stdout="", stderr="", exit_status=0))
    result = await UnifiSshLeaseCollector().run(_ctx(repo, writer, factory))
    assert result.ok is True
    assert _gauge_value(writer, "homelab_unifi_dhcp_lease_count", {}) == 0.0
    assert _gauge_value(writer, "homelab_ssh_probe_up", _PROBE_LABELS) == 1.0
    assert _gauge_value(writer, "homelab_ssh_probe_duration_seconds", _PROBE_LABELS) is not None
    assert _gauge_value(writer, "homelab_ssh_last_success_age_seconds", _PROBE_LABELS) == 0.0
    # metrics_emitted matches the recorded gauge count on success.
    assert result.metrics_emitted == len(writer.recorded)


@pytest.mark.asyncio
async def test_success_enriches_existing_client(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A populated lease for an EXISTING (uppercase-MAC) client sets lease_expiry."""
    monkeypatch.setenv(_ENV_GATE, "true")
    upper_mac = "AA:BB:CC:DD:EE:FF"
    seen = utc_now_iso()
    async with repo.transaction() as conn:
        await UnifiClientRepo.upsert_client_conn(
            conn,
            mac=upper_mac,
            ip="192.168.2.50",
            hostname="laptop",
            name=None,
            oui=None,
            network=None,
            ap_mac=None,
            sw_mac=None,
            sw_port=None,
            use_fixedip=False,
            fixed_ip=None,
            online=True,
            first_seen=seen,
            last_seen=seen,
        )

    # Lease file uses the SAME mac lowercased.
    stdout = "1700000000 aa:bb:cc:dd:ee:ff 192.168.2.50 laptop 01:aa:bb\n"
    writer = InMemoryMetricsWriter()
    factory = _FakeSshFactory(result=SshCommandResult(stdout=stdout, stderr="", exit_status=0))
    result = await UnifiSshLeaseCollector().run(_ctx(repo, writer, factory))

    assert result.ok is True
    assert _gauge_value(writer, "homelab_unifi_dhcp_lease_count", {}) == 1.0
    client_repo = UnifiClientRepo(repo)
    row = await client_repo.get_client(upper_mac)
    assert row is not None
    expected_expiry = datetime.fromtimestamp(1700000000, tz=UTC).isoformat()
    assert row.lease_expiry == expected_expiry


@pytest.mark.asyncio
async def test_success_inserts_lease_only_client(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lease MAC absent from the registry -> a new offline row with lease_expiry."""
    monkeypatch.setenv(_ENV_GATE, "true")
    new_mac = "de:ad:be:ef:00:01"
    stdout = f"1700000000 {new_mac} 192.168.2.77 printer 01:de:ad\n"
    writer = InMemoryMetricsWriter()
    factory = _FakeSshFactory(result=SshCommandResult(stdout=stdout, stderr="", exit_status=0))
    result = await UnifiSshLeaseCollector().run(_ctx(repo, writer, factory))

    assert result.ok is True
    assert _gauge_value(writer, "homelab_unifi_dhcp_lease_count", {}) == 1.0
    client_repo = UnifiClientRepo(repo)
    row = await client_repo.get_client(new_mac)
    assert row is not None
    assert row.online is False
    assert row.ip == "192.168.2.77"
    assert row.hostname == "printer"
    expected_expiry = datetime.fromtimestamp(1700000000, tz=UTC).isoformat()
    assert row.lease_expiry == expected_expiry


@pytest.mark.asyncio
async def test_success_dup_mac_in_lease_file_inserts_once(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A MAC appearing twice in the lease file -> inserted once (dup guard)."""
    monkeypatch.setenv(_ENV_GATE, "true")
    new_mac = "de:ad:be:ef:00:02"
    stdout = (
        f"1700000000 {new_mac} 192.168.2.88 host-a 01:aa\n"
        f"1700000500 {new_mac} 192.168.2.88 host-a 01:aa\n"
    )
    writer = InMemoryMetricsWriter()
    factory = _FakeSshFactory(result=SshCommandResult(stdout=stdout, stderr="", exit_status=0))
    result = await UnifiSshLeaseCollector().run(_ctx(repo, writer, factory))

    assert result.ok is True
    # lease_count counts every parsed lease line (2), but the registry holds 1 row.
    assert _gauge_value(writer, "homelab_unifi_dhcp_lease_count", {}) == 2.0  # noqa: PLR2004
    client_repo = UnifiClientRepo(repo)
    rows = [r for r in await client_repo.list_clients() if r.mac == new_mac]
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_success_combined_existing_and_new_client(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One lease file with an EXISTING-client MAC and a NEW MAC exercises both branches."""
    monkeypatch.setenv(_ENV_GATE, "true")
    existing_mac_upper = "AA:BB:CC:11:22:33"
    new_mac = "de:ad:be:ef:00:05"
    seen = utc_now_iso()

    # Seed the existing client (uppercase MAC, online=True, no lease_expiry).
    async with repo.transaction() as conn:
        await UnifiClientRepo.upsert_client_conn(
            conn,
            mac=existing_mac_upper,
            ip="192.168.2.60",
            hostname="known-host",
            name=None,
            oui=None,
            network=None,
            ap_mac=None,
            sw_mac=None,
            sw_port=None,
            use_fixedip=False,
            fixed_ip=None,
            online=True,
            first_seen=seen,
            last_seen=seen,
        )

    existing_expiry_epoch = 1700001000
    new_expiry_epoch = 1700002000
    stdout = (
        f"{existing_expiry_epoch} {existing_mac_upper.lower()} 192.168.2.60 known-host 01:aa\n"
        f"{new_expiry_epoch} {new_mac} 192.168.2.80 new-host 01:bb\n"
    )
    writer = InMemoryMetricsWriter()
    factory = _FakeSshFactory(result=SshCommandResult(stdout=stdout, stderr="", exit_status=0))
    result = await UnifiSshLeaseCollector().run(_ctx(repo, writer, factory))

    assert result.ok is True
    assert _gauge_value(writer, "homelab_unifi_dhcp_lease_count", {}) == 2.0  # noqa: PLR2004

    client_repo = UnifiClientRepo(repo)

    # Existing row: lease_expiry set (case-insensitive match); identity fields preserved.
    existing_row = await client_repo.get_client(existing_mac_upper)
    assert existing_row is not None
    assert (
        existing_row.lease_expiry
        == datetime.fromtimestamp(existing_expiry_epoch, tz=UTC).isoformat()
    )
    assert existing_row.online is True  # preserved from seed
    assert existing_row.hostname == "known-host"  # preserved from seed

    # New row: inserted offline with lease_expiry; no contamination from existing row.
    new_row = await client_repo.get_client(new_mac)
    assert new_row is not None
    assert new_row.online is False
    assert new_row.ip == "192.168.2.80"
    assert new_row.hostname == "new-host"
    assert new_row.lease_expiry == datetime.fromtimestamp(new_expiry_epoch, tz=UTC).isoformat()


# --------------------------------------------------------------------------------
# _parse_leases unit tests
# --------------------------------------------------------------------------------
def test_parse_leases_full_line() -> None:
    """A full lease line parses all four useful fields."""
    out = _parse_leases("1700000000 AA:BB:CC:DD:EE:FF 192.168.2.50 laptop 01:aa\n")
    assert len(out) == 1
    mac, ip, hostname, expiry = out[0]
    assert mac == "aa:bb:cc:dd:ee:ff"  # lowercased
    assert ip == "192.168.2.50"
    assert hostname == "laptop"
    assert expiry == datetime.fromtimestamp(1700000000, tz=UTC).isoformat()


def test_parse_leases_skips_short_line() -> None:
    """A line with fewer than two fields is skipped."""
    assert _parse_leases("1700000000\n") == []


def test_parse_leases_skips_non_int_epoch() -> None:
    """A non-integer epoch line is skipped."""
    assert _parse_leases("notanint aa:bb:cc:dd:ee:ff 192.168.2.1 host\n") == []


def test_parse_leases_hostname_star_is_none() -> None:
    """hostname '*' -> None."""
    out = _parse_leases("1700000000 aa:bb:cc:dd:ee:ff 192.168.2.1 *\n")
    assert out[0][2] is None


def test_parse_leases_missing_ip_is_none() -> None:
    """A two-field line -> ip None, hostname None."""
    out = _parse_leases("1700000000 aa:bb:cc:dd:ee:ff\n")
    assert out[0][1] is None
    assert out[0][2] is None


def test_parse_leases_missing_hostname_is_none() -> None:
    """A three-field line -> ip set, hostname None."""
    out = _parse_leases("1700000000 aa:bb:cc:dd:ee:ff 192.168.2.1\n")
    assert out[0][1] == "192.168.2.1"
    assert out[0][2] is None


def test_parse_leases_skips_negative_epoch() -> None:
    """A negative epoch is skipped by the epoch <= 0 guard (fromtimestamp would not raise)."""
    lines = (
        "-1 aa:bb:cc:00:00:09 192.168.2.9 bad-host 01:aa\n"
        "1700000000 aa:bb:cc:00:00:0b 192.168.2.11 good-host 01:bb\n"
    )
    result = _parse_leases(lines)
    assert len(result) == 1
    assert result[0][0] == "aa:bb:cc:00:00:0b"


def test_parse_leases_skips_overflow_epoch() -> None:
    """An absurdly large epoch is skipped (OverflowError from fromtimestamp)."""
    lines = (
        "99999999999999999999 aa:bb:cc:00:00:0a 192.168.2.10 overflow 01:aa\n"
        "1700000000 aa:bb:cc:00:00:0c 192.168.2.12 good-host 01:cc\n"
    )
    result = _parse_leases(lines)
    assert len(result) == 1
    assert result[0][0] == "aa:bb:cc:00:00:0c"
