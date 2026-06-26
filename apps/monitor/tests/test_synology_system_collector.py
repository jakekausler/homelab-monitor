"""Unit tests for the synology_system collector (STAGE-008-007, fixture-based).

100% branch coverage of system.py. Fixtures are hand-built from the authoritative
ground-truth live payloads in the STAGE-008-007 spec (system_info / fanspeed /
need_reboot are all FLAT dicts).
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
from homelab_monitor.plugins.collectors.integrations.synology.system import (
    M_FAN_STATUS,
    M_INFO,
    M_NEED_REBOOT,
    M_SYS_TEMP_CELSIUS,
    M_SYS_TEMP_WARNING,
    M_SYSTEM_UPTIME_SECONDS,
    SynologySystemCollector,
)

_API_TOOK = "homelab_synology_api_took_seconds"
_DROP = "homelab_metric_family_dropped_series"

_EXPECTED_INTERVAL = 60.0
_EXPECTED_TIMEOUT = 30.0

# 6 cap-routed families emitted by _emit.
_FAMILY_COUNT = 6

# Healthy fixture uptime "1205:13:7" -> 1205*3600 + 13*60 + 7 = 4338787.0
_EXPECTED_UPTIME = 4338787.0


# ---------------------------------------------------------------------------
# Scaffolding (mirrored from test_synology_pool_collector.py, 3 fetch methods)
# ---------------------------------------------------------------------------


def _resp(payload: object, endpoint: str) -> SynologyResponse:
    return SynologyResponse(payload=payload, took_seconds=0.5, endpoint=endpoint)


def _info_resp(payload: object) -> SynologyResponse:
    return _resp(payload, "SYNO.Core.System/info")


def _fan_resp(payload: object) -> SynologyResponse:
    return _resp(payload, "SYNO.Core.Hardware.FanSpeed/get")


def _reboot_resp(payload: object) -> SynologyResponse:
    return _resp(payload, "SYNO.Core.Hardware.NeedReboot/get")


class _FakeSynology:
    """Stand-in for ctx.synology with 3 independently programmable methods."""

    def __init__(
        self,
        info: object = None,
        fan: object = None,
        reboot: object = None,
    ) -> None:
        self._info = info if info is not None else _info_resp(_system_info_payload())
        self._fan = fan if fan is not None else _fan_resp(_fanspeed_payload())
        self._reboot = reboot if reboot is not None else _reboot_resp(_need_reboot_payload())

    async def system_info(self) -> object:
        return self._info

    async def hardware_fanspeed(self) -> object:
        return self._fan

    async def need_reboot(self) -> object:
        return self._reboot


@dataclass
class _Ctx:
    """Typed stand-in for CollectorContext used in system tests."""

    vm: MemoryRetainingMetricsWriter = field(default_factory=MemoryRetainingMetricsWriter)
    synology: object = None


def _ctx(writer: MemoryRetainingMetricsWriter, synology: object) -> _Ctx:
    return _Ctx(vm=writer, synology=synology)


def _gauges_named(
    writer: MemoryRetainingMetricsWriter, name: str
) -> list[tuple[str, float, dict[str, str]]]:
    return [g for g in writer.gauges if g[0] == name]


# ---------------------------------------------------------------------------
# Fixtures (authoritative ground-truth live payloads)
# ---------------------------------------------------------------------------


def _system_info_payload() -> dict[str, object]:
    return {
        "model": "DS3622xs+",
        "serial": "X",
        "firmware_ver": "DSM 7.3.2-86009",
        "cpu_series": "D-1531",
        "sys_temp": 50,
        "sys_tempwarn": False,
        "systempwarn": False,
        "temperature_warning": False,
        "up_time": "1205:13:7",
    }


def _fanspeed_payload() -> dict[str, object]:
    return {
        "cool_fan": "yes",
        "all_disk_temp_fail": "no",
        "dual_fan_speed": "quietfan",
    }


def _need_reboot_payload() -> dict[str, object]:
    return {"need_reboot": False}


# ---------------------------------------------------------------------------
# ClassVars + degraded-path scenarios
# ---------------------------------------------------------------------------


def test_system_classvars() -> None:
    assert SynologySystemCollector.name == "synology_system"
    assert SynologySystemCollector.interval == timedelta(seconds=60)
    assert SynologySystemCollector.interval.total_seconds() == _EXPECTED_INTERVAL
    assert SynologySystemCollector.timeout == timedelta(seconds=30)
    assert SynologySystemCollector.timeout.total_seconds() == _EXPECTED_TIMEOUT
    assert SynologySystemCollector.concurrency_group == "synology"


async def test_system_unconfigured_client() -> None:
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=None))
    result = await SynologySystemCollector().run(ctx)
    assert result.ok is False
    assert result.errors == ["synology client not configured"]
    assert result.metrics_emitted == 0
    assert writer.gauges == []


async def test_system_primary_error_is_ok_false() -> None:
    """system_info SynologyError -> ok=False, error recorded, no family gauges."""
    err = SynologyError(reason="unreachable", message="connection failed")
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(info=err)),
    )
    result = await SynologySystemCollector().run(ctx)
    assert result.ok is False
    assert result.errors == ["connection failed"]
    assert result.metrics_emitted == 0
    # _emit never ran -> no drop gauges, no api_took (fetch failed before emit)
    assert writer.gauges == []


async def test_system_fan_error_is_ok_true_degraded() -> None:
    """fanspeed SynologyError -> ok=True, fan error recorded, fan families empty."""
    err = SynologyError(reason="timeout", message="fan timed out")
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(fan=err)),
    )
    result = await SynologySystemCollector().run(ctx)
    assert result.ok is True
    assert result.errors == ["fan timed out"]
    # system + need_reboot families present; fan_status empty (drop gauge only)
    assert _gauges_named(writer, M_FAN_STATUS) == []
    assert len(_gauges_named(writer, M_SYSTEM_UPTIME_SECONDS)) == 1
    assert len(_gauges_named(writer, M_NEED_REBOOT)) == 1


async def test_system_reboot_error_is_ok_true_degraded() -> None:
    """need_reboot SynologyError -> ok=True, error recorded, need_reboot empty."""
    err = SynologyError(reason="timeout", message="reboot timed out")
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(reboot=err)),
    )
    result = await SynologySystemCollector().run(ctx)
    assert result.ok is True
    assert result.errors == ["reboot timed out"]
    assert _gauges_named(writer, M_NEED_REBOOT) == []
    assert len(_gauges_named(writer, M_FAN_STATUS)) >= 1


# ---------------------------------------------------------------------------
# Malformed-payload (as_dict is None) branches — one per fetch
# ---------------------------------------------------------------------------


async def test_system_info_payload_non_dict() -> None:
    """system_info payload not a dict -> system families skipped, ok=True."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(info=_info_resp(None))),
    )
    result = await SynologySystemCollector().run(ctx)
    assert result.ok is True
    assert _gauges_named(writer, M_SYSTEM_UPTIME_SECONDS) == []
    assert _gauges_named(writer, M_INFO) == []
    # fan + reboot still parsed (defaults healthy)
    assert len(_gauges_named(writer, M_NEED_REBOOT)) == 1


async def test_system_fan_payload_non_dict() -> None:
    """fanspeed payload not a dict (str) -> fan_status empty, ok=True."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(fan=_fan_resp("nope"))),
    )
    result = await SynologySystemCollector().run(ctx)
    assert result.ok is True
    assert _gauges_named(writer, M_FAN_STATUS) == []


async def test_system_reboot_payload_non_dict() -> None:
    """need_reboot payload not a dict -> need_reboot empty, ok=True."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(reboot=_reboot_resp(["x"]))),
    )
    result = await SynologySystemCollector().run(ctx)
    assert result.ok is True
    assert _gauges_named(writer, M_NEED_REBOOT) == []


# ---------------------------------------------------------------------------
# Happy path — assert exact values + accounting
# ---------------------------------------------------------------------------


async def test_system_happy_path_values() -> None:
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology()))
    result = await SynologySystemCollector().run(ctx)
    assert result.ok is True
    assert result.errors == []

    assert _gauges_named(writer, M_SYSTEM_UPTIME_SECONDS) == [
        (M_SYSTEM_UPTIME_SECONDS, _EXPECTED_UPTIME, {})
    ]
    assert _gauges_named(writer, M_SYS_TEMP_CELSIUS) == [(M_SYS_TEMP_CELSIUS, 50.0, {})]
    # all three temp-warning fields present + False -> 0.0
    assert _gauges_named(writer, M_SYS_TEMP_WARNING) == [(M_SYS_TEMP_WARNING, 0.0, {})]
    # need_reboot False -> 0.0
    assert _gauges_named(writer, M_NEED_REBOOT) == [(M_NEED_REBOOT, 0.0, {})]

    info = _gauges_named(writer, M_INFO)
    assert info == [
        (
            M_INFO,
            1.0,
            {
                "model": "DS3622xs+",
                "serial": "X",
                "firmware": "DSM 7.3.2-86009",
                "cpu_series": "D-1531",
            },
        )
    ]

    fan = _gauges_named(writer, M_FAN_STATUS)
    assert (M_FAN_STATUS, 1.0, {"state": "cool_fan"}) in fan
    assert (M_FAN_STATUS, 0.0, {"state": "all_disk_temp_fail"}) in fan
    assert (M_FAN_STATUS, 1.0, {"state": "quietfan"}) in fan
    expected_fan_obs = 3
    assert len(fan) == expected_fan_obs


async def test_system_happy_path_metrics_emitted_accounting() -> None:
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology()))
    result = await SynologySystemCollector().run(ctx)

    # 3 successful fetches -> 3 api_took gauges
    expected_api_took = 3
    assert len(_gauges_named(writer, _API_TOOK)) == expected_api_took
    assert len(_gauges_named(writer, _DROP)) == _FAMILY_COUNT

    survivors = sum(1 for g in writer.gauges if g[0] not in (_API_TOOK, _DROP))
    # metrics_emitted = 3 api_took + survivors + _FAMILY_COUNT drop gauges
    assert result.metrics_emitted == survivors + _FAMILY_COUNT + expected_api_took
    assert result.metrics_emitted == len(writer.gauges)


# ---------------------------------------------------------------------------
# _uptime_to_seconds branches
# ---------------------------------------------------------------------------


async def test_system_uptime_non_str_skipped() -> None:
    """up_time is an int (non-str) -> uptime not emitted."""
    payload = {"model": "DS", "up_time": 999, "sys_temp": 10}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(info=_info_resp(payload))),
    )
    await SynologySystemCollector().run(ctx)
    assert _gauges_named(writer, M_SYSTEM_UPTIME_SECONDS) == []


async def test_system_uptime_wrong_segment_count_skipped() -> None:
    """up_time with 2 segments -> not emitted."""
    payload = {"model": "DS", "up_time": "1:2"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(info=_info_resp(payload))),
    )
    await SynologySystemCollector().run(ctx)
    assert _gauges_named(writer, M_SYSTEM_UPTIME_SECONDS) == []


async def test_system_uptime_non_numeric_skipped() -> None:
    """up_time with non-numeric segments -> not emitted (ValueError branch)."""
    payload = {"model": "DS", "up_time": "a:b:c"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(info=_info_resp(payload))),
    )
    await SynologySystemCollector().run(ctx)
    assert _gauges_named(writer, M_SYSTEM_UPTIME_SECONDS) == []


# ---------------------------------------------------------------------------
# _yesno_to_gauge "other" branch + fan mode absent
# ---------------------------------------------------------------------------


async def test_system_fan_yesno_other_and_mode_absent() -> None:
    """cool_fan non-yes/no -> skipped; all_disk_temp_fail absent -> skipped;
    dual_fan_speed absent -> no mode obs."""
    payload = {"cool_fan": "maybe"}  # not yes/no; others absent
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(fan=_fan_resp(payload))),
    )
    await SynologySystemCollector().run(ctx)
    assert _gauges_named(writer, M_FAN_STATUS) == []


async def test_system_fan_mode_non_str_skipped() -> None:
    """dual_fan_speed is non-str -> no mode obs (cool_fan still emitted)."""
    payload = {"cool_fan": "yes", "dual_fan_speed": 5}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(fan=_fan_resp(payload))),
    )
    await SynologySystemCollector().run(ctx)
    fan = _gauges_named(writer, M_FAN_STATUS)
    assert fan == [(M_FAN_STATUS, 1.0, {"state": "cool_fan"})]


# ---------------------------------------------------------------------------
# _temp_warn branches (all-absent / one-True)
# ---------------------------------------------------------------------------


async def test_system_temp_warn_all_absent_skipped() -> None:
    """No temp-warning field present -> sys_temp_warning not emitted."""
    payload = {"model": "DS", "sys_temp": 40}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(info=_info_resp(payload))),
    )
    await SynologySystemCollector().run(ctx)
    assert _gauges_named(writer, M_SYS_TEMP_WARNING) == []


async def test_system_temp_warn_one_true_is_one() -> None:
    """One temp-warning variant True -> 1.0."""
    payload = {
        "model": "DS",
        "sys_tempwarn": False,
        "systempwarn": True,
        "temperature_warning": False,
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(info=_info_resp(payload))),
    )
    await SynologySystemCollector().run(ctx)
    assert _gauges_named(writer, M_SYS_TEMP_WARNING) == [(M_SYS_TEMP_WARNING, 1.0, {})]


# ---------------------------------------------------------------------------
# Identity (info) branches: partial labels + model absent
# ---------------------------------------------------------------------------


async def test_system_info_partial_labels() -> None:
    """model present but serial/firmware/cpu_series absent -> labels = {model}."""
    payload = {"model": "DS3622xs+"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(info=_info_resp(payload))),
    )
    await SynologySystemCollector().run(ctx)
    assert _gauges_named(writer, M_INFO) == [(M_INFO, 1.0, {"model": "DS3622xs+"})]


async def test_system_info_model_absent_no_obs() -> None:
    """model absent/non-str -> NO info obs (whole-metric skip)."""
    payload = {"serial": "X", "sys_temp": 40}  # no model
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(info=_info_resp(payload))),
    )
    await SynologySystemCollector().run(ctx)
    assert _gauges_named(writer, M_INFO) == []


# ---------------------------------------------------------------------------
# bool_to_gauge need_reboot True branch + sys_temp absent
# ---------------------------------------------------------------------------


async def test_system_need_reboot_true() -> None:
    """need_reboot True -> 1.0."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(reboot=_reboot_resp({"need_reboot": True}))),
    )
    await SynologySystemCollector().run(ctx)
    assert _gauges_named(writer, M_NEED_REBOOT) == [(M_NEED_REBOOT, 1.0, {})]


async def test_system_need_reboot_absent_skipped() -> None:
    """need_reboot absent -> not emitted."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(reboot=_reboot_resp({}))),
    )
    await SynologySystemCollector().run(ctx)
    assert _gauges_named(writer, M_NEED_REBOOT) == []


async def test_system_sys_temp_absent_skipped() -> None:
    """sys_temp absent -> as_float None -> not emitted."""
    payload = {"model": "DS"}  # no sys_temp
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(info=_info_resp(payload))),
    )
    await SynologySystemCollector().run(ctx)
    assert _gauges_named(writer, M_SYS_TEMP_CELSIUS) == []
