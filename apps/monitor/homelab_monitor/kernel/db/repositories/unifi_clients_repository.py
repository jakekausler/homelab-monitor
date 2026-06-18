"""UnifiClientRepo — unifi_clients registry + unifi_client_observations spans.

Mirrors ProbeTargetsRepository / SuggestionsRepository pattern: static *_conn
helpers operate inside an external repo.transaction() (so STAGE-007-004 can
upsert a client AND append its IP<->MAC observation atomically in ONE
transaction); instance methods serve reads + the host-row seed.

STAGE-007-003: persistent MAC-keyed client registry. All timestamps are
ISO-8601 UTC TEXT (utc_now_iso). Booleans are INTEGER 0/1 in the DB and are
exposed as bool on UnifiClientRow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso


@dataclass(frozen=True, slots=True)
class UnifiClientRow:
    """One unifi_clients row. INTEGER 0/1 columns are exposed as bool."""

    mac: str
    ip: str | None
    hostname: str | None
    name: str | None
    oui: str | None
    network: str | None
    ap_mac: str | None
    sw_mac: str | None
    sw_port: int | None
    use_fixedip: bool
    fixed_ip: str | None
    online: bool
    is_host: bool
    first_seen: str
    last_seen: str
    lease_expiry: str | None


# Column list reused by all SELECT * reads (explicit order → stable Row mapping).
_CLIENT_COLUMNS = (
    "mac, ip, hostname, name, oui, network, ap_mac, sw_mac, sw_port, "
    "use_fixedip, fixed_ip, online, is_host, first_seen, last_seen, lease_expiry"
)


def _map_client_row(r: Row[Any]) -> UnifiClientRow:
    """Map a SELECT-_CLIENT_COLUMNS Row to a UnifiClientRow.

    Row attribute access is typed Any (column-list SELECT), so str()/bool()/int()
    coercion is pyright-strict-clean without an ignore (mirrors ProbeTargetRow).
    """
    return UnifiClientRow(
        mac=str(r.mac),
        ip=None if r.ip is None else str(r.ip),
        hostname=None if r.hostname is None else str(r.hostname),
        name=None if r.name is None else str(r.name),
        oui=None if r.oui is None else str(r.oui),
        network=None if r.network is None else str(r.network),
        ap_mac=None if r.ap_mac is None else str(r.ap_mac),
        sw_mac=None if r.sw_mac is None else str(r.sw_mac),
        sw_port=None if r.sw_port is None else int(r.sw_port),
        use_fixedip=bool(r.use_fixedip),
        fixed_ip=None if r.fixed_ip is None else str(r.fixed_ip),
        online=bool(r.online),
        is_host=bool(r.is_host),
        first_seen=str(r.first_seen),
        last_seen=str(r.last_seen),
        lease_expiry=None if r.lease_expiry is None else str(r.lease_expiry),
    )


class UnifiClientRepo:
    """Repository for unifi_clients + unifi_client_observations."""

    def __init__(self, repo: SqliteRepository) -> None:
        self._repo: SqliteRepository = repo

    # ---- Static *_conn helpers usable inside repo.transaction() ----

    @staticmethod
    async def upsert_client_conn(  # noqa: PLR0913 -- one keyword arg per registry column
        conn: AsyncConnection,
        *,
        mac: str,
        ip: str | None,
        hostname: str | None,
        name: str | None,
        oui: str | None,
        network: str | None,
        ap_mac: str | None,
        sw_mac: str | None,
        sw_port: int | None,
        use_fixedip: bool,
        fixed_ip: str | None,
        online: bool,
        first_seen: str,
        last_seen: str,
    ) -> None:
        """Insert or update a client by MAC.

        On INSERT, first_seen = the passed first_seen (the record's true epoch->ISO
        first_seen; STAGE-007-004). On CONFLICT(mac), updates all mutable identity/
        connection fields + last_seen, PRESERVES first_seen (it is NOT in DO UPDATE
        SET), and does NOT touch is_host or lease_expiry (those are owned by
        ensure_host_row / promote_to_host_conn / STAGE-007-012 respectively).
        """
        await conn.execute(
            text(
                "INSERT INTO unifi_clients "
                "  (mac, ip, hostname, name, oui, network, ap_mac, sw_mac, sw_port, "
                "   use_fixedip, fixed_ip, online, first_seen, last_seen) "
                "VALUES "
                "  (:mac, :ip, :hostname, :name, :oui, :network, :ap_mac, :sw_mac, "
                "   :sw_port, :use_fixedip, :fixed_ip, :online, :first_seen, :last_seen) "
                "ON CONFLICT(mac) DO UPDATE SET "
                "  ip = excluded.ip, "
                "  hostname = excluded.hostname, "
                "  name = excluded.name, "
                "  oui = excluded.oui, "
                "  network = excluded.network, "
                "  ap_mac = excluded.ap_mac, "
                "  sw_mac = excluded.sw_mac, "
                "  sw_port = excluded.sw_port, "
                "  use_fixedip = excluded.use_fixedip, "
                "  fixed_ip = excluded.fixed_ip, "
                "  online = excluded.online, "
                "  last_seen = excluded.last_seen"
            ),
            {
                "mac": mac,
                "ip": ip,
                "hostname": hostname,
                "name": name,
                "oui": oui,
                "network": network,
                "ap_mac": ap_mac,
                "sw_mac": sw_mac,
                "sw_port": sw_port,
                "use_fixedip": 1 if use_fixedip else 0,
                "fixed_ip": fixed_ip,
                "online": 1 if online else 0,
                "first_seen": first_seen,
                "last_seen": last_seen,
            },
        )

    @staticmethod
    async def append_observation_conn(
        conn: AsyncConnection,
        *,
        mac: str,
        ip: str,
        observed_at: str,
        cutoff: str,
    ) -> None:
        """Record an IP<->MAC observation as a span, then prune old spans.

        The first sighting of (mac, ip) sets first_seen = last_seen = observed_at.
        Repeat sightings of the same (mac, ip) COLLAPSE into the existing row,
        extending last_seen (first_seen is preserved). After the append, spans
        whose last_seen < cutoff are deleted (inline retention).
        """
        await conn.execute(
            text(
                "INSERT INTO unifi_client_observations (mac, ip, first_seen, last_seen) "
                "VALUES (:mac, :ip, :observed_at, :observed_at) "
                "ON CONFLICT(mac, ip) DO UPDATE SET last_seen = excluded.last_seen"
            ),
            {"mac": mac, "ip": ip, "observed_at": observed_at},
        )
        await conn.execute(
            text("DELETE FROM unifi_client_observations WHERE last_seen < :cutoff"),
            {"cutoff": cutoff},
        )

    @staticmethod
    async def promote_to_host_conn(
        conn: AsyncConnection,
        *,
        real_mac: str,
        host_ip: str,
    ) -> bool:
        """Reconcile the sentinel host row into a real-MAC row (STAGE-007-004).

        The host's first-class row is seeded by ensure_host_row as a sentinel keyed
        mac = f"host:{host_ip}" (is_host=1). When the active-client collector observes
        the host's REAL MAC online at host_ip, this merges the sentinel into the
        real-MAC row: sets is_host=1 on the real-MAC row, carries the EARLIER first_seen
        (lexicographic MIN of the two ISO timestamps), then DELETES the sentinel.

        Returns True when a reconcile happened, False when there is nothing to do
        (sentinel absent — already reconciled or never seeded). Idempotent: once the
        sentinel is deleted, later calls return False.

        The caller MUST have already upserted the real-MAC row (so it exists for the
        UPDATE). Guards real_mac == sentinel_mac (an impossible real MAC literally equal
        to "host:<ip>") and returns False, never deleting the row it would promote.
        """
        sentinel_mac = f"host:{host_ip}"
        if real_mac == sentinel_mac:
            return False
        sentinel_row = await conn.execute(
            text("SELECT first_seen FROM unifi_clients WHERE mac = :mac"),
            {"mac": sentinel_mac},
        )
        sentinel: Row[Any] | None = sentinel_row.fetchone()
        if sentinel is None:
            return False
        real_row = await conn.execute(
            text("SELECT first_seen FROM unifi_clients WHERE mac = :mac"),
            {"mac": real_mac},
        )
        real: Row[Any] | None = real_row.fetchone()
        if real is None:
            return False
        merged_first_seen = min(str(sentinel.first_seen), str(real.first_seen))
        await conn.execute(
            text("UPDATE unifi_clients SET is_host = 1, first_seen = :fs WHERE mac = :mac"),
            {"fs": merged_first_seen, "mac": real_mac},
        )
        await conn.execute(
            text("DELETE FROM unifi_clients WHERE mac = :mac"),
            {"mac": sentinel_mac},
        )
        return True

    # ---- Instance reads ----

    async def find_mac_by_ip_at(self, ip: str, at: str) -> str | None:
        """Return the MAC whose IP-span covers `at` (the EPIC-006 join contract).

        Picks the span with first_seen <= at, breaking ties by most-recent
        last_seen. Returns None when no span matches.
        """
        row = await self._repo.fetch_one(
            text(
                "SELECT mac FROM unifi_client_observations "
                "WHERE ip = :ip AND first_seen <= :at "
                "ORDER BY last_seen DESC LIMIT 1"
            ),
            {"ip": ip, "at": at},
        )
        if row is None:
            return None
        return str(row.mac)

    async def get_client(self, mac: str) -> UnifiClientRow | None:
        """Fetch one client by MAC, or None."""
        row = await self._repo.fetch_one(
            text(f"SELECT {_CLIENT_COLUMNS} FROM unifi_clients WHERE mac = :mac"),
            {"mac": mac},
        )
        if row is None:
            return None
        return _map_client_row(row)

    async def list_clients(self) -> list[UnifiClientRow]:
        """List all clients ordered by last_seen DESC (most recent first)."""
        rows = await self._repo.fetch_all(
            text(f"SELECT {_CLIENT_COLUMNS} FROM unifi_clients ORDER BY last_seen DESC")
        )
        return [_map_client_row(r) for r in rows]

    async def ensure_host_row(self, host_ip: str, host_mac: str | None = None) -> None:
        """Idempotently guarantee a first-class host row (is_host=1) exists.

        If any is_host=1 row already exists, this is a no-op. Otherwise inserts a
        sentinel row keyed mac = f"host:{host_ip}" with is_host=1, ip=host_ip,
        online=0, first_seen=last_seen=utc_now_iso().

        SCAFFOLDING: `host_mac` is accepted for forward-compat with the
        sentinel -> real-MAC reconciliation in STAGE-007-004 / STAGE-007-007 (the
        active-client collector merges the sentinel into the real-MAC row). It is
        intentionally UNUSED in this stage; the sentinel path is the only path.
        """
        _ = host_mac  # SCAFFOLDING: reserved for STAGE-007-004/007 real-MAC merge.
        existing = await self._repo.fetch_one(
            text("SELECT mac FROM unifi_clients WHERE is_host = 1 LIMIT 1")
        )
        if existing is not None:
            return
        now = utc_now_iso()
        await self._repo.execute(
            text(
                "INSERT INTO unifi_clients "
                "  (mac, ip, use_fixedip, online, is_host, first_seen, last_seen) "
                "VALUES (:mac, :ip, 0, 0, 1, :now, :now)"
            ),
            {"mac": f"host:{host_ip}", "ip": host_ip, "now": now},
        )


__all__ = [
    "UnifiClientRepo",
    "UnifiClientRow",
]
