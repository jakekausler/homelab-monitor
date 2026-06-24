"""synology_replication collector — Btrfs share snapshots + replication availability.

EPIC-008 STAGE-008-011. Enumerates DSM shares then fans out per-share snapshot
fetches, plus one replication-availability probe, in a single 5-min tick:
  - SYNO.Core.Share/list           -> shares[] (NAMES only; drives the fan-out)
  - SYNO.Core.Share.Snapshot/list  -> per-share snapshots[] (count + latest age)
  - SYNO.Btrfs.Replica.Core/list   -> replication availability probe (sentinel)

CO-EQUAL COMBINE (mirrors STAGE-008-010 backup.py): there is NO primary. ``_fetch``
records-and-continues on ANY fetch's client error; the run is ok=False ONLY when
EVERYTHING failed (the share fetch, every per-share snapshot fetch, and the replica
probe). A partial failure is a DEGRADED ok=True run. ``_emit`` ALWAYS runs.

PER-SHARE ISOLATION: each per-share snapshot fetch is independently ``_fetch``-wrapped.
A single share's fetch failing records its error and SKIPS that share (we do NOT emit
``snapshot_count{share}=0`` for a failed fetch — a failed fetch is unknown, not zero),
while the other shares still emit.

REPLICATION SELF-CORRECTING SENTINEL: ``replication_available`` is 1.0 ONLY when the
probe returns a non-error response whose payload is a dict (a real payload), else 0.0.
Live this is 0.0 (ERR:103, package not installed). NO per-task replication families
(scoped out — package not installed).

FIELD NAMES ARE INFERRED. There are zero live snapshots, so the per-snapshot field
keys are centralized in ``_SNAP_FIELDS`` (logical name -> DSM key). The tolerant
helpers degrade a wrong/absent field to None -> the latest-age metric is skipped while
the count metric (always knowable from len) still emits.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Final

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorEvent, CollectorResult
from homelab_monitor.kernel.synology.client import SynologyResponse
from homelab_monitor.kernel.synology.errors import SynologyError
from homelab_monitor.plugins.collectors.integrations.synology._shared import (
    as_dict,
    as_float,
    as_list_of_dicts,
    cap_for_synology,
    capped_emitter,
    client_unconfigured_result,
    fetch_or_result,
    nested,
)

# --- Metric family names
# Labels (kept out of inline comments to stay <=100 cols):
#   snapshot_count          : {share}  len of snapshots[] (0 included, always per share)
#   snapshot_latest_age_seconds : {share}  now - latest snapshot time (emit-if-present)
#   snapshots_configured    : no labels  1.0 iff ANY share has >=1 snapshot (always)
#   replication_available   : no labels  1.0 iff probe returned a dict payload (always)
M_SNAPSHOT_COUNT: Final[str] = "homelab_synology_snapshot_count"
M_SNAPSHOT_LATEST_AGE_SECONDS: Final[str] = "homelab_synology_snapshot_latest_age_seconds"
M_SNAPSHOTS_CONFIGURED: Final[str] = "homelab_synology_snapshots_configured"
M_REPLICATION_AVAILABLE: Final[str] = "homelab_synology_replication_available"

# --- Inferred DSM snapshot field keys (centralized — ZERO live snapshots, unverified)
# logical name -> DSM snapshot-entry key. Correcting a wrong guess later is a 1-line edit.
_SNAP_FIELDS: Final[dict[str, str]] = {
    "time": "time",
    "create_time": "create_time",
    "snapshot": "snapshot",
    "desc": "desc",
    "lock": "lock",
}

# DSM share-list entry key holding the share name (recon: data.shares[].name).
_SHARE_NAME_KEY: Final[str] = "name"

# DSM timestamp string format (UTC). Snapshot time may be epoch OR this string.
_DSM_TIME_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# Local DSM-shape helpers (NOT shared — replication-specific; local copy mirrors
# backup.py keeping _parse_dsm_time local)
# ---------------------------------------------------------------------------


def _parse_dsm_time(v: object) -> float | None:
    """Parse a DSM timestamp to a UTC epoch float.

    Accepts EITHER a numeric epoch (int/float/numeric-str, via as_float) returned
    as-is, OR a string ``"%Y-%m-%d %H:%M:%S"`` parsed as UTC. Anything else /
    unparseable -> None. (All internal timestamps are UTC.)
    """
    epoch = as_float(v)
    if epoch is not None:
        return epoch
    if isinstance(v, str):
        try:
            parsed = datetime.strptime(v.strip(), _DSM_TIME_FORMAT)
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC).timestamp()
    return None


def _latest_snapshot_time(snapshots: list[dict[str, object]]) -> float | None:
    """Return the MAX parsed snapshot time across a share's snapshots, or None.

    Per snapshot: try ``time``, else fall back to ``create_time``. A snapshot
    whose time is missing/unparseable contributes nothing. None when no snapshot
    yielded a parseable time.
    """
    latest: float | None = None
    for snap in snapshots:
        ts = _parse_dsm_time(snap.get(_SNAP_FIELDS["time"]))
        if ts is None:
            ts = _parse_dsm_time(snap.get(_SNAP_FIELDS["create_time"]))
        if ts is None:
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


# ---------------------------------------------------------------------------
# Multi-fetch wrapper: record-and-continue for INDEPENDENT fetches
# (copied verbatim from STAGE-008-010 backup.py)
# ---------------------------------------------------------------------------


def _fetch(
    ctx: CollectorContext,
    response: SynologyResponse | SynologyError,
    start: float,
    emitted: list[int],
    errors: list[str],
) -> SynologyResponse | None:
    """Wrap fetch_or_result for INDEPENDENT (non-early-returning) fetches.

    On a client error fetch_or_result returns a CollectorResult (errors
    populated); we record those error strings into ``errors`` and return None
    instead of aborting. On success it has already emitted api_took + bumped
    emitted[0]; we return the SynologyResponse.
    """
    r = fetch_or_result(ctx, response, start, emitted)
    if isinstance(r, CollectorResult):
        errors.extend(r.errors)
        return None
    return r


# ---------------------------------------------------------------------------
# Per-tick observation accumulator
# ---------------------------------------------------------------------------


class _Built:
    """Per-tick observation lists, one per cap-routed metric family."""

    __slots__ = (
        "replication_available_obs",
        "snapshot_count_obs",
        "snapshot_latest_age_seconds_obs",
        "snapshots_configured_obs",
    )

    def __init__(self) -> None:
        """Initialise every observation list empty."""
        self.snapshot_count_obs: list[tuple[dict[str, str], float]] = []
        self.snapshot_latest_age_seconds_obs: list[tuple[dict[str, str], float]] = []
        self.snapshots_configured_obs: list[tuple[dict[str, str], float]] = []
        self.replication_available_obs: list[tuple[dict[str, str], float]] = []


# ---------------------------------------------------------------------------
# Parse passes
# ---------------------------------------------------------------------------


def _share_names(share_payload: dict[str, object]) -> list[str]:
    """Extract usable share NAMES from the share-list payload.

    Reads ``shares[]`` and returns each entry's non-empty ``name`` (stripped).
    Entries without a usable name are dropped.
    """
    names: list[str] = []
    for entry in as_list_of_dicts(nested(share_payload, "shares")):
        raw = entry.get(_SHARE_NAME_KEY)
        if isinstance(raw, str) and raw.strip():
            names.append(raw.strip())
    return names


def _parse_share_snapshots(
    built: _Built, share: str, snap_payload: dict[str, object], now: float
) -> bool:
    """Append per-share snapshot observations; return True iff this share has snapshots.

    Always emits ``snapshot_count{share}`` = len(snapshots[]) (0 included). Emits
    ``snapshot_latest_age_seconds{share}`` only when at least one snapshot time
    parsed. Returns whether the share had >=1 snapshot (drives snapshots_configured).
    """
    snapshots = as_list_of_dicts(nested(snap_payload, "snapshots"))
    built.snapshot_count_obs.append(({"share": share}, float(len(snapshots))))
    latest = _latest_snapshot_time(snapshots)
    if latest is not None:
        age = max(0.0, now - latest)
        built.snapshot_latest_age_seconds_obs.append(({"share": share}, age))
    return len(snapshots) > 0


def _emit(
    ctx: CollectorContext, built: _Built, events: list[CollectorEvent], emitted: list[int]
) -> None:
    """Cap-route every family through one CappedEmitter."""
    emitter = capped_emitter(ctx, events)

    def family(name: str, obs: list[tuple[dict[str, str], float]]) -> None:
        emitted[0] += emitter.emit_family(name, cap_for_synology(name), obs) + 1

    family(M_SNAPSHOT_COUNT, built.snapshot_count_obs)
    family(M_SNAPSHOT_LATEST_AGE_SECONDS, built.snapshot_latest_age_seconds_obs)
    family(M_SNAPSHOTS_CONFIGURED, built.snapshots_configured_obs)
    family(M_REPLICATION_AVAILABLE, built.replication_available_obs)


class SynologyReplicationCollector(BaseCollector):
    """Emit per-share Btrfs snapshot counts/age + replication availability from DSM.

    Polls once per 5-min tick in the ``synology`` concurrency group. Enumerates
    shares, fans out per-share snapshot fetches (each isolated — one failing keeps
    the rest), and probes Btrfs replication availability. No fetch is primary:
    ok=False ONLY when EVERYTHING failed. An unconfigured client is ok=False. The
    snapshots_configured + replication_available sentinels ALWAYS emit (the
    alertable empty-NAS contract).
    """

    name: ClassVar[str] = "synology_replication"
    interval: ClassVar[timedelta] = timedelta(seconds=300)
    timeout: ClassVar[timedelta] = timedelta(seconds=30)
    concurrency_group: ClassVar[str] = "synology"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Enumerate shares, fetch per-share snapshots + replica probe, emit families."""
        start = time.monotonic()
        if ctx.synology is None:
            return client_unconfigured_result(start)

        emitted: list[int] = [0]
        errors: list[str] = []
        events: list[CollectorEvent] = []
        built = _Built()
        now = datetime.now(UTC).timestamp()

        # CO-EQUAL fetch 1: share list (drives the per-share fan-out).
        share_resp = _fetch(ctx, await ctx.synology.share_list(), start, emitted, errors)
        share_names: list[str] = []
        if share_resp is not None:
            share_payload = as_dict(share_resp.payload)
            if share_payload is not None:
                share_names = _share_names(share_payload)

        # CO-EQUAL fetch 2..N: one isolated snapshot fetch per enumerated share.
        any_share_snapshot_ok = False
        any_share_has_snapshots = False
        for share in share_names:
            snap_resp = _fetch(
                ctx, await ctx.synology.share_snapshot_list(share), start, emitted, errors
            )
            if snap_resp is None:
                continue  # failed fetch: skip share (unknown, not zero)
            any_share_snapshot_ok = True
            snap_payload = as_dict(snap_resp.payload)
            if snap_payload is not None:
                has = _parse_share_snapshots(built, share, snap_payload, now)
                any_share_has_snapshots = any_share_has_snapshots or has

        # CO-EQUAL fetch last: replication availability probe (self-correcting).
        repl_resp = _fetch(ctx, await ctx.synology.replica_core_list(), start, emitted, errors)
        repl_available = 0.0
        if repl_resp is not None and as_dict(repl_resp.payload) is not None:
            repl_available = 1.0

        # ALWAYS-EMIT sentinels.
        built.snapshots_configured_obs.append(({}, 1.0 if any_share_has_snapshots else 0.0))
        built.replication_available_obs.append(({}, repl_available))

        # ALWAYS emit (even on a fully-failed run: empty families emit drop gauge only).
        _emit(ctx, built, events, emitted)

        # CO-EQUAL: ok=False ONLY when EVERYTHING failed.
        ok = share_resp is not None or any_share_snapshot_ok or repl_resp is not None
        return CollectorResult(
            ok=ok,
            metrics_emitted=emitted[0],
            errors=errors,
            events=events,
            duration_seconds=time.monotonic() - start,
        )
