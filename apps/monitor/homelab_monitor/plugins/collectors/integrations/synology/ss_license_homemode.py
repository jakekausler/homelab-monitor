"""synology_license collector — Surveillance Station License + HomeMode gauges.

EPIC-008 STAGE-008-017 (Option A). Fetches TWO CO-EQUAL DSM APIs once per 300s tick:
  - SYNO.SurveillanceStation.License/Load -> license counts (total/used/max/localCamCnt)
  - SYNO.SurveillanceStation.HomeMode/GetInfo -> Home Mode toggle + notify/schedule flags

CO-EQUAL COMBINE (mirrors STAGE-008-015 cameras.py / STAGE-008-016 events.py): there is NO
primary. ``_fetch`` records-and-continues on EITHER fetch's client error; the run is ok=False
ONLY when BOTH fetches fail (``ok = license_resp is not None or homemode_resp is not None``). A
single-fetch failure is a DEGRADED ok=True run. ``_emit`` ALWAYS runs. An unconfigured client
is ok=False.

SEEDING (alertable empty-NAS contract): ALL 10 gauges are single-series, NO labels, SEEDED
0.0 in ``_Built.__init__`` and OVERWRITTEN (list reassignment, NOT .append) on a successful
parse. A failed/absent fetch leaves the seeded 0.0 series, so every gauge ALWAYS emits.

LICENSE SCALARS (key_total/key_used/key_max/localCamCnt) parse via as_float; a non-numeric /
absent field leaves the 0.0 seed.

LICENSE EXHAUSTED (DERIVED): 1.0 when key_used > key_total, else 0.0. If EITHER key_used or
key_total is absent/non-numeric, the seed 0.0 is left (no false positive).

HOMEMODE FLAGS (on/notify_on/mode_schedule_on/rec_schedule_on/streaming_on) parse via
bool_to_gauge; a non-bool / absent value leaves the 0.0 seed (bool_to_gauge returns None).

The license parse and homemode parse are INDEPENDENT: a failed/absent license fetch does not
prevent the homemode parse, and vice-versa (mirrors cameras.py's independent passes).

CARDINALITY: every family is cap-routed through ``capped_emitter`` + ``cap_for_synology``
(default 500). ``metrics_emitted`` = sum of ``emit_family() + 1`` per family + the api_took
gauges from each successful fetch.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar, Final

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorEvent, CollectorResult
from homelab_monitor.kernel.synology.client import SynologyResponse
from homelab_monitor.kernel.synology.errors import SynologyError
from homelab_monitor.plugins.collectors.integrations.synology._shared import (
    as_dict,
    as_float,
    bool_to_gauge,
    cap_for_synology,
    capped_emitter,
    client_unconfigured_result,
    fetch_or_result,
)

# --- License families (SEEDED 0.0 — always emit) --------------------------------
M_LICENSE_TOTAL: Final[str] = "homelab_synology_ss_license_total"
M_LICENSE_USED: Final[str] = "homelab_synology_ss_license_used"
M_LICENSE_MAX: Final[str] = "homelab_synology_ss_license_max"
M_LICENSE_EXHAUSTED: Final[str] = "homelab_synology_ss_license_exhausted"
M_LICENSE_CAMERA_COUNT: Final[str] = "homelab_synology_ss_license_camera_count"

# --- HomeMode families (SEEDED 0.0 — always emit) -------------------------------
M_HOMEMODE_ON: Final[str] = "homelab_synology_ss_homemode_on"
M_HOMEMODE_NOTIFY_ON: Final[str] = "homelab_synology_ss_homemode_notify_on"
M_HOMEMODE_SCHEDULE_ON: Final[str] = "homelab_synology_ss_homemode_schedule_on"
M_HOMEMODE_REC_SCHEDULE_ON: Final[str] = "homelab_synology_ss_homemode_rec_schedule_on"
M_HOMEMODE_STREAMING_ON: Final[str] = "homelab_synology_ss_homemode_streaming_on"

# DSM license payload keys.
_K_TOTAL: Final[str] = "key_total"
_K_USED: Final[str] = "key_used"
_K_MAX: Final[str] = "key_max"
_K_CAMERA_COUNT: Final[str] = "localCamCnt"

# DSM HomeMode payload keys.
_K_ON: Final[str] = "on"
_K_NOTIFY_ON: Final[str] = "notify_on"
_K_SCHEDULE_ON: Final[str] = "mode_schedule_on"
_K_REC_SCHEDULE_ON: Final[str] = "rec_schedule_on"
_K_STREAMING_ON: Final[str] = "streaming_on"


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

    All 10 gauges are single-series, NO labels, SEEDED 0.0 in __init__ so a failed/absent fetch
    still emits the alertable series; a successful parse OVERWRITES the relevant list (list
    reassignment, NOT .append).
    """

    __slots__ = (
        "homemode_notify_on_obs",
        "homemode_on_obs",
        "homemode_rec_schedule_on_obs",
        "homemode_schedule_on_obs",
        "homemode_streaming_on_obs",
        "license_camera_count_obs",
        "license_exhausted_obs",
        "license_max_obs",
        "license_total_obs",
        "license_used_obs",
    )

    def __init__(self) -> None:
        """Initialise all 10 single-series gauges with 0.0 baselines."""
        # License (SEEDED 0.0).
        self.license_total_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        self.license_used_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        self.license_max_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        self.license_exhausted_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        self.license_camera_count_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        # HomeMode (SEEDED 0.0).
        self.homemode_on_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        self.homemode_notify_on_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        self.homemode_schedule_on_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        self.homemode_rec_schedule_on_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        self.homemode_streaming_on_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]


# ---------------------------------------------------------------------------
# Parse passes (INDEPENDENT)
# ---------------------------------------------------------------------------


def _parse_license(built: _Built, payload: dict[str, object]) -> None:
    """Parse the License/Load payload -> OVERWRITE the 5 seeded license gauges.

    Each scalar (total/used/max/camera_count) is read via as_float; a numeric value OVERWRITES
    the 0.0 seed, a None leaves it. ``license_exhausted`` is DERIVED: 1.0 when used > total,
    else 0.0 — but ONLY when BOTH used and total are numeric; if either is None the 0.0 seed is
    left (no false positive).
    """
    total = as_float(payload.get(_K_TOTAL))
    if total is not None:
        built.license_total_obs = [({}, total)]

    used = as_float(payload.get(_K_USED))
    if used is not None:
        built.license_used_obs = [({}, used)]

    max_lic = as_float(payload.get(_K_MAX))
    if max_lic is not None:
        built.license_max_obs = [({}, max_lic)]

    cam_count = as_float(payload.get(_K_CAMERA_COUNT))
    if cam_count is not None:
        built.license_camera_count_obs = [({}, cam_count)]

    # DERIVED exhausted: needs BOTH used and total numeric.
    if used is not None and total is not None:
        built.license_exhausted_obs = [({}, 1.0 if used > total else 0.0)]


def _parse_homemode(built: _Built, payload: dict[str, object]) -> None:
    """Parse the HomeMode/GetInfo payload -> OVERWRITE the 5 seeded homemode gauges.

    Each flag is read via bool_to_gauge; a bool maps to 1.0/0.0 and OVERWRITES the 0.0 seed, a
    non-bool / absent value returns None and leaves the seed.
    """
    on = bool_to_gauge(payload.get(_K_ON))
    if on is not None:
        built.homemode_on_obs = [({}, on)]

    notify_on = bool_to_gauge(payload.get(_K_NOTIFY_ON))
    if notify_on is not None:
        built.homemode_notify_on_obs = [({}, notify_on)]

    schedule_on = bool_to_gauge(payload.get(_K_SCHEDULE_ON))
    if schedule_on is not None:
        built.homemode_schedule_on_obs = [({}, schedule_on)]

    rec_schedule_on = bool_to_gauge(payload.get(_K_REC_SCHEDULE_ON))
    if rec_schedule_on is not None:
        built.homemode_rec_schedule_on_obs = [({}, rec_schedule_on)]

    streaming_on = bool_to_gauge(payload.get(_K_STREAMING_ON))
    if streaming_on is not None:
        built.homemode_streaming_on_obs = [({}, streaming_on)]


def _emit(
    ctx: CollectorContext, built: _Built, events: list[CollectorEvent], emitted: list[int]
) -> None:
    """Cap-route every family through one CappedEmitter."""
    emitter = capped_emitter(ctx, events)

    def family(name: str, obs: list[tuple[dict[str, str], float]]) -> None:
        emitted[0] += emitter.emit_family(name, cap_for_synology(name), obs) + 1

    # License (seeded)
    family(M_LICENSE_TOTAL, built.license_total_obs)
    family(M_LICENSE_USED, built.license_used_obs)
    family(M_LICENSE_MAX, built.license_max_obs)
    family(M_LICENSE_EXHAUSTED, built.license_exhausted_obs)
    family(M_LICENSE_CAMERA_COUNT, built.license_camera_count_obs)
    # HomeMode (seeded)
    family(M_HOMEMODE_ON, built.homemode_on_obs)
    family(M_HOMEMODE_NOTIFY_ON, built.homemode_notify_on_obs)
    family(M_HOMEMODE_SCHEDULE_ON, built.homemode_schedule_on_obs)
    family(M_HOMEMODE_REC_SCHEDULE_ON, built.homemode_rec_schedule_on_obs)
    family(M_HOMEMODE_STREAMING_ON, built.homemode_streaming_on_obs)


class SynologyLicenseCollector(BaseCollector):
    """Emit Surveillance Station License + HomeMode gauges from 2 CO-EQUAL DSM APIs.

    Polls once per 300s tick in the ``synology`` concurrency group. Neither fetch is primary: a
    single fetch failing records its error but keeps ok=True; ok=False ONLY when BOTH fetches
    fail. An unconfigured client is ok=False. All 10 gauges (seeded 0.0) ALWAYS emit.
    """

    name: ClassVar[str] = "synology_license"
    interval: ClassVar[timedelta] = timedelta(seconds=300)
    timeout: ClassVar[timedelta] = timedelta(seconds=30)
    concurrency_group: ClassVar[str] = "synology"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Fetch License + HomeMode co-equally, parse, emit cap-routed seeded families."""
        start = time.monotonic()
        if ctx.synology is None:
            return client_unconfigured_result(start)

        emitted: list[int] = [0]
        errors: list[str] = []
        events: list[CollectorEvent] = []
        built = _Built()

        # CO-EQUAL fetch 1: License/Load (5 license gauges).
        license_resp = _fetch(ctx, await ctx.synology.ss_license(), start, emitted, errors)
        if license_resp is not None:
            license_payload = as_dict(license_resp.payload)
            if license_payload is not None:
                _parse_license(built, license_payload)

        # CO-EQUAL fetch 2: HomeMode/GetInfo (5 homemode gauges).
        homemode_resp = _fetch(ctx, await ctx.synology.ss_homemode(), start, emitted, errors)
        if homemode_resp is not None:
            homemode_payload = as_dict(homemode_resp.payload)
            if homemode_payload is not None:
                _parse_homemode(built, homemode_payload)

        # ALWAYS emit (seeded gauges emit even on a both-failed run).
        _emit(ctx, built, events, emitted)

        # CO-EQUAL: ok=False ONLY when BOTH fetches failed.
        ok = license_resp is not None or homemode_resp is not None
        return CollectorResult(
            ok=ok,
            metrics_emitted=emitted[0],
            errors=errors,
            events=events,
            duration_seconds=time.monotonic() - start,
        )
