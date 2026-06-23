"""synology_system collector — system identity, uptime, temps, fan + reboot status.

STAGE-008-007. Fetches THREE INDEPENDENT DSM APIs in one tick:
  - SYNO.Core.System v3 info          -> model/serial/firmware/cpu/uptime/sys_temp
  - SYNO.Core.Hardware.FanSpeed v1 get -> cool_fan / all_disk_temp_fail / fan mode
  - SYNO.Core.Hardware.NeedReboot v1 get -> need_reboot bool

UNLIKE storage/pool (one fetch, early-return on error), this collector makes the
THREE calls INDEPENDENT: a failure of the PRIMARY (system_info) makes the run
ok=False; a failure of either SECONDARY (fanspeed / need_reboot) records the
error string but keeps ok=True and still emits whatever the other calls produced.
``_fetch`` wraps ``fetch_or_result`` to implement that "record-and-continue"
policy for the secondaries (and is reused for the primary, whose None triggers
the explicit ok=False return).

PARSE — defensive. All three payloads are FLAT dicts (no list, no deep nesting),
so the parse reads them with ``as_dict`` + ``.get()`` directly. Local helpers map
the DSM-specific shapes: ``_uptime_to_seconds`` parses the "HHH:MM:S" up_time
string; ``_yesno_to_gauge`` maps fan "yes"/"no"; ``_temp_warn`` ORs the three
temp-warning variant fields; ``bool_to_gauge`` (shared) maps need_reboot.

STATE-SET: ``M_INFO`` is an identity series ({model,serial,firmware,cpu_series}=1);
``M_FAN_STATUS`` is a per-state series ({state}=value).

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
#   uptime / sys_temp / sys_temp_warning / need_reboot : no labels (single series)
#   info       : {model, serial, firmware, cpu_series} = 1.0  (identity)
#   fan_status : {state} = value                              (per-state)
M_SYSTEM_UPTIME_SECONDS: Final[str] = "homelab_synology_system_uptime_seconds"
M_SYS_TEMP_CELSIUS: Final[str] = "homelab_synology_sys_temp_celsius"
M_SYS_TEMP_WARNING: Final[str] = "homelab_synology_sys_temp_warning"
M_INFO: Final[str] = "homelab_synology_info"
M_FAN_STATUS: Final[str] = "homelab_synology_fan_status"
M_NEED_REBOOT: Final[str] = "homelab_synology_need_reboot"

_UPTIME_SEGMENTS: Final[int] = 3


# ---------------------------------------------------------------------------
# Local DSM-shape helpers (NOT shared — system-specific)
# ---------------------------------------------------------------------------


def _uptime_to_seconds(v: object) -> float | None:
    """Parse a DSM up_time string 'HHH:MM:S' -> seconds.

    None on non-str / wrong-segment-count / non-numeric segment.
    """
    if not isinstance(v, str):
        return None
    parts = v.split(":")
    if len(parts) != _UPTIME_SEGMENTS:
        return None
    try:
        h, m, s = (int(p) for p in parts)
    except ValueError:
        return None
    return float(h * 3600 + m * 60 + s)


def _yesno_to_gauge(v: object) -> float | None:
    """Map a DSM 'yes'/'no' string to 1.0/0.0; None otherwise."""
    if v == "yes":
        return 1.0
    if v == "no":
        return 0.0
    return None


def _temp_warn(info: dict[str, object]) -> float | None:
    """OR the three temp-warning variant fields.

    1.0 if any present bool is True, 0.0 if >=1 present bool but none True,
    None if none of the three fields is a present bool.
    """
    vals = [
        bool_to_gauge(info.get(k)) for k in ("sys_tempwarn", "systempwarn", "temperature_warning")
    ]
    present = [v for v in vals if v is not None]
    if not present:
        return None
    return 1.0 if any(v == 1.0 for v in present) else 0.0


# ---------------------------------------------------------------------------
# Multi-fetch wrapper: record-and-continue for INDEPENDENT fetches
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
    instead of aborting the whole collector. On success it has already emitted
    api_took + bumped emitted[0]; we return the SynologyResponse.
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
        "fan_status_obs",
        "info_obs",
        "need_reboot_obs",
        "sys_temp_obs",
        "sys_temp_warning_obs",
        "uptime_obs",
    )

    def __init__(self) -> None:
        """Initialise every observation list empty."""
        self.uptime_obs: list[tuple[dict[str, str], float]] = []
        self.sys_temp_obs: list[tuple[dict[str, str], float]] = []
        self.sys_temp_warning_obs: list[tuple[dict[str, str], float]] = []
        self.info_obs: list[tuple[dict[str, str], float]] = []
        self.fan_status_obs: list[tuple[dict[str, str], float]] = []
        self.need_reboot_obs: list[tuple[dict[str, str], float]] = []


# ---------------------------------------------------------------------------
# Parse passes
# ---------------------------------------------------------------------------


def _parse_system_info(built: _Built, info: dict[str, object]) -> None:
    """Append uptime / sys_temp / temp-warning / identity observations.

    All four families are single-series (no labels) EXCEPT identity, which is
    anchored on a non-empty str ``model`` (whole-metric skip when absent).
    """
    uptime = _uptime_to_seconds(info.get("up_time"))
    if uptime is not None:
        built.uptime_obs.append(({}, uptime))

    sys_temp = as_float(info.get("sys_temp"))
    if sys_temp is not None:
        built.sys_temp_obs.append(({}, sys_temp))

    temp_warning = _temp_warn(info)
    if temp_warning is not None:
        built.sys_temp_warning_obs.append(({}, temp_warning))

    raw_model = info.get("model")
    if isinstance(raw_model, str) and raw_model:
        labels = {"model": raw_model}
        for label_key, field in (
            ("serial", "serial"),
            ("firmware", "firmware_ver"),
            ("cpu_series", "cpu_series"),
        ):
            val = info.get(field)
            if isinstance(val, str) and val:
                labels[label_key] = val
        built.info_obs.append((labels, 1.0))


def _parse_fanspeed(built: _Built, fan: dict[str, object]) -> None:
    """Append per-state fan_status observations.

    cool_fan / all_disk_temp_fail are 'yes'/'no' -> 1.0/0.0; dual_fan_speed is a
    MODE STRING emitted as state-set ({state=mode}=1.0).
    """
    cool = _yesno_to_gauge(fan.get("cool_fan"))
    if cool is not None:
        built.fan_status_obs.append(({"state": "cool_fan"}, cool))

    disk_fail = _yesno_to_gauge(fan.get("all_disk_temp_fail"))
    if disk_fail is not None:
        built.fan_status_obs.append(({"state": "all_disk_temp_fail"}, disk_fail))

    mode = fan.get("dual_fan_speed")
    if isinstance(mode, str) and mode:
        built.fan_status_obs.append(({"state": mode}, 1.0))


def _parse_need_reboot(built: _Built, reboot: dict[str, object]) -> None:
    """Append the need_reboot observation (bool -> 1.0/0.0; absent -> skip)."""
    val = bool_to_gauge(reboot.get("need_reboot"))
    if val is not None:
        built.need_reboot_obs.append(({}, val))


def _emit(
    ctx: CollectorContext, built: _Built, events: list[CollectorEvent], emitted: list[int]
) -> None:
    """Cap-route every family through one CappedEmitter."""
    emitter = capped_emitter(ctx, events)

    def family(name: str, obs: list[tuple[dict[str, str], float]]) -> None:
        emitted[0] += emitter.emit_family(name, cap_for_synology(name), obs) + 1

    family(M_SYSTEM_UPTIME_SECONDS, built.uptime_obs)
    family(M_SYS_TEMP_CELSIUS, built.sys_temp_obs)
    family(M_SYS_TEMP_WARNING, built.sys_temp_warning_obs)
    family(M_INFO, built.info_obs)
    family(M_FAN_STATUS, built.fan_status_obs)
    family(M_NEED_REBOOT, built.need_reboot_obs)


class SynologySystemCollector(BaseCollector):
    """Emit system identity / uptime / temps / fan + reboot status from 3 DSM APIs.

    Polls once per 60-s tick in the ``synology`` concurrency group. The PRIMARY
    fetch (system_info) failing makes the run ok=False; a SECONDARY (fanspeed /
    need_reboot) failing records the error but keeps ok=True with the other
    families still emitted. An unconfigured client is ok=False.
    """

    name: ClassVar[str] = "synology_system"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=30)
    concurrency_group: ClassVar[str] = "synology"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Fetch the 3 DSM APIs, parse each flat payload, emit cap-routed families."""
        start = time.monotonic()
        if ctx.synology is None:
            return client_unconfigured_result(start)

        emitted: list[int] = [0]
        errors: list[str] = []
        events: list[CollectorEvent] = []
        built = _Built()

        # PRIMARY: system_info. A failure here aborts the run (ok=False).
        info_resp = _fetch(ctx, await ctx.synology.system_info(), start, emitted, errors)
        if info_resp is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=emitted[0],
                errors=errors,
                events=events,
                duration_seconds=time.monotonic() - start,
            )
        info = as_dict(info_resp.payload)
        if info is not None:
            _parse_system_info(built, info)

        # SECONDARY: fan speed. Failure records error but keeps ok=True.
        fan_resp = _fetch(ctx, await ctx.synology.hardware_fanspeed(), start, emitted, errors)
        if fan_resp is not None:
            fan = as_dict(fan_resp.payload)
            if fan is not None:
                _parse_fanspeed(built, fan)

        # SECONDARY: need reboot. Failure records error but keeps ok=True.
        reboot_resp = _fetch(ctx, await ctx.synology.need_reboot(), start, emitted, errors)
        if reboot_resp is not None:
            reboot = as_dict(reboot_resp.payload)
            if reboot is not None:
                _parse_need_reboot(built, reboot)

        _emit(ctx, built, events, emitted)

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted[0],
            errors=errors,
            events=events,
            duration_seconds=time.monotonic() - start,
        )
