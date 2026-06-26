"""Unit tests for the synology_pool collector (STAGE-008-006, 3a fixture-based).

100% branch coverage of pool.py. The fixture is HAND-BUILT from the authoritative
ground-truth pool payload shape defined in the STAGE-008-006 spec.
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
from homelab_monitor.plugins.collectors.integrations.synology.pool import (
    M_POOL_DISK_FAILURE_NUMBER,
    M_POOL_PROGRESS_PERCENT,
    M_POOL_PROGRESS_STEP,
    M_POOL_SCRUB_CAN_DO_MANUAL,
    M_POOL_SCRUB_CAN_SCHEDULE,
    M_POOL_SCRUB_LAST_DONE_TIMESTAMP,
    M_POOL_SCRUB_NEXT_SCHEDULE_TIMESTAMP,
    M_POOL_SCRUB_STATUS,
    M_POOL_STATUS,
    M_POOL_UNVERIFIED_DISK,
    M_POOL_WRITABLE,
    M_RAID_CRASHED_REASON,
    M_RAID_DESIGNED_DISK_COUNT,
    M_RAID_HAS_PARITY,
    M_RAID_NORMAL_DISK_COUNT,
    M_RAID_STATUS,
    SynologyPoolCollector,
)

_API_TOOK = "homelab_synology_api_took_seconds"
_DROP = "homelab_metric_family_dropped_series"

_EXPECTED_INTERVAL = 300.0
_EXPECTED_TIMEOUT = 30.0

# 11 per-pool families + 5 per-raid families = 16.
_FAMILY_COUNT = 16


# ---------------------------------------------------------------------------
# Scaffolding (mirrored from test_synology_storage_collector.py)
# ---------------------------------------------------------------------------


class _FakeSynology:
    """Minimal stand-in for ctx.synology with a programmable storage_load_info()."""

    def __init__(self, result: object) -> None:
        self._result = result

    async def storage_load_info(self) -> object:
        return self._result


@dataclass
class _Ctx:
    """Typed stand-in for CollectorContext used in pool tests."""

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


def _gauges_named(
    writer: MemoryRetainingMetricsWriter, name: str
) -> list[tuple[str, float, dict[str, str]]]:
    return [g for g in writer.gauges if g[0] == name]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_info_payload() -> dict[str, object]:
    """Happy-path load_info fixture with 1 pool (reuse_1) and 1 raid (/dev/md2).

    Pool fields match the authoritative ground-truth shape from STAGE-008-006 spec:
      - status "has_unverified_disk" (top-level) + space_status.status "pool_normal"
      - scrubbingStatus "ready", progress.step "none", progress.percent "-1"
      - data_scrubbing: can_do_manual=True, can_do_schedule=True
      - disk_failure_number=0, is_writable=True
      - last_done_time=0, next_schedule_time=0 (emit 0 literally)
      - space_status.show_attention=False, space_status.show_danger=False
      => unverified_disk=1.0 (because top-level status=="has_unverified_disk")

    Raid (/dev/md2):
      - raidStatus=1 (normal -> 1.0), raidCrashedReason=0, designedDiskCount=8,
        normalDevCount=8, hasParity=True
    """
    return {
        "storagePools": [
            {
                "id": "reuse_1",
                "device_type": "raid_6",
                "status": "has_unverified_disk",
                "summary_status": "",
                "scrubbingStatus": "ready",
                "disk_failure_number": 0,
                "is_writable": True,
                "last_done_time": 0,
                "next_schedule_time": 0,
                "disks": ["sda", "sdb", "sdc", "sdd", "sde", "sdf", "sdg", "sdh"],
                "size": {"total": "176014400258048", "used": "101455241076736"},
                "space_status": {
                    "detail": "",
                    "show_attention": False,
                    "show_danger": False,
                    "status": "pool_normal",
                    "summary_status": "",
                },
                "progress": {
                    "cur_step": 0,
                    "percent": "-1",
                    "remaining_time": 0,
                    "step": "none",
                    "total_step": 0,
                },
                "data_scrubbing": {
                    "can_do_manual": True,
                    "can_do_schedule": True,
                    "reason": "",
                },
                "raids": [
                    {
                        "raidPath": "/dev/md2",
                        "raidStatus": 1,
                        "raidCrashedReason": 0,
                        "designedDiskCount": 8,
                        "normalDevCount": 8,
                        "hasParity": True,
                        "devices": [],
                    }
                ],
            }
        ]
    }


# ---------------------------------------------------------------------------
# ClassVars
# ---------------------------------------------------------------------------


def test_pool_classvars() -> None:
    assert SynologyPoolCollector.name == "synology_pool"
    assert SynologyPoolCollector.interval == timedelta(seconds=300)
    assert SynologyPoolCollector.interval.total_seconds() == _EXPECTED_INTERVAL
    assert SynologyPoolCollector.timeout == timedelta(seconds=30)
    assert SynologyPoolCollector.timeout.total_seconds() == _EXPECTED_TIMEOUT
    assert SynologyPoolCollector.concurrency_group == "synology"


# ---------------------------------------------------------------------------
# ctx.synology is None -> ok=False
# ---------------------------------------------------------------------------


async def test_pool_unconfigured_client() -> None:
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=None))
    result = await SynologyPoolCollector().run(ctx)
    assert result.ok is False
    assert result.errors == ["synology client not configured"]
    assert result.metrics_emitted == 0
    assert writer.gauges == []


# ---------------------------------------------------------------------------
# SynologyError -> ok=False, no gauges
# ---------------------------------------------------------------------------


async def test_pool_client_error() -> None:
    err = SynologyError(reason="unreachable", message="connection failed")
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(err)))
    result = await SynologyPoolCollector().run(ctx)
    assert result.ok is False
    assert result.errors == ["connection failed"]
    assert result.metrics_emitted == 0
    assert writer.gauges == []


# ---------------------------------------------------------------------------
# Malformed payload (not a dict) -> ok=True, only the api_took gauge
# ---------------------------------------------------------------------------


async def test_pool_malformed_payload_is_list() -> None:
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(["not", "a", "dict"])))
    )
    result = await SynologyPoolCollector().run(ctx)
    assert result.ok is True
    assert result.errors == []
    assert result.metrics_emitted == 1
    gauges = writer.gauges
    assert len(gauges) == 1
    assert gauges[0][0] == _API_TOOK
    assert _gauges_named(writer, _DROP) == []


async def test_pool_malformed_payload_is_none() -> None:
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(None))))
    result = await SynologyPoolCollector().run(ctx)
    assert result.ok is True
    assert result.metrics_emitted == 1  # api_took only


# ---------------------------------------------------------------------------
# Happy path — full fixture
# ---------------------------------------------------------------------------


async def test_pool_happy_path_pool_metrics() -> None:
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(_load_info_payload())))
    )
    result = await SynologyPoolCollector().run(ctx)
    assert result.ok is True
    assert result.errors == []

    # STATE-SET: pool_status -> 2 obs (top-level + space_status.status)
    status = _gauges_named(writer, M_POOL_STATUS)
    assert (M_POOL_STATUS, 1.0, {"pool": "reuse_1", "status": "has_unverified_disk"}) in status
    assert (M_POOL_STATUS, 1.0, {"pool": "reuse_1", "status": "pool_normal"}) in status
    expected_status_obs = 2  # top-level status + space_status.status double-emit
    assert len(status) == expected_status_obs

    # STATE-SET: scrub_status
    scrub_status = _gauges_named(writer, M_POOL_SCRUB_STATUS)
    assert scrub_status == [(M_POOL_SCRUB_STATUS, 1.0, {"pool": "reuse_1", "state": "ready"})]

    # STATE-SET: progress_step
    step = _gauges_named(writer, M_POOL_PROGRESS_STEP)
    assert step == [(M_POOL_PROGRESS_STEP, 1.0, {"pool": "reuse_1", "step": "none"})]

    # disk_failure_number=0 -> emitted as 0.0
    failure = _gauges_named(writer, M_POOL_DISK_FAILURE_NUMBER)
    assert failure == [(M_POOL_DISK_FAILURE_NUMBER, 0.0, {"pool": "reuse_1"})]

    # is_writable=True -> 1.0
    writable = _gauges_named(writer, M_POOL_WRITABLE)
    assert writable == [(M_POOL_WRITABLE, 1.0, {"pool": "reuse_1"})]

    # unverified_disk=1.0 (status=="has_unverified_disk")
    unverified = _gauges_named(writer, M_POOL_UNVERIFIED_DISK)
    assert unverified == [(M_POOL_UNVERIFIED_DISK, 1.0, {"pool": "reuse_1"})]

    # progress_percent="-1" -> -1.0
    pct = _gauges_named(writer, M_POOL_PROGRESS_PERCENT)
    assert pct == [(M_POOL_PROGRESS_PERCENT, -1.0, {"pool": "reuse_1"})]

    # data_scrubbing.can_do_manual=True -> 1.0
    can_manual = _gauges_named(writer, M_POOL_SCRUB_CAN_DO_MANUAL)
    assert can_manual == [(M_POOL_SCRUB_CAN_DO_MANUAL, 1.0, {"pool": "reuse_1"})]

    # data_scrubbing.can_do_schedule=True -> 1.0
    can_sched = _gauges_named(writer, M_POOL_SCRUB_CAN_SCHEDULE)
    assert can_sched == [(M_POOL_SCRUB_CAN_SCHEDULE, 1.0, {"pool": "reuse_1"})]

    # last_done_time=0 -> 0.0 (emit 0 literally)
    last_done = _gauges_named(writer, M_POOL_SCRUB_LAST_DONE_TIMESTAMP)
    assert last_done == [(M_POOL_SCRUB_LAST_DONE_TIMESTAMP, 0.0, {"pool": "reuse_1"})]

    # next_schedule_time=0 -> 0.0 (emit 0 literally)
    next_sched = _gauges_named(writer, M_POOL_SCRUB_NEXT_SCHEDULE_TIMESTAMP)
    assert next_sched == [(M_POOL_SCRUB_NEXT_SCHEDULE_TIMESTAMP, 0.0, {"pool": "reuse_1"})]


async def test_pool_happy_path_raid_metrics() -> None:
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(_load_info_payload())))
    )
    await SynologyPoolCollector().run(ctx)

    rl = {"pool": "reuse_1", "raid": "/dev/md2"}

    # raidStatus=1 -> 1.0
    raid_status = _gauges_named(writer, M_RAID_STATUS)
    assert raid_status == [(M_RAID_STATUS, 1.0, rl)]

    # normalDevCount=8 -> 8.0
    normal = _gauges_named(writer, M_RAID_NORMAL_DISK_COUNT)
    assert normal == [(M_RAID_NORMAL_DISK_COUNT, 8.0, rl)]

    # designedDiskCount=8 -> 8.0
    designed = _gauges_named(writer, M_RAID_DESIGNED_DISK_COUNT)
    assert designed == [(M_RAID_DESIGNED_DISK_COUNT, 8.0, rl)]

    # hasParity=True -> 1.0
    parity = _gauges_named(writer, M_RAID_HAS_PARITY)
    assert parity == [(M_RAID_HAS_PARITY, 1.0, rl)]

    # raidCrashedReason=0 -> 0.0 (emit 0 literally)
    crashed = _gauges_named(writer, M_RAID_CRASHED_REASON)
    assert crashed == [(M_RAID_CRASHED_REASON, 0.0, rl)]


async def test_pool_happy_path_metrics_emitted_accounting() -> None:
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(_load_info_payload())))
    )
    result = await SynologyPoolCollector().run(ctx)

    assert len(_gauges_named(writer, _API_TOOK)) == 1
    assert len(_gauges_named(writer, _DROP)) == _FAMILY_COUNT

    survivors = sum(1 for g in writer.gauges if g[0] not in (_API_TOOK, _DROP))
    assert result.metrics_emitted == survivors + _FAMILY_COUNT + 1
    assert result.metrics_emitted == len(writer.gauges)


# ---------------------------------------------------------------------------
# Edge cases — state-set FALSE branches (non-str fields -> no obs emitted)
# ---------------------------------------------------------------------------


async def test_pool_status_non_str_no_obs() -> None:
    """All three state-set fields absent/non-str -> no state-set observations."""
    payload: dict[str, object] = {
        "storagePools": [
            {
                "id": "reuse_1",
                # status absent (no top-level status key)
                # scrubbingStatus is int (non-str) -> skipped
                "scrubbingStatus": 0,
                # progress.step is int (non-str) -> skipped
                "progress": {"step": 99, "percent": "0"},
                # space_status.status is int (non-str) -> skipped
                "space_status": {"status": 42, "show_attention": False, "show_danger": False},
                "raids": [],
            }
        ]
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    await SynologyPoolCollector().run(ctx)

    assert _gauges_named(writer, M_POOL_STATUS) == []
    assert _gauges_named(writer, M_POOL_SCRUB_STATUS) == []
    assert _gauges_named(writer, M_POOL_PROGRESS_STEP) == []


# ---------------------------------------------------------------------------
# Edge cases — None-skip branches for scalar fields
# ---------------------------------------------------------------------------


async def test_pool_missing_scalars_not_emitted() -> None:
    """A pool with all optional scalars absent emits only unverified_disk."""
    payload: dict[str, object] = {
        "storagePools": [
            {
                "id": "reuse_1",
                # disk_failure_number absent -> not emitted
                # is_writable absent -> not emitted
                # progress.percent absent -> not emitted
                # data_scrubbing absent -> not emitted
                # last_done_time absent -> not emitted
                # next_schedule_time absent -> not emitted
                "raids": [],
            }
        ]
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    await SynologyPoolCollector().run(ctx)

    assert _gauges_named(writer, M_POOL_DISK_FAILURE_NUMBER) == []
    assert _gauges_named(writer, M_POOL_WRITABLE) == []
    assert _gauges_named(writer, M_POOL_PROGRESS_PERCENT) == []
    assert _gauges_named(writer, M_POOL_SCRUB_CAN_DO_MANUAL) == []
    assert _gauges_named(writer, M_POOL_SCRUB_CAN_SCHEDULE) == []
    assert _gauges_named(writer, M_POOL_SCRUB_LAST_DONE_TIMESTAMP) == []
    assert _gauges_named(writer, M_POOL_SCRUB_NEXT_SCHEDULE_TIMESTAMP) == []
    # unverified_disk is ALWAYS emitted (0.0 when no trigger)
    unverified = _gauges_named(writer, M_POOL_UNVERIFIED_DISK)
    assert unverified == [(M_POOL_UNVERIFIED_DISK, 0.0, {"pool": "reuse_1"})]


# ---------------------------------------------------------------------------
# Edge cases — unverified_disk derivation branches
# ---------------------------------------------------------------------------


async def test_pool_unverified_disk_false_branch() -> None:
    """status != 'has_unverified_disk', show_attention=False, show_danger=False -> 0.0."""
    payload: dict[str, object] = {
        "storagePools": [
            {
                "id": "reuse_1",
                "status": "normal",
                "space_status": {"show_attention": False, "show_danger": False},
                "raids": [],
            }
        ]
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    await SynologyPoolCollector().run(ctx)
    assert _gauges_named(writer, M_POOL_UNVERIFIED_DISK) == [
        (M_POOL_UNVERIFIED_DISK, 0.0, {"pool": "reuse_1"})
    ]


async def test_pool_unverified_disk_true_via_status() -> None:
    """status == 'has_unverified_disk' -> 1.0."""
    payload: dict[str, object] = {
        "storagePools": [
            {
                "id": "reuse_1",
                "status": "has_unverified_disk",
                "space_status": {"show_attention": False, "show_danger": False},
                "raids": [],
            }
        ]
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    await SynologyPoolCollector().run(ctx)
    assert _gauges_named(writer, M_POOL_UNVERIFIED_DISK) == [
        (M_POOL_UNVERIFIED_DISK, 1.0, {"pool": "reuse_1"})
    ]


async def test_pool_unverified_disk_true_via_show_attention() -> None:
    """show_attention is True -> 1.0 (even if status != 'has_unverified_disk')."""
    payload: dict[str, object] = {
        "storagePools": [
            {
                "id": "reuse_1",
                "status": "normal",
                "space_status": {"show_attention": True, "show_danger": False},
                "raids": [],
            }
        ]
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    await SynologyPoolCollector().run(ctx)
    assert _gauges_named(writer, M_POOL_UNVERIFIED_DISK) == [
        (M_POOL_UNVERIFIED_DISK, 1.0, {"pool": "reuse_1"})
    ]


async def test_pool_unverified_disk_true_via_show_danger() -> None:
    """show_danger is True -> 1.0."""
    payload: dict[str, object] = {
        "storagePools": [
            {
                "id": "reuse_1",
                "status": "normal",
                "space_status": {"show_attention": False, "show_danger": True},
                "raids": [],
            }
        ]
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    await SynologyPoolCollector().run(ctx)
    assert _gauges_named(writer, M_POOL_UNVERIFIED_DISK) == [
        (M_POOL_UNVERIFIED_DISK, 1.0, {"pool": "reuse_1"})
    ]


async def test_pool_unverified_disk_truthy_not_bool_is_false() -> None:
    """show_attention=1 (int truthy, not bool True) -> 0.0 (is True check rejects it)."""
    payload: dict[str, object] = {
        "storagePools": [
            {
                "id": "reuse_1",
                "status": "normal",
                "space_status": {"show_attention": 1, "show_danger": 0},
                "raids": [],
            }
        ]
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    await SynologyPoolCollector().run(ctx)
    assert _gauges_named(writer, M_POOL_UNVERIFIED_DISK) == [
        (M_POOL_UNVERIFIED_DISK, 0.0, {"pool": "reuse_1"})
    ]


# ---------------------------------------------------------------------------
# Edge cases — _bool_to_gauge branches
# ---------------------------------------------------------------------------


async def test_pool_bool_to_gauge_true_false_non_bool() -> None:
    """_bool_to_gauge: True->1.0, False->0.0, non-bool(None)->None (not emitted)."""
    # True and False branches covered by is_writable
    payload_true: dict[str, object] = {
        "storagePools": [{"id": "p1", "is_writable": True, "raids": []}]
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload_true))))
    await SynologyPoolCollector().run(ctx)
    assert _gauges_named(writer, M_POOL_WRITABLE) == [(M_POOL_WRITABLE, 1.0, {"pool": "p1"})]

    payload_false: dict[str, object] = {
        "storagePools": [{"id": "p1", "is_writable": False, "raids": []}]
    }
    writer2 = MemoryRetainingMetricsWriter()
    ctx2 = cast("CollectorContext", _ctx(writer2, synology=_FakeSynology(_resp(payload_false))))
    await SynologyPoolCollector().run(ctx2)
    assert _gauges_named(writer2, M_POOL_WRITABLE) == [(M_POOL_WRITABLE, 0.0, {"pool": "p1"})]

    # non-bool: is_writable absent -> not emitted
    payload_none: dict[str, object] = {"storagePools": [{"id": "p1", "raids": []}]}
    writer3 = MemoryRetainingMetricsWriter()
    ctx3 = cast("CollectorContext", _ctx(writer3, synology=_FakeSynology(_resp(payload_none))))
    await SynologyPoolCollector().run(ctx3)
    assert _gauges_named(writer3, M_POOL_WRITABLE) == []


# ---------------------------------------------------------------------------
# Edge cases — _raid_status_to_gauge branches
# ---------------------------------------------------------------------------


async def test_raid_status_normal() -> None:
    """raidStatus=1 -> 1.0."""
    payload: dict[str, object] = {
        "storagePools": [
            {
                "id": "p1",
                "raids": [{"raidPath": "/dev/md2", "raidStatus": 1}],
            }
        ]
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    await SynologyPoolCollector().run(ctx)
    assert _gauges_named(writer, M_RAID_STATUS) == [
        (M_RAID_STATUS, 1.0, {"pool": "p1", "raid": "/dev/md2"})
    ]


async def test_raid_status_degraded() -> None:
    """raidStatus=2 (non-1 int) -> 0.0."""
    payload: dict[str, object] = {
        "storagePools": [
            {
                "id": "p1",
                "raids": [{"raidPath": "/dev/md2", "raidStatus": 2}],
            }
        ]
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    await SynologyPoolCollector().run(ctx)
    assert _gauges_named(writer, M_RAID_STATUS) == [
        (M_RAID_STATUS, 0.0, {"pool": "p1", "raid": "/dev/md2"})
    ]


async def test_raid_status_bool_rejected() -> None:
    """raidStatus=True (bool, which is int subclass) -> None -> not emitted."""
    payload: dict[str, object] = {
        "storagePools": [
            {
                "id": "p1",
                "raids": [{"raidPath": "/dev/md2", "raidStatus": True}],
            }
        ]
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    await SynologyPoolCollector().run(ctx)
    assert _gauges_named(writer, M_RAID_STATUS) == []


async def test_raid_status_str_rejected() -> None:
    """raidStatus="normal" (str) -> None -> not emitted."""
    payload: dict[str, object] = {
        "storagePools": [
            {
                "id": "p1",
                "raids": [{"raidPath": "/dev/md2", "raidStatus": "normal"}],
            }
        ]
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    await SynologyPoolCollector().run(ctx)
    assert _gauges_named(writer, M_RAID_STATUS) == []


async def test_raid_status_absent_not_emitted() -> None:
    """raidStatus absent -> not emitted."""
    payload: dict[str, object] = {
        "storagePools": [
            {
                "id": "p1",
                "raids": [{"raidPath": "/dev/md2"}],
            }
        ]
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    await SynologyPoolCollector().run(ctx)
    assert _gauges_named(writer, M_RAID_STATUS) == []


# ---------------------------------------------------------------------------
# Edge cases — raid skip / missing raid scalar branches
# ---------------------------------------------------------------------------


async def test_raid_non_str_path_skipped() -> None:
    """A raid with raidPath non-str is skipped entirely."""
    payload: dict[str, object] = {
        "storagePools": [
            {
                "id": "p1",
                "raids": [
                    {"raidPath": 42, "raidStatus": 1},  # non-str -> skipped
                    {"raidPath": "/dev/md2", "raidStatus": 1},  # valid
                ],
            }
        ]
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    await SynologyPoolCollector().run(ctx)
    # Only /dev/md2 emitted
    raid_status = _gauges_named(writer, M_RAID_STATUS)
    assert len(raid_status) == 1
    assert raid_status[0][2]["raid"] == "/dev/md2"


async def test_raid_missing_scalars_not_emitted() -> None:
    """A raid with raidPath but no normalDevCount/designedDiskCount/hasParity/crash fields."""
    payload: dict[str, object] = {
        "storagePools": [
            {
                "id": "p1",
                "raids": [{"raidPath": "/dev/md2"}],
            }
        ]
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    await SynologyPoolCollector().run(ctx)
    assert _gauges_named(writer, M_RAID_NORMAL_DISK_COUNT) == []
    assert _gauges_named(writer, M_RAID_DESIGNED_DISK_COUNT) == []
    assert _gauges_named(writer, M_RAID_HAS_PARITY) == []
    assert _gauges_named(writer, M_RAID_CRASHED_REASON) == []


# ---------------------------------------------------------------------------
# Edge cases — pool-level structure skip branches
# ---------------------------------------------------------------------------


async def test_pool_non_str_id_skipped() -> None:
    """A pool with non-str id is skipped entirely."""
    payload: dict[str, object] = {
        "storagePools": [
            {"id": 42, "status": "normal"},  # non-str id -> skipped
            {"id": "reuse_1", "raids": []},  # valid
        ]
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    await SynologyPoolCollector().run(ctx)
    # Only reuse_1 emits unverified_disk
    unverified = _gauges_named(writer, M_POOL_UNVERIFIED_DISK)
    assert len(unverified) == 1
    assert unverified[0][2] == {"pool": "reuse_1"}


async def test_pool_storage_pools_absent() -> None:
    """storagePools absent -> _as_list_of_dicts returns [] -> no pool obs, no crash."""
    payload: dict[str, object] = {}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    result = await SynologyPoolCollector().run(ctx)
    assert result.ok is True
    # api_took + _FAMILY_COUNT drop gauges (all families empty)
    assert result.metrics_emitted == 1 + _FAMILY_COUNT
    assert _gauges_named(writer, M_POOL_UNVERIFIED_DISK) == []


async def test_pool_storage_pools_non_list() -> None:
    """storagePools is not a list -> _as_list_of_dicts returns [] -> no crash."""
    payload: dict[str, object] = {"storagePools": "nope"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    result = await SynologyPoolCollector().run(ctx)
    assert result.ok is True
    assert result.metrics_emitted == 1 + _FAMILY_COUNT


async def test_pool_non_dict_entry_in_storage_pools_skipped() -> None:
    """A non-dict entry in storagePools[] is skipped by _as_list_of_dicts."""
    payload: dict[str, object] = {
        "storagePools": [
            "not a dict",  # skipped by _as_list_of_dicts
            {"id": "reuse_1", "raids": []},  # valid
        ]
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology(_resp(payload))))
    await SynologyPoolCollector().run(ctx)
    unverified = _gauges_named(writer, M_POOL_UNVERIFIED_DISK)
    assert len(unverified) == 1
    assert unverified[0][2] == {"pool": "reuse_1"}
