"""Unit tests for the synology_storage collector (STAGE-008-005, 3a fixture-based).

100% branch coverage of storage.py. The fixture is HAND-BUILT from recon field
names; Refinement 3b replaces it with the real captured load_info payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from homelab_monitor.kernel.plugins.context import CollectorContext

from homelab_monitor.kernel.plugins.io import MemoryRetainingMetricsWriter
from homelab_monitor.kernel.synology.client import SynologyResponse
from homelab_monitor.kernel.synology.errors import SynologyError
from homelab_monitor.plugins.collectors.integrations.synology.storage import (
    M_DISK_REMAIN_LIFE,
    M_DISK_SB_DAYS_LEFT,
    M_DISK_SIZE_BYTES,
    M_DISK_SLOT,
    M_DISK_SMART_STATUS,
    M_DISK_STATUS,
    M_DISK_TEMP_CELSIUS,
    M_DISK_UNC_COUNT,
    M_VOLUME_ENCRYPTED,
    M_VOLUME_FS_TYPE,
    M_VOLUME_INODE_FULL,
    M_VOLUME_LOCKED,
    M_VOLUME_STATUS,
    M_VOLUME_TOTAL_BYTES,
    M_VOLUME_USED_BYTES,
    M_VOLUME_USED_PERCENT,
    M_VOLUME_WRITABLE,
    SynologyStorageCollector,
)

_API_TOOK = "homelab_synology_api_took_seconds"
_DROP = "homelab_metric_family_dropped_series"

_EXPECTED_INTERVAL = 300.0
_EXPECTED_TIMEOUT = 30.0

# Number of cap-routed families emitted by _emit (9 volume + 8 disk = 17).
# Note: volume_status emits 2 observations in the happy path (top-level status +
# space_status.status), so survivors count reflects 2 status gauges for volume_1.
_FAMILY_COUNT = 17
_NUM_DISKS = 8
_NUM_DISKS_WITH_TEMP = 7  # all 8 except the one synthetic disk missing temp
_NUM_STATUS_OBSERVATIONS = 2  # top-level status + space_status.status for volume_1
_STATUS_PERCENT_TOLERANCE = 0.1  # tolerance for percent comparison in tests


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeSynology:
    """Minimal stand-in for ctx.synology with a programmable storage_load_info()."""

    def __init__(self, result: object) -> None:
        self._result = result

    async def storage_load_info(self) -> object:
        return self._result


@dataclass
class _Ctx:
    """Typed stand-in for CollectorContext used in storage tests."""

    vm: MemoryRetainingMetricsWriter = field(default_factory=MemoryRetainingMetricsWriter)
    synology: object = None


def _ctx(writer: MemoryRetainingMetricsWriter, synology: object) -> _Ctx:
    """Partial CollectorContext: vm is the caller-supplied retaining writer."""
    return _Ctx(vm=writer, synology=synology)


def _resp(payload: object) -> SynologyResponse:
    return SynologyResponse(
        payload=payload,
        took_seconds=0.5,
        endpoint="SYNO.Storage.CGI.Storage/load_info",
    )


def _load_info_payload() -> dict[str, object]:
    """Happy-path load_info fixture modelled on the real captured payload (3b).

    Volumes:
      - volume_1: status="has_unverified_disk" (top-level) + space_status.status=
        "fs_almost_full" (nested); size.used/total as byte-count strings; fs_type=
        "btrfs"; is_writable=True, is_encrypted=False, is_locked=False,
        is_inode_full=False.  used_percent is ABSENT (derived: 46448964333568 /
        57540879458304 * 100 ≈ 80.72...).

    Disks (8, matching real NAS):
      - sda..sdh: smart_status="normal", status="normal", temp int, unc=0,
        remain_life={"trustable": True, "value": -1} (HDD = N/A), sb_days_left int,
        size_total="<string bytes>", slot_id int.
      - sdb: smart_status="failing" (exercises the 0.0 branch of _status_str_to_gauge).
      - sdc: MISSING temp / smart_status / unc (None-skip branch for those fields).
        Has status="normal" so disk_status IS emitted.

    Edge rows for coverage:
      The remain_life object nesting is exercised by ALL 8 disks (real shape).
      The smart_status 0.0 branch is exercised by sdb.
      The temp/smart/unc None-skip branches are exercised by sdc.
    """
    disks: list[dict[str, object]] = [
        {
            "id": "sda",
            "model": "WD80EFAX",
            "temp": 47,
            "smart_status": "normal",
            "status": "normal",
            "unc": 0,
            "remain_life": {"trustable": True, "value": -1},
            "sb_days_left": 0,
            "size_total": "22000969973760",
            "slot_id": 1,
        },
        {
            "id": "sdb",
            "model": "WD80EFAX",
            "temp": 41,
            "smart_status": "failing",  # exercises the 0.0 branch
            "status": "normal",
            "unc": 2,
            "remain_life": {"trustable": True, "value": -1},
            "sb_days_left": 0,
            "size_total": "22000969973760",
            "slot_id": 2,
        },
        {
            "id": "sdc",
            "model": "WD80EFAX",
            # MISSING temp / smart_status / unc / remain_life / sb_days_left -> None-skip
            "status": "normal",
            "size_total": "22000969973760",
            "slot_id": 3,
        },
    ]
    for i, letter in enumerate(("d", "e", "f", "g", "h"), start=4):
        disks.append(
            {
                "id": f"sd{letter}",
                "model": "WD80EFAX",
                "temp": 40,
                "smart_status": "normal",
                "status": "normal",
                "unc": 0,
                "remain_life": {"trustable": True, "value": -1},
                "sb_days_left": 0,
                "size_total": "22000969973760",
                "slot_id": i,
            }
        )
    return {
        "volumes": [
            {
                "id": "volume_1",
                "status": "has_unverified_disk",
                "space_status": {"status": "fs_almost_full"},
                "fs_type": "btrfs",
                "size": {
                    "used": "46448964333568",
                    "total": "57540879458304",
                    "free_inode": "0",
                    "total_inode": "0",
                },
                # No used_percent field — collector derives it from size.used/total
                "is_writable": True,
                "is_encrypted": False,
                "is_locked": False,
                "is_inode_full": False,
            }
        ],
        "disks": disks,
    }


def _gauges_named(
    writer: MemoryRetainingMetricsWriter, name: str
) -> list[tuple[str, float, dict[str, str]]]:
    return [g for g in writer.gauges if g[0] == name]


# ---------------------------------------------------------------------------
# ClassVars
# ---------------------------------------------------------------------------


def test_storage_classvars() -> None:
    assert SynologyStorageCollector.name == "synology_storage"
    assert SynologyStorageCollector.interval == timedelta(seconds=300)
    assert SynologyStorageCollector.interval.total_seconds() == _EXPECTED_INTERVAL
    assert SynologyStorageCollector.timeout == timedelta(seconds=30)
    assert SynologyStorageCollector.timeout.total_seconds() == _EXPECTED_TIMEOUT
    assert SynologyStorageCollector.concurrency_group == "synology"


# ---------------------------------------------------------------------------
# ctx.synology is None  -> ok=False
# ---------------------------------------------------------------------------


async def test_storage_unconfigured_client() -> None:
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=None))
    result = await SynologyStorageCollector().run(ctx)
    assert result.ok is False
    assert result.errors == ["synology client not configured"]
    assert result.metrics_emitted == 0
    assert writer.gauges == []


# ---------------------------------------------------------------------------
# SynologyError -> ok=False, no gauges
# ---------------------------------------------------------------------------


async def test_storage_client_error() -> None:
    err = SynologyError(reason="unreachable", message="connection failed")
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(err)))
    result = await SynologyStorageCollector().run(ctx)
    assert result.ok is False
    assert result.errors == ["connection failed"]
    assert result.metrics_emitted == 0
    assert writer.gauges == []


# ---------------------------------------------------------------------------
# Malformed payload (not a dict) -> ok=True, only the api_took gauge
# ---------------------------------------------------------------------------


async def test_storage_malformed_payload_is_list() -> None:
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(["not", "a", "dict"])))
    )
    result = await SynologyStorageCollector().run(ctx)
    assert result.ok is True
    assert result.errors == []
    # fetch_or_result emitted exactly the api_took gauge; _emit never ran.
    assert result.metrics_emitted == 1
    gauges = writer.gauges
    assert len(gauges) == 1
    assert gauges[0][0] == _API_TOOK
    # No payload families, no drop gauges.
    assert _gauges_named(writer, _DROP) == []


async def test_storage_malformed_payload_is_none() -> None:
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(None))))
    result = await SynologyStorageCollector().run(ctx)
    assert result.ok is True
    assert result.metrics_emitted == 1  # api_took only


# ---------------------------------------------------------------------------
# Happy path — full fixture
# ---------------------------------------------------------------------------


async def test_storage_happy_path_volume_metrics() -> None:
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(_load_info_payload())))
    )
    result = await SynologyStorageCollector().run(ctx)
    assert result.ok is True
    assert result.errors == []

    used = _gauges_named(writer, M_VOLUME_USED_BYTES)
    assert used == [(M_VOLUME_USED_BYTES, 46448964333568.0, {"volume": "volume_1"})]

    total = _gauges_named(writer, M_VOLUME_TOTAL_BYTES)
    assert total == [(M_VOLUME_TOTAL_BYTES, 57540879458304.0, {"volume": "volume_1"})]

    pct = _gauges_named(writer, M_VOLUME_USED_PERCENT)
    assert len(pct) == 1
    assert pct[0][0] == M_VOLUME_USED_PERCENT
    assert pct[0][2] == {"volume": "volume_1"}
    # Derived: 46448964333568 / 57540879458304 * 100 ≈ 80.72
    assert abs(pct[0][1] - 80.72) < _STATUS_PERCENT_TOLERANCE

    # Both top-level status AND space_status.status are emitted.
    status = _gauges_named(writer, M_VOLUME_STATUS)
    assert (M_VOLUME_STATUS, 1.0, {"volume": "volume_1", "status": "has_unverified_disk"}) in status
    assert (M_VOLUME_STATUS, 1.0, {"volume": "volume_1", "status": "fs_almost_full"}) in status
    assert len(status) == _NUM_STATUS_OBSERVATIONS

    fs = _gauges_named(writer, M_VOLUME_FS_TYPE)
    assert fs == [(M_VOLUME_FS_TYPE, 1.0, {"volume": "volume_1", "fs": "btrfs"})]

    # is_* bool flags emitted.
    assert _gauges_named(writer, M_VOLUME_WRITABLE) == [
        (M_VOLUME_WRITABLE, 1.0, {"volume": "volume_1"})
    ]
    assert _gauges_named(writer, M_VOLUME_ENCRYPTED) == [
        (M_VOLUME_ENCRYPTED, 0.0, {"volume": "volume_1"})
    ]
    assert _gauges_named(writer, M_VOLUME_LOCKED) == [
        (M_VOLUME_LOCKED, 0.0, {"volume": "volume_1"})
    ]
    assert _gauges_named(writer, M_VOLUME_INODE_FULL) == [
        (M_VOLUME_INODE_FULL, 0.0, {"volume": "volume_1"})
    ]


async def test_storage_happy_path_disk_metrics() -> None:
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(_load_info_payload())))
    )
    await SynologyStorageCollector().run(ctx)

    # temp: emitted for all 8 disks EXCEPT sdc (missing temp field).
    temp = _gauges_named(writer, M_DISK_TEMP_CELSIUS)
    assert len(temp) == _NUM_DISKS_WITH_TEMP
    assert (M_DISK_TEMP_CELSIUS, 47.0, {"disk": "sda", "model": "WD80EFAX"}) in temp
    assert (M_DISK_TEMP_CELSIUS, 41.0, {"disk": "sdb", "model": "WD80EFAX"}) in temp
    # sdc missing temp -> NOT emitted.
    assert all(g[2].get("disk") != "sdc" for g in temp)

    # smart_status: sda->1.0, sdb->0.0 (the "failing" branch), sdc MISSING -> skip.
    smart = _gauges_named(writer, M_DISK_SMART_STATUS)
    assert (M_DISK_SMART_STATUS, 1.0, {"disk": "sda"}) in smart
    assert (M_DISK_SMART_STATUS, 0.0, {"disk": "sdb"}) in smart
    assert all(g[2].get("disk") != "sdc" for g in smart)
    assert len(smart) == _NUM_DISKS_WITH_TEMP  # 7 (all except sdc)

    # status: present on ALL 8 disks (sdc has status="normal").
    dstatus = _gauges_named(writer, M_DISK_STATUS)
    assert (M_DISK_STATUS, 1.0, {"disk": "sdc"}) in dstatus
    assert len(dstatus) == _NUM_DISKS

    # remain_life: real payload uses nested {"trustable": bool, "value": int}.
    # All 8 disks with remain_life present emit -1.0 (HDD = N/A).
    # sdc is MISSING remain_life entirely -> not emitted.
    remain = _gauges_named(writer, M_DISK_REMAIN_LIFE)
    assert (M_DISK_REMAIN_LIFE, -1.0, {"disk": "sda"}) in remain
    assert (M_DISK_REMAIN_LIFE, -1.0, {"disk": "sdb"}) in remain
    assert all(g[2].get("disk") != "sdc" for g in remain)
    assert len(remain) == _NUM_DISKS_WITH_TEMP  # 7

    # unc: sdc missing -> not emitted; sdb carries 2.
    unc = _gauges_named(writer, M_DISK_UNC_COUNT)
    assert (M_DISK_UNC_COUNT, 2.0, {"disk": "sdb"}) in unc
    assert all(g[2].get("disk") != "sdc" for g in unc)

    # sb_days_left: sdc missing -> not emitted.
    sb = _gauges_named(writer, M_DISK_SB_DAYS_LEFT)
    assert all(g[2].get("disk") != "sdc" for g in sb)

    # size_total (disk_size_bytes): emitted for all 8 disks (even sdc has size_total).
    sizes = _gauges_named(writer, M_DISK_SIZE_BYTES)
    assert len(sizes) == _NUM_DISKS
    assert (M_DISK_SIZE_BYTES, 22000969973760.0, {"disk": "sda"}) in sizes

    # slot_id (disk_slot): emitted for all 8 disks.
    slots = _gauges_named(writer, M_DISK_SLOT)
    assert len(slots) == _NUM_DISKS
    assert (M_DISK_SLOT, 1.0, {"disk": "sda"}) in slots
    assert (M_DISK_SLOT, 3.0, {"disk": "sdc"}) in slots


async def test_storage_happy_path_metrics_emitted_accounting() -> None:
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(_load_info_payload())))
    )
    result = await SynologyStorageCollector().run(ctx)

    # The api_took gauge.
    assert len(_gauges_named(writer, _API_TOOK)) == 1
    # One drop gauge per cap-routed family (always written, even at 0 survivors).
    assert len(_gauges_named(writer, _DROP)) == _FAMILY_COUNT

    # metrics_emitted == survivors across all families + 1 drop gauge per family + 1 api_took.
    survivors = sum(1 for g in writer.gauges if g[0] not in (_API_TOOK, _DROP))
    assert result.metrics_emitted == survivors + _FAMILY_COUNT + 1
    # And metrics_emitted equals the total gauge count written.
    assert result.metrics_emitted == len(writer.gauges)


# ---------------------------------------------------------------------------
# Edge case tests for 100% branch coverage
# ---------------------------------------------------------------------------


async def test_storage_volume_size_fallback_and_derived_percent() -> None:
    """Covers: flat used_size/total_size fallback path, derived percent, and
    the False branches where used is None (no bytes) and total is None (no derive).
    """
    # Sub-case 1: no "size" key -> falls back to used_size/total_size; no used_percent
    # field -> derived = 500/2000*100 = 25.0.
    payload_fallback: dict[str, object] = {
        "volumes": [
            {
                "id": "volume_2",
                "status": "normal",
                "fs_type": "ext4",
                "used_size": 500,
                "total_size": 2000,
                # no used_percent -> derived = 25.0
            }
        ],
        "disks": [],
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload_fallback))))
    await SynologyStorageCollector().run(ctx)
    assert _gauges_named(writer, M_VOLUME_USED_BYTES) == [
        (M_VOLUME_USED_BYTES, 500.0, {"volume": "volume_2"})
    ]
    assert _gauges_named(writer, M_VOLUME_TOTAL_BYTES) == [
        (M_VOLUME_TOTAL_BYTES, 2000.0, {"volume": "volume_2"})
    ]
    assert _gauges_named(writer, M_VOLUME_USED_PERCENT) == [
        (M_VOLUME_USED_PERCENT, 25.0, {"volume": "volume_2"})
    ]

    # Sub-case 2: size.used MISSING -> used=None -> used_bytes not emitted, no derive.
    payload_no_used: dict[str, object] = {
        "volumes": [
            {
                "id": "volume_no_used",
                "status": "normal",
                "fs_type": "btrfs",
                "size": {"total": "1000"},  # no "used" key
            }
        ],
        "disks": [],
    }
    writer2 = MemoryRetainingMetricsWriter()
    ctx2 = cast("CollectorContext", _ctx(writer2, synology=_FakeSynology(_resp(payload_no_used))))
    await SynologyStorageCollector().run(ctx2)
    assert _gauges_named(writer2, M_VOLUME_USED_BYTES) == []
    # total still emitted
    assert len(_gauges_named(writer2, M_VOLUME_TOTAL_BYTES)) == 1
    # no derive possible (used is None)
    assert _gauges_named(writer2, M_VOLUME_USED_PERCENT) == []

    # Sub-case 3: size.total MISSING -> total=None -> total_bytes not emitted, no derive.
    payload_no_total: dict[str, object] = {
        "volumes": [
            {
                "id": "volume_no_total",
                "status": "normal",
                "fs_type": "btrfs",
                "size": {"used": "500"},  # no "total" key
            }
        ],
        "disks": [],
    }
    writer3 = MemoryRetainingMetricsWriter()
    ctx3 = cast("CollectorContext", _ctx(writer3, synology=_FakeSynology(_resp(payload_no_total))))
    await SynologyStorageCollector().run(ctx3)
    assert _gauges_named(writer3, M_VOLUME_USED_BYTES) == [
        (M_VOLUME_USED_BYTES, 500.0, {"volume": "volume_no_total"})
    ]
    assert _gauges_named(writer3, M_VOLUME_TOTAL_BYTES) == []
    # no derive possible (total is None)
    assert _gauges_named(writer3, M_VOLUME_USED_PERCENT) == []


async def test_storage_volume_missing_id_skipped() -> None:
    """A volume with no id (or non-str id) is skipped entirely."""
    payload: dict[str, object] = {
        "volumes": [{"status": "normal", "fs_type": "btrfs", "used_size": 1}],
        "disks": [],
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    await SynologyStorageCollector().run(ctx)
    assert _gauges_named(writer, M_VOLUME_USED_BYTES) == []
    assert _gauges_named(writer, M_VOLUME_STATUS) == []


async def test_storage_disk_missing_id_skipped_and_no_model_fallback() -> None:
    """A disk with no id is skipped; a disk with id but no model uses id as the model label.

    Also covers:
    - total==0 -> derived percent skipped
    - is_writable / is_encrypted / is_locked / is_inode_full (bool flags -> 1.0/0.0)
    - size_total (real field name) and slot_id (real field name)
    - size_total ABSENT -> if size is not None FALSE branch (no size_bytes emission)
    - slot_id ABSENT -> if slot is not None FALSE branch (no slot emission)
    """
    payload: dict[str, object] = {
        "volumes": [
            {
                "id": "volume_3",
                "status": "normal",
                # size.used present but total==0 -> derived percent skipped
                "size": {"used": "10", "total": "0"},
                "is_writable": True,
                "is_encrypted": False,
                "is_locked": False,
                "is_inode_full": True,
            }
        ],
        "disks": [
            {"temp": 30},  # no id -> skipped
            {
                "id": "sdz",
                "temp": 35,
                "size_total": "8000000000",  # real field name
                "slot_id": 3,  # real field name
            },  # no model -> model=id="sdz"
            {
                "id": "sdy",
                "status": "normal",
                # MISSING size_total and slot_id to cover their FALSE branches
            },
        ],
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    await SynologyStorageCollector().run(ctx)
    # total==0 -> no derived percent, and no used_percent field -> family empty.
    assert _gauges_named(writer, M_VOLUME_USED_PERCENT) == []
    # disk with no id skipped; sdz present, model label == id.
    temp = _gauges_named(writer, M_DISK_TEMP_CELSIUS)
    assert temp == [(M_DISK_TEMP_CELSIUS, 35.0, {"disk": "sdz", "model": "sdz"})]
    # is_* bool flags.
    assert _gauges_named(writer, M_VOLUME_WRITABLE) == [
        (M_VOLUME_WRITABLE, 1.0, {"volume": "volume_3"})
    ]
    assert _gauges_named(writer, M_VOLUME_ENCRYPTED) == [
        (M_VOLUME_ENCRYPTED, 0.0, {"volume": "volume_3"})
    ]
    assert _gauges_named(writer, M_VOLUME_LOCKED) == [
        (M_VOLUME_LOCKED, 0.0, {"volume": "volume_3"})
    ]
    # is_inode_full=True -> 1.0
    assert _gauges_named(writer, M_VOLUME_INODE_FULL) == [
        (M_VOLUME_INODE_FULL, 1.0, {"volume": "volume_3"})
    ]
    # size_total and slot_id real field names.
    # Only sdz emits size_bytes (sdy is missing size_total).
    assert _gauges_named(writer, M_DISK_SIZE_BYTES) == [
        (M_DISK_SIZE_BYTES, 8000000000.0, {"disk": "sdz"})
    ]
    # Only sdz emits slot (sdy is missing slot_id).
    assert _gauges_named(writer, M_DISK_SLOT) == [(M_DISK_SLOT, 3.0, {"disk": "sdz"})]
    # sdy has status but no size_total/slot_id, so it emits status only.
    assert (M_DISK_STATUS, 1.0, {"disk": "sdy"}) in _gauges_named(writer, M_DISK_STATUS)


async def test_storage_volume_status_absent_skipped() -> None:
    """A volume with no status string and no space_status emits no volume_status observations."""
    payload: dict[str, object] = {
        "volumes": [
            {
                "id": "volume_nostatus",
                "fs_type": "btrfs",
                # no "status" key, no "space_status" key
                "size": {"used": "100", "total": "1000"},
            }
        ],
        "disks": [],
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    await SynologyStorageCollector().run(ctx)
    assert _gauges_named(writer, M_VOLUME_STATUS) == []
    # used_bytes still emitted
    assert len(_gauges_named(writer, M_VOLUME_USED_BYTES)) == 1


async def test_storage_non_list_volumes_and_disks() -> None:
    """volumes/disks present but NOT lists -> _as_list_of_dicts returns [] (no crash)."""
    payload: dict[str, object] = {"volumes": "nope", "disks": 42}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    result = await SynologyStorageCollector().run(ctx)
    assert result.ok is True
    # api_took + 17 drop gauges (all families empty) = 18.
    assert result.metrics_emitted == 1 + _FAMILY_COUNT
