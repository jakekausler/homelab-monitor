"""synology_pool collector — per-POOL + per-RAID metrics from load_info.

Calls ``SYNO.Storage.CGI.Storage`` method ``load_info`` (the SAME DSM call that
STAGE-008-005 uses) and emits the POOL and RAID slices. The volume/disk slice
is STAGE-008-005's job; this file is single-purpose by design. Both collectors
read from the same one cached fetch via the shared client.

PARSE — defensive. ``as_dict`` / ``as_list_of_dicts`` / ``nested`` walk the
structure tolerantly and degrade a wrong-nesting guess to None (metric skipped),
never a crash or a pyright failure. ``bool_to_gauge``, ``_raid_status_to_gauge``,
and ``_unverified_disk`` map DSM bool flags / the ``raidStatus`` int / the
multi-field unverified-disk condition to gauges.

STATE-SET HANDLING: Pool status fields use STATE-SET semantics — emit one obs
``{pool, status} = 1.0`` for the observed state string. No enumeration of all
possible states.

OK SEMANTICS: ``ctx.synology is None`` -> ok=False; ``SynologyError`` -> ok=False
(errors=[msg], no payload metrics); a degraded pool (e.g. status=
"has_unverified_disk") is still ok=True (data, not a probe failure). A malformed
payload (not a dict) -> ok=True, no payload metrics.

CARDINALITY: every per-pool + per-raid family is cap-routed through
``capped_emitter(ctx, events)`` + ``cap_for_synology(family)`` (default 500).
``metrics_emitted`` accounting = ``emit_family() return + 1`` per family.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar, Final

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorEvent, CollectorResult
from homelab_monitor.plugins.collectors.integrations.synology._shared import (
    as_dict,
    as_float,
    as_list_of_dicts,
    bool_to_gauge,
    cap_for_synology,
    capped_emitter,
    client_unconfigured_result,
    fetch_or_result,
    nested,
)

# --- Per-pool metric family names -------------------------------------------
M_POOL_STATUS: Final[str] = "homelab_synology_pool_status"  # state-set {pool,status}=1
M_POOL_SCRUB_STATUS: Final[str] = "homelab_synology_pool_scrub_status"  # state-set {pool,state}=1
M_POOL_PROGRESS_STEP: Final[str] = "homelab_synology_pool_progress_step"  # state-set {pool,step}=1
M_POOL_DISK_FAILURE_NUMBER: Final[str] = "homelab_synology_pool_disk_failure_number"  # {pool}
M_POOL_WRITABLE: Final[str] = "homelab_synology_pool_writable"  # {pool}
M_POOL_UNVERIFIED_DISK: Final[str] = "homelab_synology_pool_unverified_disk"  # {pool} always emit
M_POOL_PROGRESS_PERCENT: Final[str] = "homelab_synology_pool_progress_percent"  # {pool}
M_POOL_SCRUB_CAN_DO_MANUAL: Final[str] = "homelab_synology_pool_scrub_can_do_manual"  # {pool}
M_POOL_SCRUB_CAN_SCHEDULE: Final[str] = "homelab_synology_pool_scrub_can_schedule"  # {pool}
M_POOL_SCRUB_LAST_DONE_TIMESTAMP: Final[str] = "homelab_synology_pool_scrub_last_done_timestamp"
M_POOL_SCRUB_NEXT_SCHEDULE_TIMESTAMP: Final[str] = (
    "homelab_synology_pool_scrub_next_schedule_timestamp"
)

# --- Per-raid metric family names -------------------------------------------
M_RAID_STATUS: Final[str] = "homelab_synology_raid_status"  # {pool,raid} 1=normal
M_RAID_NORMAL_DISK_COUNT: Final[str] = "homelab_synology_raid_normal_disk_count"  # {pool,raid}
M_RAID_DESIGNED_DISK_COUNT: Final[str] = "homelab_synology_raid_designed_disk_count"  # {pool,raid}
M_RAID_HAS_PARITY: Final[str] = "homelab_synology_raid_has_parity"  # {pool,raid}
M_RAID_CRASHED_REASON: Final[str] = "homelab_synology_raid_crashed_reason"  # {pool,raid}


def _raid_status_to_gauge(v: object) -> float | None:
    """Map raidStatus int to a binary gauge: 1 (normal) -> 1.0, other int -> 0.0, non-int -> None.

    bool is an int subclass; reject it explicitly so a stray bool isn't read as 0/1.
    """
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return 1.0 if v == 1 else 0.0


def _unverified_disk(pool: dict[str, object]) -> float:
    """Derive the unverified-disk flag from pool fields.

    Returns 1.0 if ANY of:
      - top-level ``status == "has_unverified_disk"``
      - ``space_status.show_attention is True``  (bool True, not truthy)
      - ``space_status.show_danger is True``      (bool True, not truthy)
    Otherwise returns 0.0. Always emitted.
    """
    if pool.get("status") == "has_unverified_disk":
        return 1.0
    if nested(pool, "space_status", "show_attention") is True:
        return 1.0
    if nested(pool, "space_status", "show_danger") is True:
        return 1.0
    return 0.0


# ---------------------------------------------------------------------------
# Per-tick observation accumulator
# ---------------------------------------------------------------------------


class _Built:
    """Per-tick observation lists, one per cap-routed metric family."""

    __slots__ = (
        "pool_disk_failure_number_obs",
        "pool_progress_percent_obs",
        "pool_progress_step_obs",
        "pool_scrub_can_do_manual_obs",
        "pool_scrub_can_schedule_obs",
        "pool_scrub_last_done_timestamp_obs",
        "pool_scrub_next_schedule_timestamp_obs",
        "pool_scrub_status_obs",
        "pool_status_obs",
        "pool_unverified_disk_obs",
        "pool_writable_obs",
        "raid_crashed_reason_obs",
        "raid_designed_disk_count_obs",
        "raid_has_parity_obs",
        "raid_normal_disk_count_obs",
        "raid_status_obs",
    )

    def __init__(self) -> None:
        """Initialise every observation list empty."""
        self.pool_status_obs: list[tuple[dict[str, str], float]] = []
        self.pool_scrub_status_obs: list[tuple[dict[str, str], float]] = []
        self.pool_progress_step_obs: list[tuple[dict[str, str], float]] = []
        self.pool_disk_failure_number_obs: list[tuple[dict[str, str], float]] = []
        self.pool_writable_obs: list[tuple[dict[str, str], float]] = []
        self.pool_unverified_disk_obs: list[tuple[dict[str, str], float]] = []
        self.pool_progress_percent_obs: list[tuple[dict[str, str], float]] = []
        self.pool_scrub_can_do_manual_obs: list[tuple[dict[str, str], float]] = []
        self.pool_scrub_can_schedule_obs: list[tuple[dict[str, str], float]] = []
        self.pool_scrub_last_done_timestamp_obs: list[tuple[dict[str, str], float]] = []
        self.pool_scrub_next_schedule_timestamp_obs: list[tuple[dict[str, str], float]] = []
        self.raid_status_obs: list[tuple[dict[str, str], float]] = []
        self.raid_normal_disk_count_obs: list[tuple[dict[str, str], float]] = []
        self.raid_designed_disk_count_obs: list[tuple[dict[str, str], float]] = []
        self.raid_has_parity_obs: list[tuple[dict[str, str], float]] = []
        self.raid_crashed_reason_obs: list[tuple[dict[str, str], float]] = []


# ---------------------------------------------------------------------------
# Parse passes
# ---------------------------------------------------------------------------


def _parse_raid(built: _Built, pool_id: str, raid: dict[str, object]) -> None:
    """Append per-raid observations for one raid record.

    PRIMARY KEY: ``raidPath`` (str). A raid with a non-str raidPath is skipped
    entirely (no label possible).
    """
    raw_path = raid.get("raidPath")
    if not isinstance(raw_path, str):
        return
    labels = {"pool": pool_id, "raid": raw_path}

    raid_status = _raid_status_to_gauge(raid.get("raidStatus"))
    if raid_status is not None:
        built.raid_status_obs.append((labels, raid_status))

    normal_count = as_float(raid.get("normalDevCount"))
    if normal_count is not None:
        built.raid_normal_disk_count_obs.append((labels, normal_count))

    designed_count = as_float(raid.get("designedDiskCount"))
    if designed_count is not None:
        built.raid_designed_disk_count_obs.append((labels, designed_count))

    has_parity = bool_to_gauge(raid.get("hasParity"))
    if has_parity is not None:
        built.raid_has_parity_obs.append((labels, has_parity))

    crashed_reason = as_float(raid.get("raidCrashedReason"))
    if crashed_reason is not None:
        built.raid_crashed_reason_obs.append((labels, crashed_reason))


def _parse_pool_scrub(built: _Built, plabels: dict[str, str], pool: dict[str, object]) -> None:
    """Append the four scrub-capability + scrub-timing scalar observations.

    Split out of _parse_pool to keep its branch count within the lint budget.
    """
    can_manual = bool_to_gauge(nested(pool, "data_scrubbing", "can_do_manual"))
    if can_manual is not None:
        built.pool_scrub_can_do_manual_obs.append((plabels, can_manual))

    can_schedule = bool_to_gauge(nested(pool, "data_scrubbing", "can_do_schedule"))
    if can_schedule is not None:
        built.pool_scrub_can_schedule_obs.append((plabels, can_schedule))

    last_done = as_float(pool.get("last_done_time"))
    if last_done is not None:
        built.pool_scrub_last_done_timestamp_obs.append((plabels, last_done))

    next_sched = as_float(pool.get("next_schedule_time"))
    if next_sched is not None:
        built.pool_scrub_next_schedule_timestamp_obs.append((plabels, next_sched))


def _parse_pool(built: _Built, pool: dict[str, object]) -> None:
    """Append per-pool observations for one pool record.

    PRIMARY KEY: ``id`` (str). A pool with a non-str id is skipped entirely.
    """
    raw_id = pool.get("id")
    if not isinstance(raw_id, str):
        return
    pool_id = raw_id
    plabels = {"pool": pool_id}

    # STATE-SET: top-level status
    status = pool.get("status")
    if isinstance(status, str):
        built.pool_status_obs.append(({"pool": pool_id, "status": status}, 1.0))
    # STATE-SET: space_status.status (second obs, mirrors storage.py volume_status double-emit)
    space_status = nested(pool, "space_status", "status")
    if isinstance(space_status, str):
        built.pool_status_obs.append(({"pool": pool_id, "status": space_status}, 1.0))

    # STATE-SET: scrubbingStatus
    scrub_status = pool.get("scrubbingStatus")
    if isinstance(scrub_status, str):
        built.pool_scrub_status_obs.append(({"pool": pool_id, "state": scrub_status}, 1.0))

    # STATE-SET: progress.step
    progress_step = nested(pool, "progress", "step")
    if isinstance(progress_step, str):
        built.pool_progress_step_obs.append(({"pool": pool_id, "step": progress_step}, 1.0))

    # Scalar: disk_failure_number
    disk_failure = as_float(pool.get("disk_failure_number"))
    if disk_failure is not None:
        built.pool_disk_failure_number_obs.append((plabels, disk_failure))

    # Scalar: is_writable (bool)
    writable = bool_to_gauge(pool.get("is_writable"))
    if writable is not None:
        built.pool_writable_obs.append((plabels, writable))

    # ALWAYS emit unverified_disk (derived from multiple fields)
    built.pool_unverified_disk_obs.append((plabels, _unverified_disk(pool)))

    # Scalar: progress.percent (string "-1" parses to -1.0)
    progress_pct = as_float(nested(pool, "progress", "percent"))
    if progress_pct is not None:
        built.pool_progress_percent_obs.append((plabels, progress_pct))

    # Scrub capability + timing scalars (extracted to keep branch count in budget)
    _parse_pool_scrub(built, plabels, pool)

    # Per-raid sub-parse
    for raid in as_list_of_dicts(pool.get("raids")):
        _parse_raid(built, pool_id, raid)


def _build(payload: dict[str, object]) -> _Built:
    """Single pass over storagePools[] -> populated observation lists."""
    built = _Built()
    for pool in as_list_of_dicts(payload.get("storagePools")):
        _parse_pool(built, pool)
    return built


def _emit(
    ctx: CollectorContext, built: _Built, events: list[CollectorEvent], emitted: list[int]
) -> None:
    """Cap-route every per-pool + per-raid family through one CappedEmitter."""
    emitter = capped_emitter(ctx, events)

    def family(name: str, obs: list[tuple[dict[str, str], float]]) -> None:
        emitted[0] += emitter.emit_family(name, cap_for_synology(name), obs) + 1

    # Per-pool
    family(M_POOL_STATUS, built.pool_status_obs)
    family(M_POOL_SCRUB_STATUS, built.pool_scrub_status_obs)
    family(M_POOL_PROGRESS_STEP, built.pool_progress_step_obs)
    family(M_POOL_DISK_FAILURE_NUMBER, built.pool_disk_failure_number_obs)
    family(M_POOL_WRITABLE, built.pool_writable_obs)
    family(M_POOL_UNVERIFIED_DISK, built.pool_unverified_disk_obs)
    family(M_POOL_PROGRESS_PERCENT, built.pool_progress_percent_obs)
    family(M_POOL_SCRUB_CAN_DO_MANUAL, built.pool_scrub_can_do_manual_obs)
    family(M_POOL_SCRUB_CAN_SCHEDULE, built.pool_scrub_can_schedule_obs)
    family(M_POOL_SCRUB_LAST_DONE_TIMESTAMP, built.pool_scrub_last_done_timestamp_obs)
    family(M_POOL_SCRUB_NEXT_SCHEDULE_TIMESTAMP, built.pool_scrub_next_schedule_timestamp_obs)
    # Per-raid
    family(M_RAID_STATUS, built.raid_status_obs)
    family(M_RAID_NORMAL_DISK_COUNT, built.raid_normal_disk_count_obs)
    family(M_RAID_DESIGNED_DISK_COUNT, built.raid_designed_disk_count_obs)
    family(M_RAID_HAS_PARITY, built.raid_has_parity_obs)
    family(M_RAID_CRASHED_REASON, built.raid_crashed_reason_obs)


class SynologyPoolCollector(BaseCollector):
    """Emit per-pool + per-raid storage metrics from SYNO.Storage.CGI.Storage load_info.

    Polls once per 5-min tick in the ``synology`` concurrency group. Parses the
    storagePools[] slice of the load_info response (volumes/disks is STAGE-008-005).
    Degraded pool state is data (ok=True); only a client error or an unconfigured
    client is ok=False.
    """

    name: ClassVar[str] = "synology_pool"
    interval: ClassVar[timedelta] = timedelta(seconds=300)
    timeout: ClassVar[timedelta] = timedelta(seconds=30)
    concurrency_group: ClassVar[str] = "synology"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Fetch load_info, parse storagePools + raids, emit cap-routed families."""
        start = time.monotonic()
        if ctx.synology is None:
            return client_unconfigured_result(start)

        emitted: list[int] = [0]
        resp = fetch_or_result(ctx, await ctx.synology.storage_load_info(), start, emitted)
        if isinstance(resp, CollectorResult):
            return resp

        events: list[CollectorEvent] = []
        payload = as_dict(resp.payload)
        if payload is not None:
            built = _build(payload)
            _emit(ctx, built, events, emitted)

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted[0],
            errors=[],
            events=events,
            duration_seconds=time.monotonic() - start,
        )
