"""synology_ups collector — UPS power state + DSM SystemHealth (STAGE-008-009).

LAST stage of Wave B. Fetches TWO INDEPENDENT DSM APIs in one tick:
  - SYNO.Core.ExternalDevice.UPS get -> charge/runtime/status/connect/model/manufacture
  - SYNO.Core.System.SystemHealth get -> rule (id/priority) OR healthy when absent

CO-EQUAL COMBINE (the key difference vs STAGE-008-007's system.py): there is NO
primary. ``_fetch`` (copied from 007) records-and-continues on EITHER fetch's
client error; the run is ok=False ONLY when BOTH fetches fail
(``ok = ups_resp is not None or health_resp is not None``). A single-fetch failure
is a DEGRADED ok=True run — the other API's families still emit; the failed API's
families are empty (drop gauge only). ``_emit`` ALWAYS runs, even on a both-failed
run (each empty family emits only its free drop gauge).

PARSE — defensive. Both payloads are FLAT dicts; the parse reads them with
``as_dict`` + ``.get()`` directly. ``as_float`` rejects bool, returns None on
non-finite / non-numeric / non-numeric-str. Local helpers map DSM-specific shapes:
``_ups_status_state`` strips the ``usb_ups_status_`` prefix; ``_strip_label``
trims DSM's trailing-newline identity strings.

STATE-SET: ``M_UPS_STATUS`` is a per-state series ({state}=1.0 for the OBSERVED
state); ``M_HEALTH_RULE`` is a per-rule series ({rule}=1.0). Derived booleans
``M_UPS_ON_BATTERY`` / ``M_UPS_LOW_BATTERY`` are 1.0/0.0 from the same state.

CARDINALITY: every family is cap-routed through ``capped_emitter`` +
``cap_for_synology`` (default 500). ``metrics_emitted`` = sum of
``emit_family() + 1`` per family + the api_took gauges from each successful fetch.
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

# --- Metric family names ----------------------------------------------------
# Labels (kept out of inline comments to stay <=100 cols):
#   charge_percent / runtime_seconds / on_battery / low_battery / connected :
#       no labels (single series)
#   status   : {state} = 1.0                       (per-state, observed only)
#   info     : {model, manufacture} = 1.0          (identity)
M_UPS_CHARGE_PERCENT: Final[str] = "homelab_synology_ups_charge_percent"
M_UPS_RUNTIME_SECONDS: Final[str] = "homelab_synology_ups_runtime_seconds"
M_UPS_STATUS: Final[str] = "homelab_synology_ups_status"
M_UPS_ON_BATTERY: Final[str] = "homelab_synology_ups_on_battery"
M_UPS_LOW_BATTERY: Final[str] = "homelab_synology_ups_low_battery"
M_UPS_CONNECTED: Final[str] = "homelab_synology_ups_connected"
M_UPS_INFO: Final[str] = "homelab_synology_ups_info"

# --- SystemHealth family names ----------------------------------------------
#   health_ok / health_priority : no labels (single series)
#   health_rule : {rule} = 1.0                     (per-rule, present only)
M_HEALTH_OK: Final[str] = "homelab_synology_health_ok"
M_HEALTH_RULE: Final[str] = "homelab_synology_health_rule"
M_HEALTH_PRIORITY: Final[str] = "homelab_synology_health_priority"

_UPS_STATUS_PREFIX: Final[str] = "usb_ups_status_"

_ON_BATTERY: Final[str] = "on_battery"
_LOW_BATTERY: Final[str] = "low_battery"


# ---------------------------------------------------------------------------
# Local DSM-shape helpers (NOT shared — ups-specific)
# ---------------------------------------------------------------------------


def _ups_status_state(v: object) -> str | None:
    """Normalize a DSM UPS status string to its bare state.

    e.g. 'usb_ups_status_on_battery' -> 'on_battery'. None for non-str / empty.
    """
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s:
        return None
    return s.removeprefix(_UPS_STATUS_PREFIX)


def _strip_label(v: object) -> str | None:
    """Return v.strip() when v is a non-empty str (after stripping), else None.

    Used for UPS model/manufacture identity labels (DSM appends a trailing
    newline / space).
    """
    if not isinstance(v, str):
        return None
    s = v.strip()
    return s if s else None


# ---------------------------------------------------------------------------
# Multi-fetch wrapper: record-and-continue for INDEPENDENT fetches
# (copied verbatim from STAGE-008-007 system.py; the co-equal combine in run()
# differs, but this wrapper is identical)
# ---------------------------------------------------------------------------


def _fetch(
    ctx: CollectorContext,
    response: SynologyResponse | SynologyError,
    start: float,
    emitted: list[int],
    errors: list[str],
) -> SynologyResponse | None:
    """Wrap fetch_or_result for INDEPENDENT (non-early-returning) fetches.

    Copied from STAGE-008-007 system.py. On a client error fetch_or_result
    returns a CollectorResult (errors populated); we record those error strings
    into ``errors`` and return None instead of aborting. On success it has
    already emitted api_took + bumped emitted[0]; we return the SynologyResponse.
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
        "health_ok_obs",
        "health_priority_obs",
        "health_rule_obs",
        "ups_charge_obs",
        "ups_connected_obs",
        "ups_info_obs",
        "ups_low_battery_obs",
        "ups_on_battery_obs",
        "ups_runtime_obs",
        "ups_status_obs",
    )

    def __init__(self) -> None:
        """Initialise every observation list empty."""
        self.ups_charge_obs: list[tuple[dict[str, str], float]] = []
        self.ups_runtime_obs: list[tuple[dict[str, str], float]] = []
        self.ups_status_obs: list[tuple[dict[str, str], float]] = []
        self.ups_on_battery_obs: list[tuple[dict[str, str], float]] = []
        self.ups_low_battery_obs: list[tuple[dict[str, str], float]] = []
        self.ups_connected_obs: list[tuple[dict[str, str], float]] = []
        self.ups_info_obs: list[tuple[dict[str, str], float]] = []
        self.health_ok_obs: list[tuple[dict[str, str], float]] = []
        self.health_rule_obs: list[tuple[dict[str, str], float]] = []
        self.health_priority_obs: list[tuple[dict[str, str], float]] = []


# ---------------------------------------------------------------------------
# Parse passes
# ---------------------------------------------------------------------------


def _parse_ups(built: _Built, ups: dict[str, object]) -> None:
    """Append UPS observations from the flat ExternalDevice.UPS payload.

    charge / runtime are single-series numerics (as_float; None -> skip). status
    drives a per-state series PLUS the two derived booleans (all three share the
    ``state is not None`` guard). connected ALWAYS emits (bool gauge when present,
    else 0.0). info identity is anchored on a non-empty ``model`` (whole-metric
    skip when absent), with ``manufacture`` added when present.
    """
    charge = as_float(ups.get("charge"))
    if charge is not None:
        built.ups_charge_obs.append(({}, charge))

    runtime = as_float(ups.get("runtime"))
    if runtime is not None:
        built.ups_runtime_obs.append(({}, runtime))

    state = _ups_status_state(ups.get("status"))
    if state is not None:
        built.ups_status_obs.append(({"state": state}, 1.0))
        built.ups_on_battery_obs.append(({}, 1.0 if state == _ON_BATTERY else 0.0))
        built.ups_low_battery_obs.append(({}, 1.0 if state == _LOW_BATTERY else 0.0))

    # ALWAYS-EMIT family: bool gauge when present, else 0.0 (no `if` guard).
    conn = bool_to_gauge(ups.get("usb_ups_connect"))
    built.ups_connected_obs.append(({}, conn if conn is not None else 0.0))

    model = _strip_label(ups.get("model"))
    if model is not None:
        labels = {"model": model}
        manu = _strip_label(ups.get("manufacture"))
        if manu is not None:
            labels["manufacture"] = manu
        built.ups_info_obs.append((labels, 1.0))


def _parse_health(built: _Built, health: dict[str, object]) -> None:
    """Append SystemHealth observations from the flat SystemHealth payload.

    ``rule`` absent / null -> healthy (health_ok=1, no rule/priority). ``rule``
    present -> health_ok=0 plus a per-rule series (when rule.id is a non-empty
    str) and a priority gauge (when rule.priority parses).
    """
    rule = as_dict(health.get("rule"))
    if rule is None:
        built.health_ok_obs.append(({}, 1.0))
        return

    built.health_ok_obs.append(({}, 0.0))
    rule_id = rule.get("id")
    if isinstance(rule_id, str) and rule_id:
        built.health_rule_obs.append(({"rule": rule_id}, 1.0))
    prio = as_float(rule.get("priority"))
    if prio is not None:
        built.health_priority_obs.append(({}, prio))


def _emit(
    ctx: CollectorContext, built: _Built, events: list[CollectorEvent], emitted: list[int]
) -> None:
    """Cap-route every family through one CappedEmitter."""
    emitter = capped_emitter(ctx, events)

    def family(name: str, obs: list[tuple[dict[str, str], float]]) -> None:
        emitted[0] += emitter.emit_family(name, cap_for_synology(name), obs) + 1

    family(M_UPS_CHARGE_PERCENT, built.ups_charge_obs)
    family(M_UPS_RUNTIME_SECONDS, built.ups_runtime_obs)
    family(M_UPS_STATUS, built.ups_status_obs)
    family(M_UPS_ON_BATTERY, built.ups_on_battery_obs)
    family(M_UPS_LOW_BATTERY, built.ups_low_battery_obs)
    family(M_UPS_CONNECTED, built.ups_connected_obs)
    family(M_UPS_INFO, built.ups_info_obs)
    family(M_HEALTH_OK, built.health_ok_obs)
    family(M_HEALTH_RULE, built.health_rule_obs)
    family(M_HEALTH_PRIORITY, built.health_priority_obs)


class SynologyUPSCollector(BaseCollector):
    """Emit UPS power state + DSM SystemHealth from 2 CO-EQUAL DSM APIs.

    Polls once per 60-s tick in the ``synology`` concurrency group. Neither fetch
    is primary: a single fetch failing records its error but keeps ok=True with
    the other API's families still emitted; ok=False ONLY when BOTH fetches fail.
    An unconfigured client is ok=False.
    """

    name: ClassVar[str] = "synology_ups"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=30)
    concurrency_group: ClassVar[str] = "synology"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Fetch UPS + SystemHealth co-equally, parse each, emit cap-routed families."""
        start = time.monotonic()
        if ctx.synology is None:
            return client_unconfigured_result(start)

        emitted: list[int] = [0]
        errors: list[str] = []
        events: list[CollectorEvent] = []
        built = _Built()

        ups_resp = _fetch(ctx, await ctx.synology.ups_get(), start, emitted, errors)
        if ups_resp is not None:
            ups = as_dict(ups_resp.payload)
            if ups is not None:
                _parse_ups(built, ups)

        health_resp = _fetch(ctx, await ctx.synology.system_health(), start, emitted, errors)
        if health_resp is not None:
            health = as_dict(health_resp.payload)
            if health is not None:
                _parse_health(built, health)

        # ALWAYS emit (even on a both-failed run: empty families emit drop gauge only).
        _emit(ctx, built, events, emitted)

        # CO-EQUAL: ok=False ONLY when BOTH fetches failed.
        ok = ups_resp is not None or health_resp is not None
        return CollectorResult(
            ok=ok,
            metrics_emitted=emitted[0],
            errors=errors,
            events=events,
            duration_seconds=time.monotonic() - start,
        )
