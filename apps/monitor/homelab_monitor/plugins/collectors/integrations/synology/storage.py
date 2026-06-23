"""synology_storage collector — per-VOLUME + per-DISK metrics from load_info.

Calls ``SYNO.Storage.CGI.Storage`` method ``load_info`` (ONE DSM call returns
volumes + disks + pools + caches) and emits the VOLUME and DISK slices. The
pool / RAID slice of the SAME response is STAGE-008-006's job — this file is
single-purpose by design; both collectors read the same one cached fetch via
the shared client.

PARSE — defensive. Recon (EPIC-008 "Verified deployment reality") confirmed the
LEAF field names but NOT the exact JSON nesting (no live Design-time capture was
possible). So the parse navigates tolerantly: ``as_dict`` / ``as_list_of_dicts``
/ ``nested`` walk the structure and degrade a wrong-nesting guess to "metric
absent" (None -> skip emit), never a crash or a pyright failure. The 3a fixture
is hand-built from recon field names; Refinement 3b captures the REAL payload and
validates/corrects the nesting (the candidate-path comments below mark where).

ENUM HANDLING (003-deferral resolution): the volume-status and disk-status shapes
diverge, so there is NO shared enum helper — local maps only:
  - Volume ``status`` -> STATE-SET: emit ``homelab_synology_volume_status{volume,
    status} = 1`` for the OBSERVED state string (e.g. status="has_unverified_disk").
    Do NOT enumerate all DSM states with 0/1 each (unbounded, version-dependent,
    untestable). Stale series stop being written on change.
  - Disk ``smart_status`` / ``status`` -> SCALAR: "normal"->1.0, any-other-str->0.0,
    non-str->None (3 branches, all tested). Binary 1=healthy / 0=not. No richer
    ladder (recon saw only "normal"; YAGNI).

OK SEMANTICS: ``ctx.synology is None`` -> ok=False ("synology client not
configured"); ``SynologyError`` -> ok=False (errors=[msg], no payload metrics);
a degraded NAS (e.g. volume status="has_unverified_disk") is still ok=True (data,
not a probe failure). A malformed payload (not a dict) -> ok=True, no payload
metrics (only the api_took gauge from fetch_or_result).

CARDINALITY: every per-volume + per-disk family is cap-routed through
``capped_emitter(ctx, events)`` + ``cap_for_synology(family)`` (default 500). 1
volume / 8 disks are far under the cap; the cap is a guardrail and each
``emit_family`` ALSO writes the free ``homelab_metric_family_dropped_series``
gauge. ``metrics_emitted`` accounting = ``emit_family() return + 1`` per family.
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
    bytes_field,
    cap_for_synology,
    capped_emitter,
    client_unconfigured_result,
    fetch_or_result,
    nested,
    percent_field,
)

# --- Per-volume metric family names (CORE — emit-always) ------------------------
M_VOLUME_USED_BYTES: Final[str] = "homelab_synology_volume_used_bytes"
M_VOLUME_TOTAL_BYTES: Final[str] = "homelab_synology_volume_total_bytes"
M_VOLUME_USED_PERCENT: Final[str] = "homelab_synology_volume_used_percent"
M_VOLUME_STATUS: Final[str] = "homelab_synology_volume_status"  # state-set {volume,status}=1
M_VOLUME_FS_TYPE: Final[str] = "homelab_synology_volume_fs_type"  # info {volume,fs}=1

# --- Per-volume OPTIONAL families (emit-if-present; graduated to CORE after 3b) ---
M_VOLUME_WRITABLE: Final[str] = "homelab_synology_volume_writable"
M_VOLUME_ENCRYPTED: Final[str] = "homelab_synology_volume_encrypted"
M_VOLUME_LOCKED: Final[str] = "homelab_synology_volume_locked"
# Real payload has `is_inode_full` (bool), NOT an inode-percent field.
# size.free_inode / size.total_inode are both "0" for all volumes on this NAS.
M_VOLUME_INODE_FULL: Final[str] = "homelab_synology_volume_inode_full"

# --- Per-disk metric family names (CORE — emit-always) --------------------------
M_DISK_TEMP_CELSIUS: Final[str] = "homelab_synology_disk_temp_celsius"  # {disk,model}
M_DISK_SMART_STATUS: Final[str] = "homelab_synology_disk_smart_status"  # {disk} 1=normal
M_DISK_STATUS: Final[str] = "homelab_synology_disk_status"  # {disk} 1=normal
M_DISK_UNC_COUNT: Final[str] = "homelab_synology_disk_unc_count"  # {disk}
M_DISK_REMAIN_LIFE: Final[str] = "homelab_synology_disk_remain_life"  # {disk}; -1 = N/A, literal
M_DISK_SB_DAYS_LEFT: Final[str] = (
    "homelab_synology_disk_sb_days_left"  # {disk} bad-sector days-left
)

# --- Per-disk OPTIONAL families (emit-if-present; graduate to CORE after 3b) -----
M_DISK_SIZE_BYTES: Final[str] = "homelab_synology_disk_size_bytes"  # {disk}
M_DISK_SLOT: Final[str] = "homelab_synology_disk_slot"  # {disk}

_SMART_NORMAL: Final[str] = "normal"


# ---------------------------------------------------------------------------
# Local enum maps (NOT shared — shapes diverge; see module docstring)
# ---------------------------------------------------------------------------


def _status_str_to_gauge(v: object) -> float | None:
    """Map a disk status string to a binary gauge: 'normal'->1.0, other-str->0.0, non-str->None.

    Three branches (all covered by tests):
      * non-str (incl. None)        -> None  (skip emit)
      * str == "normal"             -> 1.0
      * str != "normal"             -> 0.0
    """
    if not isinstance(v, str):
        return None
    return 1.0 if v == _SMART_NORMAL else 0.0


# ---------------------------------------------------------------------------
# Per-tick observation accumulator
# ---------------------------------------------------------------------------


class _Built:
    """Per-tick observation lists, one per cap-routed metric family."""

    __slots__ = (
        "disk_remain_life_obs",
        "disk_sb_days_left_obs",
        "disk_size_bytes_obs",
        "disk_slot_obs",
        "disk_smart_status_obs",
        "disk_status_obs",
        "disk_temp_obs",
        "disk_unc_obs",
        "volume_encrypted_obs",
        "volume_fs_type_obs",
        "volume_inode_full_obs",
        "volume_locked_obs",
        "volume_status_obs",
        "volume_total_bytes_obs",
        "volume_used_bytes_obs",
        "volume_used_percent_obs",
        "volume_writable_obs",
    )

    def __init__(self) -> None:
        """Initialise every observation list empty."""
        self.volume_used_bytes_obs: list[tuple[dict[str, str], float]] = []
        self.volume_total_bytes_obs: list[tuple[dict[str, str], float]] = []
        self.volume_used_percent_obs: list[tuple[dict[str, str], float]] = []
        self.volume_status_obs: list[tuple[dict[str, str], float]] = []
        self.volume_fs_type_obs: list[tuple[dict[str, str], float]] = []
        self.volume_writable_obs: list[tuple[dict[str, str], float]] = []
        self.volume_encrypted_obs: list[tuple[dict[str, str], float]] = []
        self.volume_locked_obs: list[tuple[dict[str, str], float]] = []
        self.volume_inode_full_obs: list[tuple[dict[str, str], float]] = []
        self.disk_temp_obs: list[tuple[dict[str, str], float]] = []
        self.disk_smart_status_obs: list[tuple[dict[str, str], float]] = []
        self.disk_status_obs: list[tuple[dict[str, str], float]] = []
        self.disk_unc_obs: list[tuple[dict[str, str], float]] = []
        self.disk_remain_life_obs: list[tuple[dict[str, str], float]] = []
        self.disk_sb_days_left_obs: list[tuple[dict[str, str], float]] = []
        self.disk_size_bytes_obs: list[tuple[dict[str, str], float]] = []
        self.disk_slot_obs: list[tuple[dict[str, str], float]] = []


# ---------------------------------------------------------------------------
# Parse passes
# ---------------------------------------------------------------------------


def _parse_volume(built: _Built, vol: dict[str, object]) -> None:  # noqa: PLR0912
    """Append per-volume observations for one volume record (emit-if-present per field).

    PRIMARY KEY: ``id`` (recon showed ``volume_1``). A volume with no usable id is
    skipped entirely (no observations).

    REAL PATHS (validated by 3b live capture):
      used bytes    : vol["size"]["used"]      (string of byte count, bytes_field)
                      fallback: vol["used_size"]
      total bytes   : vol["size"]["total"]     (string of byte count, bytes_field)
                      fallback: vol["total_size"]
      used %        : derived (used/total*100); no used_percent field in real payload
      status        : vol["status"]            (state-set; e.g. "has_unverified_disk")
      space status  : vol["space_status"]["status"]  (second state-set; e.g. "fs_almost_full")
      fs type       : vol["fs_type"]           (info string)
      is_writable   : vol["is_writable"]       (OPTIONAL bool -> 1.0/0.0)
      is_encrypted  : vol["is_encrypted"]      (OPTIONAL bool -> 1.0/0.0)
      is_locked     : vol["is_locked"]         (OPTIONAL bool -> 1.0/0.0)
      is_inode_full : vol["is_inode_full"]     (OPTIONAL bool -> 1.0/0.0; no inode-pct field)
    """
    raw_id = vol.get("id")
    if not isinstance(raw_id, str) or not raw_id:
        return
    vid = raw_id
    vlabels = {"volume": vid}

    used = bytes_field(nested(vol, "size", "used"))
    if used is None:
        used = bytes_field(vol.get("used_size"))
    if used is not None:
        built.volume_used_bytes_obs.append((vlabels, used))

    total = bytes_field(nested(vol, "size", "total"))
    if total is None:
        total = bytes_field(vol.get("total_size"))
    if total is not None:
        built.volume_total_bytes_obs.append((vlabels, total))

    used_pct = percent_field(vol.get("used_percent"))
    if used_pct is None and used is not None and total is not None and total > 0:
        used_pct = 100.0 * used / total
    if used_pct is not None:
        built.volume_used_percent_obs.append((vlabels, used_pct))

    status = vol.get("status")
    if isinstance(status, str) and status:
        built.volume_status_obs.append(({"volume": vid, "status": status}, 1.0))
    # Also emit space_status.status when present (e.g. "fs_almost_full").
    # Real payload has volumes[].space_status.status = "fs_almost_full" in addition to
    # the top-level status = "has_unverified_disk". Both are useful alert signals.
    space_status = nested(vol, "space_status", "status")
    if isinstance(space_status, str) and space_status:
        built.volume_status_obs.append(({"volume": vid, "status": space_status}, 1.0))

    fs_type = vol.get("fs_type")
    if isinstance(fs_type, str) and fs_type:
        built.volume_fs_type_obs.append(({"volume": vid, "fs": fs_type}, 1.0))

    # Real payload uses is_writable / is_encrypted / is_locked / is_inode_full (bools).
    writable = bool_to_gauge(vol.get("is_writable"))  # OPTIONAL — emit-if-present (None->skip)
    if writable is not None:
        built.volume_writable_obs.append((vlabels, writable))
    encrypted = bool_to_gauge(vol.get("is_encrypted"))  # OPTIONAL
    if encrypted is not None:
        built.volume_encrypted_obs.append((vlabels, encrypted))
    locked = bool_to_gauge(vol.get("is_locked"))  # OPTIONAL
    if locked is not None:
        built.volume_locked_obs.append((vlabels, locked))
    # is_inode_full is a bool (True->1.0 / False->0.0); no inode-percent field exists.
    inode_full = bool_to_gauge(vol.get("is_inode_full"))
    if inode_full is not None:
        built.volume_inode_full_obs.append((vlabels, inode_full))


def _parse_disk(built: _Built, disk: dict[str, object]) -> None:
    """Append per-disk observations for one disk record (emit-if-present per field).

    PRIMARY KEY: ``id`` (recon showed device-style ``sdc``). A disk with no usable
    id is skipped entirely.

    REAL FIELDS (validated by 3b live capture):
      model       : disk["model"]              (adds {model} to the temp family ONLY)
      temp        : disk["temp"]               (int, as_float -> {disk,model})
      smart_status: disk["smart_status"]       (_status_str_to_gauge; 1=normal)
      status      : disk["status"]             (_status_str_to_gauge; 1=normal)
      unc         : disk["unc"]                (int, top-level, as_float)
      remain_life : disk["remain_life"]["value"] (nested obj {"trustable":bool,"value":int};
                                               -1 emitted LITERALLY for HDDs = N/A)
      sb_days_left: disk["sb_days_left"]       (int, as_float)
      size_total  : disk["size_total"]         (OPTIONAL string of bytes, bytes_field)
      slot_id     : disk["slot_id"]            (OPTIONAL int, as_float)
    """
    raw_id = disk.get("id")
    if not isinstance(raw_id, str) or not raw_id:
        return
    did = raw_id
    dlabels = {"disk": did}

    raw_model = disk.get("model")
    model = raw_model if isinstance(raw_model, str) and raw_model else did

    temp = as_float(disk.get("temp"))
    if temp is not None:
        built.disk_temp_obs.append(({"disk": did, "model": model}, temp))

    smart = _status_str_to_gauge(disk.get("smart_status"))
    if smart is not None:
        built.disk_smart_status_obs.append((dlabels, smart))

    status = _status_str_to_gauge(disk.get("status"))
    if status is not None:
        built.disk_status_obs.append((dlabels, status))

    unc = as_float(disk.get("unc"))
    if unc is not None:
        built.disk_unc_obs.append((dlabels, unc))

    # Real payload: remain_life is {"trustable": bool, "value": int}, NOT a scalar.
    remain = as_float(nested(disk, "remain_life", "value"))  # -1.0 passes through LITERALLY
    if remain is not None:
        built.disk_remain_life_obs.append((dlabels, remain))

    sb_days = as_float(disk.get("sb_days_left"))
    if sb_days is not None:
        built.disk_sb_days_left_obs.append((dlabels, sb_days))

    # Real payload: size_total (string of byte count), not "size".
    size = bytes_field(disk.get("size_total"))  # OPTIONAL
    if size is not None:
        built.disk_size_bytes_obs.append((dlabels, size))
    # Real payload: slot_id (int), not "slot".
    slot = as_float(disk.get("slot_id"))  # OPTIONAL
    if slot is not None:
        built.disk_slot_obs.append((dlabels, slot))


def _build(payload: dict[str, object]) -> _Built:
    """Single pass over volumes[] + disks[] -> populated observation lists.

    CANDIDATE PATHS (3b validates): payload["volumes"], payload["disks"].
    """
    built = _Built()
    for vol in as_list_of_dicts(payload.get("volumes")):
        _parse_volume(built, vol)
    for disk in as_list_of_dicts(payload.get("disks")):
        _parse_disk(built, disk)
    return built


def _emit(
    ctx: CollectorContext, built: _Built, events: list[CollectorEvent], emitted: list[int]
) -> None:
    """Cap-route every per-volume + per-disk family through one CappedEmitter.

    ``emit_family`` returns survivor count and ALSO writes the per-family
    ``homelab_metric_family_dropped_series`` drop gauge -> each call contributes
    ``survivors + 1`` to emitted[0]. The state-set (volume_status) and info
    (fs_type) families are routed identically — each (volume,status) /
    (volume,fs) tuple is one observation.
    """
    emitter = capped_emitter(ctx, events)

    def family(name: str, obs: list[tuple[dict[str, str], float]]) -> None:
        emitted[0] += emitter.emit_family(name, cap_for_synology(name), obs) + 1

    # Per-volume CORE
    family(M_VOLUME_USED_BYTES, built.volume_used_bytes_obs)
    family(M_VOLUME_TOTAL_BYTES, built.volume_total_bytes_obs)
    family(M_VOLUME_USED_PERCENT, built.volume_used_percent_obs)
    family(M_VOLUME_STATUS, built.volume_status_obs)
    family(M_VOLUME_FS_TYPE, built.volume_fs_type_obs)
    # Per-volume OPTIONAL
    family(M_VOLUME_WRITABLE, built.volume_writable_obs)
    family(M_VOLUME_ENCRYPTED, built.volume_encrypted_obs)
    family(M_VOLUME_LOCKED, built.volume_locked_obs)
    family(M_VOLUME_INODE_FULL, built.volume_inode_full_obs)
    # Per-disk CORE
    family(M_DISK_TEMP_CELSIUS, built.disk_temp_obs)
    family(M_DISK_SMART_STATUS, built.disk_smart_status_obs)
    family(M_DISK_STATUS, built.disk_status_obs)
    family(M_DISK_UNC_COUNT, built.disk_unc_obs)
    family(M_DISK_REMAIN_LIFE, built.disk_remain_life_obs)
    family(M_DISK_SB_DAYS_LEFT, built.disk_sb_days_left_obs)
    # Per-disk OPTIONAL
    family(M_DISK_SIZE_BYTES, built.disk_size_bytes_obs)
    family(M_DISK_SLOT, built.disk_slot_obs)


class SynologyStorageCollector(BaseCollector):
    """Emit per-volume + per-disk storage metrics from SYNO.Storage.CGI.Storage load_info.

    Polls once per 5-min tick in the ``synology`` concurrency group. Parses the
    volumes[] + disks[] slices of the load_info response (pool/RAID is
    STAGE-008-006). Degraded NAS state is data (ok=True); only a client error or
    an unconfigured client is ok=False.
    """

    name: ClassVar[str] = "synology_storage"
    interval: ClassVar[timedelta] = timedelta(seconds=300)
    timeout: ClassVar[timedelta] = timedelta(seconds=30)
    concurrency_group: ClassVar[str] = "synology"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Fetch load_info, parse volumes + disks, emit cap-routed families."""
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
