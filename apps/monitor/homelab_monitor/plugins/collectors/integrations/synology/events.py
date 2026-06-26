"""synology_events collector — Surveillance Station EVENT & RECORDING aggregate counters.

EPIC-008 STAGE-008-016 (Option A). Fetches TWO CO-EQUAL DSM APIs once per 300s tick:
  - SYNO.SurveillanceStation.Event/CountByCategory -> event counts + date map + total
  - SYNO.SurveillanceStation.Recording/List -> recordings[] (count + sizeByte) + total

PRIVACY LOCK (load-bearing): this collector emits AGGREGATE COUNTERS ONLY. It NEVER emits a
per-event or per-recording time series. The recording list is iterated solely to derive
per-camera record COUNTS + summed BYTES + a global byte sum; individual recording ids /
filePaths / timestamps are NEVER surfaced.

CO-EQUAL COMBINE (mirrors STAGE-008-015 cameras.py): there is NO primary. ``_fetch`` records-
and-continues on EITHER fetch's client error; the run is ok=False ONLY when BOTH fetches fail
(``ok = event_resp is not None or rec_resp is not None``). A single-fetch failure is a DEGRADED
ok=True run. ``_emit`` ALWAYS runs. An unconfigured client is ok=False.

SEEDING (alertable empty-NAS contract):
  - The 4 system rollups (events_today / events_total_all / recordings_total /
    recordings_bytes_total) are SEEDED 0.0 in ``_Built.__init__`` and OVERWRITTEN with real
    values on a successful parse. A failed fetch leaves the seeded 0.0 series, so they ALWAYS
    emit.
  - The 3 per-camera families (events_total / recordings_count / recordings_bytes) are EMIT-ON-
    PRESENCE (you cannot seed an unknown camera set): empty when the relevant fetch fails / the
    map/list is absent / empty.

PER-ENTRY ISOLATION (mirrors storage.py / cameras.py): each ``evt_cam["0"]`` entry and each
recording record is parsed in isolation — a malformed entry/record emits what it can and NEVER
raises or aborts the loop.

EVENTS-TODAY TIMEZONE (load-bearing): ``events_today`` reads ``date[<today>]`` where the key is
"YYYY/MM/DD" in the NAS-LOCAL / DISPLAY timezone, NOT UTC. There is currently NO display-TZ
plumbing reachable from a CollectorContext, so this module pins ``_DISPLAY_TZ`` to
America/New_York. DEVIATION FOR CODE REVIEW: when a configured-display-TZ accessor becomes
reachable from ctx, replace ``_DISPLAY_TZ`` with that. ``today_key(now)`` takes an injected
``now`` so the day-boundary is unit-testable.

CARDINALITY: every family is cap-routed through ``capped_emitter`` + ``cap_for_synology``
(default 500). ``metrics_emitted`` = sum of ``emit_family() + 1`` per family + the api_took
gauges from each successful fetch.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import ClassVar, Final
from zoneinfo import ZoneInfo

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorEvent, CollectorResult
from homelab_monitor.kernel.synology.client import SynologyResponse
from homelab_monitor.kernel.synology.errors import SynologyError
from homelab_monitor.plugins.collectors.integrations.synology._shared import (
    as_dict,
    as_float,
    as_list_of_dicts,
    bytes_field,
    cap_for_synology,
    capped_emitter,
    client_unconfigured_result,
    fetch_or_result,
    nested,
)

# --- Per-camera metric family names (emit-on-presence) --------------------------
M_EVENTS_TOTAL: Final[str] = "homelab_synology_ss_events_total"
M_RECORDINGS_COUNT: Final[str] = "homelab_synology_ss_recordings_count"
M_RECORDINGS_BYTES: Final[str] = "homelab_synology_ss_recordings_bytes"

# --- System rollups (SEEDED 0.0 — always emit) ----------------------------------
M_EVENTS_TODAY: Final[str] = "homelab_synology_ss_events_today"
M_EVENTS_TOTAL_ALL: Final[str] = "homelab_synology_ss_events_total_all"
M_RECORDINGS_TOTAL: Final[str] = "homelab_synology_ss_recordings_total"
M_RECORDINGS_BYTES_TOTAL: Final[str] = "homelab_synology_ss_recordings_bytes_total"

# TODO: pin display-TZ to a configured value — deferred (non-blocking): no backend
# display-TZ config exists in kernel/config.py or CollectorContext today; the DSM `date`
# map is keyed in NAS-local time, so events_today must use the LOCAL date key. Revisit if a
# backend display-timezone config/stage lands (would plumb it via ctx instead of this pin).
_DISPLAY_TZ: Final[ZoneInfo] = ZoneInfo("America/New_York")

# evt_cam group-total sentinel key (skipped — it is NOT a camera).
_GROUP_KEY: Final[str] = "-1"

# Composite-key split: "<id>-<name>" -> exactly 2 parts when split on the first '-'.
_COMPOSITE_PARTS: Final[int] = 2

# ---------------------------------------------------------------------------
# Multi-fetch wrapper: record-and-continue for INDEPENDENT fetches
# (copied verbatim from STAGE-008-015 cameras.py)
# ---------------------------------------------------------------------------


def _fetch(
    ctx: CollectorContext,
    response: SynologyResponse | SynologyError,
    start: float,
    emitted: list[int],
    errors: list[str],
) -> SynologyResponse | None:
    """Wrap fetch_or_result for INDEPENDENT (non-early-returning) fetches.

    On a client error fetch_or_result returns a CollectorResult (errors populated); we record
    those error strings into ``errors`` and return None instead of aborting. On success it has
    already emitted api_took + bumped emitted[0]; we return the response.
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
    """Per-tick observation lists, one per cap-routed metric family.

    The 4 system rollups are SEEDED 0.0 in __init__ so a failed/absent fetch still emits the
    alertable series; a successful parse OVERWRITES the relevant list. The 3 per-camera families
    start empty (emit-on-presence).
    """

    __slots__ = (
        "events_today_obs",
        "events_total_all_obs",
        "events_total_obs",
        "recordings_bytes_obs",
        "recordings_bytes_total_obs",
        "recordings_count_obs",
        "recordings_total_obs",
    )

    def __init__(self) -> None:
        """Initialise lists; seed the 4 system rollups with 0.0 baselines."""
        # Per-camera (emit-on-presence).
        self.events_total_obs: list[tuple[dict[str, str], float]] = []
        self.recordings_count_obs: list[tuple[dict[str, str], float]] = []
        self.recordings_bytes_obs: list[tuple[dict[str, str], float]] = []
        # System rollups (SEEDED 0.0 — always emit).
        self.events_today_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        self.events_total_all_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        self.recordings_total_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        self.recordings_bytes_total_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]


# ---------------------------------------------------------------------------
# Local parse helpers
# ---------------------------------------------------------------------------


def today_key(now: datetime) -> str:
    """Return the ``date`` map key for ``now`` formatted as DSM's "YYYY/MM/DD".

    PURE + injected-now so the day boundary is unit-testable. ``now`` is expected to already be
    in the DISPLAY timezone (run() passes ``datetime.now(_DISPLAY_TZ)``); the key uses the
    local wall-clock date, NOT UTC.
    """
    return now.strftime("%Y/%m/%d")


def _event_camera_label(key: str) -> str | None:
    """Split an ``evt_cam["0"]`` composite key "<id>-<name>" into the ``camera`` label, or None.

    Splits on the FIRST '-'. name (non-empty) wins; else falls back to id_str. A key with no '-'
    at all (split length != 2) -> None (skip). Both parts empty -> None (skip).
    """
    parts = key.split("-", 1)
    if len(parts) != _COMPOSITE_PARTS:
        return None
    id_str, name = parts[0].strip(), parts[1].strip()
    if name:
        return name
    if id_str:
        return id_str
    return None


# ---------------------------------------------------------------------------
# Parse passes
# ---------------------------------------------------------------------------


def _parse_events(built: _Built, payload: dict[str, object], today_key: str) -> None:
    """Parse the event-count payload -> per-camera events_total + 2 seeded rollups.

    PER-CAMERA: iterate ``evt_cam["0"]`` (defensive: missing / non-dict -> {}). SKIP the "-1"
    group key. Split each composite key; a key with no usable label OR a non-numeric count is
    skipped in isolation.

    ROLLUPS (overwrite seeded only when present/numeric):
      - events_total_all: top-level ``total`` (numeric) wins; else ``date["-1"]`` (numeric).
      - events_today: ``date[today_key]["-1"]`` (numeric). Absent today key / nested "-1" leaves
        the seeded 0.0.
    """
    cam_map = as_dict(nested(payload, "evt_cam", "0"))
    if cam_map is not None:
        for key, raw in cam_map.items():
            if key == _GROUP_KEY:
                continue
            label = _event_camera_label(key)
            if label is None:
                continue
            count = as_float(raw)
            if count is None:
                continue
            built.events_total_obs.append(({"camera": label}, count))

    # events_total_all: prefer top-level total, fall back to date["-1"].
    total = as_float(payload.get("total"))
    if total is None:
        total = as_float(nested(payload, "date", _GROUP_KEY))
    if total is not None:
        built.events_total_all_obs = [({}, total)]

    # events_today: date[today_key]["-1"] (LOCAL date key).
    today = as_float(nested(payload, "date", today_key, _GROUP_KEY))
    if today is not None:
        built.events_today_obs = [({}, today)]


def _parse_recordings(built: _Built, payload: dict[str, object]) -> None:
    """Parse the recording-list payload -> per-camera count/bytes + 2 seeded rollups.

    recordings_total: top-level ``total`` (numeric) OVERWRITES the seeded 0.0.

    Iterate ``recordings`` (as_list_of_dicts drops non-dict records). For each record:
      - sizeByte via bytes_field; None -> contributes 0.0 (treated as 0) but the record STILL
        counts toward its camera's recordings_count.
      - cameraName label: non-empty stripped string. A record with no usable label STILL adds to
        recordings_bytes_total but is NOT counted per-camera.
    After the loop: set recordings_bytes_total (overwrite seed with the global sum) and append
    per-camera recordings_count + recordings_bytes from the accumulators.
    """
    total = as_float(payload.get("total"))
    if total is not None:
        built.recordings_total_obs = [({}, total)]

    bytes_total = 0.0
    per_cam_count: dict[str, float] = {}
    per_cam_bytes: dict[str, float] = {}
    for rec in as_list_of_dicts(nested(payload, "recordings")):
        size = bytes_field(rec.get("sizeByte"))
        size_val = size if size is not None else 0.0
        bytes_total += size_val

        name = rec.get("cameraName")
        if isinstance(name, str) and name.strip():
            label = name.strip()
            per_cam_count[label] = per_cam_count.get(label, 0.0) + 1.0
            per_cam_bytes[label] = per_cam_bytes.get(label, 0.0) + size_val

    built.recordings_bytes_total_obs = [({}, bytes_total)]
    for label, cnt in per_cam_count.items():
        built.recordings_count_obs.append(({"camera": label}, cnt))
    for label, byts in per_cam_bytes.items():
        built.recordings_bytes_obs.append(({"camera": label}, byts))


def _emit(
    ctx: CollectorContext, built: _Built, events: list[CollectorEvent], emitted: list[int]
) -> None:
    """Cap-route every family through one CappedEmitter."""
    emitter = capped_emitter(ctx, events)

    def family(name: str, obs: list[tuple[dict[str, str], float]]) -> None:
        emitted[0] += emitter.emit_family(name, cap_for_synology(name), obs) + 1

    # Per-camera (emit-on-presence)
    family(M_EVENTS_TOTAL, built.events_total_obs)
    family(M_RECORDINGS_COUNT, built.recordings_count_obs)
    family(M_RECORDINGS_BYTES, built.recordings_bytes_obs)
    # System rollups (seeded)
    family(M_EVENTS_TODAY, built.events_today_obs)
    family(M_EVENTS_TOTAL_ALL, built.events_total_all_obs)
    family(M_RECORDINGS_TOTAL, built.recordings_total_obs)
    family(M_RECORDINGS_BYTES_TOTAL, built.recordings_bytes_total_obs)


class SynologyEventsCollector(BaseCollector):
    """Emit Surveillance Station event + recording AGGREGATE counters from 2 CO-EQUAL DSM APIs.

    Polls once per 300s tick in the ``synology`` concurrency group. Neither fetch is primary: a
    single fetch failing records its error but keeps ok=True; ok=False ONLY when BOTH fetches
    fail. An unconfigured client is ok=False. The 4 system rollups (seeded 0.0) ALWAYS emit; the
    3 per-camera families are emit-on-presence. PRIVACY LOCK: aggregate counters only.
    """

    name: ClassVar[str] = "synology_events"
    interval: ClassVar[timedelta] = timedelta(seconds=300)
    timeout: ClassVar[timedelta] = timedelta(seconds=30)
    concurrency_group: ClassVar[str] = "synology"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Fetch event-count + recording-list co-equally, aggregate, emit cap-routed families."""
        start = time.monotonic()
        if ctx.synology is None:
            return client_unconfigured_result(start)

        emitted: list[int] = [0]
        errors: list[str] = []
        events: list[CollectorEvent] = []
        built = _Built()
        today_key_val = today_key(datetime.now(_DISPLAY_TZ))

        # CO-EQUAL fetch 1: event counts (per-camera events + 2 rollups).
        event_resp = _fetch(
            ctx, await ctx.synology.ss_event_count_by_category(), start, emitted, errors
        )
        if event_resp is not None:
            event_payload = as_dict(event_resp.payload)
            if event_payload is not None:
                _parse_events(built, event_payload, today_key_val)

        # CO-EQUAL fetch 2: recording list (per-camera count/bytes + 2 rollups).
        rec_resp = _fetch(ctx, await ctx.synology.ss_recording_list(), start, emitted, errors)
        if rec_resp is not None:
            rec_payload = as_dict(rec_resp.payload)
            if rec_payload is not None:
                _parse_recordings(built, rec_payload)

        # ALWAYS emit (seeded rollups emit even on a both-failed run).
        _emit(ctx, built, events, emitted)

        # CO-EQUAL: ok=False ONLY when BOTH fetches failed.
        ok = event_resp is not None or rec_resp is not None
        return CollectorResult(
            ok=ok,
            metrics_emitted=emitted[0],
            errors=errors,
            events=events,
            duration_seconds=time.monotonic() - start,
        )
