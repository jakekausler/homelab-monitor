"""Tests for UnifiClientRepo: unifi_clients registry + observation spans.

Covers upsert (insert + update + first_seen preservation + bool->int + is_host/
lease_expiry untouched), observation span append + collapse + inline prune,
find_mac_by_ip_at (match + None), get_client / list_clients (mapping + order +
None), and ensure_host_row (insert + idempotent no-op). All timestamps are
RELATIVE (now +/- delta), never hard-coded absolute dates.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.db.repositories.unifi_clients_repository import (
    UnifiClientRepo,
    UnifiClientRow,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

_HOST_IP = "192.168.2.148"
_MAC = "aa:bb:cc:dd:ee:ff"
_OTHER_MAC = "11:22:33:44:55:66"
_IP = "192.168.2.50"
_SW_PORT = 7


def _iso(dt: datetime) -> str:
    """ISO-8601 UTC string for a datetime (matches utc_now_iso shape)."""
    return dt.isoformat()


def _now() -> datetime:
    return datetime.now(tz=UTC)


# ---- upsert_client_conn ----


@pytest.mark.asyncio
async def test_upsert_client_inserts_new_row(repo: SqliteRepository) -> None:
    """A new client is inserted; first_seen == last_seen == given last_seen."""
    client_repo = UnifiClientRepo(repo)
    seen = utc_now_iso()

    async with repo.transaction() as conn:
        await client_repo.upsert_client_conn(
            conn,
            mac=_MAC,
            ip=_IP,
            hostname="laptop",
            name="My Laptop",
            oui="Intel",
            network="LAN",
            ap_mac="ap:00",
            sw_mac="sw:00",
            sw_port=_SW_PORT,
            use_fixedip=False,
            fixed_ip=None,
            online=True,
            first_seen=seen,
            last_seen=seen,
        )

    row = await client_repo.get_client(_MAC)
    assert row is not None
    assert row.mac == _MAC
    assert row.ip == _IP
    assert row.hostname == "laptop"
    assert row.name == "My Laptop"
    assert row.oui == "Intel"
    assert row.network == "LAN"
    assert row.ap_mac == "ap:00"
    assert row.sw_mac == "sw:00"
    assert row.sw_port == _SW_PORT
    assert row.use_fixedip is False  # online=True->1, use_fixedip=False->0 conversion
    assert row.fixed_ip is None
    assert row.online is True
    assert row.is_host is False
    assert row.first_seen == seen
    assert row.last_seen == seen
    assert row.lease_expiry is None


@pytest.mark.asyncio
async def test_upsert_client_updates_preserving_first_seen(repo: SqliteRepository) -> None:
    """Second upsert updates mutable fields + last_seen, preserves first_seen,
    leaves is_host + lease_expiry untouched."""
    client_repo = UnifiClientRepo(repo)
    first_seen = _iso(_now() - timedelta(hours=2))

    async with repo.transaction() as conn:
        await client_repo.upsert_client_conn(
            conn,
            mac=_MAC,
            ip="192.168.2.10",
            hostname="old-host",
            name=None,
            oui=None,
            network=None,
            ap_mac=None,
            sw_mac=None,
            sw_port=None,
            use_fixedip=False,
            fixed_ip=None,
            online=False,
            first_seen=first_seen,
            last_seen=first_seen,
        )

    # Manually mark is_host=1 + set a lease_expiry so we can assert they survive.
    await repo.execute(
        text("UPDATE unifi_clients SET is_host = 1, lease_expiry = :le WHERE mac = :mac"),
        {"le": "2099-01-01T00:00:00+00:00", "mac": _MAC},
    )

    later = _iso(_now())
    async with repo.transaction() as conn:
        await client_repo.upsert_client_conn(
            conn,
            mac=_MAC,
            ip="192.168.2.20",
            hostname="new-host",
            name="Renamed",
            oui="Dell",
            network="IoT",
            ap_mac="ap:99",
            sw_mac="sw:99",
            sw_port=_SW_PORT,
            use_fixedip=True,
            fixed_ip="192.168.2.20",
            online=True,
            first_seen=later,
            last_seen=later,
        )

    row = await client_repo.get_client(_MAC)
    assert row is not None
    # Mutable fields updated.
    assert row.ip == "192.168.2.20"
    assert row.hostname == "new-host"
    assert row.name == "Renamed"
    assert row.oui == "Dell"
    assert row.network == "IoT"
    assert row.ap_mac == "ap:99"
    assert row.sw_mac == "sw:99"
    assert row.sw_port == _SW_PORT
    assert row.use_fixedip is True
    assert row.fixed_ip == "192.168.2.20"
    assert row.online is True
    # last_seen bumped; first_seen preserved.
    assert row.last_seen == later
    assert row.first_seen == first_seen
    # is_host + lease_expiry untouched by the upsert.
    assert row.is_host is True
    assert row.lease_expiry == "2099-01-01T00:00:00+00:00"


# ---- append_observation_conn ----


@pytest.mark.asyncio
async def test_append_observation_first_sighting(repo: SqliteRepository) -> None:
    """First observation of (mac, ip): one span with first_seen == last_seen."""
    client_repo = UnifiClientRepo(repo)
    observed = utc_now_iso()
    cutoff = _iso(_now() - timedelta(days=90))

    async with repo.transaction() as conn:
        await client_repo.append_observation_conn(
            conn, mac=_MAC, ip=_IP, observed_at=observed, cutoff=cutoff
        )

    row = await repo.fetch_one(
        text(
            "SELECT mac, ip, first_seen, last_seen FROM unifi_client_observations "
            "WHERE mac = :mac AND ip = :ip"
        ),
        {"mac": _MAC, "ip": _IP},
    )
    assert row is not None
    assert row.first_seen == observed
    assert row.last_seen == observed


@pytest.mark.asyncio
async def test_append_observation_collapses_span(repo: SqliteRepository) -> None:
    """A later observation of the same (mac, ip) extends last_seen, preserves
    first_seen, and does not create a second row."""
    client_repo = UnifiClientRepo(repo)
    first = _iso(_now() - timedelta(hours=1))
    later = _iso(_now())
    cutoff = _iso(_now() - timedelta(days=90))

    async with repo.transaction() as conn:
        await client_repo.append_observation_conn(
            conn, mac=_MAC, ip=_IP, observed_at=first, cutoff=cutoff
        )
        await client_repo.append_observation_conn(
            conn, mac=_MAC, ip=_IP, observed_at=later, cutoff=cutoff
        )

    rows = await repo.fetch_all(
        text(
            "SELECT first_seen, last_seen FROM unifi_client_observations "
            "WHERE mac = :mac AND ip = :ip"
        ),
        {"mac": _MAC, "ip": _IP},
    )
    assert len(rows) == 1  # span collapsed, not duplicated
    assert rows[0].first_seen == first
    assert rows[0].last_seen == later


@pytest.mark.asyncio
async def test_append_observation_prunes_old_spans(repo: SqliteRepository) -> None:
    """The inline prune deletes spans whose last_seen < cutoff; fresh ones remain."""
    client_repo = UnifiClientRepo(repo)
    # An OLD span (last_seen ~100 days ago) seeded directly.
    old_seen = _iso(_now() - timedelta(days=100))
    await repo.execute(
        text(
            "INSERT INTO unifi_client_observations (mac, ip, first_seen, last_seen) "
            "VALUES (:mac, :ip, :ts, :ts)"
        ),
        {"mac": _OTHER_MAC, "ip": "192.168.2.99", "ts": old_seen},
    )

    # Cutoff = now - 90 days; old span (100d) < cutoff, fresh span (now) >= cutoff.
    cutoff = _iso(_now() - timedelta(days=90))
    fresh = utc_now_iso()
    async with repo.transaction() as conn:
        await client_repo.append_observation_conn(
            conn, mac=_MAC, ip=_IP, observed_at=fresh, cutoff=cutoff
        )

    # Old span pruned.
    old_row = await repo.fetch_one(
        text("SELECT mac FROM unifi_client_observations WHERE mac = :mac AND ip = :ip"),
        {"mac": _OTHER_MAC, "ip": "192.168.2.99"},
    )
    assert old_row is None
    # Fresh span remains.
    fresh_row = await repo.fetch_one(
        text("SELECT mac FROM unifi_client_observations WHERE mac = :mac AND ip = :ip"),
        {"mac": _MAC, "ip": _IP},
    )
    assert fresh_row is not None


# ---- find_mac_by_ip_at ----


@pytest.mark.asyncio
async def test_find_mac_by_ip_at_returns_covering_span(repo: SqliteRepository) -> None:
    """Two macs reused the same ip at different times; the query returns the mac
    whose span covers `at` (first_seen <= at, most-recent last_seen)."""
    client_repo = UnifiClientRepo(repo)
    cutoff = _iso(_now() - timedelta(days=90))
    early = _iso(_now() - timedelta(hours=5))
    recent_first = _iso(_now() - timedelta(hours=1))
    recent_last = _iso(_now())

    async with repo.transaction() as conn:
        # MAC A held the IP earlier.
        await client_repo.append_observation_conn(
            conn, mac=_OTHER_MAC, ip=_IP, observed_at=early, cutoff=cutoff
        )
        # MAC B holds the IP more recently.
        await client_repo.append_observation_conn(
            conn, mac=_MAC, ip=_IP, observed_at=recent_first, cutoff=cutoff
        )
        await client_repo.append_observation_conn(
            conn, mac=_MAC, ip=_IP, observed_at=recent_last, cutoff=cutoff
        )

    # `at` = now → the most-recent-last_seen span (MAC B) wins.
    found = await client_repo.find_mac_by_ip_at(_IP, _iso(_now()))
    assert found == _MAC


@pytest.mark.asyncio
async def test_find_mac_by_ip_at_returns_none_when_no_span(repo: SqliteRepository) -> None:
    """No span for the ip (or `at` before any first_seen) → None."""
    client_repo = UnifiClientRepo(repo)
    # Unknown IP entirely.
    assert await client_repo.find_mac_by_ip_at("10.0.0.1", _iso(_now())) is None

    # IP exists but `at` is before its first_seen.
    cutoff = _iso(_now() - timedelta(days=90))
    first = _iso(_now() - timedelta(hours=1))
    async with repo.transaction() as conn:
        await client_repo.append_observation_conn(
            conn, mac=_MAC, ip=_IP, observed_at=first, cutoff=cutoff
        )
    before = _iso(_now() - timedelta(hours=3))
    assert await client_repo.find_mac_by_ip_at(_IP, before) is None


# ---- get_client / list_clients ----


@pytest.mark.asyncio
async def test_get_client_returns_none_for_unknown(repo: SqliteRepository) -> None:
    """get_client on an unknown MAC → None."""
    client_repo = UnifiClientRepo(repo)
    assert await client_repo.get_client("no:such:mac") is None


@pytest.mark.asyncio
async def test_get_client_maps_booleans_and_nullables(repo: SqliteRepository) -> None:
    """get_client maps 0/1 INTEGER columns to bool and NULL columns to None."""
    client_repo = UnifiClientRepo(repo)
    seen = utc_now_iso()
    async with repo.transaction() as conn:
        await client_repo.upsert_client_conn(
            conn,
            mac=_MAC,
            ip=None,
            hostname=None,
            name=None,
            oui=None,
            network=None,
            ap_mac=None,
            sw_mac=None,
            sw_port=None,
            use_fixedip=False,
            fixed_ip=None,
            online=False,
            first_seen=seen,
            last_seen=seen,
        )

    row = await client_repo.get_client(_MAC)
    assert isinstance(row, UnifiClientRow)
    assert row.ip is None
    assert row.hostname is None
    assert row.sw_port is None
    assert row.use_fixedip is False
    assert row.online is False
    assert row.is_host is False
    assert row.lease_expiry is None


@pytest.mark.asyncio
async def test_list_clients_orders_by_last_seen_desc(repo: SqliteRepository) -> None:
    """list_clients returns all rows ordered by last_seen DESC."""
    client_repo = UnifiClientRepo(repo)
    older = _iso(_now() - timedelta(hours=2))
    newer = _iso(_now())

    async with repo.transaction() as conn:
        await client_repo.upsert_client_conn(
            conn,
            mac=_OTHER_MAC,
            ip=None,
            hostname=None,
            name=None,
            oui=None,
            network=None,
            ap_mac=None,
            sw_mac=None,
            sw_port=None,
            use_fixedip=False,
            fixed_ip=None,
            online=False,
            first_seen=older,
            last_seen=older,
        )
        await client_repo.upsert_client_conn(
            conn,
            mac=_MAC,
            ip=None,
            hostname=None,
            name=None,
            oui=None,
            network=None,
            ap_mac=None,
            sw_mac=None,
            sw_port=None,
            use_fixedip=False,
            fixed_ip=None,
            online=False,
            first_seen=newer,
            last_seen=newer,
        )

    rows = await client_repo.list_clients()
    assert [r.mac for r in rows] == [_MAC, _OTHER_MAC]  # newer last_seen first


# ---- ensure_host_row ----


@pytest.mark.asyncio
async def test_ensure_host_row_inserts_sentinel(repo: SqliteRepository) -> None:
    """On an empty table, ensure_host_row inserts a host:<ip> sentinel with is_host=1."""
    client_repo = UnifiClientRepo(repo)
    await client_repo.ensure_host_row(_HOST_IP)

    row = await client_repo.get_client(f"host:{_HOST_IP}")
    assert row is not None
    assert row.is_host is True
    assert row.ip == _HOST_IP
    assert row.online is False
    # first_seen == last_seen (both set to utc_now_iso() at insert).
    assert row.first_seen == row.last_seen


@pytest.mark.asyncio
async def test_ensure_host_row_idempotent(repo: SqliteRepository) -> None:
    """Called twice → still exactly one is_host=1 row (no-op on the second call)."""
    client_repo = UnifiClientRepo(repo)
    await client_repo.ensure_host_row(_HOST_IP)
    await client_repo.ensure_host_row(_HOST_IP)

    rows = await repo.fetch_all(text("SELECT mac FROM unifi_clients WHERE is_host = 1"))
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_ensure_host_row_noop_when_host_already_present(
    repo: SqliteRepository,
) -> None:
    """If a DIFFERENT is_host=1 row already exists (e.g. a real-MAC host row),
    ensure_host_row does NOT add a sentinel."""
    client_repo = UnifiClientRepo(repo)
    seen = utc_now_iso()
    # Seed a real-MAC host row directly with is_host=1.
    await repo.execute(
        text(
            "INSERT INTO unifi_clients "
            "  (mac, use_fixedip, online, is_host, first_seen, last_seen) "
            "VALUES (:mac, 0, 0, 1, :now, :now)"
        ),
        {"mac": _MAC, "now": seen},
    )

    # host_mac is accepted but unused (SCAFFOLDING); pass it to exercise the param.
    await client_repo.ensure_host_row(_HOST_IP, host_mac=_MAC)

    # No sentinel row created; still exactly one is_host=1 row (the real-MAC one).
    host_rows = await repo.fetch_all(text("SELECT mac FROM unifi_clients WHERE is_host = 1"))
    assert len(host_rows) == 1
    assert host_rows[0].mac == _MAC
    # Sentinel specifically absent.
    assert await client_repo.get_client(f"host:{_HOST_IP}") is None
