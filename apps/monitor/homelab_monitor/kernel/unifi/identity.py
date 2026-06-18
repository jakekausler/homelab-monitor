"""Identity-upsert helper: stat/sta + stat/alluser -> unifi_clients registry.

STAGE-007-004. A PURE transform over live Unifi classic-API client records:
maps `stat/sta` (currently-online, full fields, current `ip`) and `stat/alluser`
(all known incl. offline, sparse, `last_ip` not `ip`, no connection fields) into
the STAGE-007-003 `unifi_clients` registry, appending time-stamped IP<->MAC
observations and reconciling the first-class host row.

NOT a registered collector — STAGE-007-007 builds that and CALLS upsert_identity.

Design (STAGE-007-004 D1-D4):
  * Caller-owns-transaction: upsert_identity takes an open AsyncConnection so the
    collector can bundle these writes + its self-metrics atomically (composes with
    the 003 *_conn statics).
  * Time is INJECTED: `now` (ISO-8601 UTC) + `observation_cutoff` (ISO-8601 UTC) are
    parameters. This module NEVER calls utc_now (time-rot rule + test determinism).
  * `first_seen`/`last_seen` arrive as epoch INTEGER seconds; converted to ISO via
    datetime.fromtimestamp(v, tz=UTC).isoformat(). Missing/non-int -> injected `now`.
  * Presence in stat/sta == online. Two-pass merge: PASS 1 (sta, online) upserts +
    appends an observation + reconciles host; PASS 2 (alluser) upserts ONLY macs not
    already seen in PASS 1 as offline (no observation, no host reconcile).
  * The helper applies NO cardinality cap — the registry is the complete inventory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from homelab_monitor.kernel.db.repositories.unifi_clients_repository import UnifiClientRepo


@dataclass(frozen=True, slots=True)
class ExtractedClient:
    """One client record after isinstance-guarded extraction + epoch->ISO.

    `online` is set by the caller per-pass (True for stat/sta, False for
    stat/alluser). `ip` is the source-appropriate address: stat/sta's current `ip`
    when online, stat/alluser's `last_ip` when offline. `first_seen_iso` /
    `last_seen_iso` are already converted from epoch seconds (or the injected `now`
    fallback when the source field was missing/non-int).
    """

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
    first_seen_iso: str
    last_seen_iso: str


@dataclass(frozen=True, slots=True)
class UpsertResult:
    """Structured counts for the collector (STAGE-007-007) to emit as self-metrics."""

    clients_upserted: int
    observations_appended: int
    hosts_reconciled: int
    skipped: int


def _str_or_none(record: dict[str, object], key: str) -> str | None:
    """Return record[key] if it is a str, else None."""
    value = record.get(key)
    if isinstance(value, str):
        return value
    return None


def _int_or_none(record: dict[str, object], key: str) -> int | None:
    """Return record[key] as int if it is an int (and not a bool), else None.

    `bool` is a subclass of `int`; exclude it so a stray True/False is not coerced.
    """
    value = record.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _bool_or_false(record: dict[str, object], key: str) -> bool:
    """Return record[key] if it is a bool, else False."""
    value = record.get(key)
    if isinstance(value, bool):
        return value
    return False


def _epoch_to_iso(record: dict[str, object], key: str, now: str) -> str:
    """Convert record[key] (epoch INTEGER seconds) to ISO-8601 UTC.

    Falls back to the injected `now` when the field is missing or non-int.
    """
    seconds = _int_or_none(record, key)
    if seconds is None:
        return now
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat()


def _extract(record: dict[str, object], *, is_online: bool, now: str) -> ExtractedClient | None:
    """Extract a registry-ready ExtractedClient from a raw classic-API record.

    Returns None (-> caller counts `skipped`) when `mac` is absent or not a str.
    The ip field is source-dependent: stat/sta (online) carries the current `ip`;
    stat/alluser (offline) carries `last_ip` instead. Every other field is
    isinstance-narrowed; epoch first_seen/last_seen are converted (or fall back to
    `now`); `use_fixedip` defaults False; `sw_port` is int|None.
    """
    mac = _str_or_none(record, "mac")
    if mac is None:
        return None
    ip = _str_or_none(record, "ip") if is_online else _str_or_none(record, "last_ip")
    return ExtractedClient(
        mac=mac,
        ip=ip,
        hostname=_str_or_none(record, "hostname"),
        name=_str_or_none(record, "name"),
        oui=_str_or_none(record, "oui"),
        network=_str_or_none(record, "network"),
        ap_mac=_str_or_none(record, "ap_mac"),
        sw_mac=_str_or_none(record, "sw_mac"),
        sw_port=_int_or_none(record, "sw_port"),
        use_fixedip=_bool_or_false(record, "use_fixedip"),
        fixed_ip=_str_or_none(record, "fixed_ip"),
        online=is_online,
        first_seen_iso=_epoch_to_iso(record, "first_seen", now),
        last_seen_iso=_epoch_to_iso(record, "last_seen", now),
    )


async def _upsert(conn: AsyncConnection, ec: ExtractedClient) -> None:
    """Upsert one ExtractedClient into the registry via the 003 repo static."""
    await UnifiClientRepo.upsert_client_conn(
        conn,
        mac=ec.mac,
        ip=ec.ip,
        hostname=ec.hostname,
        name=ec.name,
        oui=ec.oui,
        network=ec.network,
        ap_mac=ec.ap_mac,
        sw_mac=ec.sw_mac,
        sw_port=ec.sw_port,
        use_fixedip=ec.use_fixedip,
        fixed_ip=ec.fixed_ip,
        online=ec.online,
        first_seen=ec.first_seen_iso,
        last_seen=ec.last_seen_iso,
    )


async def upsert_identity(  # noqa: PLR0913 -- one keyword arg per injected source/clock input
    conn: AsyncConnection,
    *,
    stat_sta: list[dict[str, object]],
    stat_alluser: list[dict[str, object]],
    host_lan_ip: str,
    observation_cutoff: str,
    now: str,
) -> UpsertResult:
    """Merge stat/sta + stat/alluser into the unifi_clients registry.

    PASS 1 (stat_sta, online): upsert each client (online=True), append an IP<->MAC
    observation when it has a current ip, and reconcile the first-class host row when
    its ip == host_lan_ip. PASS 2 (stat_alluser): for macs NOT seen in PASS 1, upsert
    them offline (ip = last_ip) with NO observation and NO host reconcile.

    Returns UpsertResult with clients_upserted / observations_appended /
    hosts_reconciled / skipped counts.
    """
    clients_upserted = 0
    observations_appended = 0
    hosts_reconciled = 0
    skipped = 0
    seen: set[str] = set()

    # PASS 1 — stat/sta (online).
    for record in stat_sta:
        ec = _extract(record, is_online=True, now=now)
        if ec is None:
            skipped += 1
            continue
        seen.add(ec.mac)
        await _upsert(conn, ec)
        clients_upserted += 1
        if ec.ip is not None:
            await UnifiClientRepo.append_observation_conn(
                conn,
                mac=ec.mac,
                ip=ec.ip,
                observed_at=ec.last_seen_iso,
                cutoff=observation_cutoff,
            )
            observations_appended += 1
        if ec.ip == host_lan_ip:
            reconciled = await UnifiClientRepo.promote_to_host_conn(
                conn, real_mac=ec.mac, host_ip=host_lan_ip
            )
            if reconciled:
                hosts_reconciled += 1

    # PASS 2 — stat/alluser (known; offline unless already seen online).
    for record in stat_alluser:
        ec = _extract(record, is_online=False, now=now)
        if ec is None:
            skipped += 1
            continue
        if ec.mac in seen:
            continue
        await _upsert(conn, ec)
        clients_upserted += 1

    return UpsertResult(
        clients_upserted=clients_upserted,
        observations_appended=observations_appended,
        hosts_reconciled=hosts_reconciled,
        skipped=skipped,
    )


__all__ = [
    "ExtractedClient",
    "UpsertResult",
    "upsert_identity",
]
