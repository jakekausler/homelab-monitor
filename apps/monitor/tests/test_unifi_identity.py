"""Tests for the identity-upsert helper (STAGE-007-004).

Covers _extract (every guard branch + epoch->ISO + online/offline ip source),
upsert_identity (PASS 1 sta upserts/observations/host-reconcile, PASS 2 alluser
offline-only + no-downgrade, all four counts), and promote_to_host_conn directly.

Timestamps are RELATIVE to a fixed injected `now`; epoch fixtures are int seconds
computed relative to that now. The helper never calls utc_now — `now` and the
observation cutoff are injected.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.db.repositories.unifi_clients_repository import UnifiClientRepo
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.unifi.identity import (
    ExtractedClient,
    UpsertResult,
    _extract,  # pyright: ignore[reportPrivateUsage]
    upsert_identity,
)

_HOST_IP = "192.168.2.148"
_MAC = "aa:bb:cc:dd:ee:ff"
_OTHER_MAC = "11:22:33:44:55:66"
_WIRELESS_MAC = "22:33:44:55:66:77"
_HOST_MAC = "de:ad:be:ef:00:01"

# Fixed injected clock for determinism. The helper treats `now` as an opaque ISO
# string; the epoch fixtures below are seconds relative to this same instant.
_NOW_DT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_NOW_ISO = _NOW_DT.isoformat()
_NOW_EPOCH = int(_NOW_DT.timestamp())
# observation cutoff = now - 90 days (the configured retention default).
_CUTOFF_ISO = (_NOW_DT - timedelta(days=90)).isoformat()


# Epoch helpers: an int N seconds before _NOW.
def _epoch_ago(seconds: int) -> int:
    return _NOW_EPOCH - seconds


def _iso_of(epoch: int) -> str:
    """The ISO-8601 UTC string the helper would produce for an epoch int."""
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _sta_record(
    mac: str,
    ip: str,
    *,
    first_seen: int,
    last_seen: int,
) -> dict[str, object]:
    """A wired online stat/sta record (full fields)."""
    return {
        "mac": mac,
        "ip": ip,
        "hostname": "laptop",
        "name": "My Laptop",
        "oui": "Intel",
        "network": "LAN",
        "is_wired": True,
        "sw_mac": "sw:00",
        "sw_port": 7,
        "use_fixedip": False,
        "fixed_ip": None,
        "first_seen": first_seen,
        "last_seen": last_seen,
    }


def _alluser_record(
    mac: str,
    last_ip: str,
    *,
    first_seen: int,
    last_seen: int,
) -> dict[str, object]:
    """A sparse stat/alluser record (offline; last_ip, no connection fields)."""
    return {
        "mac": mac,
        "last_ip": last_ip,
        "hostname": "known-device",
        "is_wired": True,
        "use_fixedip": False,
        "fixed_ip": None,
        "first_seen": first_seen,
        "last_seen": last_seen,
    }


# ---- _extract ----


def test_extract_valid_sta_record_wired() -> None:
    """A valid wired sta record -> ExtractedClient, epoch->ISO, online=True, wired fields."""
    fs = _epoch_ago(7200)
    ls = _epoch_ago(60)
    rec = _sta_record(_MAC, "192.168.2.50", first_seen=fs, last_seen=ls)
    ec = _extract(rec, is_online=True, now=_NOW_ISO)
    assert ec is not None
    assert ec.mac == _MAC
    assert ec.ip == "192.168.2.50"
    assert ec.hostname == "laptop"
    assert ec.name == "My Laptop"
    assert ec.oui == "Intel"
    assert ec.network == "LAN"
    assert ec.sw_mac == "sw:00"
    assert ec.sw_port == 7  # noqa: PLR2004
    assert ec.ap_mac is None
    assert ec.use_fixedip is False
    assert ec.fixed_ip is None
    assert ec.online is True
    assert ec.first_seen_iso == _iso_of(fs)
    assert ec.last_seen_iso == _iso_of(ls)


def test_extract_valid_wireless_record() -> None:
    """A wireless sta record narrows ap_mac (and leaves sw_* None)."""
    fs = _epoch_ago(3600)
    ls = _epoch_ago(30)
    rec: dict[str, object] = {
        "mac": _WIRELESS_MAC,
        "ip": "192.168.2.60",
        "is_wired": False,
        "ap_mac": "ap:99",
        "essid": "HomeWifi",
        "use_fixedip": True,
        "fixed_ip": "192.168.2.60",
        "first_seen": fs,
        "last_seen": ls,
    }
    ec = _extract(rec, is_online=True, now=_NOW_ISO)
    assert ec is not None
    assert ec.ap_mac == "ap:99"
    assert ec.sw_mac is None
    assert ec.sw_port is None
    assert ec.use_fixedip is True
    assert ec.fixed_ip == "192.168.2.60"


def test_extract_missing_mac_returns_none() -> None:
    """A record without `mac` -> None (skip)."""
    rec: dict[str, object] = {"ip": "192.168.2.50", "first_seen": _epoch_ago(10)}
    assert _extract(rec, is_online=True, now=_NOW_ISO) is None


def test_extract_non_str_mac_returns_none() -> None:
    """A record whose `mac` is not a str -> None (skip)."""
    rec: dict[str, object] = {"mac": 12345, "ip": "192.168.2.50"}
    assert _extract(rec, is_online=True, now=_NOW_ISO) is None


def test_extract_missing_timestamps_fall_back_to_now() -> None:
    """Missing first_seen/last_seen fall back to the injected `now`."""
    rec: dict[str, object] = {"mac": _MAC, "ip": "192.168.2.50"}
    ec = _extract(rec, is_online=True, now=_NOW_ISO)
    assert ec is not None
    assert ec.first_seen_iso == _NOW_ISO
    assert ec.last_seen_iso == _NOW_ISO


def test_extract_non_int_timestamp_falls_back_to_now() -> None:
    """A non-int (string) first_seen falls back to `now`; a bool is also rejected."""
    rec: dict[str, object] = {
        "mac": _MAC,
        "ip": "192.168.2.50",
        "first_seen": "not-an-int",
        "last_seen": True,  # bool is excluded from the int guard
    }
    ec = _extract(rec, is_online=True, now=_NOW_ISO)
    assert ec is not None
    assert ec.first_seen_iso == _NOW_ISO
    assert ec.last_seen_iso == _NOW_ISO


def test_extract_missing_use_fixedip_defaults_false() -> None:
    """Missing/non-bool use_fixedip -> False."""
    rec: dict[str, object] = {"mac": _MAC, "ip": "192.168.2.50"}
    ec = _extract(rec, is_online=True, now=_NOW_ISO)
    assert ec is not None
    assert ec.use_fixedip is False


def test_extract_sw_port_absent_is_none() -> None:
    """Absent sw_port -> None (int guard's non-int path)."""
    rec: dict[str, object] = {"mac": _MAC, "ip": "192.168.2.50"}
    ec = _extract(rec, is_online=True, now=_NOW_ISO)
    assert ec is not None
    assert ec.sw_port is None


def test_extract_offline_uses_last_ip() -> None:
    """is_online=False reads `last_ip` (not `ip`) for the address, online=False."""
    rec = _alluser_record(
        _MAC, "192.168.2.70", first_seen=_epoch_ago(86400), last_seen=_epoch_ago(3600)
    )
    ec = _extract(rec, is_online=False, now=_NOW_ISO)
    assert ec is not None
    assert ec.ip == "192.168.2.70"
    assert ec.online is False


# ---- upsert_identity PASS 1 (stat/sta) ----


@pytest.mark.asyncio
async def test_upsert_identity_pass1_upserts_and_observes(repo: SqliteRepository) -> None:
    """PASS 1: online clients are upserted with record first_seen, an observation is
    appended, and counts are correct."""
    fs = _epoch_ago(7200)
    ls = _epoch_ago(60)
    sta = [_sta_record(_MAC, "192.168.2.50", first_seen=fs, last_seen=ls)]
    async with repo.transaction() as conn:
        result = await upsert_identity(
            conn,
            stat_sta=sta,
            stat_alluser=[],
            host_lan_ip=_HOST_IP,
            observation_cutoff=_CUTOFF_ISO,
            now=_NOW_ISO,
        )
    assert result.clients_upserted == 1
    assert result.observations_appended == 1
    assert result.hosts_reconciled == 0
    assert result.skipped == 0

    client_repo = UnifiClientRepo(repo)
    row = await client_repo.get_client(_MAC)
    assert row is not None
    assert row.online is True
    assert row.ip == "192.168.2.50"
    # registry first_seen seeded from the record's epoch first_seen (NOT last_seen).
    assert row.first_seen == _iso_of(fs)
    assert row.last_seen == _iso_of(ls)

    obs = await repo.fetch_one(
        text(
            "SELECT first_seen, last_seen FROM unifi_client_observations "
            "WHERE mac = :mac AND ip = :ip"
        ),
        {"mac": _MAC, "ip": "192.168.2.50"},
    )
    assert obs is not None
    assert obs.last_seen == _iso_of(ls)


@pytest.mark.asyncio
async def test_upsert_identity_pass1_skips_record_without_mac(repo: SqliteRepository) -> None:
    """A sta record with no mac increments skipped and is not upserted."""
    sta: list[dict[str, object]] = [{"ip": "192.168.2.50", "first_seen": _epoch_ago(10)}]
    async with repo.transaction() as conn:
        result = await upsert_identity(
            conn,
            stat_sta=sta,
            stat_alluser=[],
            host_lan_ip=_HOST_IP,
            observation_cutoff=_CUTOFF_ISO,
            now=_NOW_ISO,
        )
    assert result.clients_upserted == 0
    assert result.observations_appended == 0
    assert result.skipped == 1


@pytest.mark.asyncio
async def test_upsert_identity_pass1_online_without_ip_skips_observation(
    repo: SqliteRepository,
) -> None:
    """An online client with no ip is upserted but NO observation is appended."""
    rec: dict[str, object] = {
        "mac": _MAC,
        "first_seen": _epoch_ago(100),
        "last_seen": _epoch_ago(10),
    }
    async with repo.transaction() as conn:
        result = await upsert_identity(
            conn,
            stat_sta=[rec],
            stat_alluser=[],
            host_lan_ip=_HOST_IP,
            observation_cutoff=_CUTOFF_ISO,
            now=_NOW_ISO,
        )
    assert result.clients_upserted == 1
    assert result.observations_appended == 0


# ---- upsert_identity PASS 2 (stat/alluser) ----


@pytest.mark.asyncio
async def test_upsert_identity_pass2_offline_only(repo: SqliteRepository) -> None:
    """A mac present ONLY in alluser is upserted offline with ip=last_ip and NO observation."""
    rec = _alluser_record(
        _OTHER_MAC, "192.168.2.70", first_seen=_epoch_ago(86400), last_seen=_epoch_ago(3600)
    )
    async with repo.transaction() as conn:
        result = await upsert_identity(
            conn,
            stat_sta=[],
            stat_alluser=[rec],
            host_lan_ip=_HOST_IP,
            observation_cutoff=_CUTOFF_ISO,
            now=_NOW_ISO,
        )
    assert result.clients_upserted == 1
    assert result.observations_appended == 0
    assert result.skipped == 0

    client_repo = UnifiClientRepo(repo)
    row = await client_repo.get_client(_OTHER_MAC)
    assert row is not None
    assert row.online is False
    assert row.ip == "192.168.2.70"

    # No observation row was created for the offline-only client.
    obs = await repo.fetch_one(
        text("SELECT mac FROM unifi_client_observations WHERE mac = :mac"),
        {"mac": _OTHER_MAC},
    )
    assert obs is None


@pytest.mark.asyncio
async def test_upsert_identity_pass2_skips_already_seen_online(repo: SqliteRepository) -> None:
    """A mac in BOTH sta and alluser is NOT downgraded: it stays online=True."""
    fs = _epoch_ago(7200)
    ls = _epoch_ago(60)
    sta = [_sta_record(_MAC, "192.168.2.50", first_seen=fs, last_seen=ls)]
    alluser = [_alluser_record(_MAC, "192.168.2.99", first_seen=fs, last_seen=_epoch_ago(3600))]
    async with repo.transaction() as conn:
        result = await upsert_identity(
            conn,
            stat_sta=sta,
            stat_alluser=alluser,
            host_lan_ip=_HOST_IP,
            observation_cutoff=_CUTOFF_ISO,
            now=_NOW_ISO,
        )
    # Only the sta upsert counts; the alluser record is skipped via `seen`.
    assert result.clients_upserted == 1

    client_repo = UnifiClientRepo(repo)
    row = await client_repo.get_client(_MAC)
    assert row is not None
    assert row.online is True
    assert row.ip == "192.168.2.50"  # the sta ip, not the alluser last_ip


@pytest.mark.asyncio
async def test_upsert_identity_pass2_skips_record_without_mac(repo: SqliteRepository) -> None:
    """An alluser record without mac increments skipped."""
    alluser: list[dict[str, object]] = [{"last_ip": "192.168.2.70"}]
    async with repo.transaction() as conn:
        result = await upsert_identity(
            conn,
            stat_sta=[],
            stat_alluser=alluser,
            host_lan_ip=_HOST_IP,
            observation_cutoff=_CUTOFF_ISO,
            now=_NOW_ISO,
        )
    assert result.clients_upserted == 0
    assert result.skipped == 1


# ---- host reconciliation ----


@pytest.mark.asyncio
async def test_upsert_identity_reconciles_host(repo: SqliteRepository) -> None:
    """A sta record whose ip == host_lan_ip merges the sentinel into the real-MAC row."""
    client_repo = UnifiClientRepo(repo)
    await client_repo.ensure_host_row(_HOST_IP)  # seeds the host:<ip> sentinel
    # Capture the sentinel's first_seen (utc_now_iso at seed time) to assert the MIN.
    sentinel = await client_repo.get_client(f"host:{_HOST_IP}")
    assert sentinel is not None
    sentinel_first_seen = sentinel.first_seen

    # Record first_seen is OLDER than the sentinel (epoch well in the past),
    # so the merged first_seen must be the record's.
    rec_fs = _epoch_ago(30 * 86400)  # ~30 days ago
    rec_ls = _epoch_ago(60)
    sta = [_sta_record(_HOST_MAC, _HOST_IP, first_seen=rec_fs, last_seen=rec_ls)]
    async with repo.transaction() as conn:
        result = await upsert_identity(
            conn,
            stat_sta=sta,
            stat_alluser=[],
            host_lan_ip=_HOST_IP,
            observation_cutoff=_CUTOFF_ISO,
            now=_NOW_ISO,
        )
    assert result.hosts_reconciled == 1

    # Real-MAC row is now the host; sentinel is gone.
    host_row = await client_repo.get_client(_HOST_MAC)
    assert host_row is not None
    assert host_row.is_host is True
    assert host_row.first_seen == min(sentinel_first_seen, _iso_of(rec_fs))
    assert host_row.first_seen == _iso_of(rec_fs)  # record is older
    assert await client_repo.get_client(f"host:{_HOST_IP}") is None


@pytest.mark.asyncio
async def test_upsert_identity_reconcile_idempotent(repo: SqliteRepository) -> None:
    """A second run after the sentinel is gone reconciles nothing."""
    client_repo = UnifiClientRepo(repo)
    await client_repo.ensure_host_row(_HOST_IP)
    sta = [_sta_record(_HOST_MAC, _HOST_IP, first_seen=_epoch_ago(86400), last_seen=_epoch_ago(60))]

    async with repo.transaction() as conn:
        first = await upsert_identity(
            conn,
            stat_sta=sta,
            stat_alluser=[],
            host_lan_ip=_HOST_IP,
            observation_cutoff=_CUTOFF_ISO,
            now=_NOW_ISO,
        )
    assert first.hosts_reconciled == 1

    async with repo.transaction() as conn:
        second = await upsert_identity(
            conn,
            stat_sta=sta,
            stat_alluser=[],
            host_lan_ip=_HOST_IP,
            observation_cutoff=_CUTOFF_ISO,
            now=_NOW_ISO,
        )
    assert second.hosts_reconciled == 0  # sentinel already deleted


@pytest.mark.asyncio
async def test_upsert_identity_no_host_match_keeps_sentinel(repo: SqliteRepository) -> None:
    """When no record matches host_lan_ip, the sentinel persists and nothing reconciles."""
    client_repo = UnifiClientRepo(repo)
    await client_repo.ensure_host_row(_HOST_IP)
    sta = [_sta_record(_MAC, "192.168.2.50", first_seen=_epoch_ago(7200), last_seen=_epoch_ago(60))]

    async with repo.transaction() as conn:
        result = await upsert_identity(
            conn,
            stat_sta=sta,
            stat_alluser=[],
            host_lan_ip=_HOST_IP,
            observation_cutoff=_CUTOFF_ISO,
            now=_NOW_ISO,
        )
    assert result.hosts_reconciled == 0
    assert await client_repo.get_client(f"host:{_HOST_IP}") is not None


# ---- promote_to_host_conn (direct) ----


@pytest.mark.asyncio
async def test_promote_to_host_conn_promotes(repo: SqliteRepository) -> None:
    """Sentinel present + real-MAC row present -> promotes, returns True."""
    client_repo = UnifiClientRepo(repo)
    await client_repo.ensure_host_row(_HOST_IP)
    # Upsert the real-MAC row first (the helper does this before promoting).
    real_first_seen = _iso_of(_epoch_ago(10 * 86400))
    async with repo.transaction() as conn:
        await UnifiClientRepo.upsert_client_conn(
            conn,
            mac=_HOST_MAC,
            ip=_HOST_IP,
            hostname=None,
            name=None,
            oui=None,
            network=None,
            ap_mac=None,
            sw_mac=None,
            sw_port=None,
            use_fixedip=False,
            fixed_ip=None,
            online=True,
            first_seen=real_first_seen,
            last_seen=_iso_of(_epoch_ago(60)),
        )
        promoted = await UnifiClientRepo.promote_to_host_conn(
            conn, real_mac=_HOST_MAC, host_ip=_HOST_IP
        )
    assert promoted is True
    row = await client_repo.get_client(_HOST_MAC)
    assert row is not None
    assert row.is_host is True
    assert await client_repo.get_client(f"host:{_HOST_IP}") is None


@pytest.mark.asyncio
async def test_promote_to_host_conn_no_sentinel_returns_false(repo: SqliteRepository) -> None:
    """No sentinel row -> returns False (no-op)."""
    async with repo.transaction() as conn:
        # Real-MAC row exists but no sentinel was ever seeded.
        await UnifiClientRepo.upsert_client_conn(
            conn,
            mac=_HOST_MAC,
            ip=_HOST_IP,
            hostname=None,
            name=None,
            oui=None,
            network=None,
            ap_mac=None,
            sw_mac=None,
            sw_port=None,
            use_fixedip=False,
            fixed_ip=None,
            online=True,
            first_seen=_iso_of(_epoch_ago(100)),
            last_seen=_iso_of(_epoch_ago(10)),
        )
        result = await UnifiClientRepo.promote_to_host_conn(
            conn, real_mac=_HOST_MAC, host_ip=_HOST_IP
        )
    assert result is False


@pytest.mark.asyncio
async def test_promote_to_host_conn_real_mac_absent_returns_false(repo: SqliteRepository) -> None:
    """Sentinel present but the real-MAC row was never upserted -> returns False."""
    client_repo = UnifiClientRepo(repo)
    await client_repo.ensure_host_row(_HOST_IP)
    async with repo.transaction() as conn:
        result = await UnifiClientRepo.promote_to_host_conn(
            conn, real_mac=_HOST_MAC, host_ip=_HOST_IP
        )
    assert result is False
    # Sentinel still present (not deleted on the no-op path).
    assert await client_repo.get_client(f"host:{_HOST_IP}") is not None


@pytest.mark.asyncio
async def test_promote_to_host_conn_real_mac_equals_sentinel_returns_false(
    repo: SqliteRepository,
) -> None:
    """Guard: real_mac literally equal to the sentinel mac -> False (never self-delete)."""
    client_repo = UnifiClientRepo(repo)
    await client_repo.ensure_host_row(_HOST_IP)
    sentinel_mac = f"host:{_HOST_IP}"
    async with repo.transaction() as conn:
        result = await UnifiClientRepo.promote_to_host_conn(
            conn, real_mac=sentinel_mac, host_ip=_HOST_IP
        )
    assert result is False
    # Sentinel untouched.
    assert await client_repo.get_client(sentinel_mac) is not None


# ---- UpsertResult counts across a combined fixture ----


@pytest.mark.asyncio
async def test_upsert_identity_combined_counts(repo: SqliteRepository) -> None:
    """A combined sta+alluser fixture (online, offline-only, malformed) -> all 4 counts."""
    fs = _epoch_ago(7200)
    ls = _epoch_ago(60)
    sta: list[dict[str, object]] = [
        _sta_record(_MAC, "192.168.2.50", first_seen=fs, last_seen=ls),  # online + obs
        {"ip": "192.168.2.51"},  # malformed (no mac) -> skipped
    ]
    alluser: list[dict[str, object]] = [
        _alluser_record(
            _OTHER_MAC, "192.168.2.70", first_seen=fs, last_seen=_epoch_ago(3600)
        ),  # offline-only
        _alluser_record(
            _MAC, "192.168.2.99", first_seen=fs, last_seen=_epoch_ago(3600)
        ),  # seen -> skip (no count)
        {"last_ip": "192.168.2.71"},  # malformed (no mac) -> skipped
    ]
    async with repo.transaction() as conn:
        result = await upsert_identity(
            conn,
            stat_sta=sta,
            stat_alluser=alluser,
            host_lan_ip=_HOST_IP,
            observation_cutoff=_CUTOFF_ISO,
            now=_NOW_ISO,
        )
    assert result == UpsertResult(
        clients_upserted=2,  # _MAC (sta) + _OTHER_MAC (alluser-only)
        observations_appended=1,  # only the online _MAC
        hosts_reconciled=0,
        skipped=2,  # the two no-mac records
    )


def test_extracted_client_is_frozen() -> None:
    """ExtractedClient is a frozen dataclass (sanity: construct + no mutation)."""
    ec = ExtractedClient(
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
        first_seen_iso=_NOW_ISO,
        last_seen_iso=_NOW_ISO,
    )
    assert ec.mac == _MAC
