"""Unit tests for the synology_license collector (STAGE-008-017, fixture-based).

100% branch coverage of ss_license_homemode.py. Field names + values are LIVE-VERIFIED
(Surveillance Station 9.2.4-11880: License/Load -> key_total/key_used/key_max/localCamCnt;
HomeMode/GetInfo -> on/notify_on/mode_schedule_on/rec_schedule_on/streaming_on bools).
Exercises the CO-EQUAL combine (ok=False ONLY when BOTH fetches fail), the always-emit seeded
gauges, the DERIVED exhausted (used>total / used<=total / either-None), and every conditional
guard's BOTH sides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from homelab_monitor.kernel.plugins.context import CollectorContext

from homelab_monitor.kernel.plugins.io import MemoryRetainingMetricsWriter
from homelab_monitor.kernel.synology.client import SynologyResponse
from homelab_monitor.kernel.synology.errors import SynologyError
from homelab_monitor.plugins.collectors.integrations.synology.ss_license_homemode import (
    M_HOMEMODE_NOTIFY_ON,
    M_HOMEMODE_ON,
    M_HOMEMODE_REC_SCHEDULE_ON,
    M_HOMEMODE_SCHEDULE_ON,
    M_HOMEMODE_STREAMING_ON,
    M_LICENSE_CAMERA_COUNT,
    M_LICENSE_EXHAUSTED,
    M_LICENSE_MAX,
    M_LICENSE_TOTAL,
    M_LICENSE_USED,
    SynologyLicenseCollector,
)

_API_TOOK = "homelab_synology_api_took_seconds"
_DROP = "homelab_metric_family_dropped_series"

_EXPECTED_INTERVAL = 300.0
_EXPECTED_TIMEOUT = 30.0

# 10 cap-routed families emitted by _emit.
_FAMILY_COUNT = 10
# Two co-equal fetches: license + homemode.
_EXPECTED_API_TOOK_COUNT = 2

# Live fixture values.
_LIVE_TOTAL = 3.0
_LIVE_USED = 3.0
_LIVE_MAX = 90.0
_LIVE_CAMERA_COUNT = 3.0
_NOT_EXHAUSTED = 0.0
_EXHAUSTED = 1.0
_ON = 1.0
_OFF = 0.0
# Over-subscribed fixture.
_OVER_USED = 4.0
_OVER_TOTAL = 3.0
# Partial-payload fixture expected values.
_PARTIAL_USED = 5.0
_PARTIAL_TOTAL = 7.0


def _resp(payload: object, endpoint: str) -> SynologyResponse:
    return SynologyResponse(payload=payload, took_seconds=0.5, endpoint=endpoint)


def _license_resp(payload: object) -> SynologyResponse:
    return _resp(payload, "SYNO.SurveillanceStation.License/Load")


def _homemode_resp(payload: object) -> SynologyResponse:
    return _resp(payload, "SYNO.SurveillanceStation.HomeMode/GetInfo")


def _live_license_payload() -> dict[str, object]:
    return {"key_total": 3, "key_used": 3, "key_max": 90, "localCamCnt": 3}


def _live_homemode_payload() -> dict[str, object]:
    return {
        "on": False,
        "notify_on": False,
        "mode_schedule_on": True,
        "rec_schedule_on": False,
        "streaming_on": False,
    }


class _FakeSynology:
    """Stand-in for ctx.synology with 2 independently programmable methods."""

    def __init__(self, license_: object = None, homemode: object = None) -> None:
        self._license = license_ if license_ is not None else _license_resp(_live_license_payload())
        self._homemode = (
            homemode if homemode is not None else _homemode_resp(_live_homemode_payload())
        )

    async def ss_license(self) -> object:
        return self._license

    async def ss_homemode(self) -> object:
        return self._homemode


@dataclass
class _Ctx:
    """Typed stand-in for CollectorContext used in license tests."""

    vm: MemoryRetainingMetricsWriter = field(default_factory=MemoryRetainingMetricsWriter)
    synology: object = None


def _ctx(writer: MemoryRetainingMetricsWriter, synology: object) -> _Ctx:
    return _Ctx(vm=writer, synology=synology)


def _gauges_named(
    writer: MemoryRetainingMetricsWriter, name: str
) -> list[tuple[str, float, dict[str, str]]]:
    return [g for g in writer.gauges if g[0] == name]


def _single(writer: MemoryRetainingMetricsWriter, name: str) -> float:
    """Assert exactly one series for ``name`` and return its value."""
    matches = _gauges_named(writer, name)
    assert len(matches) == 1
    return matches[0][1]


def test_license_classvars() -> None:
    assert SynologyLicenseCollector.name == "synology_license"
    assert SynologyLicenseCollector.interval == timedelta(seconds=300)
    assert SynologyLicenseCollector.interval.total_seconds() == _EXPECTED_INTERVAL
    assert SynologyLicenseCollector.timeout == timedelta(seconds=30)
    assert SynologyLicenseCollector.timeout.total_seconds() == _EXPECTED_TIMEOUT
    assert SynologyLicenseCollector.concurrency_group == "synology"


@pytest.mark.asyncio
async def test_license_full_live_shape() -> None:
    """Happy path: both fetches succeed with live values; all 10 gauges present."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology()))
    collector = SynologyLicenseCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _single(writer, M_LICENSE_TOTAL) == _LIVE_TOTAL
    assert _single(writer, M_LICENSE_USED) == _LIVE_USED
    assert _single(writer, M_LICENSE_MAX) == _LIVE_MAX
    assert _single(writer, M_LICENSE_CAMERA_COUNT) == _LIVE_CAMERA_COUNT
    # used == total -> not exhausted.
    assert _single(writer, M_LICENSE_EXHAUSTED) == _NOT_EXHAUSTED
    # homemode live: only mode_schedule_on is True.
    assert _single(writer, M_HOMEMODE_ON) == _OFF
    assert _single(writer, M_HOMEMODE_NOTIFY_ON) == _OFF
    assert _single(writer, M_HOMEMODE_SCHEDULE_ON) == _ON
    assert _single(writer, M_HOMEMODE_REC_SCHEDULE_ON) == _OFF
    assert _single(writer, M_HOMEMODE_STREAMING_ON) == _OFF
    # Two successful fetches -> two api_took gauges.
    assert len(_gauges_named(writer, _API_TOOK)) == _EXPECTED_API_TOOK_COUNT


@pytest.mark.asyncio
async def test_license_metrics_emitted_accounting() -> None:
    """metrics_emitted reconciles with the concrete writer's gauge count (happy path)."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology()))
    collector = SynologyLicenseCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert len(_gauges_named(writer, _DROP)) == _FAMILY_COUNT
    assert len(_gauges_named(writer, _API_TOOK)) == _EXPECTED_API_TOOK_COUNT
    assert result.metrics_emitted == len(writer.gauges)


@pytest.mark.asyncio
async def test_license_exhausted_over_subscribed() -> None:
    """used > total -> exhausted = 1.0 (DERIVED used>total branch)."""
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(
        license_=_license_resp({"key_used": 4, "key_total": 3, "key_max": 90, "localCamCnt": 3})
    )
    ctx = cast("CollectorContext", _ctx(writer, fake))
    result = await SynologyLicenseCollector().run(ctx)

    assert result.ok is True
    assert _single(writer, M_LICENSE_USED) == _OVER_USED
    assert _single(writer, M_LICENSE_TOTAL) == _OVER_TOTAL
    assert _single(writer, M_LICENSE_EXHAUSTED) == _EXHAUSTED


@pytest.mark.asyncio
async def test_license_exhausted_total_missing() -> None:
    """key_used present but key_total absent -> exhausted stays 0.0 (total-None branch).

    Also covers: total scalar absent -> total gauge stays seeded 0.0.
    """
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(license_=_license_resp({"key_used": 5}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    result = await SynologyLicenseCollector().run(ctx)

    assert result.ok is True
    assert _single(writer, M_LICENSE_USED) == _PARTIAL_USED
    assert _single(writer, M_LICENSE_TOTAL) == _NOT_EXHAUSTED  # seed 0.0
    assert _single(writer, M_LICENSE_EXHAUSTED) == _NOT_EXHAUSTED


@pytest.mark.asyncio
async def test_license_exhausted_used_missing() -> None:
    """key_total present but key_used absent -> exhausted stays 0.0 (used-None branch).

    Also covers: used scalar absent -> used gauge stays seeded 0.0; max/camera_count absent
    stay seeded 0.0 (the None side of every license scalar guard).
    """
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(license_=_license_resp({"key_total": 7}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    result = await SynologyLicenseCollector().run(ctx)

    assert result.ok is True
    assert _single(writer, M_LICENSE_TOTAL) == _PARTIAL_TOTAL
    assert _single(writer, M_LICENSE_USED) == _NOT_EXHAUSTED  # seed 0.0
    assert _single(writer, M_LICENSE_MAX) == _NOT_EXHAUSTED  # seed 0.0
    assert _single(writer, M_LICENSE_CAMERA_COUNT) == _NOT_EXHAUSTED  # seed 0.0
    assert _single(writer, M_LICENSE_EXHAUSTED) == _NOT_EXHAUSTED


@pytest.mark.asyncio
async def test_homemode_non_bool_fields_stay_seeded() -> None:
    """Non-bool / absent homemode flags -> bool_to_gauge None -> gauges stay seeded 0.0.

    Covers the None side of EVERY homemode bool guard: ``on`` is an int (1, not True),
    the other four keys are absent. All 5 homemode gauges stay 0.0.
    """
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(homemode=_homemode_resp({"on": 1}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    result = await SynologyLicenseCollector().run(ctx)

    assert result.ok is True
    assert _single(writer, M_HOMEMODE_ON) == _OFF
    assert _single(writer, M_HOMEMODE_NOTIFY_ON) == _OFF
    assert _single(writer, M_HOMEMODE_SCHEDULE_ON) == _OFF
    assert _single(writer, M_HOMEMODE_REC_SCHEDULE_ON) == _OFF
    assert _single(writer, M_HOMEMODE_STREAMING_ON) == _OFF


@pytest.mark.asyncio
async def test_homemode_all_true() -> None:
    """All homemode flags True -> all 5 gauges = 1.0 (the non-None side of each bool guard)."""
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(
        homemode=_homemode_resp(
            {
                "on": True,
                "notify_on": True,
                "mode_schedule_on": True,
                "rec_schedule_on": True,
                "streaming_on": True,
            }
        )
    )
    ctx = cast("CollectorContext", _ctx(writer, fake))
    result = await SynologyLicenseCollector().run(ctx)

    assert result.ok is True
    assert _single(writer, M_HOMEMODE_ON) == _ON
    assert _single(writer, M_HOMEMODE_NOTIFY_ON) == _ON
    assert _single(writer, M_HOMEMODE_SCHEDULE_ON) == _ON
    assert _single(writer, M_HOMEMODE_REC_SCHEDULE_ON) == _ON
    assert _single(writer, M_HOMEMODE_STREAMING_ON) == _ON


@pytest.mark.asyncio
async def test_license_fetch_error_homemode_ok() -> None:
    """License fetch errors, homemode succeeds -> license seeds 0.0, homemode emits, ok=True."""
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(
        license_=SynologyError(reason="timeout", message="license timed out"),
    )
    ctx = cast("CollectorContext", _ctx(writer, fake))
    result = await SynologyLicenseCollector().run(ctx)

    assert result.ok is True
    assert "license timed out" in result.errors
    # license families stay seeded 0.0.
    assert _single(writer, M_LICENSE_TOTAL) == _NOT_EXHAUSTED
    assert _single(writer, M_LICENSE_USED) == _NOT_EXHAUSTED
    assert _single(writer, M_LICENSE_EXHAUSTED) == _NOT_EXHAUSTED
    # homemode (live default) still parses: schedule_on True.
    assert _single(writer, M_HOMEMODE_SCHEDULE_ON) == _ON
    # only ONE successful fetch -> one api_took.
    assert len(_gauges_named(writer, _API_TOOK)) == 1
    assert result.metrics_emitted == len(writer.gauges)


@pytest.mark.asyncio
async def test_homemode_fetch_error_license_ok() -> None:
    """HomeMode fetch errors, license succeeds -> homemode seeds 0.0, license emits, ok=True."""
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(
        homemode=SynologyError(reason="timeout", message="homemode timed out"),
    )
    ctx = cast("CollectorContext", _ctx(writer, fake))
    result = await SynologyLicenseCollector().run(ctx)

    assert result.ok is True
    assert "homemode timed out" in result.errors
    # license (live default) parses.
    assert _single(writer, M_LICENSE_TOTAL) == _LIVE_TOTAL
    # homemode families stay seeded 0.0.
    assert _single(writer, M_HOMEMODE_ON) == _OFF
    assert _single(writer, M_HOMEMODE_SCHEDULE_ON) == _OFF
    assert len(_gauges_named(writer, _API_TOOK)) == 1
    assert result.metrics_emitted == len(writer.gauges)


@pytest.mark.asyncio
async def test_both_fetch_error_emits_seeds() -> None:
    """BOTH fetches error -> all 10 gauges emit seed 0.0, ok=False, no api_took."""
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(
        license_=SynologyError(reason="timeout", message="license timed out"),
        homemode=SynologyError(reason="timeout", message="homemode timed out"),
    )
    ctx = cast("CollectorContext", _ctx(writer, fake))
    result = await SynologyLicenseCollector().run(ctx)

    assert result.ok is False
    assert "license timed out" in result.errors
    assert "homemode timed out" in result.errors
    assert len(_gauges_named(writer, _API_TOOK)) == 0
    assert len(_gauges_named(writer, _DROP)) == _FAMILY_COUNT
    # every seeded gauge present at 0.0.
    for name in (
        M_LICENSE_TOTAL,
        M_LICENSE_USED,
        M_LICENSE_MAX,
        M_LICENSE_EXHAUSTED,
        M_LICENSE_CAMERA_COUNT,
        M_HOMEMODE_ON,
        M_HOMEMODE_NOTIFY_ON,
        M_HOMEMODE_SCHEDULE_ON,
        M_HOMEMODE_REC_SCHEDULE_ON,
        M_HOMEMODE_STREAMING_ON,
    ):
        assert _single(writer, name) == 0.0
    assert result.metrics_emitted == len(writer.gauges)


@pytest.mark.asyncio
async def test_non_dict_payloads_skip_parse() -> None:
    """Successful fetches whose payload is non-dict -> as_dict None -> parse skipped (seeds).

    Covers the False side of BOTH ``if <payload> is not None`` guards in run(): the response is
    a success (api_took emitted, ok True) but payload is a list, so neither parse runs and all
    gauges stay seeded 0.0.
    """
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(
        license_=_license_resp([]),
        homemode=_homemode_resp([]),
    )
    ctx = cast("CollectorContext", _ctx(writer, fake))
    result = await SynologyLicenseCollector().run(ctx)

    assert result.ok is True
    assert len(_gauges_named(writer, _API_TOOK)) == _EXPECTED_API_TOOK_COUNT
    assert _single(writer, M_LICENSE_TOTAL) == 0.0
    assert _single(writer, M_HOMEMODE_SCHEDULE_ON) == 0.0
    assert result.metrics_emitted == len(writer.gauges)


@pytest.mark.asyncio
async def test_license_unconfigured_client() -> None:
    """ctx.synology is None -> client_unconfigured_result; ok False, no gauges, emitted 0."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, None))
    result = await SynologyLicenseCollector().run(ctx)

    assert result.ok is False
    assert result.errors == ["synology client not configured"]
    assert result.metrics_emitted == 0
    assert len(writer.gauges) == 0
