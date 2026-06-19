"""unifi_ssh_lease collector -- opt-in SSH DHCP-lease enrichment (STAGE-007-012).

Env-gated (``HOMELAB_MONITOR_UNIFI_SSH_LEASE_ENABLED``, default off). When enabled,
SSHes to the configured target (default ``udm``), reads the dnsmasq lease file,
parses each lease, and:

- Enriches an EXISTING ``unifi_clients`` row with its ``lease_expiry`` (matched
  case-insensitively on MAC -- the UniFi API stores MACs verbatim while dnsmasq
  lowercases them).
- Inserts a lease-only row (online=False) for a MAC absent from the registry,
  then sets its lease_expiry.

Emits:
- ``homelab_unifi_dhcp_lease_count`` -- GAUGE (no labels) of leases parsed.
- the standard ``homelab_ssh_*`` probe-health gauges (up / host_key_mismatch /
  duration / last_success_age) labeled ``{target, probe="unifi_dhcp_lease"}``.

This is NOT an SshProbe subclass; it mirrors SshProbe's ctx.ssh usage + error
mapping inline so it can also drive the DB write. It does NOT use ctx.unifi.
NO SuggestionEvents.

FAILURE / OK SEMANTICS:
- gate off -> ok=True, metrics_emitted=0, inert (no SSH, no DB).
- HostKeyMismatch -> ok=False, up=0 + host_key_mismatch=1 (2 metrics).
- other SshTransportError -> ok=False, up=0 + host_key_mismatch=0 (2 metrics).
- non-zero exit -> ok=False, up=0 + host_key_mismatch=0 + duration (3 metrics).
- success -> ok=True, lease_count + up=1 + duration + last_success_age=0.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Final

from homelab_monitor.kernel.config import load_unifi_config
from homelab_monitor.kernel.db.repositories.unifi_clients_repository import UnifiClientRepo
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult
from homelab_monitor.kernel.ssh.errors import HostKeyMismatch, SshTransportError

# --- Metric names ---------------------------------------------------------------
M_LEASE_COUNT: Final[str] = "homelab_unifi_dhcp_lease_count"
M_PROBE_UP: Final[str] = "homelab_ssh_probe_up"
M_PROBE_HOST_KEY_MISMATCH: Final[str] = "homelab_ssh_probe_host_key_mismatch"
M_PROBE_DURATION: Final[str] = "homelab_ssh_probe_duration_seconds"
M_PROBE_LAST_SUCCESS_AGE: Final[str] = "homelab_ssh_last_success_age_seconds"

_PROBE_LABEL: Final[str] = "unifi_dhcp_lease"

_MIN_LEASE_FIELDS: Final[int] = 2


def _parse_leases(stdout: str) -> list[tuple[str, str | None, str | None, str]]:
    """Parse dnsmasq lease lines into ``(mac_lower, ip|None, hostname|None, expiry_iso)``.

    Each dnsmasq lease line is ``<epoch> <mac> <ip> <hostname> <clientid>``. We use
    only the first four fields. A line is skipped (not an error) when it has fewer
    than two fields, its first field is not an integer epoch, the epoch is
    non-positive (<= 0), or the epoch is out of range for the platform clock. The MAC is
    lowercased defensively (dnsmasq already lowercases). hostname ``"*"`` -> None.
    """
    leases: list[tuple[str, str | None, str | None, str]] = []
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) < _MIN_LEASE_FIELDS:
            continue
        try:
            epoch = int(parts[0])
            if epoch <= 0:
                continue
            lease_expiry = datetime.fromtimestamp(epoch, tz=UTC).isoformat()
        except (ValueError, OverflowError, OSError):
            continue
        mac = parts[1].lower()
        ip = parts[2] if len(parts) >= 3 else None  # noqa: PLR2004
        hostname = parts[3] if len(parts) >= 4 and parts[3] != "*" else None  # noqa: PLR2004
        leases.append((mac, ip, hostname, lease_expiry))
    return leases


class UnifiSshLeaseCollector(BaseCollector):
    """SSH the UniFi gateway, parse dnsmasq leases, enrich the registry, emit count.

    Env-gated (off by default). Mirrors SshProbe's ctx.ssh open->run->error-map
    surface inline, then upserts lease_expiry into unifi_clients. See module
    docstring for the full failure/OK matrix.
    """

    name: ClassVar[str] = "unifi_ssh_lease"
    interval: ClassVar[timedelta] = timedelta(seconds=300)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "unifi"

    def __init__(self) -> None:
        """Initialize per-instance last-success monotonic timestamp."""
        super().__init__()
        self._last_success_monotonic: float | None = None

    command: ClassVar[str] = "cat /data/udapi-config/dnsmasq.lease"

    def _emit_probe_failure_age(
        self,
        ctx: CollectorContext,
        probe_labels: dict[str, str],
        emitted: int,
    ) -> int:
        """Emit climbing last_success_age if a prior success exists; return new emitted count."""
        if self._last_success_monotonic is not None:
            age = time.monotonic() - self._last_success_monotonic
            ctx.vm.write_gauge(M_PROBE_LAST_SUCCESS_AGE, age, probe_labels)
            emitted += 1
        return emitted

    async def run(self, ctx: CollectorContext) -> CollectorResult:  # noqa: PLR0915
        """Gate -> SSH fetch -> parse -> enrich DB -> emit. See module docstring.

        PLR0915: the linear gate -> open -> error-map (3 paths) -> parse ->
        snapshot -> transaction (enrich/insert) -> emit flow is intentionally
        kept in one method for readability; splitting it would scatter the flow.
        """
        start = time.monotonic()

        cfg = load_unifi_config()
        if not cfg.ssh_lease_enabled:
            return CollectorResult(
                ok=True,
                metrics_emitted=0,
                errors=[],
                duration_seconds=time.monotonic() - start,
            )

        target_id = cfg.ssh_lease_target_id
        probe_labels = {"target": target_id, "probe": _PROBE_LABEL}

        ssh_start = time.monotonic()
        try:
            async with ctx.ssh.open(target_id) as conn:
                result = await conn.run(self.command)
        except HostKeyMismatch as exc:
            emitted = 0
            ctx.vm.write_gauge(M_PROBE_UP, 0.0, probe_labels)
            emitted += 1
            ctx.vm.write_gauge(M_PROBE_HOST_KEY_MISMATCH, 1.0, probe_labels)
            emitted += 1
            emitted = self._emit_probe_failure_age(ctx, probe_labels, emitted)
            return CollectorResult(
                ok=False,
                metrics_emitted=emitted,
                errors=[str(exc)],
                duration_seconds=time.monotonic() - start,
            )
        except SshTransportError as exc:
            emitted = 0
            ctx.vm.write_gauge(M_PROBE_UP, 0.0, probe_labels)
            emitted += 1
            ctx.vm.write_gauge(M_PROBE_HOST_KEY_MISMATCH, 0.0, probe_labels)
            emitted += 1
            emitted = self._emit_probe_failure_age(ctx, probe_labels, emitted)
            return CollectorResult(
                ok=False,
                metrics_emitted=emitted,
                errors=[str(exc)],
                duration_seconds=time.monotonic() - start,
            )
        ssh_duration = time.monotonic() - ssh_start

        if result.exit_status != 0:
            emitted = 0
            ctx.vm.write_gauge(M_PROBE_UP, 0.0, probe_labels)
            emitted += 1
            ctx.vm.write_gauge(M_PROBE_HOST_KEY_MISMATCH, 0.0, probe_labels)
            emitted += 1
            ctx.vm.write_gauge(M_PROBE_DURATION, ssh_duration, probe_labels)
            emitted += 1
            emitted = self._emit_probe_failure_age(ctx, probe_labels, emitted)
            return CollectorResult(
                ok=False,
                metrics_emitted=emitted,
                errors=[f"lease command exit {result.exit_status}"],
                duration_seconds=time.monotonic() - start,
            )

        leases = _parse_leases(result.stdout)

        repo = UnifiClientRepo(ctx.db)
        prior = await repo.list_clients()
        prior_macs_lower = {row.mac.lower() for row in prior}
        now_iso = datetime.now(tz=UTC).isoformat()

        async with ctx.db.transaction() as conn:
            for mac, ip, hostname, expiry in leases:
                if mac in prior_macs_lower:
                    await UnifiClientRepo.set_lease_expiry_conn(conn, mac=mac, lease_expiry=expiry)
                else:
                    await UnifiClientRepo.upsert_client_conn(
                        conn,
                        mac=mac,
                        ip=ip,
                        hostname=hostname,
                        name=None,
                        oui=None,
                        network=None,
                        ap_mac=None,
                        sw_mac=None,
                        sw_port=None,
                        use_fixedip=False,
                        fixed_ip=None,
                        online=False,
                        first_seen=now_iso,
                        last_seen=now_iso,
                    )
                    await UnifiClientRepo.set_lease_expiry_conn(conn, mac=mac, lease_expiry=expiry)
                    prior_macs_lower.add(mac)

        self._last_success_monotonic = time.monotonic()
        emitted = 0
        ctx.vm.write_gauge(M_LEASE_COUNT, float(len(leases)), {})
        emitted += 1
        ctx.vm.write_gauge(M_PROBE_UP, 1.0, probe_labels)
        emitted += 1
        ctx.vm.write_gauge(M_PROBE_DURATION, ssh_duration, probe_labels)
        emitted += 1
        ctx.vm.write_gauge(M_PROBE_LAST_SUCCESS_AGE, 0.0, probe_labels)
        emitted += 1

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted,
            errors=[],
            duration_seconds=time.monotonic() - start,
        )
