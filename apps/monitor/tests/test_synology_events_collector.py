"""Unit tests for the synology_events collector (STAGE-008-016, fixture-based).

100% branch coverage of events.py. Field names + payload shapes are LIVE-VERIFIED (captured
JSON: event-count has evt_cam["0"] composite "<id>-<name>" keys + a "-1" group key + a
date{"YYYY/MM/DD":{"-1":..}} map + top-level total; recording-list has recordings[] each with
cameraName + sizeByte + top-level total). Exercises the CO-EQUAL combine (ok=False ONLY when
BOTH fetches fail), the always-emit seeded rollups, the emit-on-presence per-camera families,
the privacy-lock aggregation, and every conditional guard's BOTH sides — including the
load-bearing LOCAL-vs-UTC day-boundary key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import pytest

if TYPE_CHECKING:
    from homelab_monitor.kernel.plugins.context import CollectorContext

import homelab_monitor.plugins.collectors.integrations.synology.events as events_mod
from homelab_monitor.kernel.plugins.io import MemoryRetainingMetricsWriter
from homelab_monitor.kernel.synology.client import SynologyResponse
from homelab_monitor.kernel.synology.errors import SynologyError
from homelab_monitor.plugins.collectors.integrations.synology.events import (
    M_EVENTS_TODAY,
    M_EVENTS_TOTAL,
    M_EVENTS_TOTAL_ALL,
    M_RECORDINGS_BYTES,
    M_RECORDINGS_BYTES_TOTAL,
    M_RECORDINGS_COUNT,
    M_RECORDINGS_TOTAL,
    SynologyEventsCollector,
    today_key,
)

_API_TOOK = "homelab_synology_api_took_seconds"
_DROP = "homelab_metric_family_dropped_series"

_EXPECTED_INTERVAL = 300.0
_EXPECTED_TIMEOUT = 30.0

# 7 cap-routed families emitted by _emit.
_FAMILY_COUNT = 7
# Two co-equal fetches: event count + recording list.
_EXPECTED_API_TOOK_COUNT = 2

# Live fixture cardinalities / values.
_CAMERA_COUNT = 3
_TODAY_KEY = "2026/06/26"
_LIVE_EVENTS_DRIVEWAY = 746.0
_LIVE_EVENTS_BACKYARD = 803.0
_LIVE_EVENTS_DOORBELL = 1096.0
_LIVE_EVENTS_TOTAL_ALL = 2645.0
_LIVE_EVENTS_TODAY = 108.0
_LIVE_RECORDINGS_TOTAL = 2645.0
# Fixture recording set (NOT all 2645 — a handful across the 3 cameras).
_FIXTURE_REC_BYTES_TOTAL = 4584.0  # 920+1376 + 344+1376 + 568 = see _live_recording_payload
_FIXTURE_DRIVEWAY_COUNT = 2.0
_FIXTURE_BACKYARD_COUNT = 2.0
_FIXTURE_DOORBELL_COUNT = 1.0
_FIXTURE_DRIVEWAY_BYTES = 2296.0  # 920 + 1376
_FIXTURE_BACKYARD_BYTES = 1720.0  # 344 + 1376
_FIXTURE_DOORBELL_BYTES = 568.0

# Magic numbers for specific test cases (PLR2004).
_EXPECTED_STORAGE_INTERVAL = 300
_TIMEOUT_SECONDS = 30
_NUM_RECORDINGS_IN_FIXTURE = 5
_RECORD_ID_OFFSET = 1
_RECORD_WITHOUT_SIZE_OFFSET = 6
_EXPECTED_COUNT_WITHOUT_NAME = 1.0
_EXPECTED_BYTES_NO_CAMERA_NAME = 700.0
_EXPECTED_BYTES_WITH_CAMERA_NAME = 300.0
_EXPECTED_TOTAL_BYTES_WITH_NAMELESS = 1000.0
_EXPECTED_EMPTY_LIST_BYTES = 0.0
_EXPECTED_SEEDED_ZERO = 0.0
_EXPECTED_DATETIME_HOUR = 12
_DATETIME_YEAR = 2026
_DATETIME_MONTH = 6
_DATETIME_DAY = 26
_DATETIME_YEAR_ALT = 2026
_DATETIME_MONTH_ALT = 7
_DATETIME_DAY_ALT = 1
_DATETIME_EVENING_HOUR = 23
# Test case-specific magic numbers (PLR2004).
_DRIVEWAY_COUNT_NO_DASH = 746.0
_ID_FALLBACK_LABEL = 5.0
_ID_FALLBACK_COUNT = 9.0
_DRIVEWAY_SINGLE_RECORD_BYTES = 100.0
_DRIVEWAY_DOUBLE_RECORD_BYTES = 500.0
_DRIVEWAY_DOUBLE_RECORD_COUNT = 2.0
_NAMELESS_RECORD_BYTES = 400.0


def _resp(payload: object, endpoint: str) -> SynologyResponse:
    return SynologyResponse(payload=payload, took_seconds=0.5, endpoint=endpoint)


def _event_resp(payload: object) -> SynologyResponse:
    return _resp(payload, "SYNO.SurveillanceStation.Event/CountByCategory")


def _rec_resp(payload: object) -> SynologyResponse:
    return _resp(payload, "SYNO.SurveillanceStation.Recording/List")


def _live_event_payload() -> dict[str, object]:
    """The verified live event-count shape (3 cameras + a date map incl. today)."""
    return {
        "date": {
            "-1": 2645,
            "2026/06/25": {"-1": 144, "am": 72, "pm": 72},
            "2026/06/26": {"-1": 108, "am": 72, "pm": 36},
        },
        "evt_cam": {
            "-1": 2645,
            "0": {
                "-1": 2645,
                "1-Driveway": 746,
                "2-Backyard": 803,
                "3-Doorbell": 1096,
            },
        },
        "recCntTmstmp": 4589855410684,
        "total": 2645,
    }


def _recording(
    *, rec_id: int, camera_id: int, camera_name: str, size_byte: int
) -> dict[str, object]:
    """One live-shaped recording record (only cameraName + sizeByte are consumed)."""
    return {
        "audioCodec": 0,
        "cameraId": camera_id,
        "cameraName": camera_name,
        "filePath": f"20260626PM/{camera_name}-{rec_id}.mp4",
        "height": 1440,
        "id": rec_id,
        "locked": False,
        "sizeByte": size_byte,
        "videoCodec": 3,
        "width": 2560,
    }


def _live_recording_payload() -> dict[str, object]:
    """A representative recording-list shape: 5 records across the 3 cameras.

    Driveway: 920 + 1376 = 2296 ; Backyard: 344 + 1376 = 1720 ; Doorbell: 568.
    Sum = 4584. total reported as 2645 (the all-time count, independent of this list).
    """
    return {
        "dsId": 0,
        "total": 2645,
        "recordings": [
            _recording(rec_id=1, camera_id=1, camera_name="Driveway", size_byte=920),
            _recording(rec_id=2, camera_id=1, camera_name="Driveway", size_byte=1376),
            _recording(rec_id=3, camera_id=2, camera_name="Backyard", size_byte=344),
            _recording(rec_id=4, camera_id=2, camera_name="Backyard", size_byte=1376),
            _recording(rec_id=5, camera_id=3, camera_name="Doorbell", size_byte=568),
        ],
    }


class _FakeSynology:
    """Stand-in for ctx.synology with 2 independently programmable methods."""

    def __init__(self, events: object = None, recordings: object = None) -> None:
        self._events = events if events is not None else _event_resp(_live_event_payload())
        self._recordings = (
            recordings if recordings is not None else _rec_resp(_live_recording_payload())
        )

    async def ss_event_count_by_category(self) -> object:
        return self._events

    async def ss_recording_list(self) -> object:
        return self._recordings


@dataclass
class _Ctx:
    """Typed stand-in for CollectorContext used in events tests."""

    vm: MemoryRetainingMetricsWriter = field(default_factory=MemoryRetainingMetricsWriter)
    synology: object = None


def _ctx(writer: MemoryRetainingMetricsWriter, synology: object) -> _Ctx:
    return _Ctx(vm=writer, synology=synology)


def _gauges_named(
    writer: MemoryRetainingMetricsWriter, name: str
) -> list[tuple[str, float, dict[str, str]]]:
    return [g for g in writer.gauges if g[0] == name]


def _value_for_camera(gauges: list[tuple[str, float, dict[str, str]]], camera: str) -> float:
    matches = [g[1] for g in gauges if g[2].get("camera") == camera]
    assert len(matches) == 1
    return matches[0]


class _FrozenNow:
    """Callable stand-in for ``datetime`` exposing only the ``.now`` the collector uses."""

    def __init__(self, frozen: datetime) -> None:
        self._frozen = frozen

    def now(self, tz: object = None) -> datetime:
        return self._frozen


# === classvars ===


def test_events_classvars() -> None:
    assert SynologyEventsCollector.name == "synology_events"
    assert SynologyEventsCollector.interval == timedelta(seconds=300)
    assert SynologyEventsCollector.interval.total_seconds() == _EXPECTED_INTERVAL
    assert SynologyEventsCollector.timeout == timedelta(seconds=30)
    assert SynologyEventsCollector.timeout.total_seconds() == _EXPECTED_TIMEOUT
    assert SynologyEventsCollector.concurrency_group == "synology"


# === today_key (TZ — load-bearing) ===


def test_today_key_basic() -> None:
    """A morning local datetime yields its own date key."""
    now = datetime(
        _DATETIME_YEAR, _DATETIME_MONTH, _DATETIME_DAY, 2, 0, tzinfo=ZoneInfo("America/New_York")
    )
    assert today_key(now) == "2026/06/26"


def test_today_key_late_evening_local_differs_from_utc() -> None:
    """LOAD-BEARING: 23:00 ET on 2026-06-26 is 03:00 UTC on 2026-06-27.

    The key MUST use the LOCAL wall-clock date (2026/06/26), NOT the UTC date.
    """
    now = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _DATETIME_EVENING_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    assert today_key(now) == "2026/06/26"
    # Sanity: the same instant in UTC is the next day — proving local != utc here.
    assert now.astimezone(ZoneInfo("UTC")).strftime("%Y/%m/%d") == "2026/06/27"


# === run() tests ===


@pytest.mark.asyncio
async def test_events_unconfigured_client() -> None:
    """run: ctx.synology is None -> unconfigured."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _Ctx(vm=writer, synology=None))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is False
    assert result.errors == ["synology client not configured"]
    assert result.metrics_emitted == 0
    assert len(writer.gauges) == 0


@pytest.mark.asyncio
async def test_events_full_live_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row A: Full live shape — all 7 families, today value resolved via frozen now."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology()))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True

    events_total = _gauges_named(writer, M_EVENTS_TOTAL)
    assert len(events_total) == _CAMERA_COUNT
    assert _value_for_camera(events_total, "Driveway") == _LIVE_EVENTS_DRIVEWAY
    assert _value_for_camera(events_total, "Backyard") == _LIVE_EVENTS_BACKYARD
    assert _value_for_camera(events_total, "Doorbell") == _LIVE_EVENTS_DOORBELL

    today = _gauges_named(writer, M_EVENTS_TODAY)
    assert len(today) == 1
    assert today[0][1] == _LIVE_EVENTS_TODAY

    total_all = _gauges_named(writer, M_EVENTS_TOTAL_ALL)
    assert len(total_all) == 1
    assert total_all[0][1] == _LIVE_EVENTS_TOTAL_ALL

    rec_total = _gauges_named(writer, M_RECORDINGS_TOTAL)
    assert len(rec_total) == 1
    assert rec_total[0][1] == _LIVE_RECORDINGS_TOTAL

    rec_bytes_total = _gauges_named(writer, M_RECORDINGS_BYTES_TOTAL)
    assert len(rec_bytes_total) == 1
    assert rec_bytes_total[0][1] == _FIXTURE_REC_BYTES_TOTAL

    rec_count = _gauges_named(writer, M_RECORDINGS_COUNT)
    assert len(rec_count) == _CAMERA_COUNT
    assert _value_for_camera(rec_count, "Driveway") == _FIXTURE_DRIVEWAY_COUNT
    assert _value_for_camera(rec_count, "Backyard") == _FIXTURE_BACKYARD_COUNT
    assert _value_for_camera(rec_count, "Doorbell") == _FIXTURE_DOORBELL_COUNT

    rec_bytes = _gauges_named(writer, M_RECORDINGS_BYTES)
    assert len(rec_bytes) == _CAMERA_COUNT
    assert _value_for_camera(rec_bytes, "Driveway") == _FIXTURE_DRIVEWAY_BYTES
    assert _value_for_camera(rec_bytes, "Backyard") == _FIXTURE_BACKYARD_BYTES
    assert _value_for_camera(rec_bytes, "Doorbell") == _FIXTURE_DOORBELL_BYTES


@pytest.mark.asyncio
async def test_events_event_ok_rec_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 6: event fetch ok, rec fetch FAIL."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(recordings=SynologyError(reason="timeout", message="rec timed out"))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == ["rec timed out"]
    events_total = _gauges_named(writer, M_EVENTS_TOTAL)
    assert len(events_total) == _CAMERA_COUNT
    rec_total = _gauges_named(writer, M_RECORDINGS_TOTAL)
    assert rec_total[0][1] == _EXPECTED_SEEDED_ZERO
    rec_bytes_total = _gauges_named(writer, M_RECORDINGS_BYTES_TOTAL)
    assert rec_bytes_total[0][1] == _EXPECTED_SEEDED_ZERO
    rec_count = _gauges_named(writer, M_RECORDINGS_COUNT)
    assert len(rec_count) == 0


@pytest.mark.asyncio
async def test_events_event_fails_rec_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 7: event fetch FAIL, rec fetch ok."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(events=SynologyError(reason="timeout", message="evt timed out"))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == ["evt timed out"]
    events_total = _gauges_named(writer, M_EVENTS_TOTAL)
    assert len(events_total) == 0
    events_today = _gauges_named(writer, M_EVENTS_TODAY)
    assert events_today[0][1] == _EXPECTED_SEEDED_ZERO
    events_total_all = _gauges_named(writer, M_EVENTS_TOTAL_ALL)
    assert events_total_all[0][1] == _EXPECTED_SEEDED_ZERO
    rec_total = _gauges_named(writer, M_RECORDINGS_TOTAL)
    assert rec_total[0][1] == _LIVE_RECORDINGS_TOTAL
    rec_count = _gauges_named(writer, M_RECORDINGS_COUNT)
    assert len(rec_count) == _CAMERA_COUNT


@pytest.mark.asyncio
async def test_events_both_fetches_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 8: BOTH fetches fail."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(
        events=SynologyError(reason="timeout", message="evt timed out"),
        recordings=SynologyError(reason="timeout", message="rec timed out"),
    )
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is False
    assert result.errors == ["evt timed out", "rec timed out"]
    events_total = _gauges_named(writer, M_EVENTS_TOTAL)
    assert len(events_total) == 0
    rec_count = _gauges_named(writer, M_RECORDINGS_COUNT)
    assert len(rec_count) == 0
    events_today = _gauges_named(writer, M_EVENTS_TODAY)
    assert events_today[0][1] == _EXPECTED_SEEDED_ZERO
    events_total_all = _gauges_named(writer, M_EVENTS_TOTAL_ALL)
    assert events_total_all[0][1] == _EXPECTED_SEEDED_ZERO
    rec_total = _gauges_named(writer, M_RECORDINGS_TOTAL)
    assert rec_total[0][1] == _EXPECTED_SEEDED_ZERO
    rec_bytes_total = _gauges_named(writer, M_RECORDINGS_BYTES_TOTAL)
    assert rec_bytes_total[0][1] == _EXPECTED_SEEDED_ZERO
    drop_gauges = _gauges_named(writer, _DROP)
    assert len(drop_gauges) == _FAMILY_COUNT


@pytest.mark.asyncio
async def test_events_event_payload_not_a_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 9: event payload not a dict."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(events=_event_resp("nope"))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    events_total = _gauges_named(writer, M_EVENTS_TOTAL)
    assert len(events_total) == 0
    events_total_all = _gauges_named(writer, M_EVENTS_TOTAL_ALL)
    assert events_total_all[0][1] == _EXPECTED_SEEDED_ZERO
    events_today = _gauges_named(writer, M_EVENTS_TODAY)
    assert events_today[0][1] == _EXPECTED_SEEDED_ZERO
    rec_total = _gauges_named(writer, M_RECORDINGS_TOTAL)
    assert rec_total[0][1] == _LIVE_RECORDINGS_TOTAL


@pytest.mark.asyncio
async def test_events_rec_payload_not_a_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 10: rec payload not a dict."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(recordings=_rec_resp(123))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    rec_total = _gauges_named(writer, M_RECORDINGS_TOTAL)
    assert rec_total[0][1] == _EXPECTED_SEEDED_ZERO
    rec_count = _gauges_named(writer, M_RECORDINGS_COUNT)
    assert len(rec_count) == 0
    events_total = _gauges_named(writer, M_EVENTS_TOTAL)
    assert len(events_total) == _CAMERA_COUNT


@pytest.mark.asyncio
async def test_events_evt_cam_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 12: evt_cam missing."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    payload = {"date": {"-1": 2645, "2026/06/26": {"-1": 108}}, "total": 2645}
    fake = _FakeSynology(events=_event_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    events_total = _gauges_named(writer, M_EVENTS_TOTAL)
    assert len(events_total) == 0
    events_total_all = _gauges_named(writer, M_EVENTS_TOTAL_ALL)
    assert events_total_all[0][1] == _LIVE_EVENTS_TOTAL_ALL
    events_today = _gauges_named(writer, M_EVENTS_TODAY)
    assert events_today[0][1] == _LIVE_EVENTS_TODAY


@pytest.mark.asyncio
async def test_events_evt_cam_not_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 13: evt_cam present but not a dict."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    payload = _live_event_payload().copy()
    payload["evt_cam"] = "x"
    fake = _FakeSynology(events=_event_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    events_total = _gauges_named(writer, M_EVENTS_TOTAL)
    assert len(events_total) == 0


@pytest.mark.asyncio
async def test_events_evt_cam_zero_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 14: evt_cam present but "0" missing."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    payload = _live_event_payload().copy()
    payload["evt_cam"] = {"-1": 5}
    fake = _FakeSynology(events=_event_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    events_total = _gauges_named(writer, M_EVENTS_TOTAL)
    assert len(events_total) == 0


@pytest.mark.asyncio
async def test_events_key_without_dash_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 17: composite key WITHOUT dash is skipped."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    payload = _live_event_payload().copy()
    evt_cam = cast(dict[str, object], payload["evt_cam"])
    evt_cam["0"] = {"NoDash": 7, "1-Driveway": _DRIVEWAY_COUNT_NO_DASH}
    fake = _FakeSynology(events=_event_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    events_total = _gauges_named(writer, M_EVENTS_TOTAL)
    assert len(events_total) == 1
    assert _value_for_camera(events_total, "Driveway") == _DRIVEWAY_COUNT_NO_DASH


@pytest.mark.asyncio
async def test_events_name_empty_falls_back_to_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 18: empty name falls back to id."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    payload = _live_event_payload().copy()
    evt_cam = cast(dict[str, object], payload["evt_cam"])
    evt_cam["0"] = {"5-": 9}
    fake = _FakeSynology(events=_event_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    events_total = _gauges_named(writer, M_EVENTS_TOTAL)
    assert len(events_total) == 1
    assert _value_for_camera(events_total, "5") == _ID_FALLBACK_COUNT


@pytest.mark.asyncio
async def test_events_both_parts_empty_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 19: both parts empty is skipped."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    payload = _live_event_payload().copy()
    evt_cam = cast(dict[str, object], payload["evt_cam"])
    evt_cam["0"] = {"-": 3}
    fake = _FakeSynology(events=_event_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    events_total = _gauges_named(writer, M_EVENTS_TOTAL)
    assert len(events_total) == 0


@pytest.mark.asyncio
async def test_events_count_non_numeric_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 21: non-numeric count is skipped."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    payload = _live_event_payload().copy()
    evt_cam = cast(dict[str, object], payload["evt_cam"])
    evt_cam["0"] = {"1-Driveway": "foo"}
    fake = _FakeSynology(events=_event_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    events_total = _gauges_named(writer, M_EVENTS_TOTAL)
    assert len(events_total) == 0


@pytest.mark.asyncio
async def test_events_total_missing_uses_date_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 23: total missing, falls back to date["-1"]."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    payload = _live_event_payload().copy()
    del payload["total"]
    fake = _FakeSynology(events=_event_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    events_total_all = _gauges_named(writer, M_EVENTS_TOTAL_ALL)
    assert events_total_all[0][1] == _LIVE_EVENTS_TOTAL_ALL


@pytest.mark.asyncio
async def test_events_total_and_date_group_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 24: total and date["-1"] both missing."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    payload = {"date": {"2026/06/26": {"-1": 108}}, "evt_cam": {"0": {"1-Driveway": 746}}}
    fake = _FakeSynology(events=_event_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    events_total_all = _gauges_named(writer, M_EVENTS_TOTAL_ALL)
    assert events_total_all[0][1] == _EXPECTED_SEEDED_ZERO


@pytest.mark.asyncio
async def test_events_today_key_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 26: today key absent from date."""
    frozen = datetime(
        _DATETIME_YEAR_ALT,
        _DATETIME_MONTH_ALT,
        _DATETIME_DAY_ALT,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    payload = _live_event_payload()
    fake = _FakeSynology(events=_event_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    events_today = _gauges_named(writer, M_EVENTS_TODAY)
    assert events_today[0][1] == _EXPECTED_SEEDED_ZERO
    events_total_all = _gauges_named(writer, M_EVENTS_TOTAL_ALL)
    assert events_total_all[0][1] == _LIVE_EVENTS_TOTAL_ALL


@pytest.mark.asyncio
async def test_events_today_missing_inner_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 27: today key present but nested "-1" missing."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    payload = _live_event_payload().copy()
    date_map = cast(dict[str, object], payload["date"])
    date_map["2026/06/26"] = {"am": 1, "pm": 2}
    fake = _FakeSynology(events=_event_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    events_today = _gauges_named(writer, M_EVENTS_TODAY)
    assert events_today[0][1] == _EXPECTED_SEEDED_ZERO


@pytest.mark.asyncio
async def test_recordings_total_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 29: recordings total missing."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    payload = _live_recording_payload().copy()
    del payload["total"]
    fake = _FakeSynology(recordings=_rec_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    rec_total = _gauges_named(writer, M_RECORDINGS_TOTAL)
    assert rec_total[0][1] == _EXPECTED_SEEDED_ZERO
    rec_count = _gauges_named(writer, M_RECORDINGS_COUNT)
    assert len(rec_count) == _CAMERA_COUNT


@pytest.mark.asyncio
async def test_recordings_list_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 31: recordings list missing."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    payload = {"total": 2645}
    fake = _FakeSynology(recordings=_rec_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    rec_count = _gauges_named(writer, M_RECORDINGS_COUNT)
    assert len(rec_count) == 0
    rec_bytes = _gauges_named(writer, M_RECORDINGS_BYTES)
    assert len(rec_bytes) == 0
    rec_bytes_total = _gauges_named(writer, M_RECORDINGS_BYTES_TOTAL)
    assert rec_bytes_total[0][1] == _EXPECTED_EMPTY_LIST_BYTES
    rec_total = _gauges_named(writer, M_RECORDINGS_TOTAL)
    assert rec_total[0][1] == _LIVE_RECORDINGS_TOTAL


@pytest.mark.asyncio
async def test_recordings_not_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 32: recordings not a list."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    payload = {"total": 2645, "recordings": "x"}
    fake = _FakeSynology(recordings=_rec_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    rec_count = _gauges_named(writer, M_RECORDINGS_COUNT)
    assert len(rec_count) == 0
    rec_bytes_total = _gauges_named(writer, M_RECORDINGS_BYTES_TOTAL)
    assert rec_bytes_total[0][1] == _EXPECTED_EMPTY_LIST_BYTES


@pytest.mark.asyncio
async def test_recordings_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 33: empty recordings list."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    payload: dict[str, object] = {"total": 2645, "recordings": []}
    fake = _FakeSynology(recordings=_rec_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    rec_count = _gauges_named(writer, M_RECORDINGS_COUNT)
    assert len(rec_count) == 0
    rec_bytes_total = _gauges_named(writer, M_RECORDINGS_BYTES_TOTAL)
    assert rec_bytes_total[0][1] == _EXPECTED_EMPTY_LIST_BYTES
    rec_total = _gauges_named(writer, M_RECORDINGS_TOTAL)
    assert rec_total[0][1] == _LIVE_RECORDINGS_TOTAL


@pytest.mark.asyncio
async def test_recordings_non_dict_record_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 34: non-dict record dropped."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    payload = {
        "total": 2645,
        "recordings": [
            "x",
            _recording(rec_id=1, camera_id=1, camera_name="Driveway", size_byte=100),
        ],
    }
    fake = _FakeSynology(recordings=_rec_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    rec_count = _gauges_named(writer, M_RECORDINGS_COUNT)
    assert len(rec_count) == 1
    assert _value_for_camera(rec_count, "Driveway") == _EXPECTED_COUNT_WITHOUT_NAME
    rec_bytes = _gauges_named(writer, M_RECORDINGS_BYTES)
    assert _value_for_camera(rec_bytes, "Driveway") == _DRIVEWAY_SINGLE_RECORD_BYTES
    rec_bytes_total = _gauges_named(writer, M_RECORDINGS_BYTES_TOTAL)
    assert rec_bytes_total[0][1] == _DRIVEWAY_SINGLE_RECORD_BYTES


@pytest.mark.asyncio
async def test_recordings_size_missing_counts_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 36: sizeByte missing still counts, contributes 0.0 bytes."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    payload = {
        "total": 2645,
        "recordings": [
            {"cameraName": "Driveway", "audioCodec": 0},
            _recording(rec_id=2, camera_id=1, camera_name="Driveway", size_byte=500),
        ],
    }
    fake = _FakeSynology(recordings=_rec_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    rec_count = _gauges_named(writer, M_RECORDINGS_COUNT)
    assert _value_for_camera(rec_count, "Driveway") == _DRIVEWAY_DOUBLE_RECORD_COUNT
    rec_bytes = _gauges_named(writer, M_RECORDINGS_BYTES)
    assert _value_for_camera(rec_bytes, "Driveway") == _DRIVEWAY_DOUBLE_RECORD_BYTES
    rec_bytes_total = _gauges_named(writer, M_RECORDINGS_BYTES_TOTAL)
    assert rec_bytes_total[0][1] == _DRIVEWAY_DOUBLE_RECORD_BYTES


@pytest.mark.asyncio
async def test_recordings_no_camera_name_only_bytes_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 38: record with no cameraName only counts toward bytes_total."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    payload = {
        "total": 2645,
        "recordings": [
            {"sizeByte": 700, "audioCodec": 0},
            _recording(rec_id=1, camera_id=1, camera_name="Driveway", size_byte=300),
        ],
    }
    fake = _FakeSynology(recordings=_rec_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    rec_count = _gauges_named(writer, M_RECORDINGS_COUNT)
    assert len(rec_count) == 1
    assert _value_for_camera(rec_count, "Driveway") == _EXPECTED_COUNT_WITHOUT_NAME
    rec_bytes = _gauges_named(writer, M_RECORDINGS_BYTES)
    assert _value_for_camera(rec_bytes, "Driveway") == _EXPECTED_BYTES_WITH_CAMERA_NAME
    rec_bytes_total = _gauges_named(writer, M_RECORDINGS_BYTES_TOTAL)
    assert rec_bytes_total[0][1] == _EXPECTED_TOTAL_BYTES_WITH_NAMELESS


@pytest.mark.asyncio
async def test_recordings_camera_name_non_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 39: non-string cameraName skips per-camera."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    payload = {
        "total": 2645,
        "recordings": [
            {"cameraName": 123, "sizeByte": 400},
        ],
    }
    fake = _FakeSynology(recordings=_rec_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    rec_count = _gauges_named(writer, M_RECORDINGS_COUNT)
    assert len(rec_count) == 0
    rec_bytes_total = _gauges_named(writer, M_RECORDINGS_BYTES_TOTAL)
    assert rec_bytes_total[0][1] == _NAMELESS_RECORD_BYTES


@pytest.mark.asyncio
async def test_events_metrics_emitted_accounting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 42: metrics_emitted accounting."""
    frozen = datetime(
        _DATETIME_YEAR,
        _DATETIME_MONTH,
        _DATETIME_DAY,
        _EXPECTED_DATETIME_HOUR,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )
    monkeypatch.setattr(events_mod, "datetime", _FrozenNow(frozen))
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology()))
    collector = SynologyEventsCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    api_took = _gauges_named(writer, _API_TOOK)
    assert len(api_took) == _EXPECTED_API_TOOK_COUNT
    drop_gauges = _gauges_named(writer, _DROP)
    assert len(drop_gauges) == _FAMILY_COUNT
    assert result.metrics_emitted == len(writer.gauges)
