"""Unit tests for the synology_ups collector (STAGE-008-009, fixture-based).

100% branch coverage of ups.py. Fixtures are hand-built from the authoritative
ground-truth live payloads (ExternalDevice.UPS get / SystemHealth get are FLAT
dicts). Exercises the CO-EQUAL combine: ok=False ONLY when BOTH fetches fail.
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
from homelab_monitor.plugins.collectors.integrations.synology.ups import (
    M_HEALTH_OK,
    M_HEALTH_PRIORITY,
    M_HEALTH_RULE,
    M_UPS_CHARGE_PERCENT,
    M_UPS_CONNECTED,
    M_UPS_INFO,
    M_UPS_LOW_BATTERY,
    M_UPS_ON_BATTERY,
    M_UPS_RUNTIME_SECONDS,
    M_UPS_STATUS,
    SynologyUPSCollector,
)

_API_TOOK = "homelab_synology_api_took_seconds"
_DROP = "homelab_metric_family_dropped_series"

_EXPECTED_INTERVAL = 60.0
_EXPECTED_TIMEOUT = 30.0

# 10 cap-routed families emitted by _emit.
_FAMILY_COUNT = 10

_EXPECTED_CHARGE = 100.0
_EXPECTED_RUNTIME = 3518.0
_EXPECTED_PRIORITY = 2.2


# ---------------------------------------------------------------------------
# Scaffolding (mirrored from test_synology_system_collector.py, 2 fetch methods)
# ---------------------------------------------------------------------------


def _resp(payload: object, endpoint: str) -> SynologyResponse:
    return SynologyResponse(payload=payload, took_seconds=0.5, endpoint=endpoint)


def _ups_resp(payload: object) -> SynologyResponse:
    return _resp(payload, "SYNO.Core.ExternalDevice.UPS/get")


def _health_resp(payload: object) -> SynologyResponse:
    return _resp(payload, "SYNO.Core.System.SystemHealth/get")


class _FakeSynology:
    """Stand-in for ctx.synology with 2 independently programmable methods."""

    def __init__(self, ups: object = None, health: object = None) -> None:
        self._ups = ups if ups is not None else _ups_resp(_ups_payload())
        self._health = health if health is not None else _health_resp(_health_payload())

    async def ups_get(self) -> object:
        return self._ups

    async def system_health(self) -> object:
        return self._health


@dataclass
class _Ctx:
    """Typed stand-in for CollectorContext used in ups tests."""

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


def _ups_payload() -> dict[str, object]:
    return {
        "charge": 100,
        "runtime": 3518,
        "status": "usb_ups_status_online",
        "usb_ups_connect": True,
        "model": "Smart-UPS_1500\n",
        "manufacture": "American Power Conversion \n",
        "mode": "USB",
    }


def _health_payload() -> dict[str, object]:
    return {
        "rule": {"id": "storage_is_attention", "priority": 2.2, "type": 1},
        "hostname": "NAS",
        "uptime": "1205:13:7",
    }


# ---------------------------------------------------------------------------
# ClassVars + unconfigured
# ---------------------------------------------------------------------------


def test_ups_classvars() -> None:
    assert SynologyUPSCollector.name == "synology_ups"
    assert SynologyUPSCollector.interval == timedelta(seconds=60)
    assert SynologyUPSCollector.interval.total_seconds() == _EXPECTED_INTERVAL
    assert SynologyUPSCollector.timeout == timedelta(seconds=30)
    assert SynologyUPSCollector.timeout.total_seconds() == _EXPECTED_TIMEOUT
    assert SynologyUPSCollector.concurrency_group == "synology"


async def test_ups_unconfigured_client() -> None:
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=None))
    result = await SynologyUPSCollector().run(ctx)
    assert result.ok is False
    assert result.errors == ["synology client not configured"]
    assert result.metrics_emitted == 0
    assert writer.gauges == []


# ---------------------------------------------------------------------------
# Happy path — assert exact values + STRIPPED identity labels
# ---------------------------------------------------------------------------


async def test_ups_happy_path_values() -> None:
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology()))
    result = await SynologyUPSCollector().run(ctx)
    assert result.ok is True
    assert result.errors == []

    assert _gauges_named(writer, M_UPS_CHARGE_PERCENT) == [
        (M_UPS_CHARGE_PERCENT, _EXPECTED_CHARGE, {})
    ]
    assert _gauges_named(writer, M_UPS_RUNTIME_SECONDS) == [
        (M_UPS_RUNTIME_SECONDS, _EXPECTED_RUNTIME, {})
    ]
    assert _gauges_named(writer, M_UPS_STATUS) == [(M_UPS_STATUS, 1.0, {"state": "online"})]
    assert _gauges_named(writer, M_UPS_ON_BATTERY) == [(M_UPS_ON_BATTERY, 0.0, {})]
    assert _gauges_named(writer, M_UPS_LOW_BATTERY) == [(M_UPS_LOW_BATTERY, 0.0, {})]
    assert _gauges_named(writer, M_UPS_CONNECTED) == [(M_UPS_CONNECTED, 1.0, {})]

    # Identity labels STRIPPED (no trailing newline/space).
    info = _gauges_named(writer, M_UPS_INFO)
    assert info == [
        (
            M_UPS_INFO,
            1.0,
            {"model": "Smart-UPS_1500", "manufacture": "American Power Conversion"},
        )
    ]
    # Belt-and-suspenders: assert no residual whitespace leaked into the labels.
    labels = info[0][2]
    assert labels["model"] == labels["model"].strip()
    assert labels["manufacture"] == labels["manufacture"].strip()

    # Health: rule present -> health_ok=0, rule series, priority.
    assert _gauges_named(writer, M_HEALTH_OK) == [(M_HEALTH_OK, 0.0, {})]
    assert _gauges_named(writer, M_HEALTH_RULE) == [
        (M_HEALTH_RULE, 1.0, {"rule": "storage_is_attention"})
    ]
    assert _gauges_named(writer, M_HEALTH_PRIORITY) == [(M_HEALTH_PRIORITY, _EXPECTED_PRIORITY, {})]


async def test_ups_happy_path_metrics_emitted_accounting() -> None:
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, synology=_FakeSynology()))
    result = await SynologyUPSCollector().run(ctx)

    expected_api_took = 2  # 2 successful fetches -> 2 api_took gauges
    assert len(_gauges_named(writer, _API_TOOK)) == expected_api_took
    assert len(_gauges_named(writer, _DROP)) == _FAMILY_COUNT

    survivors = sum(1 for g in writer.gauges if g[0] not in (_API_TOOK, _DROP))
    assert result.metrics_emitted == survivors + _FAMILY_COUNT + expected_api_took
    assert result.metrics_emitted == len(writer.gauges)


# ---------------------------------------------------------------------------
# UPS status variants (_ups_status_state branches + derived booleans)
# ---------------------------------------------------------------------------


async def test_ups_status_on_battery() -> None:
    """status on_battery -> status{state=on_battery}=1, on_battery=1, low_battery=0."""
    payload = {**_ups_payload(), "status": "usb_ups_status_on_battery"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(ups=_ups_resp(payload))),
    )
    await SynologyUPSCollector().run(ctx)
    assert _gauges_named(writer, M_UPS_STATUS) == [(M_UPS_STATUS, 1.0, {"state": "on_battery"})]
    assert _gauges_named(writer, M_UPS_ON_BATTERY) == [(M_UPS_ON_BATTERY, 1.0, {})]
    assert _gauges_named(writer, M_UPS_LOW_BATTERY) == [(M_UPS_LOW_BATTERY, 0.0, {})]


async def test_ups_status_low_battery() -> None:
    """status low_battery -> low_battery=1, on_battery=0."""
    payload = {**_ups_payload(), "status": "usb_ups_status_low_battery"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(ups=_ups_resp(payload))),
    )
    await SynologyUPSCollector().run(ctx)
    assert _gauges_named(writer, M_UPS_LOW_BATTERY) == [(M_UPS_LOW_BATTERY, 1.0, {})]
    assert _gauges_named(writer, M_UPS_ON_BATTERY) == [(M_UPS_ON_BATTERY, 0.0, {})]
    assert _gauges_named(writer, M_UPS_STATUS) == [(M_UPS_STATUS, 1.0, {"state": "low_battery"})]


async def test_ups_status_non_str_skipped() -> None:
    """status is non-str -> NO status/on_battery/low_battery obs (state-is-None branch)."""
    payload = {**_ups_payload(), "status": 5}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(ups=_ups_resp(payload))),
    )
    await SynologyUPSCollector().run(ctx)
    assert _gauges_named(writer, M_UPS_STATUS) == []
    assert _gauges_named(writer, M_UPS_ON_BATTERY) == []
    assert _gauges_named(writer, M_UPS_LOW_BATTERY) == []


async def test_ups_status_empty_str_skipped() -> None:
    """status empty after strip -> state None -> no status/on_battery/low_battery obs."""
    payload = {**_ups_payload(), "status": "   "}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(ups=_ups_resp(payload))),
    )
    await SynologyUPSCollector().run(ctx)
    assert _gauges_named(writer, M_UPS_STATUS) == []
    assert _gauges_named(writer, M_UPS_ON_BATTERY) == []
    assert _gauges_named(writer, M_UPS_LOW_BATTERY) == []


# ---------------------------------------------------------------------------
# connected ALWAYS-EMIT family (conn-is-None fallback branch)
# ---------------------------------------------------------------------------


async def test_ups_connected_false() -> None:
    """usb_ups_connect False -> connected=0.0."""
    payload = {**_ups_payload(), "usb_ups_connect": False}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(ups=_ups_resp(payload))),
    )
    await SynologyUPSCollector().run(ctx)
    assert _gauges_named(writer, M_UPS_CONNECTED) == [(M_UPS_CONNECTED, 0.0, {})]


async def test_ups_connected_absent_falls_back_to_zero() -> None:
    """usb_ups_connect absent/non-bool -> bool_to_gauge None -> connected=0.0 fallback."""
    payload = {k: v for k, v in _ups_payload().items() if k != "usb_ups_connect"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(ups=_ups_resp(payload))),
    )
    await SynologyUPSCollector().run(ctx)
    # ALWAYS emits one obs, value 0.0 (the conn-is-None branch).
    assert _gauges_named(writer, M_UPS_CONNECTED) == [(M_UPS_CONNECTED, 0.0, {})]


# ---------------------------------------------------------------------------
# charge / runtime None branches
# ---------------------------------------------------------------------------


async def test_ups_charge_runtime_absent_skipped() -> None:
    """charge + runtime absent -> as_float None -> no obs."""
    payload = {k: v for k, v in _ups_payload().items() if k not in ("charge", "runtime")}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(ups=_ups_resp(payload))),
    )
    await SynologyUPSCollector().run(ctx)
    assert _gauges_named(writer, M_UPS_CHARGE_PERCENT) == []
    assert _gauges_named(writer, M_UPS_RUNTIME_SECONDS) == []


async def test_ups_charge_non_numeric_skipped() -> None:
    """charge non-numeric str -> as_float None -> no obs."""
    payload = {**_ups_payload(), "charge": "n/a"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(ups=_ups_resp(payload))),
    )
    await SynologyUPSCollector().run(ctx)
    assert _gauges_named(writer, M_UPS_CHARGE_PERCENT) == []


# ---------------------------------------------------------------------------
# _strip_label / info identity branches
# ---------------------------------------------------------------------------


async def test_ups_info_model_absent_skips_whole_metric() -> None:
    """model absent/non-str -> _strip_label None -> NO ups_info obs."""
    payload = {k: v for k, v in _ups_payload().items() if k != "model"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(ups=_ups_resp(payload))),
    )
    await SynologyUPSCollector().run(ctx)
    assert _gauges_named(writer, M_UPS_INFO) == []


async def test_ups_info_model_empty_after_strip_skipped() -> None:
    """model is whitespace-only -> _strip_label None (empty-after-strip) -> NO ups_info obs."""
    payload = {**_ups_payload(), "model": "   "}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(ups=_ups_resp(payload))),
    )
    await SynologyUPSCollector().run(ctx)
    assert _gauges_named(writer, M_UPS_INFO) == []


async def test_ups_info_manufacture_absent_model_only() -> None:
    """model present, manufacture absent/non-str -> ups_info with only {model}."""
    payload = {k: v for k, v in _ups_payload().items() if k != "manufacture"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(ups=_ups_resp(payload))),
    )
    await SynologyUPSCollector().run(ctx)
    assert _gauges_named(writer, M_UPS_INFO) == [(M_UPS_INFO, 1.0, {"model": "Smart-UPS_1500"})]


# ---------------------------------------------------------------------------
# HEALTH branches
# ---------------------------------------------------------------------------


async def test_health_healthy_rule_absent() -> None:
    """rule absent -> health_ok=1, NO rule/priority obs (rule-is-None branch)."""
    payload = {"hostname": "NAS", "uptime": "1:2:3"}  # no rule
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(health=_health_resp(payload))),
    )
    await SynologyUPSCollector().run(ctx)
    assert _gauges_named(writer, M_HEALTH_OK) == [(M_HEALTH_OK, 1.0, {})]
    assert _gauges_named(writer, M_HEALTH_RULE) == []
    assert _gauges_named(writer, M_HEALTH_PRIORITY) == []


async def test_health_rule_null_is_healthy() -> None:
    """rule explicitly null -> as_dict None -> healthy (health_ok=1)."""
    payload = {"rule": None}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(health=_health_resp(payload))),
    )
    await SynologyUPSCollector().run(ctx)
    assert _gauges_named(writer, M_HEALTH_OK) == [(M_HEALTH_OK, 1.0, {})]
    assert _gauges_named(writer, M_HEALTH_RULE) == []


async def test_health_rule_id_non_str_skips_rule_series() -> None:
    """rule present but rule.id non-str/absent -> health_ok=0, NO rule series."""
    payload = {"rule": {"priority": 2.2, "type": 1}}  # no id
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(health=_health_resp(payload))),
    )
    await SynologyUPSCollector().run(ctx)
    assert _gauges_named(writer, M_HEALTH_OK) == [(M_HEALTH_OK, 0.0, {})]
    assert _gauges_named(writer, M_HEALTH_RULE) == []
    # priority still parses
    assert _gauges_named(writer, M_HEALTH_PRIORITY) == [(M_HEALTH_PRIORITY, _EXPECTED_PRIORITY, {})]


async def test_health_priority_absent_skipped() -> None:
    """rule present, priority absent/non-numeric -> NO priority obs (prio-None branch)."""
    payload = {"rule": {"id": "storage_is_attention"}}  # no priority
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(health=_health_resp(payload))),
    )
    await SynologyUPSCollector().run(ctx)
    assert _gauges_named(writer, M_HEALTH_OK) == [(M_HEALTH_OK, 0.0, {})]
    assert _gauges_named(writer, M_HEALTH_RULE) == [
        (M_HEALTH_RULE, 1.0, {"rule": "storage_is_attention"})
    ]
    assert _gauges_named(writer, M_HEALTH_PRIORITY) == []


# ---------------------------------------------------------------------------
# CO-EQUAL multi-fetch combine
# ---------------------------------------------------------------------------


async def test_ups_fetch_error_health_ok_is_degraded_ok_true() -> None:
    """UPS SynologyError, health ok -> ok=True, error recorded, ups families empty."""
    err = SynologyError(reason="timeout", message="ups timed out")
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(ups=err)),
    )
    result = await SynologyUPSCollector().run(ctx)
    assert result.ok is True
    assert result.errors == ["ups timed out"]
    assert _gauges_named(writer, M_UPS_CHARGE_PERCENT) == []
    assert _gauges_named(writer, M_UPS_CONNECTED) == []
    # health still emitted
    assert len(_gauges_named(writer, M_HEALTH_OK)) == 1


async def test_health_fetch_error_ups_ok_is_degraded_ok_true() -> None:
    """health SynologyError, UPS ok -> ok=True, error recorded, health families empty."""
    err = SynologyError(reason="timeout", message="health timed out")
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(health=err)),
    )
    result = await SynologyUPSCollector().run(ctx)
    assert result.ok is True
    assert result.errors == ["health timed out"]
    assert _gauges_named(writer, M_HEALTH_OK) == []
    # ups still emitted
    assert len(_gauges_named(writer, M_UPS_CONNECTED)) == 1


async def test_both_fetch_errors_is_ok_false() -> None:
    """BOTH SynologyError -> ok=False, both errors recorded (both-None FALSE branch)."""
    ups_err = SynologyError(reason="timeout", message="ups timed out")
    health_err = SynologyError(reason="timeout", message="health timed out")
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(ups=ups_err, health=health_err)),
    )
    result = await SynologyUPSCollector().run(ctx)
    assert result.ok is False
    assert result.errors == ["ups timed out", "health timed out"]
    # _emit still ran -> drop gauges for all families, no survivors
    assert len(_gauges_named(writer, _DROP)) == _FAMILY_COUNT
    assert _gauges_named(writer, M_UPS_CONNECTED) == []
    assert _gauges_named(writer, M_HEALTH_OK) == []


# ---------------------------------------------------------------------------
# Non-dict payload per fetch (as_dict None branch)
# ---------------------------------------------------------------------------


async def test_ups_payload_non_dict_skips_ups_parse() -> None:
    """UPS payload not a dict -> ups parse skipped, health still parsed, ok=True."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(ups=_ups_resp(None))),
    )
    result = await SynologyUPSCollector().run(ctx)
    assert result.ok is True
    assert _gauges_named(writer, M_UPS_CONNECTED) == []
    assert len(_gauges_named(writer, M_HEALTH_OK)) == 1


async def test_health_payload_non_dict_skips_health_parse() -> None:
    """health payload not a dict (str) -> health parse skipped, ups parsed, ok=True."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, synology=_FakeSynology(health=_health_resp("nope"))),
    )
    result = await SynologyUPSCollector().run(ctx)
    assert result.ok is True
    assert _gauges_named(writer, M_HEALTH_OK) == []
    assert len(_gauges_named(writer, M_UPS_CONNECTED)) == 1
